import asyncio
from dataclasses import replace
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
from unittest.mock import patch, AsyncMock

import httpx
import numpy as np
import pytest

from aiserver.api.models import model_description
from aiserver.api.openai import ChatValidator
from aiserver.api.validation import RequestValidator, canonical
from aiserver.context.coder_budget import CoderCounter
from aiserver.context.prompt_builder import prepare_messages
from aiserver.inference.configuration import engine_environment, process_signature
from aiserver.inference.runtime import PracticalBudget, load_profile
from aiserver.settings import ROOT, Settings
from aiserver.inference.contracts import TaskInput, Profile


def config():
    return json.loads((ROOT / "config/inference.coder.example.json").read_text(encoding="utf-8"))


def test_process_options_are_explicit_and_conflicts_rejected():
    cfg = config()
    with patch.dict("os.environ", {"OLLAMA_KV_CACHE_TYPE": "q4_0", "OLLAMA_FLASH_ATTENTION": "0"}):
        env = engine_environment(cfg, ROOT)
    assert env["OLLAMA_FLASH_ATTENTION"] == "1" and env["OLLAMA_KV_CACHE_TYPE"] == "q8_0"
    assert env["OLLAMA_NUM_PARALLEL"] == "1"
    other = {**cfg, "KvCacheType": "f16"}
    assert process_signature(cfg) != process_signature(other)
    with pytest.raises(ValueError, match="FlashAttention"):
        engine_environment({**cfg, "FlashAttention": False}, ROOT)
    with pytest.raises(ValueError, match="NumBatch"):
        engine_environment({**cfg, "NumBatch": 0}, ROOT)


def test_large_unicode_native_and_openai_materials_pass_global_byte_guard():
    message = "中文code\n" * 18000
    native = {"clientRequestId": "416417b5-e936-4cde-b0df-969281936318", "model": "coder",
              "messages": [{"role": "user", "content": [{"type": "text", "text": message}]}]}
    RequestValidator(ROOT / "contracts/api/v1").validate(canonical(native).encode())
    chat = {"model": "coder", "messages": [{"role": "user", "content": message}]}
    ChatValidator(ROOT / "contracts/api/v1").validate(canonical(chat).encode())
    chat["messages"][0]["content"] = "中" * 700000
    from aiserver.api.errors import ApiError
    with pytest.raises(ApiError) as raised:
        ChatValidator(ROOT / "contracts/api/v1").validate(canonical(chat).encode())
    assert raised.value.code == "REQUEST_TOO_LARGE"


def test_coder_budget_counts_the_engine_rendered_prompt_and_tools(tmp_path):
    # A tiny WordLevel tokenizer verifies boundary accounting without downloading.
    from tokenizers import Tokenizer, models, pre_tokenizers
    tokenizer = Tokenizer(models.WordLevel({"[UNK]": 0, "system": 1, "tool": 2, "code": 3}, unk_token="[UNK]"))
    tokenizer.pre_tokenizer = pre_tokenizers.Whitespace()
    raw = tokenizer.to_str().encode()
    path = tmp_path / "tokenizer.json"
    path.write_bytes(raw)
    cfg = config()
    cfg["Budget"].update(TokenizerPath=str(path), TokenizerSha256=hashlib.sha256(raw).hexdigest())
    counter = CoderCounter(cfg, ROOT)
    request = {"protocol": "openai-chat-v1", "messages": [{"role": "user", "content": [{"type": "text", "text": "code"}]}],
               "tools": [{"type": "function", "function": {"name": "read", "parameters": {"type": "object"}}}]}
    prompt = prepare_messages(request, (), cfg["ImagePolicy"])
    actual_client = httpx.AsyncClient
    expected_output = cfg["NumPredict"]
    def handler(req):
        data = json.loads(req.content)
        assert data["_debug_render_only"] and data["truncate"] is False and data["shift"] is False
        assert data["tools"] and data["options"]["num_batch"] == 512
        assert data["options"]["num_predict"] == expected_output
        return httpx.Response(200, json={"model": cfg["Model"], "_debug_info": {"rendered_template": "system tool code"}})
    with patch("aiserver.context.coder_budget.httpx.AsyncClient", side_effect=lambda **kwargs: actual_client(
            **kwargs, transport=httpx.MockTransport(handler))):
        assert asyncio.run(counter.count(prompt)) == 3
        expected_output = 128
        assert asyncio.run(counter.count(replace(prompt, options_json='{"num_predict":128}'))) == 3
    profile = Profile("test", "Ollama", "{}", ("text",))
    task = TaskInput(canonical(request), ())
    budget = PracticalBudget(cfg)
    with patch.object(budget.counter, "count", AsyncMock(return_value=64000)):
        decision = asyncio.run(budget.prepare(task, profile))
        assert decision.prepared.input_tokens is None and decision.prepared.estimated_input_tokens == 64000
    with patch.object(budget.counter, "count", AsyncMock(return_value=114176)):
        assert asyncio.run(budget.prepare(task, profile)).prepared is not None
    with patch.object(budget.counter, "count", AsyncMock(return_value=114177)):
        assert asyncio.run(budget.prepare(task, profile)).code == "CONTEXT_TOO_LARGE"
    path.write_text("corrupt")
    with pytest.raises(ValueError, match="identity"):
        CoderCounter(cfg, ROOT)


