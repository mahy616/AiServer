"""Pinned download, transactional registration, thinking and comparison boundaries."""
import asyncio
from contextlib import contextmanager, nullcontext
import hashlib
import importlib.util
import json
from pathlib import Path

import httpx
import pytest

from aiserver.settings import ROOT
from aiserver.context.prompt_builder import prepare_messages
from aiserver.inference.ollama import OllamaAdapter


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


@pytest.mark.parametrize("failure", [None, "gpu", "identity", "concurrent", "save"])
def test_qwen35_registration_preserves_existing_models_and_failed_install(tmp_path, monkeypatch, failure):
    install = module("qwen35_test_installer", "scripts/inference/Install-Qwen35.py")
    (tmp_path / "config/models").mkdir(parents=True)
    for name in (install.TEMPLATE, install.SOURCE):
        (tmp_path / name).write_bytes((ROOT / name).read_bytes())
    cfg = json.loads((ROOT / "config/inference.coder.example.json").read_text())
    cfg["ModelLockPath"] = "config/models/old.lock.json"
    (tmp_path / "config/old.json").write_text(json.dumps(cfg))
    (tmp_path / cfg["ModelLockPath"]).write_text("{}")
    server = {"provider": "Ollama", "inference_config": "config/old.json", "port": 19999,
              "data_directory": ".local/private", "public_base_url": "https://example.test"}
    path = tmp_path / "config/server.local.json"
    raw = json.dumps(server).encode()
    path.write_bytes(raw)
    monkeypatch.setattr(install.legacy, "offline", lambda *args: nullcontext())
    monkeypatch.setattr(install, "download_model", lambda *args: (None, b""))
    monkeypatch.setattr(install, "prepare_tokenizer", lambda *args: json.loads(
        (ROOT / install.TEMPLATE).read_text())["Budget"])
    if failure == "save":
        monkeypatch.setattr(install.legacy.support, "atomic_write", lambda *args: (_ for _ in ()).throw(OSError()))
    lock = json.loads((ROOT / install.SOURCE).read_text())
    @contextmanager
    def engine(root, cfg, operation):
        if failure == "concurrent":
            path.write_text(json.dumps({**server, "port": 20000}))
        def fetch(endpoint, body=None):
            if endpoint == "/api/version": return {"version": cfg["EngineVersion"]}
            if endpoint == "/api/tags": return {"models": [{"name": cfg["Model"], "digest": "wrong" if failure == "identity" else lock["manifest_sha256"]}]}
            if endpoint == "/api/show": return {"template": "{{ .Prompt }}", "details": {"quantization_level": "Q4_K_M"},
                "model_info": {"general.architecture": "qwen35"}, "capabilities": ["completion", "tools", "thinking", "vision"]}
            if endpoint == "/api/generate":
                assert body["options"]["num_gpu"] == 999 and body["prompt"] == ""
                return {"done": True}
            if endpoint == "/api/ps": return {"models": [{"name": cfg["Model"], "size": 1000,
                "size_vram": 900 if failure == "gpu" else 1000, "context_length": cfg["NumCtx"]}]}
            raise AssertionError(endpoint)
        yield fetch
    if failure:
        with pytest.raises((ValueError, OSError)):
            install.install(tmp_path, server_config=path, engine_factory=engine)
        assert not (tmp_path / install.CONFIG).exists()
        assert not (tmp_path / "config/models/qwen35.local.json").exists()
        assert path.read_bytes() == raw if failure != "concurrent" else json.loads(path.read_bytes())["port"] == 20000
    else:
        result = install.install(tmp_path, server_config=path, engine_factory=engine)
        current = json.loads(path.read_bytes())
        assert current["port"] == server["port"] and current["data_directory"] == server["data_directory"]
        assert current["inference_config"] == install.CONFIG and current["additional_inference_configs"] == ["config/old.json"]
        assert Path(result["backup"]).read_bytes() == raw
        tuned = json.loads((tmp_path / install.CONFIG).read_bytes())
        tuned.update(NumBatch=64, Think=True)
        (tmp_path / install.CONFIG).write_text(json.dumps(tuned))
        assert install.install(tmp_path, server_config=path, engine_factory=engine)["backup"] is None
        assert json.loads((tmp_path / install.CONFIG).read_bytes()) == tuned


@pytest.mark.parametrize("thinking", [False, True])
def test_explicit_thinking_is_sent_and_reasoning_is_separate_output(thinking):
    async def run():
        cfg = json.loads((ROOT / "config/inference.qwen35.example.json").read_text())
        cfg.update(Think=thinking, RequireFullGpu=False)
        def handler(req):
            body = json.loads(req.content)
            assert body["think"] is thinking
            assert "presence_penalty" not in body["options"]
            return httpx.Response(200, json={"model": cfg["Model"], "done": True, "done_reason": "stop",
                "message": {"thinking": "private internal reasoning", "content": "OK"}})
        adapter = OllamaAdapter(cfg)
        await adapter.client.aclose()
        adapter.client = httpx.AsyncClient(base_url=cfg["BaseUrl"], transport=httpx.MockTransport(handler))
        try:
            prompt = prepare_messages({"protocol": "openai-chat-v1", "think": thinking, "messages": [{"role": "user", "content": [
                {"type": "text", "text": "Hello"}]}]}, (), cfg["ImagePolicy"])
            chunks = []
            async def delta(text, channel="content"): chunks.append((channel, text))
            result = await adapter.chat(prompt, delta)
            assert result.text == "OK" and result.reasoning_text == "private internal reasoning"
            assert chunks == [("reasoning_content", "private internal reasoning"), ("content", "OK")]
        finally:
            await adapter.aclose()
    asyncio.run(run())


