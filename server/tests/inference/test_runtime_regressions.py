"""October capture regressions; no real model or personal key."""
import asyncio
from contextlib import nullcontext
import importlib.util
import json
import logging
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from aiserver.context.prompt_builder import prepare_messages
from aiserver.inference.ollama import OllamaAdapter, ModelCallError, engine_error
from aiserver.inference.registry import ModelBackends
from aiserver.inference.contracts import ModelError
from aiserver.settings import ROOT, Settings


def config():
    return json.loads((ROOT / "config/inference.development.example.json").read_text())


@pytest.mark.parametrize("text", [
    "request (88355 tokens) exceeds the available context size (81920 tokens), try increasing it",
    '{"error":"request (82091 tokens) exceeds the available context size (81920 tokens)"}',
])
def test_captured_context_errors_are_not_config_errors(text):
    assert engine_error(text) == "CONTEXT_TOO_LARGE"


@pytest.mark.parametrize("resident", [True, False])
def test_gpu_verification_reuses_runner_and_matches_shift(resident):
    async def run():
        cfg = {**config(), "RequireFullGpu": True}
        calls, loaded = [], resident
        def handler(req):
            nonlocal loaded
            calls.append(req.url.path)
            if req.url.path == "/api/ps":
                return httpx.Response(200, json={"models": [{"name": cfg["Model"], "size": 10,
                    "size_vram": 10, "context_length": cfg["NumCtx"]}] if loaded else []})
            payload = json.loads(req.content)
            assert req.url.path == "/api/generate"
            assert payload["shift"] is False and payload["truncate"] is False
            loaded = True
            return httpx.Response(200, json={"done": True})
        adapter = OllamaAdapter(cfg)
        await adapter.client.aclose()
        adapter.client = httpx.AsyncClient(base_url=cfg["BaseUrl"], transport=httpx.MockTransport(handler))
        try:
            await adapter.ensure_full_gpu()
            await adapter.ensure_full_gpu()
            assert calls.count("/api/generate") == (0 if resident else 1)
        finally:
            await adapter.aclose()
    asyncio.run(run())


@pytest.mark.parametrize("kind,code,reason", [
    ("unknown_tool", "MODEL_PROTOCOL_ERROR", "tool_name_not_allowed"),
    ("context_http", "CONTEXT_TOO_LARGE", "engine_http_rejection"),
])
def test_failure_diagnostics_keep_metrics_not_content(caplog, kind, code, reason):
    caplog.set_level(logging.INFO)
    async def run():
        cfg = {**config(), "RequireFullGpu": False}
        prompt = prepare_messages({"protocol": "openai-chat-v1", "messages": [{"role": "user",
            "content": [{"type": "text", "text": "PRIVATE_PROMPT_SENTINEL"}]}], "tools": [{"type": "function",
            "function": {"name": "read", "parameters": {"type": "object"}}}]}, (), cfg["ImagePolicy"])
        def handler(req):
            if kind == "context_http":
                return httpx.Response(400, json={"error": "request (88355 tokens) exceeds the available context size (81920 tokens)"})
            message = {"thinking": "PRIVATE_THINKING_SENTINEL", "content": ""}
            if kind == "unknown_tool":
                message["tool_calls"] = [{"function": {"name": "PRIVATE_UNKNOWN_NAME", "arguments": {"secret": "PRIVATE_ARG_SENTINEL"}}}]
            rows = [{"model": cfg["Model"], "done": False, "message": message},
                    {"model": cfg["Model"], "done": True, "done_reason": "stop", "eval_count": 124,
                     "prompt_eval_count": 5297, "message": {"content": ""}}]
            return httpx.Response(200, content="\n".join(json.dumps(r) for r in rows)+"\n")
        adapter = OllamaAdapter(cfg)
        await adapter.client.aclose()
        adapter.client = httpx.AsyncClient(base_url=cfg["BaseUrl"], transport=httpx.MockTransport(handler))
        try:
            with pytest.raises(ModelCallError) as failed:
                await adapter.chat(prompt)
            assert failed.value.code == code and failed.value.request_finished
        finally:
            await adapter.aclose()
    asyncio.run(run())
    assert reason in caplog.text and "PRIVATE_" not in caplog.text
    if kind != "context_http":
        assert '"eval_count":124' in caplog.text and '"prompt_eval_count":5297' in caplog.text


