"""Full API -> production mapper/budget/adapter -> scripted engine round trips."""
import asyncio
import importlib.util
import json
from pathlib import Path
import sqlite3
import socket
import subprocess
import threading
import time

import httpx
import pytest

from aiserver.api.openai import ChatValidator
from aiserver.api.validation import wire_json
from aiserver.context.prompt_builder import PreparedPrompt, prepare_messages
from aiserver.context.window_budget import freeze_output
from aiserver.inference.configuration import chat_payload
from aiserver.inference.contracts import Generation, Profile, TaskInput
from aiserver.inference.fake import FakeProvider
from aiserver.inference.ollama import OllamaAdapter, ModelCallError
from aiserver.inference.runtime import PracticalBudget
from aiserver.settings import ROOT, Settings
from aiserver.storage.db import Store
from test_fake_api import service, events, request, post, terminal, assert_schema


BODY = " \r\n```csharp\n// 原文🙂  \nreturn 1;\n```\n "
REASONING = " 思考\r\n🙂 <tag>\t "


def config():
    cfg = json.loads((ROOT / "config/inference.development.example.json").read_text())
    cfg.update(Model="fake-v1", NumCtx=81920, NumPredict=128, ReservedTokens=512,
               RequireFullGpu=False, Budget={},
               Temperature=0.99, TopP=0.1, TopK=1, Seed=42, Think=False, PresencePenalty=1)
    return cfg


class ScriptedProvider(FakeProvider):
    def __init__(self, cfg, *, body=BODY, reasoning=REASONING, reason="stop", broken=False, hold=False, tools=False):
        super().__init__(0)
        self.cfg, self.body, self.reasoning, self.reason = cfg, body, reasoning, reason
        self.broken, self.hold, self.tools = broken, hold, tools
        self.payloads = []
        self.first = asyncio.Event()

    async def generate(self, prepared, stop, on_delta):
        self.calls += 1
        raw = json.loads(prepared.payload_json)
        raw["image_sha256"] = tuple(raw["image_sha256"])
        prompt = PreparedPrompt(**raw)
        owner = self

        class Stream(httpx.AsyncByteStream):
            async def __aiter__(self):
                rows = [{"model": owner.cfg["Model"], "done": False,
                         "message": {"thinking": owner.reasoning}},
                        {"model": owner.cfg["Model"], "done": False,
                         "message": {"content": owner.body}}]
                for row in rows:
                    # Split across UTF-8 code points and JSON framing.
                    data = (json.dumps(row, ensure_ascii=False) + "\n").encode()
                    for offset in range(0, len(data), 3):
                        yield data[offset:offset+3]
                owner.first.set()
                if owner.hold:
                    await asyncio.Event().wait()
                if not owner.broken:
                    message = {"content": ""}
                    if owner.tools:
                        message["tool_calls"] = [{"function": {"name": "read", "arguments": {"path": "原文.cs"}}}]
                    yield (json.dumps({"model": owner.cfg["Model"], "done": True, "done_reason": owner.reason,
                        "prompt_eval_count": 31, "eval_count": 9, "message": message}, ensure_ascii=False)+"\n").encode()

        def respond(req):
            self.payloads.append(json.loads(req.content))
            return httpx.Response(200, stream=Stream())

        adapter = OllamaAdapter(self.cfg)
        await adapter.client.aclose()
        adapter.client = httpx.AsyncClient(base_url=self.cfg["BaseUrl"], transport=httpx.MockTransport(respond))
        try:
            try:
                result = await adapter.chat(prompt, on_delta)
            except ModelCallError as error:
                from aiserver.inference.contracts import ModelError
                raise ModelError(error.code, request_finished=True) from error
            return Generation(result.text, result.is_complete, result.finish_reason,
                result.metrics["prompt_eval_count"], result.metrics["eval_count"],
                tool_calls=result.tool_calls, reasoning_text=result.reasoning_text)
        finally:
            await adapter.aclose()


