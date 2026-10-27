"""OpenAI/Ollama protocol integration with mock model transport, never real inference."""
import asyncio
from dataclasses import replace
import json
import time

import httpx
import pytest
from jsonschema import Draft202012Validator

from aiserver.api.openai import ChatValidator
from aiserver.api.validation import canonical
from aiserver.context.prompt_builder import prepare_messages
from aiserver.context.input_budget import check_budget
from aiserver.inference.contracts import Generation
from aiserver.inference.fake import FakeProvider
from aiserver.inference.ollama import OllamaAdapter, ModelCallError
from aiserver.settings import ROOT
from test_fake_api import service, request, assert_schema
from test_stream_disconnect import live, wait_state


TOOL = {"type": "function", "function": {"name": "read_file", "description": "Read a file",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}}


def chat(**kwargs):
    return {"model": "fake-v1", "messages": [{"role": "user", "content": "Read example.txt"}], **kwargs}


def frames(response):
    values = [json.loads(line[6:]) if line[6:] != "[DONE]" else "[DONE]"
              for line in response.iter_lines() if line.startswith("data: ")]
    for value in values:
        if isinstance(value, dict):
            assert_openai("error" if "error" in value else "chunk", value)
    return values


def assert_openai(name, value):
    schema = json.loads((ROOT / "contracts/api/v1/openai-responses.schema.json").read_text())
    Draft202012Validator(dict(schema, **{"$ref": "#/$defs/" + name})).validate(value)


class ToolProvider(FakeProvider):
    """Use the production message mapper and NDJSON decoder with a scripted engine."""
    def __init__(self):
        super().__init__(0)
        self.payloads = []
        self.cfg = json.loads((ROOT / "config/inference.development.example.json").read_text())
        self.cfg["Model"] = "fake-v1"

    async def generate(self, prepared, stop, on_delta):
        self.calls += 1
        material = json.loads(prepared.payload_json)
        prompt = prepare_messages(material, (), self.cfg["ImagePolicy"])
        adapter = OllamaAdapter(self.cfg)
        await adapter.client.aclose()
        def answer(req):
            payload = json.loads(req.content)
            self.payloads.append(payload)
            if payload["messages"][-1]["role"] == "tool":
                assert payload["messages"][-1]["tool_name"] == "read_file"
                assert payload["messages"][-2]["tool_calls"][0]["function"]["arguments"] == {"path": "example.txt"}
                message = {"content": "文件内容是中文🙂"}
            else:
                message = {"content": "", "tool_calls": [{"function": {
                    "name": "read_file", "arguments": {"path": "example.txt"}}}]}
            return httpx.Response(200, content=json.dumps({"model": "fake-v1", "done": True,
                "done_reason": "stop", "message": message, "prompt_eval_count": 42, "eval_count": 7},
                ensure_ascii=False).encode() + b"\n")
        adapter.client = httpx.AsyncClient(base_url=self.cfg["BaseUrl"], transport=httpx.MockTransport(answer))
        try:
            output = await adapter.chat(prompt, on_delta)
            return Generation(output.text, output.is_complete, output.finish_reason,
                              output.metrics["prompt_eval_count"], output.metrics["eval_count"],
                              tool_calls=output.tool_calls)
        finally:
            await adapter.aclose()


