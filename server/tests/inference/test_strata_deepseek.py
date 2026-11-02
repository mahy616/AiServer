"""DeepSeek complete preparation with tiny fixtures; no real weights or GPU work."""
import asyncio
import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import zipfile

import httpx
import pytest

from aiserver.inference.contracts import Generation
from aiserver.inference.strata import validate_config, verify_local_files, load_strata_profile, StrataBudget, StrataProvider
from aiserver.settings import ROOT, Settings

sys.path.insert(0, str(ROOT / "scripts/inference"))
import strata_prepare as install


def fixture(tmp_path, monkeypatch, family="deepseek"):
    deep = family == "deepseek"
    suffix = ".deepseek" if deep else ""
    def write(path, data):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path
    def dump(path, data):
        return write(path, install.json_bytes(data))
    dump(tmp_path / "config/server.local.json", {"environment": "Development", "provider": "Ollama",
        "manage_natapp": False, "data_directory": str(tmp_path / "data")})
    write(tmp_path / "server/src/aiserver/operations/lock.py", (ROOT / "server/src/aiserver/operations/lock.py").read_bytes())
    write(tmp_path / "scripts/inference/strata.requirements.txt", b"tiny-dependency==1.0\n")
    cfg = json.loads((ROOT / ("config/inference.strata" + suffix + ".example.json")).read_bytes())
    cfg.update(RunConfigPath=str(tmp_path / (".local/strata/deepseek/run.local.json" if deep else ".local/strata/run.local.json")),
               ModelLockPath=str(tmp_path / ("config/models/strata" + suffix + ".local.json")))
    dump(tmp_path / ("config/inference.strata" + suffix + ".local.json"), cfg)
    source = json.loads((ROOT / ("config/models/strata" + suffix + ".source.json")).read_bytes())
    runtime_files = {"requirements.txt": b"tiny-runtime", "serve/server.py": b"tiny-server",
        "tools/dsv41/pack_gguf.py": b"tiny-pack", "data/dsv41/profile-2.00.bin": b"hot-experts",
        "serve/fixtures/deepseek41/chat_template.jinja": b"canonical calls>{{ messages }}", "LICENSE": b"MIT",
        "tools/mtp_fetch.py": b"tiny-mtp-fetch", "tools/iq_pack.py": b"tiny-iq-pack"}
    archives = {}
    payloads = [("source", {"Strata-0.2.9/" + k: v for k, v in runtime_files.items()}),
                ("engine", {"strata-dsv41.exe" if deep else "strata.exe": b"tiny-exe", "BUILD.json": b'{"version":"0.1.55"}',
                            "engine-component.json": b'{"id":"strata-dsv41"}'})]
    if not deep:
        payloads.append(("gguf", {"llama.cpp-" + source["gguf_revision"] + "/gguf-py/gguf/__init__.py": b"tiny-gguf"}))
    for kind, files in payloads:
        archive = tmp_path / (kind + ".zip")
        with zipfile.ZipFile(archive, "w") as packed:
            for name, data in files.items():
                packed.writestr(name, data)
        asset = source["artifacts"][kind]
        asset.update(url="fixture:" + kind, filename=archive.name, size=archive.stat().st_size, sha256=install.digest(archive))
        archives[asset["url"]] = archive.read_bytes()
    import hashlib
    sha = lambda data: hashlib.sha256(data).hexdigest()
    source.update(requirements_sha256=sha(runtime_files["requirements.txt"]), pack_script_sha256=sha(runtime_files["tools/dsv41/pack_gguf.py"]),
                  expert_profile_sha256=sha(runtime_files["data/dsv41/profile-2.00.bin"]))
    if not deep:
        source["mtp_fetch_sha256"] = sha(runtime_files["tools/mtp_fetch.py"])
    source["artifacts"]["engine"]["manifest_sha256"] = sha(b'{"id":"strata-dsv41"}')
    for i, row in enumerate(source["model_files"]):
        content = ("tiny-weight-" + str(i)).encode()
        row.update(url="fixture:weight-" + str(i), size=len(content), sha256=sha(content))
        archives[row["url"]] = content
    dump(tmp_path / ("config/models/strata" + suffix + ".source.json"), source)
    other_suffix = "" if deep else ".deepseek"
    qwen = dump(tmp_path / ("config/inference.strata" + other_suffix + ".local.json"), {
        "RunConfigPath": str(tmp_path / (".local/strata/run.local.json" if deep else ".local/strata/deepseek/run.local.json")),
        "ModelLockPath": str(tmp_path / ("config/models/strata" + other_suffix + ".local.json"))})
    before_qwen = qwen.read_bytes()
    before_base = (tmp_path / "config/server.local.json").read_bytes()
    monkeypatch.setattr(install, "ROOT", tmp_path)
    def download(url, target, digest, size):
        data = archives[url]
        assert len(data) == size and sha(data) == digest
        if Path(target).exists():
            assert Path(target).read_bytes() == data
            return Path(target)
        return write(target, data)
    monkeypatch.setattr(install, "download", download)
    commands = []
    def run(command, **kwargs):
        commands.append(command)
        if "--target" in command:
            Path(command[command.index("--target") + 1]).mkdir(parents=True)
        elif any(str(p).endswith("pack_gguf.py") for p in command):
            assert "--skip-tensors" not in command and "--dense-override" not in command
            assert command[command.index("--dspark-noise-token") + 1] == "128799"
            assert command[command.index("--dspark-markov-rank") + 1] == "256"
            pack = Path(command[command.index("--out") + 1])
            for name in ("ds4.txt", "tensors.txt", "engram.bin"):
                write(pack / name, b"tiny-pack-record")
            write(pack / "tokenizer/chat_template.jinja", b"old template")
        elif any(str(p).endswith("iq_pack.py") for p in command):
            pack = Path(command[command.index("--out") + 1])
            write(pack / "index.txt", b"tiny-qwen-pack")
            write(pack / "tokenizer/chat_template.jinja", b"qwen-template")
        elif any(str(p).endswith("mtp_fetch.py") for p in command):
            Path(command[command.index("--out") + 1]).mkdir(parents=True, exist_ok=True)
        elif any(str(p).endswith("mtp_pack.py") for p in command):
            write(command[command.index("--out") + 1], b"tiny-mtp-gguf")
        elif any(str(p).endswith("mtp_rt.py") for p in command):
            write(Path(command[command.index("--out") + 1]) / "dense.bin", b"tiny-mtp-runtime")
    monkeypatch.setattr(install, "run", run)
    async def preflight(settings, prepared_cfg, identity):
        verify_local_files(prepared_cfg, identity)
        assert identity["family"] == ("deepseek41" if deep else "qwen4exp")
        run_cfg = json.loads(Path(prepared_cfg["RunConfigPath"]).read_bytes())
        if deep:
            assert run_cfg["args"][run_cfg["args"].index("--experts") + 1] == "6"
            assert (Path(run_cfg["tokenizer"]) / "chat_template.jinja").read_bytes() == runtime_files["serve/fixtures/deepseek41/chat_template.jinja"]
        else:
            assert "--mtp" in run_cfg["args"] and "--native" in run_cfg["args"]
    monkeypatch.setattr(install, "preflight", preflight)
    args = SimpleNamespace(server_config=str(tmp_path / "config/server.local.json"), family=family,
        context=None, max_output=None, source_models=None, install_dependencies=True)
    return args, cfg, qwen, before_qwen, before_base, commands


