"""Minimal multi-model routing/lifecycle checks; all engines and model files are synthetic."""
import asyncio
import hashlib
import json
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient
import httpx
import pytest

from aiserver.app import create_app
from aiserver.api.openai import ChatValidator
from aiserver.inference import strata_registry as registry
from aiserver.inference.contracts import ModelError, Profile, TaskInput
from aiserver.inference.strata import ManagedStrataEngine, validate_config
from aiserver.settings import ROOT, Settings


def deployment(tmp_path, monkeypatch):
    configs = []
    for suffix in ("", ".deepseek"):
        cfg = json.loads((ROOT / f"config/inference.strata{suffix}.example.json").read_bytes())
        if suffix:
            cfg.update(NumCtx=32768, NumPredict=2048)
        path = tmp_path / f"inference.strata{suffix}.json"
        path.write_text(json.dumps(cfg), encoding="utf-8")
        configs.append(path)
    def load(settings):
        cfg = json.loads(settings.inference_config.read_bytes())
        validate_config(cfg)
        identity = {"engine_model_path": str(tmp_path / cfg["ProfileId"]),
                    "chat_template_sha256": hashlib.sha256(b"fixture").hexdigest()}
        return cfg, identity, Profile(cfg["ProfileId"], "Strata", json.dumps({"config": cfg}), ("text",))
    monkeypatch.setattr(registry, "load_strata_profile", load)
    return Settings(provider="Strata", inference_config=configs[0], additional_inference_configs=configs[1:],
        data_directory=tmp_path / "data", requests_per_minute=10000, submissions_per_minute=1000)


class Engine:
    def __init__(self):
        self.events, self.resident, self.cfg = [], None, None
        self.entered, self.release = asyncio.Event(), asyncio.Event()
        self.block = self.fail = False

    async def start(self):
        assert self.resident is None
        self.resident = self.cfg["Model"]
        self.events.append(("load", self.resident))
        if self.fail:
            raise RuntimeError("synthetic load failure")
        if self.block:
            self.entered.set()
            await self.release.wait()

    async def unload(self):
        if self.resident:
            self.events.append(("unload", self.resident))
        self.resident = None

    async def close(self):
        await self.unload()