@pytest.mark.parametrize("stream", [False, True])
def test_tool_only_response_followup_and_usage(tmp_path, stream):
    provider = ToolProvider()
    with service(tmp_path, provider=provider) as (client, store, scheduler, other):
        payload = chat(tools=[TOOL], stream=stream, stream_options={"include_usage": True},
                           tool_choice="auto", temperature=0.2, max_tokens=provider.cfg["NumPredict"])
        response = client.post("/v1/chat/completions", json=payload)
        assert response.status_code == 200, response.text
        job = response.headers["x-job-id"]
        if stream:
            received = frames(response)
            assert received[-1] == "[DONE]"
            assert received[-3]["choices"][0]["finish_reason"] == "tool_calls"
            assert received[-2]["usage"] == {"prompt_tokens": 42, "completion_tokens": 7, "total_tokens": 49}
            calls = received[1]["choices"][0]["delta"]["tool_calls"]
            calls = [{k: v for k, v in call.items() if k != "index"} for call in calls]
        else:
            result = response.json()
            assert_openai("completion", result)
            assert result["choices"][0]["finish_reason"] == "tool_calls"
            assert result["usage"]["total_tokens"] == 49
            calls = result["choices"][0]["message"]["tool_calls"]
        assert store.get("alice", job=job)["status"] == "succeeded"
        assert_schema("result", client.get("/api/v1/jobs/" + job + "/result").json())
        assert client.get("/api/v1/jobs/" + job, headers=other).status_code == 404
        assert provider.payloads[0]["tools"] == [TOOL]
        assert provider.payloads[0]["options"]["temperature"] == 0.2
        assert provider.payloads[0]["options"]["num_predict"] == provider.cfg["NumPredict"]
        payload["messages"] += [{"role": "assistant", "content": None, "tool_calls": calls},
                                {"role": "tool", "tool_call_id": calls[0]["id"], "content": "中文🙂"}]
        response = client.post("/v1/chat/completions", json=payload)
        if stream:
            received = frames(response)
            assert "".join(x["choices"][0]["delta"].get("content", "") for x in received
                           if isinstance(x, dict) and x.get("choices")) == "文件内容是中文🙂"
        else:
            assert response.json()["choices"][0]["message"]["content"] == "文件内容是中文🙂"
        assert provider.calls == 2


def test_auth_revocation_errors_and_shared_submission_limit(tmp_path):
    with service(tmp_path, fake_delay_seconds=0, submissions_per_minute=1) as (client, store, scheduler, other):
        denied = client.get("/v1/models", headers={"Authorization": ""})
        assert denied.status_code == 401 and denied.json()["error"]["type"] == "authentication_error"
        assert client.get("/v1/models").json()["data"][0]["id"] == "fake-v1"
        assert client.post("/v1/chat/completions", json=chat(model="missing")).status_code == 404
        # A rejected submission also consumes the common per-user submission budget.
        response = client.post("/api/v1/jobs", json=request())
        assert response.status_code == 429
        assert client.post("/v1/chat/completions", json=chat()).json()["error"]["type"] == "rate_limit_error"
        key = client.headers["Authorization"].split("_", 2)[1]
        store.token_revoke(key)
        assert client.get("/v1/models").status_code == 401


@pytest.mark.parametrize("overrides", [
    {"n": 2}, {"unexpected": True}, {"temperature": 3}, {"max_tokens": 1, "max_completion_tokens": 2},
    {"tool_choice": "required"}, {"tools": [TOOL, TOOL]},
    {"messages": [{"role": "tool", "tool_call_id": "missing", "content": "x"}]},
    {"messages": [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "https://example.com/a.png"}}]}]},
])
def test_invalid_requests_never_enter_queue(tmp_path, overrides):
    with service(tmp_path, fake_delay_seconds=0) as (client, store, scheduler, other):
        response = client.post("/v1/chat/completions", json=chat(**overrides))
        assert response.status_code == 400
        assert store.list("alice", 50, None)[0] == []


def test_disabled_tools_and_partial_finish(tmp_path):
    provider = FakeProvider(0)
    provider.supports_tools = False
    with service(tmp_path, provider=provider) as (client, store, scheduler, other):
        models = client.get("/api/v1/models").json()
        assert_schema("models", models)
        assert models["models"][0]["toolCall"] is False
        response = client.post("/v1/chat/completions", json=chat(tools=[TOOL]))
        assert response.json()["error"]["code"] == "TOOLS_NOT_SUPPORTED"
        assert response.json()["error"]["param"] == "tools"
        assert "text-only agent" in response.json()["error"]["message"]
        assert client.post("/v1/chat/completions", json=chat(tools=[TOOL], tool_choice="none")).status_code == 200
    with service(tmp_path / "length", fake_delay_seconds=0, fake_scenario="truncated") as (client, store, scheduler, other):
        response = client.post("/v1/chat/completions", json=chat())
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "OUTPUT_TRUNCATED"
        assert "已截断" in response.json()["error"]["message"]


