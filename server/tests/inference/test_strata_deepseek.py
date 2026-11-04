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


def fixture(tmp_path, monkeypatch, family="deepseek", layout="four-shard"):
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
        "tools/dsv41/pack_gguf.py": b"tiny-pack", "data/dsv41/profile-2.00.bin": b"\x01\x00\x00\x00" * (43 * 384),
        "serve/fixtures/deepseek41/chat_template.jinja": b"canonical calls>\r\n{{ messages }}\r\n", "LICENSE": b"MIT",
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
    for key, rows in {"four-shard": source["model_files"], **source.get("model_file_sets", {})}.items():
        for i, row in enumerate(rows):
            content = ("tiny-weight-" + key + str(i)).encode()
            row.update(url="fixture:weight-" + key + str(i), size=len(content), sha256=sha(content))
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
            folder = Path(command[command.index("--out") + 1])
            folder.mkdir(parents=True, exist_ok=True)
            if "fetch" in command:
                write(folder / "tensors/tiny.bin", b"tiny-mtp")
                dump(folder / "mtp-manifest.json", [{"file": "tensors/tiny.bin", "bytes": 8}])
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
            assert "--profile-readonly" not in run_cfg["args"]
    monkeypatch.setattr(install, "preflight", preflight)
    args = SimpleNamespace(server_config=str(tmp_path / "config/server.local.json"), family=family,
        context=None, max_output=None, source_models=None, install_dependencies=True,
        deepseek_layout=layout if deep else None)
    return args, cfg, qwen, before_qwen, before_base, commands