def profile(cfg):
    return Profile("transparency-test", "Ollama", wire_json({"config": cfg}), ("text", "image"), True)


def frames(response):
    return [json.loads(line[6:]) for line in response.iter_lines()
            if line.startswith("data: ") and line != "data: [DONE]"]


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("body,reasoning,reason", [(BODY, REASONING, "stop"), ("", REASONING, "length"),
                                                     (BODY, REASONING, "length"), ("", "", "length"),
                                                     (" \r\n\t ", "", "stop"), ("", "", "stop")])
def test_normal_stream_and_saved_results_preserve_both_channels(tmp_path, stream, body, reasoning, reason, caplog):
    cfg = config()
    provider = ScriptedProvider(cfg, body=body, reasoning=reasoning, reason=reason)
    with service(tmp_path, provider=provider, profile=profile(cfg), budget=PracticalBudget(cfg)) as (client, store, scheduler, other):
        response = client.post("/v1/chat/completions", json={"model": "fake-v1", "stream": stream,
            "messages": [{"role": "user", "content": BODY}]})
        assert response.status_code == (422 if reason == "length" and not stream else 200), response.text
        if stream:
            values = frames(response)
            deltas = [v["choices"][0]["delta"] for v in values if v.get("choices")]
            assert "".join(d.get("content", "") for d in deltas) == body
            assert "".join(d.get("reasoning_content", "") for d in deltas) == reasoning
            if reason == "length":
                assert values[-1]["error"]["code"] == "OUTPUT_TRUNCATED"
                assert "128 token" in json.loads(values[-1]["error"]["message"])["error"]["message"]
                assert "data: [DONE]" not in response.text
                assert not any(v.get("choices", [{}])[0].get("finish_reason") for v in values[:-1])
            else:
                assert values[-1]["choices"][0]["finish_reason"] == reason
        elif reason == "length":
            assert response.json()["error"]["code"] == "OUTPUT_TRUNCATED"
            assert "128 token" in response.json()["error"]["message"]
        else:
            result = response.json()["choices"][0]
            assert (result["message"]["content"] or "") == body
            assert result["message"].get("reasoning_content", "") == reasoning
            assert result["finish_reason"] == reason
        payload = provider.payloads[0]
        assert payload["messages"] == [{"role": "user", "content": BODY}]
        assert "think" not in payload
        assert not {"temperature", "top_p", "top_k", "seed", "presence_penalty", "stop"} & payload["options"].keys()
        assert payload["options"]["num_predict"] == 128
        saved = client.get("/api/v1/jobs/"+response.headers["x-job-id"]+"/result").json()
        assert_schema("result", saved)
        assert saved["output"]["text"] == body
        assert saved["output"].get("reasoningText", "") == reasoning
        assert saved["output"]["isComplete"] is (reason == "stop")
        assert client.get("/api/v1/jobs/"+response.headers["x-job-id"]+"/result", headers=other).status_code == 404
        assert REASONING not in caplog.text and BODY not in caplog.text


def test_native_sse_keeps_reasoning_out_of_legacy_text_deltas(tmp_path):
    cfg = config()
    provider = ScriptedProvider(cfg)
    with service(tmp_path, provider=provider, profile=profile(cfg), budget=PracticalBudget(cfg)) as (client, *_):
        values = list(events(client.post("/api/v1/jobs", json=request())))
        chunks = [(kind, value) for kind, value in values if kind in ("delta", "reasoning")]
        assert [v["sequence"] for _, v in chunks] == [1, 2]
        assert "".join(v["text"] for kind, v in chunks if kind == "reasoning") == REASONING
        assert "".join(v["text"] for kind, v in chunks if kind == "delta") == BODY
        assert values[-1][1]["output"]["reasoningText"] == REASONING


