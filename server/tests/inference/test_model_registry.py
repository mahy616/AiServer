"""Multiple model profiles and real-provider bridging against a scripted Ollama transport."""
import asyncio
import hashlib
import json

from fastapi.testclient import TestClient
import httpx
import pytest

from aiserver.app import create_app
from aiserver.inference.registry import load_models, ModelBackends
from aiserver.settings import ROOT, Settings
from unittest.mock import AsyncMock
from types import SimpleNamespace


def settings_for(tmp_path):
    configs = []
    for index, model in enumerate(("fixture-4b", "fixture-8b")):
        cfg = json.loads((ROOT / "config/inference.development.example.json").read_text())
        lock = json.loads((ROOT / "config/models/qwen3vl4b.lock.json").read_text())
        cfg.update(Model=model, ProfileId=model)
        lock["model"]["name"] = model
        lock["model"]["digest"] = "synthetic-digest-" + str(index)
        lock["template_sha256"] = hashlib.sha256(b"synthetic-template").hexdigest()
        lock_path = tmp_path / (model + ".lock.json")
        lock_path.write_text(json.dumps(lock))
        cfg["ModelLockPath"] = str(lock_path)
        path = tmp_path / (model + ".json")
        path.write_text(json.dumps(cfg))
        configs.append(path)
    return Settings(provider="Ollama", manage_engine=False, data_directory=tmp_path / "data",
                    inference_config=configs[0], additional_inference_configs=[configs[1]],
                    requests_per_minute=10000, submissions_per_minute=1000)


@pytest.mark.parametrize("different_kv", [False, True])
def test_real_provider_multi_model_tool_cycle_and_legacy(tmp_path, different_kv):
    settings = settings_for(tmp_path)
    if different_kv:
        settings.manage_engine = True
        for index, path in enumerate([settings.inference_config, *settings.additional_inference_configs]):
            cfg = json.loads(path.read_text())
            cfg.update(FlashAttention=True, KvCacheType="q8_0" if index == 0 else "q4_0")
            path.write_text(json.dumps(cfg))
    app = create_app(settings)
    backend = app.state.scheduler.provider
    restarts = []
    if different_kv:
        backend.engine.start = AsyncMock()
        async def restart(): restarts.append(backend.engine.cfg["KvCacheType"])
        backend.engine.restart = restart
    seen = []
    locks = {p[2].cfg["Model"]: p[2].lock for p in backend.bindings.values()}
    cfgs = {p[2].cfg["Model"]: p[2].cfg for p in backend.bindings.values()}
    loaded = []
    def handler(req):
        body = json.loads(req.content) if req.content else {}
        if req.url.path == "/api/version":
            return httpx.Response(200, json={"version": next(iter(cfgs.values()))["EngineVersion"]})
        if req.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [lock["model"] for lock in locks.values()]})
        if req.url.path == "/api/show":
            return httpx.Response(200, json={"details": {"quantization_level": cfgs[body["model"]]["ExpectedQuantization"]},
                "capabilities": ["completion", "vision", "tools"] + (["thinking"] if body["model"] == "fixture-8b" else []),
                "template": "synthetic-template"})
        if req.url.path == "/api/generate":
            if different_kv:
                assert backend.engine.cfg["KvCacheType"] == cfgs[body["model"]]["KvCacheType"]
            loaded[:] = [body["model"]]
            assert body["options"]["num_gpu"] == 999
            return httpx.Response(200, json={"done": True})
        if req.url.path == "/api/ps":
            return httpx.Response(200, json={"models": [{"name": name, "size": 1000,
                "size_vram": 1000, "context_length": cfgs[name]["NumCtx"]} for name in loaded]})
        assert req.url.path == "/api/chat"
        seen.append(body)
        if body.get("tools") and body["messages"][-1]["role"] != "tool":
            message = {"content": "", "tool_calls": [{"function": {"name": "read_file", "arguments": {"path": "a.txt"}}}]}
        else:
            message = {"content": body["model"] + ": 完成"}
        return httpx.Response(200, json={"model": body["model"], "done": True, "done_reason": "stop",
            "prompt_eval_count": 10, "eval_count": 5, "message": message})
    for _, _, provider in backend.bindings.values():
        asyncio.run(provider.adapter.client.aclose())
        provider.adapter.client = httpx.AsyncClient(base_url=provider.cfg["BaseUrl"], transport=httpx.MockTransport(handler))
    store = app.state.store
    store.initialize()
    store.user_add("fixture", "Fixture")
    token = store.token_create("fixture")
    with TestClient(app, headers={"Authorization": "Bearer " + token}) as client:
        assert [item["id"] for item in client.get("/v1/models").json()["data"]] == ["fixture-4b", "fixture-8b"]
        tool = {"type": "function", "function": {"name": "read_file", "parameters": {"type": "object"}}}
        payload = {"model": "fixture-8b", "think": True, "messages": [{"role": "user", "content": "Read a.txt"}], "tools": [tool]}
        result = client.post("/v1/chat/completions", json=payload)
        assert result.status_code == 200, result.text
        call = result.json()["choices"][0]["message"]["tool_calls"][0]
        assert result.json()["usage"]["total_tokens"] == 15
        payload["messages"] += [{"role": "assistant", "content": None, "tool_calls": [call]},
                                {"role": "tool", "tool_call_id": call["id"], "content": "data"}]
        result = client.post("/v1/chat/completions", json=payload)
        assert result.json()["choices"][0]["message"]["content"] == "fixture-8b: 完成"
        assert seen[-1]["messages"][-1] == {"role": "tool", "content": "data", "tool_name": "read_file"}
        legacy = {"clientRequestId": "416417b5-e936-4cde-b0df-969281936318", "model": "fixture-4b", "think": True,
                  "messages": [{"role": "user", "content": [{"type": "text", "text": "Hello"}]}]}
        assert "fixture-4b: 完成" in client.post("/api/v1/jobs", json=legacy).text
        assert [p["model"] for p in seen] == ["fixture-8b", "fixture-8b", "fixture-4b"]
        assert all(p["think"] is True for p in seen[:2]) and "think" not in seen[-1]
        assert app.state.scheduler.state != "paused"
        if different_kv:
            assert restarts == ["q4_0", "q8_0", "q4_0", "q8_0"]