@pytest.mark.parametrize("layout", ["four-shard", "six-shard"])
@pytest.mark.parametrize("tamper", [False, True])
def test_deepseek_profile_stays_pinned_across_preflight_and_serving(tmp_path, monkeypatch, layout, tamper):
    """Reproduce 66048 -> 61440 on each ds4 request; readonly must prevent it."""
    import os
    args, _, other_config, before_other, before_base, _ = fixture(tmp_path, monkeypatch, layout=layout)
    cfg_path = tmp_path / "config/inference.strata.deepseek.local.json"
    before_cfg = cfg_path.read_bytes()
    seen = []
    def engine_request(cfg, identity):
        run = json.loads(Path(cfg["RunConfigPath"]).read_bytes())
        profile = Path(identity["engine_model_path"]) / "expert-profile.bin"
        seen.append(profile)
        if "--profile-readonly" not in run["args"]:
            profile.write_bytes(b"\x02\x00\x00\x00" * (40 * 384))
    async def preflight(settings, cfg, identity):
        verify_local_files(cfg, identity)
        engine_request(cfg, identity)
        if tamper:
            # A readonly flag must not turn into an exemption from integrity checks.
            profile = seen[-1]
            stat = profile.stat()
            profile.write_bytes(b"\x03" + profile.read_bytes()[1:])
            os.utime(profile, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    monkeypatch.setattr(install, "preflight", preflight)
    if tamper:
        with pytest.raises(ValueError, match="MODEL_IDENTITY_CHECK_FAILED") as error:
            install.prepare(args)
        assert error.value.identity_details["local_check"] == "file_sha256"
        assert cfg_path.read_bytes() == before_cfg
        assert not (tmp_path / "config/models/strata.deepseek.local.json").exists()
    else:
        assert install.prepare(args)["prepared"]
        settings = Settings(provider="Strata", data_directory=tmp_path / "data", inference_config=cfg_path)
        cfg, identity, _ = load_strata_profile(settings)
        # The published configuration must keep subsequent real-service restarts safe too.
        engine_request(cfg, identity)
        verify_local_files(cfg, identity)
        profile = seen[-1]
        seed = Path(cfg["RuntimePath"]) / "data/dsv41/profile-2.00.bin"
        assert profile.stat().st_size == 66048 and profile.read_bytes() == seed.read_bytes()
        record = next(row for row in identity["files"] if Path(row["path"]) == profile)
        assert record["startup_hash"] is True and record["sha256"] == install.digest(seed)
    assert other_config.read_bytes() == before_other
    assert (tmp_path / "config/server.local.json").read_bytes() == before_base


@pytest.mark.parametrize("family,layout", [("qwen", None), ("deepseek", "four-shard"), ("deepseek", "six-shard")])
@pytest.mark.parametrize("external_storage", [False, True])
def test_complete_preparation_and_failed_repeat_preserve_other_model_and_last_working_set(tmp_path, monkeypatch, family, layout, external_storage):
    args, cfg, qwen, before_qwen, before_base, commands = fixture(tmp_path, monkeypatch, family, layout)
    storage = tmp_path / "selected SSD with spaces" if external_storage else tmp_path / ".local/strata"
    if external_storage:
        args.storage_root = storage
        original_download = install.download
        def download(url, target, *values):
            assert Path(target).is_relative_to(storage)
            with pytest.raises(RuntimeError, match="active instance"):
                install.InstanceLock(storage / "preparation").acquire()
            return original_download(url, target, *values)
        monkeypatch.setattr(install, "download", download)
    report = install.prepare(args)
    assert report["prepared"] and report["context"] == cfg["NumCtx"]
    assert report["storage_root"] == str(storage)
    assert qwen.read_bytes() == before_qwen
    assert (tmp_path / "config/server.local.json").read_bytes() == before_base
    suffix = ".deepseek" if family == "deepseek" else ""
    config = tmp_path / ("config/inference.strata" + suffix + ".local.json")
    settings = Settings(provider="Strata", data_directory=tmp_path / "data", inference_config=config)
    saved_cfg, identity, profile = load_strata_profile(settings)
    assert profile.provider == "Strata" and saved_cfg["ExpectedQuantization"] == cfg["ExpectedQuantization"]
    if external_storage:
        assert saved_cfg["StorageRoot"] == str(storage)
        assert Path(saved_cfg["EnginePath"]).is_relative_to(storage / "runtime")
        assert Path(identity["engine_model_path"]).is_relative_to(storage)
        assert not (tmp_path / ".local/strata/models").exists()
        assert not (tmp_path / ".local/strata/prepared").exists()
        assert not (tmp_path / ".local/apps/strata").exists()
        if family == "qwen":
            assert (storage / "mtp-source").is_dir()
        # A later double-click, without the option, must reuse this model's saved SSD.
        args.storage_root = None
        args.deepseek_layout = None
        assert install.prepare(args)["storage_root"] == str(storage)
        saved_cfg, identity, profile = load_strata_profile(settings)
    if family == "deepseek":
        assert any("--pack-info" in cmd for cmd in commands)
        assert saved_cfg["ModelLayout"] == layout
        command = next(cmd for cmd in commands if any(str(p).endswith("pack_gguf.py") for p in cmd))
        shards = command[command.index("--shards") + 1:command.index("--engram")]
        assert len(shards) == (5 if layout == "six-shard" else 4)
        assert all(name.endswith("-of-00006.gguf" if layout == "six-shard" else "-of-00004.gguf") for name in shards)
        if layout == "six-shard":
            assert shards[0].endswith("00001-of-00006.gguf")
            assert "UD-Q8_K_XL" not in " ".join(command)
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


@pytest.mark.parametrize("layout", ["four-shard", "six-shard"])
def test_existing_deepseek_upgrades_offline_to_128k_with_matching_runtime_and_lock(tmp_path, monkeypatch, layout):
    args, cfg, other, before_other, before_base, _ = fixture(tmp_path, monkeypatch, layout=layout)
    config = tmp_path / "config/inference.strata.deepseek.local.json"
    cfg.update(NumCtx=32768, NumPredict=8192, MaxOutputTokens=8192, private_marker="KEEP")
    config.write_bytes(install.json_bytes(cfg))
    args.storage_root = tmp_path / "selected SSD with spaces"
    install.prepare(args)
    original = config.read_bytes()

    def no_download(*args, **kwargs):
        raise AssertionError("Offline window upgrade must not download")
    monkeypatch.setattr(install, "download", no_download)
    args.offline = True
    args.install_dependencies = False
    args.storage_root = None
    args.deepseek_layout = None
    args.context, args.max_output = 131072, 16384
    report = install.prepare(args)
    saved, identity, _ = load_strata_profile(Settings(provider="Strata", data_directory=tmp_path / "data", inference_config=config))
    assert report["context"] == saved["NumCtx"] == 131072
    assert saved["NumPredict"] == saved["MaxOutputTokens"] == 16384
    assert saved["NumCtx"] - saved["NumPredict"] - saved["ReservedTokens"] == 114176
    assert saved["StorageRoot"] == str(tmp_path / "selected SSD with spaces")
    assert saved["ModelLayout"] == layout and saved["ActiveExperts"] == 6 and saved["private_marker"] == "KEEP"
    run = json.loads(Path(saved["RunConfigPath"]).read_bytes())
    assert run["args"][run["args"].index("--max-context") + 1] == "131072"
    assert identity["run_config_sha256"] == install.digest(Path(saved["RunConfigPath"]))
    assert any(path.read_bytes() == original for path in config.parent.glob(config.name + ".before-strata-*.bak"))
    assert other.read_bytes() == before_other
    assert (tmp_path / "config/server.local.json").read_bytes() == before_base

    targets = [config, Path(saved["RunConfigPath"]), Path(saved["ModelLockPath"]),
               tmp_path / "config/server.strata.deepseek.local.json"]
    before = {path: path.read_bytes() for path in targets}
    async def fail(*args):
        raise ValueError("synthetic larger-window load failure")
    monkeypatch.setattr(install, "preflight", fail)
    args.context = 262144
    with pytest.raises(ValueError, match="larger-window load failure"):
        install.prepare(args)
    assert {path: path.read_bytes() for path in targets} == before
    verify_local_files(saved, identity)


@pytest.mark.parametrize("value", ["relative/models", "E:models", ""])
def test_storage_root_must_be_absolute_before_downloading(tmp_path, monkeypatch, value):
    args, *_ = fixture(tmp_path, monkeypatch)
    args.storage_root = value
    with pytest.raises(ValueError, match="absolute folder"):
        install.prepare(args)
    assert not (tmp_path / ".local/strata/downloads").exists()


def test_selected_storage_lock_refuses_concurrent_preparation_and_releases_local_lock(tmp_path, monkeypatch):
    args, *_ = fixture(tmp_path, monkeypatch)
    args.storage_root = tmp_path / "other SSD"
    lock = install.InstanceLock(args.storage_root / "preparation").acquire()
    try:
        with pytest.raises(RuntimeError, match="active instance"):
            install.prepare(args)
        assert not (args.storage_root / "downloads").exists()
        local = install.InstanceLock(tmp_path / ".local/strata/preparation").acquire()
        local.release()
    finally:
        lock.release()


def test_failed_storage_change_keeps_the_original_working_configuration(tmp_path, monkeypatch):
    args, *_ = fixture(tmp_path, monkeypatch)
    install.prepare(args)
    config = tmp_path / "config/inference.strata.deepseek.local.json"
    settings = Settings(provider="Strata", data_directory=tmp_path / "data", inference_config=config)
    old_cfg, identity, _ = load_strata_profile(settings)
    paths = [config, Path(old_cfg["ModelLockPath"]), Path(old_cfg["RunConfigPath"]),
             tmp_path / "config/server.strata.deepseek.local.json"]
    before = {p: p.read_bytes() for p in paths}
    args.storage_root = tmp_path / "new SSD"
    async def fail(*args):
        raise ValueError("synthetic storage-change load failure")
    monkeypatch.setattr(install, "preflight", fail)
    with pytest.raises(ValueError, match="storage-change"):
        install.prepare(args)
    assert {p: p.read_bytes() for p in paths} == before
    assert "StorageRoot" not in old_cfg
    verify_local_files(old_cfg, identity)


def test_six_shard_autodetection_sources_and_explicit_override(tmp_path):
    source = json.loads((ROOT / "config/models/strata.deepseek.source.json").read_bytes())
    models = tmp_path / "models"
    models.mkdir()
    filename = source["model_file_sets"]["six-shard"][0]["filename"]
    (models / (filename + ".download")).touch()
    selected = install.select_deepseek_layout(source, tmp_path / "new SSD", source_models=models)
    assert selected["model_layout"] == "six-shard"
    assert selected["model_files"][0]["role"] == "metadata"
    selected = install.select_deepseek_layout(source, models, requested="four-shard")
    assert selected["model_layout"] == "four-shard"
    selected = install.select_deepseek_layout(source, tmp_path / "empty", saved="six-shard")
    assert selected["model_layout"] == "six-shard"
    with pytest.raises(ValueError, match="Unknown or unavailable"):
        install.select_deepseek_layout(source, models, requested="not-a-layout")


@pytest.mark.parametrize("compatible", [True, False])
def test_six_shard_engram_accepts_only_explicitly_pinned_hashes(tmp_path, monkeypatch, compatible):
    args, cfg, *_ = fixture(tmp_path, monkeypatch, layout="six-shard")
    source_path = tmp_path / "config/models/strata.deepseek.source.json"
    source = json.loads(source_path.read_bytes())
    row = next(row for row in source["model_file_sets"]["six-shard"] if row["role"] == "engram")
    data = b"x" * row["size"]
    path = tmp_path / ".local/strata/models/deepseek41/IQ2_XXS" / row["filename"]
    path.parent.mkdir(parents=True)
    path.write_bytes(data)
    if compatible:
        row["compatible_sha256"] = [install.digest(path)]
    source_path.write_bytes(install.json_bytes(source))
    config_path = tmp_path / "config/inference.strata.deepseek.local.json"
    before = config_path.read_bytes()
    if not compatible:
        with pytest.raises(ValueError, match="pinned SHA256"):
            install.prepare(args)
        assert config_path.read_bytes() == before
        return
    install.prepare(args)
    settings = Settings(provider="Strata", data_directory=tmp_path / "data", inference_config=config_path)
    _, identity, _ = load_strata_profile(settings)
    record = next(record for record in identity["files"] if record["path"] == str(path.resolve()))
    assert record["sha256"] == install.digest(path)


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
            assert provider.identity_observation["props"]["chat_template_sha256"] == identity["chat_template_sha256"]
            assert "chat_template" not in provider.identity_observation["props"]
            slot["is_processing"] = True
            with pytest.raises(RuntimeError, match="MODEL_IDENTITY") as error:
                await provider.verify_identity()
            assert error.value.identity_details["mismatches"] == [
                {"field": "slot.is_processing", "expected": False, "actual": True}]
        finally:
            await provider.aclose()
    asyncio.run(check())


@pytest.mark.parametrize("family,build,caps,probe_passed,probe_hash,valid", [
    ("deepseek", "Strata ds4-0.1", False, True, "same", True),
    ("deepseek", "Strata 0.1.55", False, True, "same", True),
    ("deepseek", "Strata ds4-0.1", False, False, "same", False),
    ("deepseek", "Strata ds4-0.1", False, True, "different", False),
    ("deepseek", "Strata unknown", False, True, "same", False),
    ("deepseek", "Strata ds4-0.1", None, True, "same", False),
    ("qwen", "Strata ds4-0.1", True, True, "same", False),
    ("qwen", "Strata 0.1.55", False, True, "same", False),
])
def test_ds4_build_and_false_generic_hint_require_matching_dsml_probe(tmp_path, monkeypatch, family, build, caps, probe_passed, probe_hash, valid):
    import aiserver.inference.strata as strata
    import hashlib
    cfg = json.loads((ROOT / ("config/inference.strata.deepseek.example.json" if family == "deepseek" else
                             "config/inference.strata.example.json")).read_bytes())
    template = "DSML calls>"
    template_hash = hashlib.sha256(template.encode()).hexdigest()
    identity = {"engine_model_path": str(tmp_path / "pack"), "chat_template_sha256": template_hash}
    probes = []
    def probe(*args):
        probes.append(args)
        return {"passed": probe_passed, "template_sha256": template_hash if probe_hash == "same" else "not-matching"}
    monkeypatch.setattr(strata, "probe_dsml_template", probe)
    async def check():
        provider = StrataProvider(Settings(provider="Strata", data_directory=tmp_path / "data"), cfg, identity)
        await provider.adapter.client.aclose()
        def response(request):
            return httpx.Response(200, json=[{"n_ctx": cfg["NumCtx"], "is_processing": False}] if request.url.path == "/slots" else {
                "model_alias": cfg["Model"], "total_slots": 1, "build_info": build, "is_sleeping": False,
                "default_generation_settings": {"n_ctx": cfg["NumCtx"]}, "model_path": identity["engine_model_path"],
                "chat_template": template, "chat_template_caps": {"supports_tools": caps}})
        provider.adapter.client = httpx.AsyncClient(base_url=cfg["BaseUrl"], transport=httpx.MockTransport(response))
        try:
            if valid:
                await provider.verify_identity()
                await provider.verify_identity()  # reuse the proof for this immutable template
                assert provider.supports_tools
                assert len(probes) == 1
            else:
                with pytest.raises(RuntimeError, match="MODEL_IDENTITY"):
                    await provider.verify_identity()
            if family == "qwen" or build == "Strata unknown" or caps is None:
                assert not probes
        finally:
            await provider.aclose()
    asyncio.run(check())


@pytest.mark.parametrize("tool_works", [True, False])
def test_deepseek_preflight_requires_native_tool_and_associated_result(tmp_path, monkeypatch, tool_works):
    cfg = json.loads((ROOT / "config/inference.strata.deepseek.example.json").read_bytes())
    cfg["RunConfigPath"] = str(tmp_path / "run.local.json")
    calls, closed = [], []
    class Provider:
        def __init__(self, *args):
            pass
        async def start(self):
            pass
        async def generate(self, prepared, stop, callback):
            raw = json.loads(prepared.payload_json)
            messages = json.loads(raw["messages_json"])
            calls.append(messages)
            if len(calls) == 1:
                await callback("Paris.")
                return Generation("Paris.", True, "stop", output_tokens=2)
            if len(calls) == 2:
                if not tool_works:
                    return Generation("No tool call", True, "stop", output_tokens=3)
                return Generation("", True, "tool_calls", tool_calls=({"id": "original-id", "type": "function", "function": {
                    "name": "read_sample", "arguments": '{"path":"sample.txt"}'}},))
            assert messages[-1]["tool_call_id"] == "original-id"
            assert messages[-1]["content"] == "STRATA_TOOL_OK_42"
            return Generation("STRATA_TOOL_OK_42", True, "stop", output_tokens=8)
        async def aclose(self):
            closed.append(True)
    monkeypatch.setattr(install, "StrataProvider", Provider)
    if tool_works:
        asyncio.run(install.preflight(SimpleNamespace(), cfg, {}))
        assert len(calls) == 3
    else:
        with pytest.raises(ValueError, match="native tool call"):
            asyncio.run(install.preflight(SimpleNamespace(), cfg, {}))
        assert len(calls) == 2
    assert closed == [True]
    report = json.loads((tmp_path / "preflight.json").read_bytes())
    assert report["passed"] is tool_works
    assert report["phase"] == ("model_shutdown" if tool_works else "native_tool_call")
    assert report["checks"][-1]["text_preview"] == ("STRATA_TOOL_OK_42" if tool_works else "No tool call")
    assert report["checks"][-1]["finish_reason"] == "stop"


@pytest.mark.parametrize("phase", ["model_start", "associated_tool_result", "model_shutdown"])
def test_preflight_failure_identifies_phase_and_preserves_original_during_shutdown(tmp_path, monkeypatch, phase):
    cfg = json.loads((ROOT / "config/inference.strata.deepseek.example.json").read_bytes())
    cfg["RunConfigPath"] = str(tmp_path / "run.local.json")
    failure = RuntimeError("MODEL_IDENTITY_CHECK_FAILED")
    failure.identity_details = {"mismatches": [{"field": "model_path", "expected": "pack", "actual": "other"}]}
    class Provider:
        def __init__(self, *args):
            self.count = 0
            self.engine = SimpleNamespace(log_directory=None)
            self.identity_observation = {"props_http_status": 200}
        async def start(self):
            assert self.engine.log_directory == tmp_path
            if phase == "model_start":
                raise failure
        async def generate(self, prepared, stop, callback):
            self.count += 1
            if self.count == 1:
                await callback("Paris.")
                return Generation("Paris.", True, "stop", output_tokens=2)
            if self.count == 2:
                return Generation("", True, "tool_calls", tool_calls=({"id": "original-id", "type": "function", "function": {
                    "name": "read_sample", "arguments": '{"path":"sample.txt"}'}},))
            if phase == "associated_tool_result":
                raise failure
            return Generation("STRATA_TOOL_OK_42", True, "stop", output_tokens=8)
        async def aclose(self):
            if phase == "model_shutdown":
                raise failure
            raise OSError("secondary cleanup failure")
    monkeypatch.setattr(install, "StrataProvider", Provider)
    with pytest.raises(RuntimeError) as error:
        asyncio.run(install.preflight(SimpleNamespace(), cfg, {}))
    assert error.value is failure
    report = json.loads((tmp_path / "preflight.json").read_bytes())
    assert report["phase"] == phase and not report["passed"]
    assert "shutdown_error" in report and report["runtime_identity"] == {"props_http_status": 200}
    identity = json.loads((tmp_path / "identity-check.json").read_bytes())
    assert identity["phase"] == phase and identity["mismatches"] == failure.identity_details["mismatches"]


def test_windows_template_bytes_match_the_text_reported_by_strata(tmp_path, monkeypatch):
    args, *_ = fixture(tmp_path, monkeypatch, layout="six-shard")
    install.prepare(args)
    settings = Settings(provider="Strata", data_directory=tmp_path / "data",
                        inference_config=tmp_path / "config/inference.strata.deepseek.local.json")
    cfg, identity, _ = load_strata_profile(settings)
    import hashlib
    pack = Path(identity["engine_model_path"])
    assert install.digest(pack / "tokenizer/chat_template.jinja") != identity["chat_template_sha256"]
    assert identity["chat_template_sha256"] == hashlib.sha256(b"canonical calls>\n{{ messages }}\n").hexdigest()
    async def check():
        provider = StrataProvider(settings, cfg, identity)
        await provider.adapter.client.aclose()
        def response(request):
            return httpx.Response(200, json=[{"n_ctx": cfg["NumCtx"], "is_processing": False}] if request.url.path == "/slots" else {
                "model_alias": cfg["Model"], "total_slots": 1, "build_info": "Strata 0.1.55", "is_sleeping": False,
                "default_generation_settings": {"n_ctx": cfg["NumCtx"]}, "model_path": identity["engine_model_path"],
                "chat_template": "canonical calls>\n{{ messages }}\n", "chat_template_caps": {"supports_tools": True}})
        provider.adapter.client = httpx.AsyncClient(base_url=cfg["BaseUrl"], transport=httpx.MockTransport(response))
        try:
            await provider.verify_identity()
        finally:
            await provider.aclose()
    asyncio.run(check())


@pytest.mark.parametrize("family,layout", [("qwen", None), ("deepseek", "four-shard"), ("deepseek", "six-shard")])
def test_offline_preparation_reuses_extraction_without_archive_or_any_downloader(tmp_path, monkeypatch, family, layout):
    args, *_ = fixture(tmp_path, monkeypatch, family, layout)
    install.prepare(args)
    storage = tmp_path / ".local/strata"
    for archive in (storage / "downloads").iterdir():
        archive.unlink()
    def forbidden(*args, **kwargs):
        pytest.fail("Offline preparation invoked a downloader")
    monkeypatch.setattr(install, "download", forbidden)
    import requests
    monkeypatch.setattr(requests, "get", forbidden)
    args.offline = True
    args.install_dependencies = True  # Offline wins even if another caller supplies this.
    assert install.prepare(args)["prepared"]
    commands = []
    previous_run = install.run
    def checked_run(command, **kwargs):
        assert "pip" not in command
        assert not (any(str(p).endswith("mtp_fetch.py") for p in command) and "fetch" in command)
        commands.append(command)
        return previous_run(command, **kwargs)
    monkeypatch.setattr(install, "run", checked_run)
    args.storage_root = tmp_path / "new SSD"
    source = json.loads((tmp_path / ("config/models/strata.deepseek.source.json" if family == "deepseek" else "config/models/strata.source.json")).read_bytes())
    rows = source["model_file_sets"][layout] if layout == "six-shard" else source["model_files"]
    import shutil
    folder = "models/deepseek41/IQ2_XXS" if family == "deepseek" else "models/IQ3_XXS"
    output = args.storage_root / folder
    output.mkdir(parents=True)
    for row in rows:
        shutil.copyfile(storage / folder / row["filename"], output / row["filename"])
    if family == "qwen":
        shutil.copytree(storage / "mtp-source", args.storage_root / "mtp-source")
    assert install.prepare(args)["prepared"]
    assert commands


@pytest.mark.parametrize("missing", ["runtime", "dependencies", "weights", "mtp"])
def test_offline_missing_files_stop_without_download_or_configuration_publish(tmp_path, monkeypatch, missing):
    args, cfg, *_ = fixture(tmp_path, monkeypatch, "qwen")
    install.prepare(args)
    config_path = tmp_path / "config/inference.strata.local.json"
    before = config_path.read_bytes()
    saved = json.loads(before)
    paths = {
        "runtime": tmp_path / ".local/apps/strata/v0.2.9/source/.aiserver-archive.sha256",
        "dependencies": Path(saved["DependencyPath"]) / ".aiserver-requirements.sha256",
        "weights": next((tmp_path / ".local/strata/models/IQ3_XXS").glob("*.gguf")),
        "mtp": tmp_path / ".local/strata/mtp-source/tensors/tiny.bin",
    }
    paths[missing].unlink()
    def forbidden(*args, **kwargs):
        pytest.fail("Offline mode attempted a download")
    monkeypatch.setattr(install, "download", forbidden)
    args.offline = True
    with pytest.raises(ValueError):
        install.prepare(args)
    assert config_path.read_bytes() == before