@pytest.mark.parametrize("stream", [False, True])
def test_truncation_reports_the_requested_budget_and_never_publishes_tools(tmp_path, stream):
    cfg = config()
    provider = ScriptedProvider(cfg, body="PARTIAL", reasoning="", reason="length", tools=True)
    tool = {"type":"function", "function":{"name":"read", "parameters":{"type":"object"}}}
    with service(tmp_path, provider=provider, profile=profile(cfg), budget=PracticalBudget(cfg)) as (client, *_):
        response = client.post("/v1/chat/completions", json={"model":"fake-v1", "stream":stream,
            "max_completion_tokens":64, "tools":[tool], "messages":[{"role":"user", "content":"Read file"}]})
        assert response.status_code == (200 if stream else 422)
        error = frames(response)[-1]["error"] if stream else response.json()["error"]
        assert error["code"] == "OUTPUT_TRUNCATED" and "64 token" in error["message"]
        assert '"tool_calls"' not in response.text
        saved = client.get("/api/v1/jobs/"+response.headers["x-job-id"]+"/result").json()
        assert saved["status"] == "failed" and saved["generation"]["finishReason"] == "length"
        assert saved["output"]["text"] == "PARTIAL" and saved["output"]["isComplete"] is False


@pytest.mark.parametrize("field", ["max_tokens", "max_completion_tokens"])
def test_output_limit_is_rejected_before_engine_or_counting(tmp_path, field):
    cfg = config()
    provider = ScriptedProvider(cfg)
    with service(tmp_path, provider=provider, profile=profile(cfg), budget=PracticalBudget(cfg)) as (client, store, *_):
        response = client.post("/v1/chat/completions", json={"model":"fake-v1", field:129,
            "messages":[{"role":"user","content":"test"}]})
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "OUTPUT_LIMIT_EXCEEDED"
        assert response.json()["error"]["param"] == field
        assert provider.calls == 0 and store.list("alice", 10, None)[0] == []


@pytest.mark.parametrize("kind", ["name", "detail", "nonassistant_reasoning"])
def test_unsupported_or_misplaced_fields_fail_instead_of_disappearing(kind):
    payload = {"model":"fake-v1", "messages":[{"role":"user", "content":"test"}]}
    if kind == "name": payload["messages"][0]["name"] = "speaker"
    elif kind == "nonassistant_reasoning": payload["messages"][0]["reasoning_content"] = "history"
    else:
        image = request(True)["messages"][0]["content"][1]
        payload["messages"][0]["content"] = [{"type":"image_url", "image_url":{
            "url":"data:"+image["mediaType"]+";base64,"+image["dataBase64"], "detail":"high"}}]
    from aiserver.api.errors import ApiError
    with pytest.raises(ApiError) as error:
        ChatValidator(ROOT/"contracts/api/v1").validate(wire_json(payload).encode())
    assert error.value.status == 400


def test_admin_sampling_defaults_cannot_enable_thinking_or_change_resource_fields():
    from aiserver.inference.configuration import validate_options
    cfg = config(); cfg["GenerationDefaults"] = {"temperature":0.2, "think":True, "seed":7}
    prompt = freeze_output(prepare_messages({"messages":[{"role":"user", "content":[{"type":"text","text":"x"}]}]},
        (), cfg["ImagePolicy"]), cfg, supports_thinking=True)
    payload = chat_payload(cfg, prompt, stream=True)
    assert payload["think"] is False and payload["options"]["seed"] == 7
    cfg["GenerationDefaults"]["num_gpu"] = 0
    with pytest.raises(ValueError, match="GenerationDefaults"): validate_options(cfg)