def test_model_capabilities_reach_opencode_without_defaulting_to_tools(tmp_path, monkeypatch):
    import importlib.util
    spec = importlib.util.spec_from_file_location("capability_opencode", ROOT / "scripts/client/opencode_support.py")
    connection = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(connection)
    monkeypatch.setattr(connection, "WORK", tmp_path / "client")
    monkeypatch.setattr(connection, "CONFIG", tmp_path / "client/opencode.json")
    with service(tmp_path / "server", fake_delay_seconds=0) as (client, store, scheduler, other):
        first = scheduler.profile
        second = replace(first, profile_id="text-only", snapshot_json=json.dumps({
            "config": {"Model": "text-only"}, "budgetMethod": "fake-bytes-only-v1"}))
        scheduler.profiles["text-only"] = second
        scheduler.provider.tools_supported = lambda profile: profile.profile_id != "text-only"
        models = client.get("/api/v1/models").json()
        assert_schema("models", models)
        assert [item["toolCall"] for item in models["models"]] == [True, False]
        result = {"/api/v1/models": models, "/api/v1/ready": {"canAcceptJobs": True},
                  "/api/v1/auth/me": {"user": {"userId": "synthetic"}, "expiresAt": None}}
        connection.configure({"server": "http://127.0.0.1"}, result)
        cfg = json.loads(connection.CONFIG.read_bytes())["provider"]["aiserver"]["models"]
        assert cfg["fake-v1"]["tool_call"] is True and cfg["text-only"]["tool_call"] is False
        models["models"][0].pop("toolCall")
        connection.configure({"server": "http://127.0.0.1"}, result)
        assert json.loads(connection.CONFIG.read_bytes())["provider"]["aiserver"]["models"]["fake-v1"]["tool_call"] is False


def test_stream_error_is_not_a_successful_finish(tmp_path):
    with service(tmp_path, fake_delay_seconds=0, fake_scenario="failure") as (client, store, scheduler, other):
        response = client.post("/v1/chat/completions", json=chat(stream=True))
        received = frames(response)
        assert received[-1]["error"]["code"] == "MODEL_UNAVAILABLE"
        assert "[DONE]" not in received
        assert not any(x.get("choices", [{}])[0].get("finish_reason") for x in received)


def test_nonstream_generation_failure_returns_http_error(tmp_path):
    with service(tmp_path, fake_delay_seconds=0, fake_scenario="failure") as (client, store, scheduler, other):
        response = client.post("/v1/chat/completions", json=chat())
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "MODEL_UNAVAILABLE"


def test_openai_image_uses_original_bytes_and_developer_maps_to_system():
    source = request(True)["messages"][0]["content"][1]
    payload = chat(messages=[{"role": "developer", "content": "custom rules"}, {"role": "user", "content": [
        {"type": "text", "text": "Before"}, {"type": "image_url", "image_url": {
            "url": "data:" + source["mediaType"] + ";base64," + source["dataBase64"]}},
        {"type": "text", "text": "After"}]}])
    _, normalized, _, task = ChatValidator(ROOT / "contracts/api/v1").validate(canonical(payload).encode())
    cfg = json.loads((ROOT / "config/inference.development.example.json").read_text())
    prompt = prepare_messages(normalized, task.images, cfg["ImagePolicy"])
    assert prompt.messages()[0] == {"role": "system", "content": "custom rules"}
    assert prompt.messages()[1]["content"] == "Before[img]After"
    assert prompt.messages()[1]["images"] == [source["dataBase64"]]


@pytest.mark.parametrize("scenario,expected", [("required", "TOOL_CHOICE_NOT_SATISFIED"),
                                               ("parallel", "MODEL_PROTOCOL_ERROR"),
                                               ("unknown", "MODEL_PROTOCOL_ERROR")])
def test_adapter_enforces_tool_selection_before_publishing_calls(scenario, expected):
    async def check():
        cfg = json.loads((ROOT / "config/inference.development.example.json").read_text())
        _, normalized, _, task = ChatValidator(ROOT / "contracts/api/v1").validate(canonical(chat(
            tools=[TOOL], tool_choice={"type": "function", "function": {"name": "read_file"}},
            parallel_tool_calls=False)).encode())
        prompt = prepare_messages(normalized, task.images, cfg["ImagePolicy"])
        calls = [{"function": {"name": "read_file", "arguments": {"path": "x"}}}]
        if scenario == "required": calls = []
        if scenario == "parallel": calls = calls * 2
        if scenario == "unknown": calls[0]["function"]["name"] = "unlisted_function"
        adapter = OllamaAdapter(cfg)
        await adapter.client.aclose()
        adapter.client = httpx.AsyncClient(base_url=cfg["BaseUrl"], transport=httpx.MockTransport(
            lambda req: httpx.Response(200, json={"model": cfg["Model"], "done": True, "done_reason": "stop",
                "message": {"content": "text", "tool_calls": calls}})))
        try:
            with pytest.raises(ModelCallError) as raised:
                await adapter.chat(prompt)
            assert raised.value.code == expected
        finally:
            await adapter.aclose()
    asyncio.run(check())