@pytest.mark.parametrize("family", ["qwen", "deepseek"])
def test_complete_preparation_and_failed_repeat_preserve_other_model_and_last_working_set(tmp_path, monkeypatch, family):
    args, cfg, qwen, before_qwen, before_base, commands = fixture(tmp_path, monkeypatch, family)
    report = install.prepare(args)
    assert report["prepared"] and report["context"] == cfg["NumCtx"]
    assert qwen.read_bytes() == before_qwen
    assert (tmp_path / "config/server.local.json").read_bytes() == before_base
    suffix = ".deepseek" if family == "deepseek" else ""
    config = tmp_path / ("config/inference.strata" + suffix + ".local.json")
    settings = Settings(provider="Strata", data_directory=tmp_path / "data", inference_config=config)
    saved_cfg, identity, profile = load_strata_profile(settings)
    assert profile.provider == "Strata" and saved_cfg["ExpectedQuantization"] == cfg["ExpectedQuantization"]
    if family == "deepseek":
        assert any("--pack-info" in cmd for cmd in commands)
    targets = [config, Path(cfg["ModelLockPath"]), Path(cfg["RunConfigPath"]), tmp_path / ("config/server.strata" + suffix + ".local.json")]
    before = {p: p.read_bytes() for p in targets}
    async def fail(*args):
        raise ValueError("synthetic model-load failure")
    monkeypatch.setattr(install, "preflight", fail)
    with pytest.raises(ValueError, match="synthetic model-load"):
        install.prepare(args)
    assert {p: p.read_bytes() for p in targets} == before
    verify_local_files(saved_cfg, identity)
    assert qwen.read_bytes() == before_qwen