def transport(backend):
    engine = Engine()
    backend.engine = engine
    seen = []
    def handler(req):
        provider = backend.active
        cfg, identity = provider.cfg, provider.identity
        assert engine.resident == cfg["Model"]
        if req.url.path == "/props":
            return httpx.Response(200, json={"model_alias": cfg["Model"], "total_slots": 1,
                "build_info": "Strata " + cfg["EngineVersion"], "is_sleeping": False,
                "default_generation_settings": {"n_ctx": cfg["NumCtx"]},
                "model_path": identity["engine_model_path"], "chat_template": "fixture",
                "chat_template_caps": {"supports_tools": True}})
        if req.url.path == "/slots":
            return httpx.Response(200, json=[{"n_ctx": cfg["NumCtx"], "is_processing": False}])
        body = json.loads(req.content)
        assert body["model"] == cfg["Model"]
        seen.append(body)
        tool = body.get("tools") and body["messages"][-1]["role"] != "tool"
        delta = {"tool_calls": [{"index": 0, "id": "call_read", "type": "function",
            "function": {"name": "read", "arguments": '{"path":"a.txt"}'}}]} if tool else {"content": cfg["Model"]}
        rows = [{"choices": [{"index": 0, "delta": delta, "finish_reason": "tool_calls" if tool else "stop"}]},
                {"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 5}}]
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
            text="".join("data: " + json.dumps(row) + "\n\n" for row in rows) + "data: [DONE]\n\n")
    for _, _, provider in backend.bindings.values():
        asyncio.run(provider.adapter.aclose())
        provider.adapter.client = httpx.AsyncClient(base_url=provider.cfg["BaseUrl"], transport=httpx.MockTransport(handler))
        provider.engine = engine
    return engine, seen


def test_api_lists_both_models_and_routes_tool_roundtrip_without_reloading_same_model(tmp_path, monkeypatch):
    app = create_app(deployment(tmp_path, monkeypatch))
    backend, store = app.state.scheduler.provider, app.state.store
    engine, seen = transport(backend)
    qwen, deepseek = [p.cfg["Model"] for _, _, p in backend.bindings.values()]
    store.initialize()
    store.user_add("fixture", "Fixture")
    with TestClient(app, headers={"Authorization": "Bearer " + store.token_create("fixture")}) as client:
        assert engine.events == [("load", qwen)]
        assert [m["id"] for m in client.get("/v1/models").json()["data"]] == [qwen, deepseek]
        models = client.get("/api/v1/models").json()["models"]
        assert all(m["toolCall"] and m["thinking"] for m in models)
        assert [m["limits"]["maxOutputTokens"] for m in models] == [16384, 2048]
        payload = {"model": deepseek, "think": True, "messages": [{"role": "user", "content": "read a.txt"}],
                   "tools": [{"type": "function", "function": {"name": "read", "parameters": {"type": "object"}}}]}
        assert client.post("/v1/chat/completions", json={**payload, "max_tokens": 2049}).status_code == 400
        assert engine.events == [("load", qwen)]
        result = client.post("/v1/chat/completions", json=payload)
        assert result.status_code == 200, result.text
        call = result.json()["choices"][0]["message"]["tool_calls"][0]
        payload["messages"] += [{"role": "assistant", "content": None, "tool_calls": [call]},
                                {"role": "tool", "tool_call_id": call["id"], "content": "file content"}]
        assert client.post("/v1/chat/completions", json=payload).json()["choices"][0]["message"]["content"] == deepseek
        response = client.post("/v1/chat/completions", json={"model": qwen, "stream": True,
            "messages": [{"role": "user", "content": "hello"}]})
        assert response.status_code == 200 and qwen in response.text and "[DONE]" in response.text
        assert engine.events == [("load", qwen), ("unload", qwen), ("load", deepseek), ("unload", deepseek), ("load", qwen)]
        assert [r["reasoning_effort"] for r in seen] == ["high", "high", "none"]
        assert seen[1]["messages"][-1]["tool_call_id"] == "call_read"
        assert app.state.scheduler.state == "available"
    assert engine.resident is None


def test_scheduler_keeps_fifo_and_accepts_cancel_during_cold_load(tmp_path, monkeypatch):
    settings = deployment(tmp_path, monkeypatch)
    settings.execution_timeout_seconds = .05
    app = create_app(settings)
    scheduler, store = app.state.scheduler, app.state.store
    engine, _ = transport(scheduler.provider)
    store.initialize()
    store.user_add("fixture", "Fixture")
    with TestClient(app, headers={"Authorization": "Bearer " + store.token_create("fixture")}) as client:
        qwen, deepseek = [p.cfg["Model"] for _, _, p in scheduler.provider.bindings.values()]
        async def submit(model):
            _, normalized, fingerprint, task = ChatValidator(settings.contract_directory).validate(json.dumps({
                "model": model, "messages": [{"role": "user", "content": "hello"}]}).encode())
            row, _ = await scheduler.submit("fixture", normalized, fingerprint, task)
            return row["job_id"]
        async def entered():
            await asyncio.wait_for(engine.entered.wait(), 2)
            await asyncio.sleep(.12)
        engine.block = True
        first = client.portal.call(submit, deepseek)
        client.portal.call(entered)
        assert client.get("/api/v1/jobs/" + first).json()["status"] == "running"
        second = client.portal.call(submit, qwen)
        assert client.get("/api/v1/jobs/" + second).json()["status"] == "queued"
        assert engine.resident == deepseek
        engine.block = False
        assert client.post("/api/v1/jobs/" + first + "/cancel").status_code == 202
        async def complete():
            async with asyncio.timeout(3):
                while store.by_job(second)["status"] != "succeeded":
                    await asyncio.sleep(.01)
        client.portal.call(complete)
        cancelled = client.get("/api/v1/jobs/" + first).json()
        assert cancelled["status"] == "cancelled" and cancelled["inferenceStopConfirmed"]
        assert engine.events == [("load", qwen), ("unload", qwen), ("load", deepseek), ("unload", deepseek), ("load", qwen)]


@pytest.mark.parametrize("failure", ["cancel", "load", "identity"])
def test_switch_failure_or_cancel_cleans_engine_and_next_job_can_run(tmp_path, monkeypatch, failure):
    backend = registry.StrataBackends(deployment(tmp_path, monkeypatch))
    engine, _ = transport(backend)
    async def run():
        await backend.start()
        task = TaskInput(json.dumps({"messages": [{"role": "user", "content": [{"type": "text", "text": "hello"}]}]}), ())
        first, second = [(await backend.prepare(task, p)).prepared for p in backend.profiles]
        assert backend.execution_timeout(second) > backend.execution_timeout(first)
        if failure == "identity":
            backend.bindings[second.profile_id][2].verify_identity = AsyncMock(side_effect=RuntimeError("wrong identity"))
        engine.block, engine.fail = failure == "cancel", failure == "load"
        generate = asyncio.create_task(backend.generate(second, asyncio.Event(), AsyncMock()))
        if failure == "cancel":
            await asyncio.wait_for(engine.entered.wait(), 2)
            # Admission is offline and may proceed while the generation slot is loading.
            assert (await backend.prepare(task, backend.profiles[0])).prepared
            waiting = asyncio.create_task(backend.generate(first, asyncio.Event(), AsyncMock()))
            await asyncio.sleep(.01)
            assert not waiting.done()
            waiting.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiting
            generate.cancel()
        with pytest.raises(asyncio.CancelledError if failure == "cancel" else ModelError):
            await generate
        assert engine.resident is None
        assert await backend.recover()
        engine.block = engine.fail = False
        result = await backend.generate(first, asyncio.Event(), AsyncMock())
        assert result.text == backend.default.cfg["Model"]
        await backend.aclose()
    asyncio.run(run())


def test_registration_rejects_duplicate_models_and_different_endpoints(tmp_path, monkeypatch):
    settings = deployment(tmp_path, monkeypatch)
    settings.additional_inference_configs = [settings.inference_config]
    with pytest.raises(ValueError, match="Duplicate"):
        registry.load_strata_models(settings)
    path = tmp_path / "inference.strata.deepseek.json"
    cfg = json.loads(path.read_bytes())
    cfg["BaseUrl"] = "http://127.0.0.1:18082"
    path.write_text(json.dumps(cfg))
    settings.additional_inference_configs = [path]
    with pytest.raises(ValueError, match="endpoint"):
        registry.load_strata_models(settings)


def test_unload_waits_for_tree_shutdown_even_when_cancelled(monkeypatch):
    import threading
    entered, release = threading.Event(), threading.Event()
    engine = ManagedStrataEngine(json.loads((ROOT / "config/inference.strata.example.json").read_bytes()), ROOT)
    engine.lock = object()
    def stop():
        entered.set()
        assert release.wait(2)
    monkeypatch.setattr(engine, "_stop_recorded", stop)
    async def run():
        unloading = asyncio.create_task(engine.unload())
        assert await asyncio.to_thread(entered.wait, 2)
        unloading.cancel()
        await asyncio.sleep(.01)
        assert not unloading.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await unloading
    asyncio.run(run())
