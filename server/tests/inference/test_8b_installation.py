"""Offline deployment transactions with tiny model files and synthetic metadata."""
from contextlib import contextmanager
from copy import deepcopy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import zipfile

import pytest

ROOT = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location("install_8b", ROOT / "scripts/inference/Install-Qwen3VL8B.py")
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


def dump(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8-sig")


def fixture(tmp_path):
    root = tmp_path / "工程 with spaces"
    source = tmp_path / "用户模型"
    cfg = json.loads((ROOT / "config/inference.5080.example.json").read_text(encoding="utf-8"))
    cfg.update(EngineVersion="fixture-engine", ExecutablePath=sys.executable)
    server = {"provider": "Ollama", "inference_config": "config/current-4b.local.json",
              "additional_inference_configs": ["config/other.local.json"],
              "private_sentinel": "DO_NOT_PRINT", "allowed_hosts": ["original.example"],
              "data_directory": ".local/original-data"}
    dump(root / "config/server.local.json", server)
    dump(root / server["inference_config"], cfg)
    template = "中文模板\n{{ .Prompt }}"
    old_lock = {"model": {"digest": "b" * 64}, "template_sha256": hashlib.sha256(template.encode()).hexdigest()}
    dump(root / cfg["ModelLockPath"], old_lock)
    other = {**cfg, "Model": "other-installed-model", "ModelLockPath": "config/models/other.local.json"}
    dump(root / "config/other.local.json", other)
    dump(root / other["ModelLockPath"], old_lock)
    dump(root / installer.provision.TEMPLATE, json.loads((ROOT / installer.provision.TEMPLATE).read_text(encoding="utf-8")))
    metadata = {"/api/version": {"version": cfg["EngineVersion"]},
                "/api/tags": {"models": [{"name": c["Model"], "digest": "b" * 64} for c in (cfg, other)]},
                "/api/show": {"template": template, "capabilities": ["completion", "vision", "tools"],
                              "details": {"quantization_level": "Q4_K_M"}}}
    entries = []
    for data, media in ((b"config", "application/vnd.docker.container.image.v1+json"),
                        (b"tiny model", "application/vnd.ollama.image.model"),
                        (b"tiny projector", "application/vnd.ollama.image.projector")):
        digest = hashlib.sha256(data).hexdigest()
        path = source / "blobs" / ("sha256-" + digest)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        entries.append({"mediaType": media, "digest": "sha256:" + digest, "size": len(data)})
    manifest = {"schemaVersion": 2, "config": entries[0], "layers": entries[1:]}
    dump(source / installer.MANIFEST, manifest)
    raw = (source / installer.MANIFEST).read_bytes()
    metadata["/api/tags"]["models"].append({"name": installer.MODEL, "digest": hashlib.sha256(raw).hexdigest(),
                                              "details": {"quantization_level": "Q4_K_M"}})
    (source / "blobs/unrelated-model").write_bytes(b"do not copy")
    (root / "server/src/aiserver/inference").mkdir(parents=True)
    (root / "server/src/aiserver/settings.py").write_text("additional_inference_configs = []", encoding="utf-8")
    (root / "server/src/aiserver/inference/registry.py").write_text("# fixture", encoding="utf-8")
    calls = []

    def fetch(endpoint, body=None):
        calls.append((endpoint, body))
        return deepcopy(metadata[endpoint])

    @contextmanager
    def engine(*args):
        calls.append("started")
        try:
            yield fetch
        finally:
            calls.append("stopped")
    return root, source, cfg, server, metadata, calls, engine


def run(f):
    root, source, _, _, _, _, engine = f
    return installer.install(root, source_models=source, engine_factory=engine)


def test_transaction_preserves_4b_private_settings_other_models_and_repeats(tmp_path, capsys):
    f = fixture(tmp_path)
    root, source, cfg, server, _, calls, _ = f
    preserved = installer.snapshot([root / server["inference_config"], root / cfg["ModelLockPath"], root / "config/other.local.json"])
    original = (root / "config/server.local.json").read_bytes()
    result = run(f)
    assert Path(result["backup"]).read_bytes() == original
    actual = installer.read(root / "config/server.local.json")
    assert actual == {**server, "additional_inference_configs": ["config/other.local.json", installer.provision.OUTPUT_CONFIG]}
    installer.unchanged(preserved)
    target = Path(result["targetModels"])
    assert (target / installer.MANIFEST).read_bytes() == (source / installer.MANIFEST).read_bytes()
    assert len(list((target / "blobs").iterdir())) == 3
    assert "DO_NOT_PRINT" not in capsys.readouterr().out
    output_before = installer.snapshot([Path(result[key]) for key in ("config", "lock", "patch")])
    again = run(f)
    assert again["backup"] is None
    installer.unchanged(output_before)
    assert calls.count("started") == calls.count("stopped") == 2


@pytest.mark.parametrize("failure", ["version", "old_identity", "vision", "new_digest", "existing_config", "existing_lock"])
def test_metadata_or_existing_output_failure_keeps_server_and_4b(tmp_path, failure):
    f = fixture(tmp_path)
    root, _, cfg, server, metadata, calls, _ = f
    if failure == "version": metadata["/api/version"]["version"] = "wrong"
    if failure == "old_identity": metadata["/api/tags"]["models"][0]["digest"] = "c" * 64
    if failure == "vision": metadata["/api/show"]["capabilities"] = ["completion"]
    if failure == "new_digest": metadata["/api/tags"]["models"][-1]["digest"] = "d" * 64
    existing = None
    if failure in ("existing_config", "existing_lock"):
        existing = root / (installer.provision.OUTPUT_CONFIG if failure == "existing_config" else "config/models/qwen3vl8b.local.json")
        dump(existing, {"preserve": "existing"})
    before = installer.snapshot([root / "config/server.local.json", root / cfg["ModelLockPath"], root / server["inference_config"]])
    with pytest.raises(ValueError): run(f)
    installer.unchanged(before)
    assert not (root / cfg["ModelsPath"] / installer.MANIFEST).exists()
    assert "stopped" in calls
    if existing: assert installer.read(existing) == {"preserve": "existing"}


@pytest.mark.parametrize("where", ["source", "destination"])
def test_corrupt_blob_is_rejected_without_publishing_manifest(tmp_path, where):
    f = fixture(tmp_path)
    root, source, cfg, _, _, _, _ = f
    raw, blobs = installer.manifest_info(source)
    name = next(iter(blobs))
    path = (source if where == "source" else root / cfg["ModelsPath"]) / "blobs" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"wrong!")
    before = (root / "config/server.local.json").read_bytes()
    with pytest.raises(ValueError): run(f)
    assert (root / "config/server.local.json").read_bytes() == before
    assert not (root / cfg["ModelsPath"] / installer.MANIFEST).exists()
    assert path.read_bytes() == b"wrong!"