def test_explicit_parameters_reasoning_history_and_tool_order_survive(tmp_path):
    cfg = config()
    cfg["GenerationDefaults"] = {"temperature":0.8, "think":False}
    provider = ScriptedProvider(cfg, tools=True)
    tool = {"type":"function", "function":{"name":"read", "parameters":{"type":"object",
        "properties":{"z_last":{"type":"string"},"a_first":{"type":"string"}}}}}
    with service(tmp_path, provider=provider, profile=profile(cfg), budget=PracticalBudget(cfg, supports_thinking=True)) as (client, *_):
        response = client.post("/v1/chat/completions", json={"model":"fake-v1", "think":True,
            "temperature":0, "top_p":0.9, "top_k":20, "seed":7, "stop":["STOP"], "presence_penalty":0,
            "repeat_penalty":1.1, "max_tokens":64, "tools":[tool],
            "messages":[{"role":"user","content":"first"},
                        {"role":"assistant","content":"answer", "reasoning_content":REASONING},
                        {"role":"user","content":BODY}]})
        assert response.status_code == 200, response.text
        payload = provider.payloads[0]
        assert payload["think"] is True
        assert payload["options"]["temperature"] == 0 and payload["options"]["num_predict"] == 64
        assert payload["options"]["seed"] == 7 and payload["options"]["stop"] == ["STOP"]
        assert payload["options"]["repeat_penalty"] == 1.1
        assert payload["messages"][1]["thinking"] == REASONING
        assert list(payload["tools"][0]["function"]["parameters"]["properties"]) == ["z_last", "a_first"]
        assert response.json()["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] == '{"path":"原文.cs"}'


@pytest.mark.parametrize("think", [None, False, True])
def test_counting_and_generation_use_the_same_effective_thinking_and_budget(tmp_path, think):
    from tokenizers import Tokenizer, models
    from aiserver.context.coder_budget import CoderCounter
    import hashlib
    tokenizer = Tokenizer(models.WordLevel({"[UNK]":0,"rendered":1}, unk_token="[UNK]"))
    path = tmp_path/"tokenizer.json"
    path.write_text(tokenizer.to_str())
    cfg = config()
    cfg["GenerationDefaults"] = {"think":False, "temperature":0.7}
    cfg["Budget"] = {"TokenizerPath":str(path), "TokenizerSha256":hashlib.sha256(path.read_bytes()).hexdigest()}
    prepared = freeze_output(prepare_messages({"protocol":"openai-chat-v1", "think":think,
        "options":{"num_predict":64}, "messages":[{"role":"user","content":[{"type":"text","text":BODY}]}]},
        (), cfg["ImagePolicy"]), cfg, supports_thinking=True)
    actual_client = httpx.AsyncClient
    captured = []
    def render(req):
        captured.append(json.loads(req.content))
        return httpx.Response(200,json={"model":cfg["Model"],"_debug_info":{"rendered_template":"rendered"}})
    from unittest.mock import patch
    with patch("aiserver.context.coder_budget.httpx.AsyncClient", side_effect=lambda **kw: actual_client(
            **kw, transport=httpx.MockTransport(render))):
        assert asyncio.run(CoderCounter(cfg, ROOT).count(prepared)) == 1
    expected = chat_payload(cfg, prepared, stream=True)
    counted = captured[0]
    assert counted.pop("_debug_render_only") is True
    counted["stream"] = True
    assert counted == expected
    assert expected["think"] is (think is True)


@pytest.mark.parametrize("variant,expected_think,reason,body", [
    (None, False, "stop", "GATEWAY_FINAL"), ("normal", False, "stop", "GATEWAY_FINAL"),
    ("thinking", True, "stop", "GATEWAY_FINAL"),
    ("thinking", True, "length", ""), (None, False, "length", "GATEWAY_PARTIAL")])
def test_real_opencode_variants_reach_engine_and_preserve_output(tmp_path, variant, expected_think, reason, body):
    import uvicorn
    from aiserver.app import create_app
    binary = ROOT/".local/apps/opencode/node_modules/opencode-ai/bin/opencode.exe"
    if not binary.is_file():
        pytest.skip("Requires installed OpenCode")
    spec = importlib.util.spec_from_file_location("transparency_opencode_fixture", ROOT/"scripts/validation/Check-OpenCodeCompaction.py")
    helper = importlib.util.module_from_spec(spec); spec.loader.exec_module(helper)
    cfg = config(); cfg.update(Model="context-fixture", NumPredict=1024)
    provider = ScriptedProvider(cfg, body=body, reasoning="GATEWAY_REASONING", reason=reason)
    provider.supports_thinking = True
    app = create_app(Settings(data_directory=tmp_path/"server"), profile=profile(cfg), budget=PracticalBudget(cfg, supports_thinking=True), provider=provider)
    store = app.state.store; store.initialize(); store.user_add("test", "Synthetic")
    key = store.token_create("test")
    listener = socket.socket(); listener.bind(("127.0.0.1",0)); listener.listen(128)
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False))
    thread = threading.Thread(target=server.run, kwargs={"sockets":[listener]}, daemon=True); thread.start()
    work = tmp_path/"client"; work.mkdir()
    try:
        deadline = time.monotonic()+10
        while not server.started and time.monotonic() < deadline: time.sleep(.01)
        assert server.started
        env = helper.client_environment(work, f"http://127.0.0.1:{listener.getsockname()[1]}/v1", key)
        path = work/"opencode.json"; client_cfg = json.loads(path.read_text())
        spec = importlib.util.spec_from_file_location("thinking_client_fixture", ROOT/"scripts/client/opencode_support.py")
        support = importlib.util.module_from_spec(spec); spec.loader.exec_module(support)
        support.CONFIG = work/"generated.json"; support.WORK = work
        support.configure({"server":f"http://127.0.0.1:{listener.getsockname()[1]}"}, {
            "/api/v1/models":{"models":[{"id":"context-fixture", "thinking":True, "toolCall":True,
                "limits":{"contextTokens":81920, "maxInputTokens":80384, "maxOutputTokens":1024}}]},
            "/api/v1/ready":{"canAcceptJobs":True}, "/api/v1/auth/me":{"user":{"userId":"test"}, "expiresAt":None}})
        client_cfg["provider"]["aiserver"]["models"] = json.loads(support.CONFIG.read_text())["provider"]["aiserver"]["models"]
        path.write_text(json.dumps(client_cfg))
        result = subprocess.run([str(binary),"run","--pure","--format","json","--thinking",
            *(["--variant", variant] if variant else []),
            "--title","Synthetic output preservation", "Reply GATEWAY_FINAL. Do not use tools."],
            cwd=work, env=env, timeout=60, capture_output=True, text=True, encoding="utf-8")
        assert result.returncode == (1 if reason == "length" else 0), result.stderr.replace(key,"[redacted]")
        assert provider.payloads and all(p["think"] is expected_think for p in provider.payloads)
        values = [json.loads(line) for line in result.stdout.splitlines() if line.startswith("{")]
        errors = [v for v in values if v.get("type")=="error"]
        if reason == "length":
            rendered = json.dumps(errors, ensure_ascii=False)
            assert "OUTPUT_TRUNCATED" in rendered and "已截断" in rendered and "1024 token" in rendered
            assert errors[0]["error"]["data"]["isRetryable"] is False
            assert not any(v.get("type")=="tool_use" for v in values)
            assert len(provider.payloads) == 1  # No automatic generation retry.
            # The CLI prints parts on normal completion. On errors, verify the
            # same partial parts remain in OpenCode's persistent session/UI.
            with sqlite3.connect((work/"data/opencode/opencode.db").resolve().as_uri()+"?mode=ro", uri=True) as conn:
                parts = [json.loads(row[0]) for row in conn.execute("SELECT data FROM part")]
            assert any(p.get("type")=="reasoning" and p.get("text")=="GATEWAY_REASONING" for p in parts)
            if body:
                assert any(p.get("type")=="text" and p.get("text")==body for p in parts)
        else:
            assert not errors
            assert any(v.get("type")=="reasoning" and v.get("part",{}).get("text")=="GATEWAY_REASONING" for v in values)
            assert any(v.get("type")=="text" and v.get("part",{}).get("text")==body for v in values)
    finally:
        server.should_exit = True; thread.join(10); listener.close()
        assert not thread.is_alive()