def test_deepseek_rejects_cross_model_output_paths_and_invalid_expert_setting(tmp_path, monkeypatch):
    args, cfg, *_ = fixture(tmp_path, monkeypatch)
    path = tmp_path / "config/inference.strata.deepseek.local.json"
    cfg["RunConfigPath"] = str(tmp_path / ".local/strata/run.local.json")
    path.write_bytes(install.json_bytes(cfg))
    with pytest.raises(ValueError, match="independent"):
        install.prepare(args)
    cfg["ActiveExperts"] = "6"
    with pytest.raises(ValueError, match="explicit active experts"):
        validate_config(cfg)


def validation_entry():
    spec = importlib.util.spec_from_file_location("deepseek_validation", ROOT / "scripts/validation/Test-Strata.py")
    entry = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(entry)
    return entry


@pytest.mark.parametrize("failed", [None, "deepseek"])
def test_dual_validation_runs_sequentially_and_reports_both(tmp_path, monkeypatch, failed):
    entry = validation_entry()
    monkeypatch.setattr(entry, "ROOT", tmp_path)
    monkeypatch.setattr(sys, "argv", ["Test-Strata.py", "--all", "--benchmark"])
    seen = []
    def run_one(path, long_context, output, *, benchmark):
        name = "deepseek" if ".deepseek." in path.name else "qwen"
        seen.append(name)
        assert benchmark
        if name == failed:
            raise ValueError("synthetic failure")
        output.write_text(json.dumps({"passed": True, "model": name}))
        return True
    monkeypatch.setattr(entry, "run_one", run_one)
    assert entry.main() == (1 if failed else 0)
    summary = json.loads(next((tmp_path / ".local/strata/validation").glob("*/summary.json")).read_bytes())
    assert seen == ["qwen", "deepseek"] and summary["passed"] == (failed is None)
    assert set(summary["models"]) == {"qwen", "deepseek"}


def test_target_tool_roundtrip_uses_original_call_id_and_result():
    entry = validation_entry()
    cfg = json.loads((ROOT / "config/inference.strata.deepseek.example.json").read_bytes())
    calls = []
    class Provider:
        async def generate(self, prepared, stop, callback):
            material = json.loads(prepared.payload_json)
            messages = json.loads(material["messages_json"])
            calls.append(messages)
            if len(calls) == 1:
                return Generation("", True, "tool_calls", tool_calls=({"id": "original-id", "type": "function", "function": {
                    "name": "read_sample", "arguments": '{"path":"sample.txt"}'}},))
            assert messages[-1]["tool_call_id"] == messages[-2]["tool_calls"][0]["id"] == "original-id"
            return Generation("STRATA_TOOL_OK_42", True, "stop")
    profile = SimpleNamespace(profile_id="synthetic")
    assert asyncio.run(entry.tool_roundtrip(Provider(), StrataBudget(cfg), profile))["passed"]
    assert len(calls) == 2


def test_deepseek_live_identity_uses_pack_directory_and_exact_idle_window(tmp_path):
    async def check():
        cfg = json.loads((ROOT / "config/inference.strata.deepseek.example.json").read_bytes())
        import hashlib
        template = "canonical calls>"
        identity = {"engine_model_path": str(tmp_path / "pack"), "chat_template_sha256": hashlib.sha256(template.encode()).hexdigest()}
        provider = StrataProvider(Settings(provider="Strata", data_directory=tmp_path / "data"), cfg, identity)
        await provider.adapter.client.aclose()
        slot = {"n_ctx": cfg["NumCtx"], "is_processing": False}
        def response(request):
            return httpx.Response(200, json=[slot] if request.url.path == "/slots" else {
                "model_alias": cfg["Model"], "total_slots": 1, "build_info": "Strata 0.1.55", "is_sleeping": False,
                "default_generation_settings": {"n_ctx": cfg["NumCtx"]}, "model_path": identity["engine_model_path"],
                "chat_template": template, "chat_template_caps": {"supports_tools": True}})
        provider.adapter.client = httpx.AsyncClient(base_url=cfg["BaseUrl"], transport=httpx.MockTransport(response))
        try:
            await provider.verify_identity()
            assert provider.supports_tools and provider.supports_thinking
            slot["is_processing"] = True
            with pytest.raises(RuntimeError, match="MODEL_IDENTITY"):
                await provider.verify_identity()
        finally:
            await provider.aclose()
    asyncio.run(check())
