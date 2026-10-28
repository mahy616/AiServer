"""Two-model deployment migration; synthetic configs only, no real model load."""
from contextlib import nullcontext
import importlib.util
import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture
def deployment(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("two_models_test", ROOT / "scripts/inference/Use-9B27B.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    (tmp_path / "config/models").mkdir(parents=True)
    files = []
    for name in ("coder", "coder14b", "qwen35", "qwen38"):
        relative = f"config/inference.{name}.example.json"
        raw = (ROOT / relative).read_bytes()
        (tmp_path / relative).write_bytes(raw)
        cfg = json.loads(raw)
        cfg.update(NumCtx=32768, NumPredict=8192, MaxOutputTokens=4096, NumBatch=32, private_marker="KEEP")
        identity = tmp_path / cfg["ModelLockPath"]
        identity.write_text('{"private_identity":"KEEP"}')
        path = tmp_path / f"config/inference.{name}.local.json"
        path.write_text(json.dumps(cfg))
        files.append(path)
    server = tmp_path / "config/server.local.json"
    server.write_text(json.dumps({"provider": "Ollama", "inference_config": files[0].relative_to(tmp_path).as_posix(),
        "additional_inference_configs": [p.relative_to(tmp_path).as_posix() for p in files[1:]],
        "data_directory": ".local/private", "port": 18888, "private_marker": "KEEP"}))
    manifest = tmp_path / "scripts/deployment/coder-retired-files.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_bytes((ROOT / "scripts/deployment/coder-retired-files.json").read_bytes())
    (tmp_path / "Install-Qwen25Coder7B.cmd").write_text("old entry")
    weights = tmp_path / ".local/ollama/models/keep.bin"
    weights.parent.mkdir(parents=True)
    weights.write_bytes(b"MODEL_FILE_PRESERVED")
    monkeypatch.setattr(module.legacy, "offline", lambda *args: nullcontext())
    return module, tmp_path, server, files, weights


def test_migration_keeps_only_9b_27b_with_limits_backups_and_idempotency(deployment):
    module, root, server, files, weights = deployment
    before = {p: p.read_bytes() for p in [server, *files]}
    changed = module.migrate(root, server)
    assert len(changed) == 3
    expected_server = {**json.loads(before[server]), "inference_config": files[2].relative_to(root).as_posix(),
                       "additional_inference_configs": [files[3].relative_to(root).as_posix()]}
    assert json.loads(server.read_bytes()) == expected_server
    for path, context in zip(files[2:], (131072, 65536)):
        assert json.loads(path.read_bytes()) == {**json.loads(before[path]), "NumCtx": context,
                                                 "NumPredict": 16384, "MaxOutputTokens": 16384}
    assert all(p.read_bytes() == before[p] for p in files[:2])
    assert weights.read_bytes() == b"MODEL_FILE_PRESERVED"
    assert not (root / "Install-Qwen25Coder7B.cmd").exists()
    retired = list((root / ".local/two-model-upgrade").glob("*/Install-Qwen25Coder7B.cmd"))
    assert len(retired) == 1 and retired[0].read_text() == "old entry"
    for item in changed:
        assert Path(item["backup"]).read_bytes() == before[Path(item["config"])]
    assert module.migrate(root, server) == []


@pytest.mark.parametrize("failure", ["save", "concurrent", "active", "missing_model", "missing_lock", "different_engine"])
def test_migration_failure_preserves_configs_and_retired_entries(deployment, monkeypatch, failure):
    module, root, server, files, weights = deployment
    if failure == "missing_model":
        value = json.loads(server.read_bytes())
        value["additional_inference_configs"] = value["additional_inference_configs"][:-1]
        server.write_text(json.dumps(value))
        files[3].unlink()
    if failure == "missing_lock":
        cfg = json.loads(files[3].read_bytes())
        (root / cfg["ModelLockPath"]).unlink()
    if failure == "different_engine":
        cfg = json.loads(files[3].read_bytes())
        files[3].write_text(json.dumps({**cfg, "EngineVersion": "different"}))
    before = {p: p.read_bytes() for p in [server, *files] if p.is_file()}
    if failure == "save":
        write = module.legacy.support.atomic_write
        def fail_server(path, data):
            if path == server:
                raise OSError("synthetic save failure")
            write(path, data)
        monkeypatch.setattr(module.legacy.support, "atomic_write", fail_server)
    if failure == "concurrent":
        unchanged = module.legacy.unchanged
        calls = 0
        def concurrent(expected):
            nonlocal calls
            calls += 1
            if calls == 3:
                files[0].write_text(json.dumps({**json.loads(before[files[0]]), "private_marker": "OTHER SESSION"}))
            unchanged(expected)
        monkeypatch.setattr(module.legacy, "unchanged", concurrent)
    if failure == "active":
        monkeypatch.setattr(module.legacy, "offline", lambda *args: (_ for _ in ()).throw(RuntimeError("active instance")))
    with pytest.raises((OSError, ValueError, RuntimeError)):
        module.migrate(root, server)
    for path, raw in before.items():
        if failure == "concurrent" and path == files[0]:
            assert json.loads(path.read_bytes())["private_marker"] == "OTHER SESSION"
        else:
            assert path.read_bytes() == raw
    assert (root / "Install-Qwen25Coder7B.cmd").read_text() == "old entry"
    assert weights.read_bytes() == b"MODEL_FILE_PRESERVED"


def test_registration_never_rediscovers_unused_coder_configs(deployment):
    _, root, server, files, _ = deployment
    from aiserver.settings import Settings
    spec = importlib.util.spec_from_file_location("two_model_registration", ROOT / "scripts/inference/model_registration.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    current = {**json.loads(server.read_bytes()), "inference_config": files[2].relative_to(root).as_posix(),
               "additional_inference_configs": [files[3].relative_to(root).as_posix()]}
    updated, _ = module.registration(root, current, files[2], json.loads(files[2].read_bytes()))
    assert updated["inference_config"] == current["inference_config"]
    assert updated["additional_inference_configs"] == current["additional_inference_configs"]
    assert Settings().inference_config.name == "inference.qwen35.example.json"


def test_new_benchmark_prepares_only_active_models_without_reading_keys(tmp_path, monkeypatch):
    sys.path.insert(0, str(ROOT / "scripts/validation"))
    spec = importlib.util.spec_from_file_location("two_model_benchmark", ROOT / "scripts/validation/Benchmark-CSharp.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    output = tmp_path / "benchmark"
    monkeypatch.setattr(sys, "argv", ["Benchmark-CSharp.py", "--prepare-only", "--output", str(output)])
    assert module.main() == 0
    state = json.loads((output / ".state.json").read_bytes())
    assert state["selected_models"] == ["9b", "qwen38"]
    assert set(state["conditions"]) == {"9b", "qwen38", "9b-thinking", "qwen38-thinking"}
    monkeypatch.setattr(sys, "argv", ["Benchmark-CSharp.py", "--prepare-only", "--models", "7b"])
    with pytest.raises(SystemExit) as result:
        module.main()
    assert result.value.code == 2