@pytest.mark.parametrize("native", [False, True])
@pytest.mark.parametrize("supported", [False, True])
@pytest.mark.parametrize("setting", ["absent", None, False, True])
def test_thinking_requires_explicit_opt_in_and_verified_capability(tmp_path, native, supported, setting):
    cfg = config(); cfg["GenerationDefaults"] = {"think":True}
    provider = ScriptedProvider(cfg, body="OK", reasoning="")
    budget = PracticalBudget(cfg, supports_thinking=supported)
    with service(tmp_path, provider=provider, profile=profile(cfg), budget=budget) as (client, *_):
        payload = request() if native else {"model":"fake-v1", "messages":[{"role":"user", "content":"Hello"}]}
        if setting != "absent":
            payload["think"] = setting
        response = client.post("/api/v1/jobs" if native else "/v1/chat/completions", json=payload)
        assert response.status_code == 200, response.text
        assert provider.payloads
        if supported:
            assert provider.payloads[0]["think"] is (setting is True)
        else:
            assert "think" not in provider.payloads[0]


@pytest.mark.parametrize("broken", [True, False])
def test_partial_reasoning_survives_engine_error_or_cancel(tmp_path, broken):
    cfg = config()
    provider = ScriptedProvider(cfg, broken=broken, hold=not broken)
    with service(tmp_path, provider=provider, profile=profile(cfg), budget=PracticalBudget(cfg)) as (client, store, scheduler, _):
        job = post(client, request())
        client.portal.call(provider.first.wait)
        if not broken:
            client.portal.call(scheduler.cancel, "alice", job)
        status = terminal(client, job)
        assert status["status"] == ("failed" if broken else "cancelled")
        output = client.get("/api/v1/jobs/"+job+"/result").json()["output"]
        assert output == {"text":BODY, "reasoningText":REASONING, "isComplete":False}