@pytest.mark.parametrize("layers", [28, 48])
def test_yarn_metadata_copy_preserves_tensors(tmp_path, layers):
    import gguf
    sys.path.insert(0, str(ROOT / "scripts/inference"))
    from coder_support import make_yarn, YARN
    source, target = tmp_path / "source.gguf", tmp_path / "yarn.gguf"
    writer = gguf.GGUFWriter(str(source), "qwen2")
    writer.add_block_count(layers)
    writer.add_context_length(32768)
    writer.add_tensor("test.weight", np.arange(64, dtype=np.float32).reshape(8, 8))
    writer.write_header_to_file(); writer.write_kv_data_to_file(); writer.write_tensors_to_file(); writer.close()
    make_yarn(source, target, chat_template="{{ messages }}", expected_layers=layers)
    reader = gguf.GGUFReader(str(target))
    assert all(reader.get_field(k).contents() == v for k, v in YARN.items())
    np.testing.assert_equal(reader.tensors[0].data, np.arange(64, dtype=np.float32).reshape(8, 8))


def test_text_model_advertises_no_images():
    cfg = config()
    profile = Profile("test", "Ollama", json.dumps({"config": cfg}), ("text",))
    value = model_description(profile, Settings())
    assert value["limits"]["maxImages"] == 0 and value["limits"]["imageMediaTypes"] == []
    assert value["limits"]["maxTextUtf8Bytes"] == 2097152


def test_coder_delivery_is_complete_and_help_runs_from_extracted_package(tmp_path):
    import subprocess
    import zipfile
    spec = importlib.util.spec_from_file_location("coder_delivery", ROOT / "scripts/deployment/Build-Update.py")
    builder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(builder)
    result = builder.build(output=tmp_path / "delivery")
    with zipfile.ZipFile(result["zip"]) as archive:
        names = set(archive.namelist())
        assert {"payload/Use-9B27B.cmd", "payload/scripts/inference/Use-9B27B.py",
                "payload/Install-Qwen35.cmd", "payload/Install-Qwen38.cmd",
                "payload/config/inference.qwen35.example.json", "payload/config/inference.qwen38.example.json",
                "payload/scripts/inference/coder_support.py", "payload/contracts/examples/opencode.example.json",
                "payload/Test-ModelContexts128K.cmd", "payload/scripts/validation/Tune-ModelContexts.py",
                "payload/Apply-5080ModelContexts.cmd", "payload/scripts/validation/context_capacity.py",
                "payload/scripts/validation/gpu_capacity_sampler.py"}.issubset(names)
        retired = json.loads(archive.read("payload/scripts/deployment/coder-retired-files.json"))
        assert not any("payload/" + name in names for name in retired)
        assert not any(".local" in Path(n).parts or n.endswith(".gguf") or ".local.json" in n for n in names)
        archive.extractall(tmp_path / "unpacked")
    root = tmp_path / "unpacked/payload"
    answer = subprocess.run([sys.executable, str(root / "scripts/inference/Install-Qwen25Coder7B.py"), "--help"],
                            cwd=tmp_path, capture_output=True, timeout=20)
    assert answer.returncode == 0 and b"--context" in answer.stdout
    for script in ("scripts/inference/Use-9B27B.py", "scripts/inference/Install-Qwen38.py",
                   "scripts/validation/Tune-ModelContexts.py", "scripts/inference/Repair-InferenceRuntime.py"):
        answer = subprocess.run([sys.executable, str(root / script), "--help"], cwd=tmp_path,
                                capture_output=True, timeout=20)
        assert answer.returncode == 0, answer.stderr
        if script.endswith("Repair-InferenceRuntime.py"):
            assert b"--recommended-contexts" in answer.stdout
        if script.endswith("Tune-ModelContexts.py"):
            assert b"--context" in answer.stdout
    for script in ("scripts/inference/Install-Qwen35.py",
                   "scripts/validation/Compare-Models.py", "scripts/client/OpenCode.py"):
        assert "payload/" + script in names
        answer = subprocess.run([sys.executable, str(root / script), "--help"], cwd=tmp_path,
                                capture_output=True, timeout=20)
        assert answer.returncode == 0, answer.stderr


@pytest.mark.parametrize("variant,failure", [(v, f) for v in ("7b", "14b") for f in (None, "save", "concurrent")]
                         + [("14b", "gpu"), ("14b", "context"), ("14b", "allocation")])