def test_failed_final_save_rolls_back_new_config_files_and_manifest(tmp_path, monkeypatch):
    f = fixture(tmp_path)
    root = f[0]
    original = (root / "config/server.local.json").read_bytes()
    def fail(*args): raise OSError("disk write failure")
    monkeypatch.setattr(installer.support, "atomic_write", fail)
    with pytest.raises(OSError): run(f)
    assert (root / "config/server.local.json").read_bytes() == original
    assert not (root / installer.provision.OUTPUT_CONFIG).exists()
    assert not (root / "config/models/qwen3vl8b.local.json").exists()
    assert not (root / installer.provision.OUTPUT_PATCH).exists()
    assert not (root / f[2]["ModelsPath"] / installer.MANIFEST).exists()
    assert len(list((root / f[2]["ModelsPath"] / "blobs").iterdir())) == 3


def test_concurrent_edit_is_preserved(tmp_path):
    f = list(fixture(tmp_path))
    root, _, _, server, _, _, original_engine = f
    @contextmanager
    def engine(*args):
        with original_engine(*args) as fetch:
            yield fetch
            dump(root / "config/server.local.json", {**server, "concurrent_field": "keep"})
    f[-1] = engine
    with pytest.raises(ValueError, match="其他操作修改"): run(f)
    assert installer.read(root / "config/server.local.json")["concurrent_field"] == "keep"


def test_busy_service_is_rejected_before_copy(tmp_path):
    from aiserver.operations.lock import InstanceLock
    f = fixture(tmp_path)
    lock = InstanceLock(f[0] / f[3]["data_directory"]).acquire()
    try:
        with pytest.raises(ValueError, match="仍在运行"): run(f)
        assert not (f[0] / f[2]["ModelsPath"]).exists()
    finally:
        lock.release()


def test_discovery_uses_environment_and_saved_custom_config(tmp_path, monkeypatch):
    root, source, cfg, _, _, _, _ = fixture(tmp_path)
    monkeypatch.setenv("OLLAMA_MODELS", str(source))
    assert installer.find_source(root, cfg)[0] == source
    custom = root / "config/custom.local.json"
    custom.write_bytes((root / "config/server.local.json").read_bytes())
    dump(root / ".local/server-manager/launcher.json", {"config": str(custom)})
    assert installer.select_config(root) == custom
    dump(root / ".local/server-manager/launcher.json", {"config": "missing.json"})
    with pytest.raises(ValueError, match="记住的服务配置不存在"): installer.select_config(root)


def test_ambiguous_model_stores_require_explicit_choice(tmp_path, monkeypatch):
    root, source, cfg, _, _, _, _ = fixture(tmp_path)
    other = tmp_path / "other"
    value = installer.read(source / installer.MANIFEST)
    dump(other / installer.MANIFEST, {**value, "different": True})
    monkeypatch.setattr(installer, "source_candidates", lambda *args: [source, other])
    with pytest.raises(ValueError, match="不同版本"): installer.find_source(root, cfg)
    assert installer.find_source(root, cfg, source)[0] == source