def test_other_model_rendering_waits_and_activates_target(tmp_path):
    async def run():
        paths = []
        for name in ["first", "second"]:
            cfg = config()
            cfg.update(Model=name, ProfileId=name, ModelLockPath=str(tmp_path / (name+".lock.json")))
            (tmp_path / (name+".lock.json")).write_text("{}")
            path = tmp_path / (name+".json")
            path.write_text(json.dumps(cfg))
            paths.append(path)
        backend = ModelBackends(Settings(provider="Ollama", manage_engine=False,
            inference_config=paths[0], additional_inference_configs=[paths[1]], data_directory=tmp_path/"data"))
        first, second = list(backend.bindings.values())
        entered, release = asyncio.Event(), asyncio.Event()
        events = []
        async def generate(*args):
            events.append("generate")
            entered.set()
            await release.wait()
            return "done"
        async def activate(provider):
            events.append("activate:"+provider.cfg["Model"])
        backend.activate = activate
        first[2].generate = generate
        second[1].counter = object()
        second[1].prepare = AsyncMock(return_value="counted")
        task = asyncio.create_task(backend.generate(SimpleNamespace(profile_id=first[0].profile_id), asyncio.Event(), None))
        await entered.wait()
        waiting = asyncio.create_task(backend.prepare(None, second[0]))
        await asyncio.sleep(.02)
        second[1].prepare.assert_not_awaited()
        assert "activate:second" not in events
        release.set()
        assert await task == "done" and await waiting == "counted"
        assert events == ["activate:first", "generate", "activate:second"]
        first[2].generate = AsyncMock(side_effect=ModelError("MODEL_UNAVAILABLE"))
        with pytest.raises(ModelError):
            await backend.generate(SimpleNamespace(profile_id=first[0].profile_id), asyncio.Event(), None)
        assert backend.needs_recovery
        second[1].prepare.reset_mock()
        assert (await backend.prepare(None, second[0])).code == "ENGINE_RECOVERING"
        second[1].prepare.assert_not_awaited()
        await backend.aclose()
    asyncio.run(run())


