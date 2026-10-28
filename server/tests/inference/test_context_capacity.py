"""Synthetic capacity boundaries and safe writes; no real model or personal key."""
from contextlib import nullcontext
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from urllib.error import HTTPError

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "scripts/validation"))
from context_capacity import CapacityFailure, largest_context
from gpu_capacity_sampler import validate_samples


def load():
    spec = importlib.util.spec_from_file_location("context_tuner_test", ROOT / "scripts/validation/Tune-ModelContexts.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("limit", [32768, 65536, 82943, 131072])
def test_search_finds_largest_passing_grid_point(limit):
    checked = []
    def probe(context):
        checked.append(context)
        return context <= limit
    selected = largest_context(32768, 131072, 1024, probe)
    assert selected == (limit // 1024) * 1024
    assert selected in checked
    assert selected == 131072 or selected + 1024 in checked
    assert len(checked) <= 10


def test_unknown_failure_does_not_become_a_capacity_limit():
    with pytest.raises(RuntimeError, match="TRANSPORT"):
        largest_context(32768, 131072, 1024, lambda n: (_ for _ in ()).throw(RuntimeError("TRANSPORT")))
    with pytest.raises(CapacityFailure, match="MINIMUM"):
        largest_context(32768, 131072, 1024, lambda n: False)


def test_shared_memory_missing_data_and_spill_are_distinct():
    valid = {"shared_bytes": 0, "shared_backend_verified": True,
             "gpus": [{"memory_used_mib": 16000, "memory_total_mib": 16384}]}
    assert validate_samples([valid], 64*1024**2)["shared_peak_bytes"] == 0
    with pytest.raises(RuntimeError, match="COUNTERS"):
        validate_samples([{"shared_bytes": None, "gpus": []}], 64*1024**2)
    with pytest.raises(CapacityFailure, match="SHARED"):
        validate_samples([valid, {**valid, "shared_bytes": 65*1024**2}], 64*1024**2)


def test_engine_errors_only_classify_known_memory_failures():
    module = load()
    cfg = json.loads((ROOT / "config/inference.qwen38.example.json").read_bytes())
    def failure(message):
        def fetch(*args):
            raise HTTPError("http://127.0.0.1", 500, "failed", {}, io.BytesIO(message.encode()))
        return fetch
    with pytest.raises(CapacityFailure):
        module.generate(failure("CUDA out of memory"), cfg, "", 1)
    with pytest.raises(RuntimeError, match="ENGINE_HTTP_500"):
        module.generate(failure("model template is invalid"), cfg, "", 1)


def test_templated_input_calibration_uses_actual_usage_and_preserves_full_window():
    module = load()
    cfg = json.loads((ROOT / "config/inference.qwen38.example.json").read_bytes())
    calls = []
    def fetch(endpoint, body):
        calls.append(body)
        assert endpoint == "/api/generate" and body["raw"] is False and body["truncate"] is False and body["shift"] is False
        assert body["options"]["num_ctx"] == cfg["NumCtx"] and body["options"]["num_gpu"] == 999
        return {"done": True, "prompt_eval_count": body["prompt"].count("// entry ") * 30 + 50,
                "eval_count": min(body["options"]["num_predict"], 32), "done_reason": "stop"}
    cfg["RequireFullGpu"] = True
    result = module.long_input(fetch, cfg, True)
    assert 0 <= cfg["NumCtx"] - 16384 - cfg["ReservedTokens"] - result["actual_input_tokens"] <= 64
    assert result["probe_output_limit"] == 256 and len(calls) == 3
    assert all(body["think"] is True for body in calls)
    assert len({body["prompt"][0] for body in calls}) == len(calls)


@pytest.fixture
def deployment(tmp_path, monkeypatch):
    module = load()
    config = tmp_path / "config"
    (config / "models").mkdir(parents=True)
    paths = []
    for key, (name, cap) in module.PROFILES.items():
        filename = f"config/inference.{name}.example.json"
        cfg = json.loads((ROOT / filename).read_bytes())
        (tmp_path / filename).write_text(json.dumps(cfg))
        if key in ("9b", "qwen38"):
            (config / f"models/{name}.source.json").write_bytes((ROOT / f"config/models/{name}.source.json").read_bytes())
        identity = config / f"models/{key}.json"
        identity.write_text(json.dumps({"model": {"digest": "fixture"}, "yarn": {"qwen2.context_length": cap}}
                                       if key in ("7b", "14b") else {}))
        cfg.update(ModelLockPath=str(identity), private_marker="KEEP", NumPredict=8192)
        path = config / f"{key}.json"
        path.write_text(json.dumps(cfg))
        paths.append(path)
    server = config / "server.local.json"
    server.write_text(json.dumps({"provider": "Ollama", "inference_config": str(paths[0]),
                                 "additional_inference_configs": [str(p) for p in paths[1:]], "private_marker": "KEEP"}))
    monkeypatch.setattr(module.legacy, "offline", lambda *args: nullcontext())
    monkeypatch.setattr(module, "matching_processes", lambda *args: [])
    monkeypatch.setattr(module, "HostSampler", lambda: SimpleNamespace(sample=lambda: {"gpus": [{"name": "NVIDIA RTX 5080"}]}))
    return module, tmp_path, server, paths


@pytest.mark.parametrize("apply", [False, True])
def test_four_models_select_repeatable_boundaries_and_only_apply_after_completion(deployment, apply):
    module, root, server, paths = deployment
    original = {p: p.read_bytes() for p in [server, *paths]}
    def probe(root, cfg, identity, work, *, stress, max_shared_bytes):
        cap = 98304 if "coder" in cfg["Model"] else 196608
        assert cfg["NumPredict"] == 16384 and cfg["RequireFullGpu"]
        return {"passed": cfg["NumCtx"] <= (cap - 1024 if stress else cap), "context": cfg["NumCtx"],
                "stress": stress, "failure": "SYNTHETIC_CAPACITY_BOUNDARY",
                "memory": {"peak_used_fraction": cfg["NumCtx"] / cap}}
    report = module.tune(root, server, apply=apply, probe=probe)
    assert report["completed"] and report["applied"] is apply
    assert server.read_bytes() == original[server]
    for model, path in zip(report["models"], paths):
        expected = 93184 if model["key"] in ("7b", "14b") else 186368
        assert model["selected_context"] == expected
        assert sum(t["passed"] and t["stress"] and t["context"] == expected for t in model["trials"]) >= 2
        cfg = json.loads(path.read_bytes())
        assert cfg["private_marker"] == "KEEP"
        if apply:
            assert cfg["NumCtx"] == expected and cfg["NumPredict"] == 16384
        else:
            assert path.read_bytes() == original[path]
    if apply:
        for item in report["backups"]:
            assert Path(item["backup"]).read_bytes() == original[Path(item["config"])]


@pytest.mark.parametrize("failure", ["transport", "concurrent", "save"])
def test_failure_preserves_configs_including_other_session_updates(deployment, monkeypatch, failure):
    module, root, server, paths = deployment
    original = {p: p.read_bytes() for p in [server, *paths]}
    calls = 0
    def probe(root, cfg, identity, work, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 4 and failure == "transport":
            raise RuntimeError("TRANSPORT_FAILED")
        if calls == 4 and failure == "concurrent":
            paths[0].write_text(json.dumps({**json.loads(original[paths[0]]), "private_marker": "OTHER SESSION"}))
        return {"passed": True, "context": cfg["NumCtx"], "stress": kwargs["stress"],
                "memory": {"peak_used_fraction": 0.8}}
    if failure == "save":
        write = module.legacy.support.atomic_write
        def failed_write(path, data):
            if path == paths[1]:
                raise OSError("synthetic save failure")
            write(path, data)
        monkeypatch.setattr(module.legacy.support, "atomic_write", failed_write)
    with pytest.raises((ValueError, RuntimeError, OSError)):
        module.tune(root, server, apply=True, probe=probe)
    for path, raw in original.items():
        if failure == "concurrent" and path == paths[0]:
            assert json.loads(path.read_bytes())["private_marker"] == "OTHER SESSION"
        else:
            assert path.read_bytes() == raw


@pytest.mark.parametrize("apply", [False, True])
@pytest.mark.parametrize("failure", [None, "capacity", "engine", "second-round"])
def test_fixed_128k_sets_only_passed_models_and_returns_failures(deployment, apply, failure):
    module, root, server, paths = deployment
    original = {p: p.read_bytes() for p in [server, *paths]}
    limited = json.loads(paths[1].read_bytes())["Model"]
    calls = {}
    def probe(root, cfg, identity, work, *, stress, max_shared_bytes):
        calls[cfg["Model"]] = calls.get(cfg["Model"], 0) + 1
        assert cfg["NumCtx"] == 131072 and cfg["NumPredict"] == 16384
        assert cfg["RequireFullGpu"] and stress
        if cfg["Model"] == limited:
            if failure == "engine":
                raise RuntimeError("ENGINE_HTTP_500")
            if failure == "capacity" or (failure == "second-round" and calls[limited] == 2):
                return {"passed": False, "failure": "GPU_ALLOCATION_FAILED"}
        return {"passed": True}
    report = module.check_128k(root, server, apply=apply, probe=probe)
    assert report["completed"] and report["all_passed"] is (failure is None)
    assert report["applied"] is apply and server.read_bytes() == original[server]
    for model, path in zip(report["models"], paths):
        passed = model["key"] != "qwen38" or failure is None
        assert model["passed"] is passed and model["applied"] is (apply and passed)
        assert calls[model["model"]] == (2 if passed or failure == "second-round" else 1)
        if not passed:
            assert model["capacity_failure"] is (failure != "engine")
        if apply and passed:
            saved = json.loads(path.read_bytes())
            assert saved == {**json.loads(original[path]), "NumCtx": 131072,
                             "NumPredict": 16384, "RequireFullGpu": True}
            assert model["default_input_tokens"] == 114176
        else:
            assert path.read_bytes() == original[path]
    for item in report.get("backups", []):
        assert Path(item["backup"]).read_bytes() == original[Path(item["config"])]
    saved_report = json.loads(next((root / ".local/context-capacity").glob("128k-*/summary.json")).read_bytes())
    assert saved_report == report


def test_fixed_128k_discovers_only_registered_models_and_respects_output_override(deployment):
    module, root, server, paths = deployment
    service = json.loads(server.read_bytes())
    service["additional_inference_configs"] = []
    server.write_text(json.dumps(service))
    before = json.loads(paths[0].read_bytes())
    before["MaxOutputTokens"] = 8192
    paths[0].write_text(json.dumps(before))
    report = module.check_128k(root, server, probe=lambda *args, **kwargs: {"passed": True})
    assert [model["key"] for model in report["models"]] == ["9b"]
    saved = json.loads(paths[0].read_bytes())
    assert saved["NumCtx"] == 131072 and saved["NumPredict"] == saved["MaxOutputTokens"] == 16384


def test_fixed_128k_all_fail_without_any_config_write(deployment):
    module, root, server, paths = deployment
    original = {p: p.read_bytes() for p in [server, *paths]}
    report = module.check_128k(root, server, probe=lambda *args, **kwargs: {
        "passed": False, "failure": "NOT_FULL_GPU_OR_WINDOW"})
    assert not report["all_passed"] and not report["applied"]
    assert len(report["models"]) == 2
    assert not list((root / "config").glob("*.bak"))
    assert all(path.read_bytes() == raw for path, raw in original.items())


@pytest.mark.parametrize("failure", ["cancel", "concurrent", "save"])
def test_fixed_128k_cancellation_or_commit_failure_preserves_configs(deployment, monkeypatch, failure):
    module, root, server, paths = deployment
    original = {p: p.read_bytes() for p in [server, *paths]}
    calls = 0
    def probe(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 4 and failure == "cancel":
            raise KeyboardInterrupt()
        if calls == 4 and failure == "concurrent":
            paths[0].write_text(json.dumps({**json.loads(original[paths[0]]), "private_marker": "OTHER SESSION"}))
        return {"passed": True}
    if failure == "save":
        write = module.legacy.support.atomic_write
        def failed_write(path, data):
            if path == paths[1]:
                raise OSError("synthetic save failure")
            write(path, data)
        monkeypatch.setattr(module.legacy.support, "atomic_write", failed_write)
    with pytest.raises((KeyboardInterrupt, ValueError, OSError)):
        module.check_128k(root, server, probe=probe)
    for path, raw in original.items():
        if failure == "concurrent" and path == paths[0]:
            assert json.loads(path.read_bytes())["private_marker"] == "OTHER SESSION"
        else:
            assert path.read_bytes() == raw


@pytest.mark.parametrize("stream_error", [False, True])
def test_probe_reports_engine_context_overflow_with_explicit_code(stream_error):
    module = load()
    cfg = json.loads((ROOT / "config/inference.coder.example.json").read_bytes())
    def fetch(*args):
        if stream_error:
            return {"error": "input length exceeds the available context size"}
        raise HTTPError("http://127.0.0.1", 400, "failed", {},
                        io.BytesIO(b'{"error":"input length exceeds the available context size"}'))
    with pytest.raises(CapacityFailure, match="CONTEXT_TOO_LARGE"):
        module.generate(fetch, cfg, "", 1)


def test_long_prefill_transport_respects_generation_timeout():
    module = load()
    client = module.legacy.provision.LocalOllama("http://127.0.0.1:11434", generate_timeout=3600)
    calls = []
    def open_request(request, *, timeout):
        calls.append(timeout)
        return io.BytesIO(b'{}')
    client.opener = SimpleNamespace(open=open_request)
    client("/api/generate", {})
    client("/api/show", {})
    assert calls == [3600, 15]


@pytest.mark.parametrize("all_passed", [False, True])
def test_fixed_128k_cli_returns_nonzero_for_partial_failure(monkeypatch, all_passed):
    module = load()
    def run(**kwargs):
        assert kwargs["apply"] is True
        return {"all_passed": all_passed, "applied": True}
    monkeypatch.setattr(module, "check_128k", run)
    monkeypatch.setattr(sys, "argv", ["Tune-ModelContexts.py", "--context", "131072", "--apply"])
    assert module.main() == (0 if all_passed else 1)


@pytest.mark.parametrize("exit_code", [0, 1])
@pytest.mark.parametrize("entry,relative,options", [
    ("Test-ModelContexts128K.cmd", "scripts/validation/Tune-ModelContexts.py", ["--context", "131072", "--apply"]),
    ("Apply-5080ModelContexts.cmd", "scripts/inference/Repair-InferenceRuntime.py", ["--recommended-contexts"]),
    ("Use-9B27B.cmd", "scripts/inference/Use-9B27B.py", []),
])
@pytest.mark.skipif(os.name != "nt", reason="Windows one-click launcher")
def test_context_windows_launchers_pass_options_and_preserve_exit_code(tmp_path, exit_code, entry, relative, options):
    root = tmp_path / "server with spaces"
    script = root / relative
    prefs = root / ".local/server-manager/launcher.json"
    script.parent.mkdir(parents=True)
    prefs.parent.mkdir(parents=True)
    prefs.write_text(json.dumps({"python": sys.executable}), encoding="utf-8")
    script.write_text("import json, sys\nfrom pathlib import Path\n"
                      "Path(__file__).with_suffix('.argv.json').write_text(json.dumps(sys.argv[1:]))\n"
                      f"raise SystemExit({exit_code})\n", encoding="utf-8")
    launcher = root / entry
    launcher.write_bytes((ROOT / launcher.name).read_bytes())
    result = subprocess.run([os.environ["COMSPEC"], "/d", "/c", str(launcher)], cwd=tmp_path,
                            input=b"\n", capture_output=True, timeout=30)
    assert result.returncode == exit_code, (result.stdout + result.stderr).decode("utf-8", "replace")
    assert json.loads(script.with_suffix(".argv.json").read_bytes()) == options