def test_pinned_qwen35_manifest_and_reused_blob_corruption(tmp_path, monkeypatch):
    install = module("qwen35_download_test", "scripts/inference/Install-Qwen35.py")
    lock = json.loads((ROOT / install.SOURCE).read_text())
    assert hashlib.sha256(lock["manifest"].encode()).hexdigest() == lock["manifest_sha256"]
    data = b"synthetic-blob"
    sha = hashlib.sha256(data).hexdigest()
    manifest = {"config": {"digest": "sha256:" + sha, "size": len(data)}, "layers": []}
    raw = json.dumps(manifest)
    lock.update(manifest=raw, manifest_sha256=hashlib.sha256(raw.encode()).hexdigest())
    cfg = {"ModelsPath": "store"}
    source = tmp_path / "source/blobs"; source.mkdir(parents=True)
    (source / ("sha256-" + sha)).write_bytes(data)
    path, _ = install.download_model(tmp_path, cfg, lock, source.parent)
    assert path.is_file()
    (tmp_path / "store/blobs" / ("sha256-" + sha)).write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="differs"):
        install.download_model(tmp_path, cfg, lock)


def test_comparison_grades_executable_correctness_and_rejects_external_actions():
    compare = module("compare_test", "scripts/validation/Compare-Models.py")
    code = '''def merge_intervals(intervals):
    result = []
    for start, end in sorted(intervals):
        if result and start <= result[-1][1]:
            result[-1] = (result[-1][0], max(result[-1][1], end))
        else:
            result.append((start, end))
    return result
'''
    assert compare.evaluate_code(code) == {"passed": True, "passed_cases": 4, "total_cases": 4}
    assert not compare.evaluate_code("def merge_intervals(intervals):\n    return []")["passed"]
    assert not compare.evaluate_code("import os\ndef merge_intervals(intervals):\n    return os.listdir('.')")["passed"]


@pytest.mark.parametrize("failure", [None, "gpu", "save", "concurrent"])
def test_enable_prepared_models_is_atomic_and_keeps_tuning(tmp_path, monkeypatch, failure):
    enable = module("enable_models_test", "scripts/inference/Enable-CoderModels.py")
    (tmp_path / "config/models").mkdir(parents=True)
    configs = []
    before = {}
    for source, target in (("inference.coder.example.json", "inference.coder.local.json"),
                           ("inference.coder14b.example.json", "inference.coder14b.local.json")):
        cfg = json.loads((ROOT / "config" / source).read_text())
        cfg["NumBatch"] = 64
        path = tmp_path / "config" / target
        path.write_text(json.dumps(cfg))
        before[path] = path.read_bytes()
        (tmp_path / cfg["ModelLockPath"]).write_text("{}")
        configs.append(cfg)
    server = {"provider": "Ollama", "inference_config": "config/inference.coder14b.local.json", "port": 12345}
    path = tmp_path / "config/server.local.json"
    raw = json.dumps(server).encode(); path.write_bytes(raw)
    monkeypatch.setattr(enable.legacy, "offline", lambda *args: nullcontext())
    monkeypatch.setattr(enable.legacy, "verify_profiles", lambda *args: None)
    if failure == "save":
        monkeypatch.setattr(enable.legacy.support, "atomic_write", lambda *args: (_ for _ in ()).throw(OSError()))
    seen = []
    @contextmanager
    def engine(root, cfg, operation):
        seen.append(cfg["KvCacheType"])
        if failure == "concurrent":
            path.write_text(json.dumps({**server, "port": 23456}))
        def fetch(endpoint, body=None):
            if endpoint == "/api/generate": return {"done": True}
            if endpoint == "/api/ps": return {"models": [{"name": cfg["Model"], "size": 100,
                "size_vram": 90 if failure == "gpu" else 100, "context_length": cfg["NumCtx"]}]}
            raise AssertionError(endpoint)
        yield fetch
    if failure:
        with pytest.raises((ValueError, OSError)):
            enable.enable(tmp_path, path, engine)
        if failure != "concurrent":
            assert path.read_bytes() == raw
        else:
            assert json.loads(path.read_bytes())["port"] == 23456
    else:
        updated = enable.enable(tmp_path, path, engine)
        assert updated["port"] == server["port"]
        assert updated["inference_config"] == "config/inference.coder.local.json"
        assert updated["additional_inference_configs"] == ["config/inference.coder14b.local.json"]
        assert seen == ["q8_0", "q4_0"]
        assert next(path.parent.glob("*.before-multimodel-*.bak")).read_bytes() == raw
    assert all(path.read_bytes() == content for path, content in before.items())