def test_budget_upgrade_idempotent_preserves_tuning(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("budget_upgrade_test", ROOT/"scripts/inference/Repair-InferenceRuntime.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    (tmp_path/"config/models").mkdir(parents=True)
    (tmp_path/"config/models/qwen35.source.json").write_bytes((ROOT/"config/models/qwen35.source.json").read_bytes())
    cfg = {**config(), "Model": "qwen3.5:9b-q4_K_M", "NumCtx": 49152, "NumBatch": 64, "Think": True}
    cfg["Budget"]["private_budget_marker"] = "KEEP"
    path = tmp_path/"config/model.json"
    raw = json.dumps(cfg).encode()
    path.write_bytes(raw)
    server = tmp_path/"config/server.local.json"
    server.write_text(json.dumps({"provider":"Ollama", "inference_config":"config/model.json", "private_marker":"KEEP"}))
    original_server = server.read_bytes()
    budget = {"Method":"ollama-render-tokenizer-v1", "TokenizerPath":"fixture", "TokenizerSha256":"fixture"}
    monkeypatch.setattr(module, "prepare_tokenizer", lambda *args: budget)
    monkeypatch.setattr(module.legacy, "offline", lambda *args: nullcontext())
    changed = module.repair(tmp_path, server)
    assert len(changed) == 1 and Path(changed[0]["backup"]).read_bytes() == raw
    assert json.loads(path.read_bytes()) == {**cfg, "Budget": {**cfg["Budget"], **budget}}
    assert server.read_bytes() == original_server and module.repair(tmp_path, server) == []


def test_qwen38_context_upgrade_preserves_private_fields_and_uses_no_engine(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("context_upgrade_test", ROOT/"scripts/inference/Repair-InferenceRuntime.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    (tmp_path/"config/models").mkdir(parents=True)
    (tmp_path/"config/models/qwen38.source.json").write_bytes((ROOT/"config/models/qwen38.source.json").read_bytes())
    cfg = json.loads((ROOT/"config/inference.qwen38.example.json").read_bytes())
    cfg.update(NumCtx=32768, NumBatch=32, private_marker="KEEP", GenerationDefaults={"think": True})
    target = tmp_path/"config/model.json"
    raw = json.dumps(cfg).encode()
    target.write_bytes(raw)
    server = tmp_path/"config/server.local.json"
    server.write_text(json.dumps({"provider":"Ollama", "inference_config":"config/model.json", "private_marker":"KEEP"}))
    original_server = server.read_bytes()
    monkeypatch.setattr(module.legacy, "offline", lambda *args: nullcontext())
    monkeypatch.setattr(module, "prepare_tokenizer", lambda *args: pytest.fail("context change must not download tokenizer"))
    changed = module.set_qwen38_context(65536, tmp_path, server)
    assert Path(changed[0]["backup"]).read_bytes() == raw
    assert json.loads(target.read_bytes()) == {**cfg, "NumCtx": 65536}
    assert server.read_bytes() == original_server
    assert module.set_qwen38_context(65536, tmp_path, server) == []
    for invalid in (True, 1024, 10**9):
        with pytest.raises(ValueError):
            module.set_qwen38_context(invalid, tmp_path, server)
    updated = target.read_bytes()
    def concurrent(before):
        target.write_text(json.dumps({**cfg, "private_marker": "OTHER SESSION"}))
        raise ValueError("concurrent update")
    monkeypatch.setattr(module.legacy, "unchanged", concurrent)
    with pytest.raises(ValueError, match="concurrent"):
        module.set_qwen38_context(32768, tmp_path, server)
    assert target.read_bytes() != updated
    assert json.loads(target.read_bytes())["private_marker"] == "OTHER SESSION"


@pytest.mark.parametrize("override", [False, True])
def test_qwen38_output_upgrade_preserves_window_and_handles_priority_override(tmp_path, monkeypatch, override):
    spec = importlib.util.spec_from_file_location("output_upgrade_test", ROOT/"scripts/inference/Repair-InferenceRuntime.py")
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    (tmp_path/"config/models").mkdir(parents=True)
    (tmp_path/"config/models/qwen38.source.json").write_bytes((ROOT/"config/models/qwen38.source.json").read_bytes())
    cfg = json.loads((ROOT/"config/inference.qwen38.example.json").read_bytes())
    cfg.update(NumPredict=4096, private_marker="KEEP", GenerationDefaults={"temperature":0.3})
    if override:
        cfg["MaxOutputTokens"] = 8192
    target = tmp_path/"config/model.json"; raw = json.dumps(cfg).encode(); target.write_bytes(raw)
    server = tmp_path/"config/server.local.json"
    server.write_text(json.dumps({"provider":"Ollama", "inference_config":"config/model.json"}))
    original_server = server.read_bytes()
    monkeypatch.setattr(module.legacy, "offline", lambda *args: nullcontext())
    changed = module.set_qwen38_context(None, tmp_path, server, output=16384)
    expected = {**cfg, "NumPredict":16384, **({"MaxOutputTokens":16384} if override else {})}
    assert json.loads(target.read_bytes()) == expected
    assert Path(changed[0]["backup"]).read_bytes() == raw and server.read_bytes() == original_server
    assert module.set_qwen38_context(None, tmp_path, server, output=16384) == []
    for invalid in (True, 0, -1, 65536):
        with pytest.raises(ValueError):
            module.set_qwen38_context(None, tmp_path, server, output=invalid)
    assert json.loads(target.read_bytes()) == expected


@pytest.fixture
def model_output_migration(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("models_output_upgrade_test", ROOT/"scripts/inference/Repair-InferenceRuntime.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    (tmp_path/"config").mkdir()
    paths = []
    for name in ("qwen35", "qwen38"):
        template = f"config/inference.{name}.example.json"
        (tmp_path/template).write_bytes((ROOT/template).read_bytes())
        cfg = json.loads((ROOT/template).read_bytes())
        cfg.update(NumPredict=8192,
                   private_marker="KEEP", GenerationDefaults={"temperature": 0.3})
        if name == "qwen35":
            cfg["MaxOutputTokens"] = 4096
        path = tmp_path/f"config/{name}.json"
        path.write_text(json.dumps(cfg))
        paths.append(path)
    retired = tmp_path/"config/vl.json"
    retired.write_bytes((ROOT/"config/inference.development.example.json").read_bytes())
    paths.append(retired)
    server = tmp_path/"config/server.local.json"
    server.write_text(json.dumps({"provider": "Ollama", "inference_config": "config/qwen35.json",
        "additional_inference_configs": [p.relative_to(tmp_path).as_posix() for p in paths[1:]],
        "private_marker": "KEEP"}))
    monkeypatch.setattr(module.legacy, "offline", lambda *args: nullcontext())
    monkeypatch.setattr(module, "prepare_tokenizer", lambda *args: pytest.fail("output change must not download tokenizer"))
    return module, server, paths


@pytest.mark.parametrize("qwen38_output", [4096, 16384])
def test_all_model_outputs_preserve_windows_retired_models_and_private_fields(model_output_migration, qwen38_output):
    module, server, paths = model_output_migration
    qwen38 = json.loads(paths[1].read_bytes())
    paths[1].write_text(json.dumps({**qwen38, "NumPredict": qwen38_output}))
    originals = {p: p.read_bytes() for p in [server, *paths]}
    changed = module.set_model_outputs(16384, server.parent.parent, server)
    assert len(changed) == (2 if qwen38_output != 16384 else 1)
    for path in paths[:2]:
        before = json.loads(originals[path])
        assert json.loads(path.read_bytes()) == {**before, "NumPredict": 16384,
            **({"MaxOutputTokens": 16384} if "MaxOutputTokens" in before else {})}
    assert paths[-1].read_bytes() == originals[paths[-1]] and server.read_bytes() == originals[server]
    for item in changed:
        assert Path(item["backup"]).read_bytes() == originals[Path(item["config"])]
    assert module.set_model_outputs(16384, server.parent.parent, server) == []


@pytest.mark.parametrize("invalid", [True, 0, -1, 81920])
def test_all_model_output_validation_happens_before_any_write(model_output_migration, invalid):
    module, server, paths = model_output_migration
    originals = {p: p.read_bytes() for p in [server, *paths]}
    with pytest.raises(ValueError):
        module.set_model_outputs(invalid, server.parent.parent, server)
    assert all(p.read_bytes() == raw for p, raw in originals.items())
    assert not list(server.parent.glob("*.bak"))


@pytest.mark.parametrize("failure", ["save", "concurrent"])
@pytest.mark.parametrize("recommended", [False, True])
def test_all_model_output_failure_rolls_back_own_writes_preserving_other_updates(model_output_migration, monkeypatch, failure, recommended):
    module, server, paths = model_output_migration
    originals = {p: p.read_bytes() for p in [server, *paths]}
    write = module.legacy.support.atomic_write
    if failure == "save":
        def fail_second(path, data):
            if path == paths[1]:
                raise OSError("synthetic write failure")
            write(path, data)
        monkeypatch.setattr(module.legacy.support, "atomic_write", fail_second)
    else:
        unchanged = module.legacy.unchanged
        calls = 0
        def concurrent(expected):
            nonlocal calls
            calls += 1
            if calls == 3:
                paths[1].write_text(json.dumps({**json.loads(originals[paths[1]]), "private_marker": "OTHER SESSION"}))
            unchanged(expected)
        monkeypatch.setattr(module.legacy, "unchanged", concurrent)
    with pytest.raises((ValueError, OSError)):
        module.set_model_outputs(16384, server.parent.parent, server, recommended_contexts=recommended)
    for path, raw in originals.items():
        if failure == "concurrent" and path == paths[1]:
            assert json.loads(path.read_bytes())["private_marker"] == "OTHER SESSION"
        else:
            assert path.read_bytes() == raw


def test_recommended_contexts_update_registered_models_and_preserve_other_fields(model_output_migration):
    module, server, paths = model_output_migration
    for path in paths[:2]:
        value = json.loads(path.read_bytes())
        value.update(NumCtx=32768, NumPredict=8192, MaxInputTokens=12345)
        path.write_text(json.dumps(value))
    originals = {path: path.read_bytes() for path in [server, *paths]}
    changes = module.set_model_outputs(16384, server.parent.parent, server, recommended_contexts=True)
    assert len(changes) == 2
    for path, context in zip(paths[:2], [131072, 65536]):
        before = json.loads(originals[path])
        expected = {**before, "NumCtx": context, "NumPredict": 16384,
                    **({"MaxOutputTokens": 16384} if "MaxOutputTokens" in before else {})}
        assert json.loads(path.read_bytes()) == expected
    assert paths[-1].read_bytes() == originals[paths[-1]] and server.read_bytes() == originals[server]
    for item in changes:
        assert ".before-recommended-" in item["backup"]
        assert Path(item["backup"]).read_bytes() == originals[Path(item["config"])]
    assert module.set_model_outputs(16384, server.parent.parent, server, recommended_contexts=True) == []


def test_recommended_contexts_cli_needs_no_download_or_engine(model_output_migration, monkeypatch):
    module, server, _ = model_output_migration
    monkeypatch.setattr(module.install.shared, "ensure_dependencies", lambda **kwargs: pytest.fail("must not install dependencies"))
    def apply(output, **kwargs):
        assert output == 16384 and kwargs == {"server_config": server, "recommended_contexts": True}
    monkeypatch.setattr(module, "set_model_outputs", apply)
    monkeypatch.setattr(sys, "argv", ["Repair-InferenceRuntime.py", "--server-config", str(server), "--recommended-contexts"])
    assert module.main() == 0