def test_v5_migration_retains_keys_results_chunks_and_failure_backup(tmp_path, monkeypatch):
    cfg = config()
    with service(tmp_path, fake_delay_seconds=0) as (client, store, *_):
        values = list(events(client.post("/api/v1/jobs", json=request())))
        job = values[-1][1]["jobId"]
        key = store.token_create("alice")
        before = store.get("alice", job=job)
    with store.connection(True) as conn:
        conn.execute("ALTER TABLE output_chunks DROP COLUMN channel")
        conn.execute("PRAGMA user_version=5")
    original = Path.read_text
    def fail(path, *args, **kwargs):
        if path.name == "006_output_channels.sql":
            return "ALTER TABLE output_chunks ADD COLUMN channel TEXT DEFAULT 'content'; SELECT * FROM no_such_table;"
        return original(path, *args, **kwargs)
    with monkeypatch.context() as patcher:
        patcher.setattr(Path, "read_text", fail)
        with pytest.raises(sqlite3.OperationalError): store.initialize()
    with store.connection() as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 5
        assert "channel" not in {r[1] for r in conn.execute("PRAGMA table_info(output_chunks)")}
    store.initialize()
    assert store.authenticate(key) == "alice" and store.get("alice", job=job)["output_json"] == before["output_json"]
    assert all(c["channel"] == "content" for c in store.chunks(job, 0))
    backup = next((tmp_path/"backups").glob("schema-v5-*.db"))
    restored = Store(Settings(data_directory=tmp_path/"restored"))
    restored.restore(backup); restored.initialize()
    assert restored.authenticate(key) == "alice" and restored.get("alice", job=job)["output_json"] == before["output_json"]
