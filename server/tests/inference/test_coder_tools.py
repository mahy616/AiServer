"""Coder template migration preserves model weights/configuration and rolls back as a unit."""
from contextlib import contextmanager, nullcontext
import hashlib
import importlib.util
import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "scripts/inference"))
from coder_support import compatible_template, legacy_json_template, TOOL_TEMPLATE_VERSION, YARN

SOURCE = '''{{ .Function }}
within <tool_call></tool_call> with NO other text. Do not include any backticks or ```json.
{{ if .Content }}{{ .Content }}
{{- else if .ToolCalls }}<tool_call>
{{ range .ToolCalls }}{"name": "{{ .Function.Name }}", "arguments": {{ .Function.Arguments }}}
{{ end }}</tool_call>
{{- end }}'''


def load():
    spec = importlib.util.spec_from_file_location("repair_tools_test", ROOT / "scripts/inference/Repair-CoderTools.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_xml_template_preserves_prose_calls_and_serializes_typed_objects():
    value = compatible_template(SOURCE)
    assert "{{ json .Function }}" in value
    assert "{{ if .Content }}{{ .Content }}{{ end }}" in value
    assert '{{ range .ToolCalls }}<tool_call>\n{"name": {{ json .Function.Name }}, "arguments": {{ json .Function.Arguments }}}\n</tool_call>\n{{ end }}' in value
    assert "within <tool_call></tool_call>" in value
    assert "<tool_call>" not in legacy_json_template(SOURCE)
    with pytest.raises(ValueError, match="shape changed"):
        compatible_template("unexpected template")


@pytest.mark.parametrize("failure", [None, "second_model", "save", "foreign_template"])
def test_repair_is_atomic_reuses_weights_and_preserves_private_tuning(tmp_path, monkeypatch, failure):
    module = load()
    server = tmp_path / "config/server.local.json"
    server.parent.mkdir()
    profiles, originals = {}, {}
    for variant in ("7b", "14b"):
        cfg = json.loads((ROOT / module.install.VARIANTS[variant]["template"]).read_bytes())
        cfg.update(NumBatch=32, NumCtx=49152, private_marker="KEEP")
        config = tmp_path / f"config/{variant}.json"
        config.write_text(json.dumps(cfg))
        manifest = tmp_path / cfg["ModelsPath"] / "manifests/registry.ollama.ai/library" / cfg["Model"].replace(":", "/")
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text(json.dumps({"weights": "PINNED_UNCHANGED_" + variant}))
        lock = tmp_path / cfg["ModelLockPath"]
        lock.parent.mkdir(parents=True, exist_ok=True)
        template = legacy_json_template(SOURCE) if failure != "foreign_template" else "CUSTOM"
        lock.write_text(json.dumps({"model": {"digest": hashlib.sha256(manifest.read_bytes()).hexdigest()},
            "template_sha256": hashlib.sha256(template.encode()).hexdigest(), "tool_template_version": "qwen25-json-tools-v1",
            "yarn": YARN, "provenance": {"derived_sha256": "KEEP_WEIGHTS"}, "private_marker": "KEEP"}))
        pin = tmp_path / f"config/models/qwen25coder{variant}.source.json"
        pin.write_text(json.dumps({"template_sha256": hashlib.sha256(SOURCE.encode()).hexdigest()}))
        cache = tmp_path / module.install.VARIANTS[variant]["directory"] / "source/ollama.template"
        cache.parent.mkdir(parents=True)
        cache.write_text(SOURCE)
        profiles[cfg["Model"]] = {"cfg": cfg, "manifest": manifest, "template": template}
        originals.update({path: path.read_bytes() for path in (config, manifest, lock)})
    server.write_text(json.dumps({"provider": "Ollama", "inference_config": "config/7b.json",
                                 "additional_inference_configs": ["config/14b.json"], "private_marker": "KEEP"}))
    originals[server] = server.read_bytes()
    monkeypatch.setattr(module.install.legacy, "offline", lambda *args: nullcontext())
    monkeypatch.setattr(module, "download", lambda url, path, *args: path)
    @contextmanager
    def engine(root, cfg, work):
        current = profiles[cfg["Model"]]
        def fetch(endpoint, body=None):
            if endpoint == "/api/version":
                return {"version": "0.40.9"}
            if endpoint == "/api/show":
                return {"template": current["template"], "details": {"quantization_level": cfg["ExpectedQuantization"]},
                        "capabilities": ["completion", "tools"], "model_info": YARN}
            if endpoint == "/api/create":
                assert body["from"] == cfg["Model"] and "files" not in body
                if failure == "second_model" and "14b" in cfg["Model"]:
                    raise ValueError("synthetic second model failure")
                current["template"] = body["template"]
                return {"status": "success"}
            if endpoint == "/api/chat":
                if failure == "second_model" and "14b" in cfg["Model"]:
                    return {"done": True, "done_reason": "stop", "message": {"content": "fake tool text"}}
                if body["messages"][-1]["role"] == "tool":
                    message = {"content": body["messages"][-1]["content"]}
                else:
                    message = {"content": "I will read it.", "role": "assistant", "tool_calls": [
                        {"function": {"name": "read", "arguments": {"filePath": "tool-proof.txt"}}}]}
                return {"done": True, "done_reason": "stop", "message": message}
            if endpoint == "/api/tags":
                return {"models": [{"name": cfg["Model"], "digest": hashlib.sha256(current["manifest"].read_bytes()).hexdigest()}]}
            if endpoint == "/api/ps":
                return {"models": [{"name": cfg["Model"], "size": 100, "size_vram": 100, "context_length": cfg["NumCtx"]}]}
            raise AssertionError(endpoint)
        yield fetch
    if failure == "save":
        original_write = module.install.support.atomic_write
        failed = False
        def fail_once(path, data):
            nonlocal failed
            if not failed and path.name.endswith(".local.json"):
                failed = True
                raise OSError("synthetic save failure")
            return original_write(path, data)
        monkeypatch.setattr(module.install.support, "atomic_write", fail_once)
    if failure:
        with pytest.raises((ValueError, OSError)):
            module.repair(tmp_path, server, engine_factory=engine)
        assert all(path.read_bytes() == raw for path, raw in originals.items())
    else:
        changed = module.repair(tmp_path, server, engine_factory=engine)
        assert len(changed) == 2
        for current in profiles.values():
            lock = tmp_path / current["cfg"]["ModelLockPath"]
            identity = json.loads(lock.read_bytes())
            assert identity["tool_template_version"] == TOOL_TEMPLATE_VERSION
            assert identity["private_marker"] == "KEEP" and identity["provenance"]["derived_sha256"] == "KEEP_WEIGHTS"
        for path, raw in originals.items():
            if path.name in ("7b.json", "14b.json", "server.local.json"):
                assert path.read_bytes() == raw
        assert module.repair(tmp_path, server, engine_factory=engine) == []
