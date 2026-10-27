"""Removal safety and retry behavior using an isolated store and synthetic engine."""
from contextlib import contextmanager
import importlib.util
import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location("remove_deepseek_test", ROOT / "scripts/inference/Remove-DeepSeek.py")
remover = importlib.util.module_from_spec(spec)
spec.loader.exec_module(remover)


def dump(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def tree(root, *, deepseek_primary=False):
    entries = ["config/qwen.local.json", "config/ds.local.json", "config/tools.local.json"]
    for entry, model in zip(entries, ["fixture-qwen", remover.MODELS[1], remover.MODELS[0]]):
        dump(root / entry, {"Model": model, "ModelsPath": "models", "EngineVersion": "0.40.9",
                            "ExecutablePath": sys.executable, "BaseUrl": "http://127.0.0.1:11434"})
    if deepseek_primary:
        entries = [entries[1], entries[0], entries[2]]
    path = root / "config/server.local.json"
    dump(path, {"provider": "Ollama", "manage_engine": True, "inference_config": entries[0],
                "additional_inference_configs": entries[1:], "data_directory": "data",
                "private_sentinel": "preserve", "port": 19001})
    dump(root / remover.RETIRED_LIST, ["Install-DeepSeekCoderV2Lite.cmd"])
    (root / "Install-DeepSeekCoderV2Lite.cmd").write_text("retired-source", encoding="utf-8")
    return path


def engine(tags, *, before_yield=None):
    @contextmanager
    def factory(root, cfg, work):
        if before_yield:
            before_yield()
        def fetch(endpoint):
            if endpoint == "/api/version":
                return {"version": cfg["EngineVersion"]}
            assert endpoint == "/api/tags"
            return {"models": [{"name": name, "digest": digest} for name, digest in tags.items()]}
        yield fetch
    return factory


@pytest.mark.parametrize("deepseek_primary", [False, True])
def test_removal_preserves_private_fields_other_models_and_is_repeatable(tmp_path, deepseek_primary):
    path = tree(tmp_path, deepseek_primary=deepseek_primary)
    original = path.read_bytes()
    private_configs = {p: p.read_bytes() for p in path.parent.glob("*.local.json") if p != path}
    tags = {"fixture-qwen": "unchanged", "unrelated-model": "shared-blob", **dict.fromkeys(remover.MODELS, "deepseek")}
    deleted = []
    def delete(fetch, name):
        deleted.append(name)
        del tags[name]
    result = remover.remove(tmp_path, server_config=path, engine_factory=engine(tags), delete=delete)
    assert result["passed"] and deleted == list(remover.MODELS)
    assert Path(result["backup"]).read_bytes() == original
    current = json.loads(path.read_bytes())
    assert current["inference_config"] == "config/qwen.local.json" and current["additional_inference_configs"] == []
    assert current["private_sentinel"] == "preserve" and current["port"] == 19001
    assert tags == {"fixture-qwen": "unchanged", "unrelated-model": "shared-blob"}
    assert all(p.read_bytes() == raw for p, raw in private_configs.items())
    assert not (tmp_path / "Install-DeepSeekCoderV2Lite.cmd").exists()
    retired = list((tmp_path / ".local/deepseek-removal").glob("*/retired-source/Install-DeepSeekCoderV2Lite.cmd"))
    assert len(retired) == 1 and retired[0].read_text() == "retired-source"
    again = remover.remove(tmp_path, server_config=path, engine_factory=engine(tags), delete=delete)
    assert again["passed"] and not again["deleted"] and again["backup"] is None


def test_unregistered_tool_model_and_partial_delete_are_retryable(tmp_path):
    path = tree(tmp_path)
    server = json.loads(path.read_bytes())
    server["additional_inference_configs"] = ["config/ds.local.json"]
    dump(path, server)
    tags = {"fixture-qwen": "preserve", **dict.fromkeys(remover.MODELS, "deepseek")}
    calls = []
    def delete(fetch, name):
        calls.append(name)
        if name == remover.MODELS[1]:
            raise OSError("synthetic failure")
        del tags[name]
    with pytest.raises(OSError):
        remover.remove(tmp_path, server_config=path, engine_factory=engine(tags), delete=delete)
    assert calls == list(remover.MODELS) and remover.MODELS[0] not in tags
    assert json.loads(path.read_bytes())["additional_inference_configs"] == []
    assert (tmp_path / "Install-DeepSeekCoderV2Lite.cmd").exists()
    result = remover.remove(tmp_path, server_config=path, engine_factory=engine(tags),
                            delete=lambda fetch, name: tags.pop(name))
    assert result["passed"] and result["deleted"] == [remover.MODELS[1]]


def test_active_server_and_missing_replacement_refuse_without_changes(tmp_path):
    path = tree(tmp_path)
    original = path.read_bytes()
    from aiserver.operations.lock import InstanceLock
    lock = InstanceLock(tmp_path / "data").acquire()
    try:
        with pytest.raises(ValueError, match="仍在运行"):
            remover.remove(tmp_path, server_config=path)
        assert path.read_bytes() == original
    finally:
        lock.release()
    server = json.loads(original)
    server.update(inference_config="config/ds.local.json", additional_inference_configs=[])
    dump(path, server)
    original = path.read_bytes()
    with pytest.raises(ValueError, match="没有主模型"):
        remover.remove(tmp_path, server_config=path)
    assert path.read_bytes() == original


def test_concurrent_config_change_stops_before_deleting_models(tmp_path):
    path = tree(tmp_path)
    tags = {"fixture-qwen": "preserve", remover.MODELS[1]: "deepseek"}
    target = tmp_path / "config/qwen.local.json"
    def change():
        value = json.loads(target.read_bytes())
        dump(target, {**value, "changed": True})
    deleted = []
    with pytest.raises(ValueError, match="其他操作修改"):
        remover.remove(tmp_path, server_config=path, engine_factory=engine(tags, before_yield=change),
                       delete=lambda fetch, name: deleted.append(name))
    assert not deleted


@pytest.mark.parametrize("relative", ["../outside.json", ".local/private.json", "config/server.local.json"])
def test_retired_source_list_cannot_delete_private_or_external_files(tmp_path, relative):
    path = tree(tmp_path)
    original = path.read_bytes()
    dump(tmp_path / remover.RETIRED_LIST, [relative])
    with pytest.raises(ValueError, match="不允许删除"):
        remover.remove(tmp_path, server_config=path)
    assert path.read_bytes() == original


def test_delete_uses_ollama_delete_method_and_exact_model():
    from types import SimpleNamespace
    seen = []
    @contextmanager
    def open_request(request, timeout):
        seen.append((request.method, request.full_url, json.loads(request.data), timeout))
        yield SimpleNamespace(read=lambda limit: b"")
    fetch = SimpleNamespace(base_url="http://127.0.0.1:19999", opener=SimpleNamespace(open=open_request))
    remover.delete_model(fetch, remover.MODELS[1])
    assert seen == [("DELETE", "http://127.0.0.1:19999/api/delete", {"model": remover.MODELS[1]}, 600)]