def test_only_primary_provider_owns_engine(tmp_path):
    backend = ModelBackends(settings_for(tmp_path).model_copy(update={"manage_engine": True}))
    try:
        providers = [entry[2] for entry in backend.bindings.values()]
        assert providers[0].engine is not None and providers[1].engine is None
    finally:
        asyncio.run(backend.aclose())


def test_text_only_model_does_not_advertise_vision(tmp_path):
    settings = settings_for(tmp_path)
    path = settings.additional_inference_configs[0]
    cfg = json.loads(path.read_text())
    cfg["RequiredCapabilities"] = ["completion"]
    path.write_text(json.dumps(cfg))
    profiles = [entry[3] for entry in load_models(settings)]
    assert profiles[0].accepted_input_types == ("text", "image")
    assert profiles[1].accepted_input_types == ("text",)


@pytest.mark.parametrize("change", ["duplicate", "port"])
def test_reject_mixed_processes_and_duplicate_model_ids(tmp_path, change):
    settings = settings_for(tmp_path)
    path = settings.additional_inference_configs[0]
    cfg = json.loads(path.read_text())
    cfg.update({"Model": "fixture-4b"} if change == "duplicate" else {"BaseUrl": "http://127.0.0.1:11435"})
    path.write_text(json.dumps(cfg))
    with pytest.raises(ValueError, match="Duplicate model id" if change == "duplicate" else "same Ollama"):
        load_models(settings)


def test_owned_engine_switches_precision_without_changing_profiles(tmp_path):
    async def run():
        settings = settings_for(tmp_path).model_copy(update={"manage_engine": True})
        for index, path in enumerate([settings.inference_config, *settings.additional_inference_configs]):
            cfg = json.loads(path.read_text())
            cfg.update(FlashAttention=True, KvCacheType="q8_0" if index == 0 else "q4_0")
            path.write_text(json.dumps(cfg))
        backend = ModelBackends(settings)
        providers = [entry[2] for entry in backend.bindings.values()]
        engine = providers[0].engine
        events = []
        async def restarted():
            events.append(engine.cfg["KvCacheType"])
        engine.restart = restarted
        for provider in providers:
            provider.verify_identity = AsyncMock()
            provider.verify_gpu = AsyncMock()
            provider.generate = AsyncMock(return_value="done")
        try:
            for provider in [providers[1], providers[1], providers[0]]:
                profile = next(entry[0] for entry in backend.bindings.values() if entry[2] is provider)
                assert await backend.generate(SimpleNamespace(profile_id=profile.profile_id), asyncio.Event(), None) == "done"
            assert events == ["q4_0", "q8_0"]
            assert providers[0].cfg["KvCacheType"] == "q8_0" and providers[1].cfg["KvCacheType"] == "q4_0"
            assert await backend.recover()
            assert events == ["q4_0", "q8_0", "q8_0"]
        finally:
            await backend.aclose()
        with pytest.raises(ValueError, match="same Ollama"):
            load_models(settings.model_copy(update={"manage_engine": False}))
    asyncio.run(run())


def test_engine_restart_cancellation_settles_before_releasing_gate(tmp_path):
    async def run():
        backend = ModelBackends(settings_for(tmp_path).model_copy(update={"manage_engine": True}))
        began, finish = asyncio.Event(), asyncio.Event()
        async def restart():
            began.set()
            await finish.wait()
        backend.engine.restart = restart
        async def switch():
            async with backend.engine_gate:
                await backend.restart()
        task = asyncio.create_task(switch())
        await began.wait()
        task.cancel()
        await asyncio.sleep(0)
        assert backend.engine_gate.locked()
        finish.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not backend.engine_gate.locked()
        await backend.aclose()
    asyncio.run(run())