def test_existing_manifest_is_never_replaced(tmp_path):
    f = fixture(tmp_path)
    target = f[0] / f[2]["ModelsPath"] / installer.MANIFEST
    dump(target, {"preserve": "existing-8b"})
    before = installer.snapshot([target, f[0] / "config/server.local.json"])
    with pytest.raises(ValueError, match="不同的8B清单"): run(f)
    installer.unchanged(before)


def test_source_can_already_be_the_target(tmp_path):
    f = list(fixture(tmp_path))
    target = f[0] / f[2]["ModelsPath"]
    shutil.copytree(f[1], target)
    f[1] = target
    original = installer.snapshot([target / installer.MANIFEST])
    result = run(f)
    assert result["sourceModels"] == result["targetModels"]
    installer.unchanged(original)


@pytest.mark.parametrize("failure", ["digest", "size", "projector", "duplicate_key", "legacy_server"])
def test_invalid_input_cannot_change_service_config(tmp_path, failure):
    f = fixture(tmp_path)
    root, source, _, _, _, _, _ = f
    path = source / installer.MANIFEST
    value = installer.read(path)
    if failure == "digest": value["config"]["digest"] = "sha256:../../escape"
    if failure == "size": value["config"]["size"] = -1
    if failure == "projector": value["layers"] = value["layers"][:1]
    dump(path, value)
    if failure == "duplicate_key":
        path.write_text('{"schemaVersion":2,"schemaVersion":2}', encoding="utf-8")
    if failure == "legacy_server":
        (root / "server/src/aiserver/settings.py").write_text("# old server", encoding="utf-8")
    before = (root / "config/server.local.json").read_bytes()
    with pytest.raises(ValueError): run(f)
    assert (root / "config/server.local.json").read_bytes() == before


def test_subprocess_uses_real_managed_temporary_engine_without_generation(tmp_path):
    root, source, cfg, server, metadata, _, _ = fixture(tmp_path)
    builder = installer.module("build_8b_installer", ROOT / "scripts/deployment/Build-8BInstaller.py")
    package = builder.build(output=tmp_path / "delivery")
    with zipfile.ZipFile(package["zip"]) as archive:
        assert set(archive.namelist()) == set(builder.FILES) | {"8B-installer-manifest.json"}
        archive.extractall(root)
    for relative in ("server/src/aiserver/__init__.py",
                     "server/src/aiserver/inference/configuration.py",
                     "server/src/aiserver/operations/__init__.py", "server/src/aiserver/operations/lock.py",
                     "server/src/aiserver/operations/engine.py", "server/src/aiserver/operations/processes.py"):
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / relative, target)
    dump(root / "synthetic-metadata.json", metadata)
    (root / "serve").write_text('''import json, os
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
metadata=json.loads(Path("synthetic-metadata.json").read_text(encoding="utf-8-sig"))
class Handler(BaseHTTPRequestHandler):
    def do_GET(self): self.reply()
    def do_POST(self):
        assert self.path == "/api/show"
        self.rfile.read(int(self.headers["Content-Length"]))
        self.reply()
    def reply(self):
        value=metadata[self.path]
        raw=json.dumps(value).encode()
        self.send_response(200)
        self.send_header("Content-Length",str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)
    def log_message(self,*args): pass
Path("synthetic-engine-pid").write_text(str(os.getpid()))
HTTPServer(("127.0.0.1",int(os.environ["OLLAMA_HOST"].split(":")[-1])),Handler).serve_forever()
''', encoding="utf-8")
    dump(root / ".local/server-manager/launcher.json", {"python": sys.executable, "config": str(root / "config/server.local.json")})
    environment = {**os.environ, "OLLAMA_MODELS": str(source)}
    if os.name == "nt":
        command = [os.environ["COMSPEC"], "/d", "/c", str(root / "Install-Qwen3VL8B.cmd")]
    else:
        command = [sys.executable, "-X", "utf8", str(root / "scripts/inference/Install-Qwen3VL8B.py")]
    result = subprocess.run(command, cwd=tmp_path, env=environment, input=b"\n", capture_output=True, timeout=45)
    output = (result.stdout + result.stderr).decode("utf-8", "replace")
    assert result.returncode == 0, output
    assert "DO_NOT_PRINT" not in output
    assert installer.read(root / "config/server.local.json")["additional_inference_configs"][-1] == installer.provision.OUTPUT_CONFIG
    from aiserver.operations.engine import process_identity
    with pytest.raises((ProcessLookupError, FileNotFoundError)):
        process_identity(int((root / "synthetic-engine-pid").read_text()))
    assert not list((root / ".local/model-install").glob("*/engine/process.json"))