def test_switch_preserves_private_fields_backup_and_failure_state(tmp_path, monkeypatch, failure, variant):
    from contextlib import contextmanager, nullcontext
    sys.path.insert(0, str(ROOT / "scripts/inference"))
    spec = importlib.util.spec_from_file_location("install_coder_test", ROOT / "scripts/inference/Install-Qwen25Coder7B.py")
    install = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(install)
    selected = install.VARIANTS[variant]
    options = {"variant": variant}
    if failure == "allocation":
        options.update(context=49152, batch_size=64, kv_cache="q8_0")
    selected_cfg = json.loads((ROOT / selected["template"]).read_text(encoding="utf-8"))
    (tmp_path / "config").mkdir()
    primary = json.loads((ROOT / "config/inference.5080.example.json").read_text())
    (tmp_path / "config/old.json").write_text(json.dumps(primary))
    primary_lock = tmp_path / primary["ModelLockPath"]
    primary_lock.parent.mkdir(parents=True, exist_ok=True)
    primary_lock.write_bytes((ROOT / primary["ModelLockPath"]).read_bytes())
    (tmp_path / "old8b").write_text(json.dumps({**primary, "Model": "fixture-old-8b"}))
    (tmp_path / selected["template"]).write_text(json.dumps(selected_cfg))
    server = {"provider": "Ollama", "inference_config": "config/old.json", "additional_inference_configs": ["old8b"],
              "port": 18888, "data_directory": ".local/private", "public_base_url": "https://example.test"}
    path = tmp_path / "config/server.local.json"
    original = json.dumps(server).encode()
    path.write_bytes(original)
    work = tmp_path / "work"; work.mkdir()
    source_template = '{{ .Function }} within <tool_call></tool_call> with NO other text. Do not include any backticks or ```json.'
    template = install.compatible_template(source_template)
    (work / "ollama.template").write_text(source_template)
    (work / "LICENSE.txt").write_text("fixture-license")
    source_lock = {"template_sha256": hashlib.sha256(template.encode()).hexdigest()}
    monkeypatch.setattr(install, "source_files", lambda *args, **kwargs: (None, work, source_lock))
    monkeypatch.setattr(install, "prepared_blob", lambda *args: (None, {"derived_sha256": "a"*64}))
    monkeypatch.setattr(install.legacy, "offline", lambda *args: nullcontext())
    if failure == "save":
        monkeypatch.setattr(install.support, "atomic_write", lambda *args: (_ for _ in ()).throw(OSError("fixture save failure")))
    @contextmanager
    def engine(root, cfg, operation):
        if failure == "allocation":
            assert (cfg["NumCtx"], cfg["NumBatch"], cfg["KvCacheType"]) == (49152, 64, "q8_0")
        if failure == "concurrent":
            path.write_text(json.dumps({**server, "port": 19999}))
        entry = {"name": selected["model"], "digest": "b"*64}
        def fetch(endpoint, body=None):
            if endpoint == "/api/version": return {"version": "0.40.9"}
            if endpoint == "/api/tags": return {"models": [entry]}
            if endpoint == "/api/show":
                return {"details": {"quantization_level": selected["quantization"]}, "capabilities": ["completion", "tools"],
                        "template": template, "model_info": install.YARN,
                        "modelfile": 'FROM "'+str(tmp_path / ("sha256-" + "a"*64))+'"\n'}
            if endpoint == "/api/generate":
                assert body["prompt"] == "" and body["options"]["num_gpu"] == 999
                assert body["options"]["num_ctx"] == cfg["NumCtx"] and body["options"]["num_batch"] == cfg["NumBatch"]
                return {"done": True}
            if endpoint == "/api/ps":
                return {"models": [{"name": selected["model"], "size": 1000,
                                    "size_vram": 900 if failure == "gpu" else 1000,
                                    "context_length": 32768 if failure == "context" else cfg["NumCtx"]}]}
            raise AssertionError(endpoint)
        yield fetch
    if failure in ("save", "concurrent", "gpu", "context"):
        with pytest.raises((OSError, ValueError)):
            install.install(tmp_path, server_config=path, engine_factory=engine, **options)
        assert json.loads(path.read_text())["port"] == (19999 if failure == "concurrent" else 18888)
        assert not (tmp_path / selected["config"]).exists()
    else:
        result = install.install(tmp_path, server_config=path, engine_factory=engine, **options)
        if failure is None:
            assert json.loads(Path(result["config"]).read_bytes())["NumCtx"] == selected_cfg["NumCtx"]
        updated = json.loads(path.read_text())
        assert updated["port"] == 18888 and updated["data_directory"] == ".local/private"
        assert updated["additional_inference_configs"] == ["config/old.json", "old8b"]
        assert updated["execution_timeout_seconds"] == 3600
        assert updated["submissions_per_minute"] == 60
        assert updated["inference_config"] == selected["config"]
        assert Path(result["backup"]).read_bytes() == original
        tuned = json.loads((tmp_path / selected["config"]).read_text())
        tuned.update(NumBatch=64, NumCtx=49152, MaxInputTokens=32768)
        (tmp_path / selected["config"]).write_text(json.dumps(tuned))
        again = install.install(tmp_path, server_config=path, engine_factory=engine, **options)
        assert again["backup"] is None
        assert json.loads((tmp_path / selected["config"]).read_text()) == tuned
    assert json.loads((tmp_path / "config/old.json").read_text()) == primary