def test_live_openai_and_legacy_share_slot_queue_and_disconnect(tmp_path):
    with live(tmp_path) as (client, store, provider):
        with client.stream("POST", "/v1/chat/completions", json=chat(stream=True)) as first:
            job = first.headers["x-job-id"]
            lines = first.iter_lines()
            next(line for line in lines if '"content":"FAKE' in line)
            with client.stream("POST", "/api/v1/jobs", json=request()) as second:
                second_lines = second.iter_lines()
                waiting = next(json.loads(line[6:])["jobId"] for line in second_lines if line.startswith("data:"))
                assert store.get("alice", job=waiting)["status"] == "queued"
            wait_state(store, waiting, "cancelled")
        wait_state(store, job, "cancelled")
        assert store.get("alice", job=job)["stop_reason"] == "client_disconnected"
        provider.delay = 0
        assert client.post("/v1/chat/completions", json=chat()).status_code == 200
        assert provider.calls == 2 and provider.max_active == 1


def test_nonstream_disconnect_cancels_generation(tmp_path):
    with live(tmp_path) as (client, store, provider):
        # Close a real socket before response headers, while model generation is running.
        import socket
        connection = socket.create_connection((client.base_url.host, client.base_url.port))
        body = canonical(chat()).encode()
        headers = (f"POST /v1/chat/completions HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                   f"Authorization: {client.headers['Authorization']}\r\nContent-Type: application/json\r\n"
                   f"Content-Length: {len(body)}\r\n\r\n").encode()
        connection.sendall(headers + body)
        until = time.monotonic() + 5
        while not provider.calls and time.monotonic() < until:
            time.sleep(0.01)
        rows = store.list("alice", 10, None)[0]
        assert rows and provider.calls == 1
        connection.close()
        wait_state(store, rows[0]["job_id"], "cancelled")


def test_tool_schemas_and_history_are_in_budget_and_hash():
    validator = ChatValidator(ROOT / "contracts/api/v1")
    _, normalized, _, task = validator.validate(canonical(chat(tools=[TOOL])).encode())
    cfg = json.loads((ROOT / "config/inference.development.example.json").read_text())
    prompt = prepare_messages(normalized, task.images, cfg["ImagePolicy"])
    changed = replace(prompt, tools_json=prompt.tools_json.replace("Read a file", "A" * (cfg["MaxInputTokens"] * 2)))
    assert prompt.material_sha256() != changed.material_sha256()
    assert check_budget(prompt, cfg).ready
    assert not check_budget(changed, cfg).ready


def test_two_models_are_listed_and_route_to_their_own_profile(tmp_path):
    # A mock second profile exercises the real scheduler without inventing a production 8B lock.
    with service(tmp_path, fake_delay_seconds=0) as (client, store, scheduler, other):
        first = scheduler.profile
        second = replace(first, profile_id="fixture-8b", snapshot_json=json.dumps({
            "config": {"Model": "fixture-8b"}, "budgetMethod": "fake-bytes-only-v1"}))
        scheduler.profiles["fixture-8b"] = second
        assert [x["id"] for x in client.get("/v1/models").json()["data"]] == ["fake-v1", "fixture-8b"]
        assert [x["id"] for x in client.get("/api/v1/models").json()["models"]] == ["fake-v1", "fixture-8b"]
        for model in ("fake-v1", "fixture-8b", "fake-v1"):
            response = client.post("/v1/chat/completions", json=chat(model=model))
            assert response.status_code == 200 and response.json()["model"] == model
            row = store.get("alice", job=response.headers["x-job-id"])
            assert row["profile_id"] == scheduler.profiles[model].profile_id
        assert scheduler.provider.max_active == 1
