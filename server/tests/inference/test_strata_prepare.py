"""Installer safety with tiny local artifacts; never downloads engines or weights."""
import builtins
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tarfile
from types import SimpleNamespace
import zipfile

import pytest
import requests

from aiserver.settings import ROOT
from aiserver.operations.lock import InstanceLock

sys.path.insert(0, str(ROOT / "scripts/inference"))
import strata_prepare as install


def preparation_fixture(tmp_path):
    for name in ("config/models/strata.source.json", "config/inference.strata.example.json",
                 "server/src/aiserver/operations/lock.py"):
        output = tmp_path / name
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes((ROOT / name).read_bytes())
    base = tmp_path / "config/server.local.json"
    base.write_text(json.dumps({"environment": "Development", "provider": "Ollama", "manage_natapp": False,
                               "data_directory": str(tmp_path / "data")}), encoding="utf-8")
    return SimpleNamespace(server_config=str(base), context=None, install_dependencies=False, source_models=None)


def test_real_prepare_entry_reaches_download_with_both_locks_and_releases_on_failure(tmp_path, monkeypatch):
    args = preparation_fixture(tmp_path)
    monkeypatch.setattr(install, "ROOT", tmp_path)
    class DownloadFailure(Exception):
        pass
    def download(*args):
        for directory in (tmp_path / ".local/strata/preparation", tmp_path / "data"):
            with pytest.raises(RuntimeError, match="active instance"):
                InstanceLock(directory).acquire()
        raise DownloadFailure("synthetic download failure")
    monkeypatch.setattr(install, "download", download)
    base = Path(args.server_config).read_bytes()
    with pytest.raises(DownloadFailure):
        install.prepare(args)
    assert Path(args.server_config).read_bytes() == base
    assert not (tmp_path / "config/server.strata.local.json").exists()
    for directory in (tmp_path / ".local/strata/preparation", tmp_path / "data"):
        lock = InstanceLock(directory).acquire()
        lock.release()


def test_real_prepare_entry_refuses_running_service_and_releases_preparation_lock(tmp_path, monkeypatch):
    args = preparation_fixture(tmp_path)
    monkeypatch.setattr(install, "ROOT", tmp_path)
    calls = []
    monkeypatch.setattr(install, "download", lambda *args: calls.append(args))
    active = InstanceLock(tmp_path / "data").acquire()
    try:
        with pytest.raises(RuntimeError, match="active instance"):
            install.prepare(args)
        lock = InstanceLock(tmp_path / ".local/strata/preparation").acquire()
        lock.release()
        assert not calls
    finally:
        active.release()


@pytest.mark.parametrize("crash", [False, True])
def test_real_validation_entry_holds_and_releases_data_lock(tmp_path, monkeypatch, crash):
    import importlib.util
    spec = importlib.util.spec_from_file_location("strata_validation_entry", ROOT / "scripts/validation/Test-Strata.py")
    entry = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(entry)
    monkeypatch.setattr(entry, "ROOT", tmp_path)
    monkeypatch.setattr(sys, "argv", ["Test-Strata.py"])
    monkeypatch.setattr(entry.Settings, "load", lambda _: SimpleNamespace(provider="Strata", data_directory=tmp_path / "data"))
    async def verify(settings, long_context, destination):
        with pytest.raises(RuntimeError, match="active instance"):
            InstanceLock(settings.data_directory).acquire()
        if crash:
            raise ValueError("synthetic validation failure")
        return True
    monkeypatch.setattr(entry, "verify", verify)
    if crash:
        with pytest.raises(ValueError, match="synthetic validation failure"):
            entry.main()
    else:
        assert entry.main() == 0
    lock = InstanceLock(tmp_path / "data").acquire()
    lock.release()


class Response:
    def __init__(self, body, status=200, headers=None, interrupted=False):
        self.body, self.status_code, self.headers, self.interrupted = body, status, headers or {}, interrupted

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def raise_for_status(self):
        pass

    def iter_content(self, _):
        yield self.body
        if self.interrupted:
            raise requests.ConnectionError("synthetic interruption")


def test_interrupted_download_resumes_and_reuses_verified_file(tmp_path, monkeypatch):
    body = b"model-artifact-for-test"
    sha = hashlib.sha256(body).hexdigest()
    calls = []
    def get(url, *, headers, **kwargs):
        calls.append(headers)
        if len(calls) == 1:
            return Response(body[:5], interrupted=True)
        assert headers == {"Range": "bytes=5-"}
        return Response(body[5:], 206, {"Content-Range": f"bytes 5-{len(body)-1}/{len(body)}"})
    monkeypatch.setattr(requests, "get", get)
    monkeypatch.setattr(install.time, "sleep", lambda _: None)
    target = tmp_path / "tiny.gguf"
    install.download("https://example.test/artifact", target, sha, len(body))
    assert target.read_bytes() == body and len(calls) == 2
    install.download("https://example.test/artifact", target, sha, len(body))
    assert len(calls) == 2


def test_hash_and_resume_range_mismatch_do_not_publish(tmp_path, monkeypatch):
    target = tmp_path / "tiny"
    monkeypatch.setattr(requests, "get", lambda *args, **kwargs: Response(b"bad"))
    with pytest.raises(ValueError, match="SHA256"):
        install.download("https://example.test/a", target, "0" * 64, 3)
    assert not target.exists()
    target.with_name("tiny.download").write_bytes(b"b")
    monkeypatch.setattr(requests, "get", lambda *args, **kwargs: Response(b"ad", 206, {"Content-Range": "bytes 0-2/3"}))
    with pytest.raises(ValueError, match="range"):
        install.download("https://example.test/a", target, "0" * 64, 3)
    assert target.with_name("tiny.download").read_bytes() == b"b"


def test_extraction_reuse_detects_tampering_and_rejects_traversal(tmp_path):
    archive, output = tmp_path / "runtime.zip", tmp_path / "runtime"
    with zipfile.ZipFile(archive, "w") as packed:
        packed.writestr("vendor/runtime.py", "pinned")
    sha = install.digest(archive)
    install.unpack(archive, output, sha)
    install.unpack(archive, output, sha)
    (output / "vendor/runtime.py").write_text("changed")
    with pytest.raises(ValueError, match="modified"):
        install.unpack(archive, output, sha)
    with zipfile.ZipFile(archive, "w") as packed:
        packed.writestr("../escaped.py", "unsafe")
    with pytest.raises(ValueError, match="Unsafe"):
        install.unpack(archive, tmp_path / "unsafe", install.digest(archive))
    assert not (tmp_path / "escaped.py").exists()


@pytest.mark.parametrize("kind", ["zip", "tar"])
@pytest.mark.parametrize("legacy_limit", [False, True])
def test_extract_verify_and_reuse_windows_long_paths(tmp_path, monkeypatch, kind, legacy_limit):
    name = "vendor/" + "/".join("nested-" + str(i) + "-" + "x" * 24 for i in range(6)) + "/runtime.py"
    destination = tmp_path / "AiServer-0.2.0-be0e34e" / ".local/apps/strata/v0.2.9/source"
    target = destination / name
    assert len(str(target)) > 260
    archive = tmp_path / ("runtime." + kind)
    content = b"pinned runtime under a long Windows path"
    if kind == "zip":
        with zipfile.ZipFile(archive, "w") as packed:
            packed.writestr(name, content)
    else:
        with tarfile.open(archive, "w") as packed:
            row = tarfile.TarInfo(name)
            row.size = len(content)
            packed.addfile(row, io.BytesIO(content))
    sha = install.digest(archive)
    if legacy_limit and os.name == "nt":
        # This development host opts into long paths. Emulate an older Python/Windows
        # combination at the file API boundary without editing system policy.
        original_open = builtins.open
        def legacy_open(file, *args, **kwargs):
            if not isinstance(file, int):
                value = os.fspath(file)
                if len(value) >= 260 and not value.startswith("\\\\?\\"):
                    raise FileNotFoundError(2, "Simulated legacy MAX_PATH", value)
            return original_open(file, *args, **kwargs)
        monkeypatch.setattr(builtins, "open", legacy_open)
        monkeypatch.setattr(io, "open", legacy_open)
    install.unpack(archive, destination, sha)
    readable = Path("\\\\?\\" + str(target)) if os.name == "nt" else target
    assert readable.read_bytes() == content
    install.unpack(archive, destination, sha)
    readable.write_bytes(b"modified")
    with pytest.raises(ValueError, match="modified"):
        install.unpack(archive, destination, sha)


def test_source_selection_keeps_runtime_and_license_and_skips_benchmark_data(tmp_path):
    source = json.loads((ROOT / "config/models/strata.source.json").read_bytes())
    selection = source["artifacts"]["source"]["include"]
    archive, destination = tmp_path / "source.zip", tmp_path / "source"
    with zipfile.ZipFile(archive, "w") as packed:
        for name in ("serve/server.py", "tools/mtp_fetch.py", "strata_control/__init__.py", "data/expert-profile.bin", "LICENSE"):
            packed.writestr("Strata-0.2.9/" + name, "pinned")
        packed.writestr("Strata-0.2.9/bench/results/report/requests/" + "a" * 64 + ".json.gz", "unused")
    sha = install.digest(archive)
    install.unpack(archive, destination, sha, include=selection)
    assert (destination / "Strata-0.2.9/serve/server.py").is_file()
    assert (destination / "Strata-0.2.9/LICENSE").is_file()
    assert not (destination / "Strata-0.2.9/bench").exists()
    install.unpack(archive, destination, sha, include=selection)
    with pytest.raises(ValueError, match="selection differs"):
        install.unpack(archive, destination, sha)


def test_old_full_extraction_manifest_can_be_reused_for_runtime_selection(tmp_path):
    archive, destination = tmp_path / "source.zip", tmp_path / "source"
    with zipfile.ZipFile(archive, "w") as packed:
        packed.writestr("vendor/runtime.py", "pinned")
        packed.writestr("vendor/bench/report.txt", "original")
    sha = install.digest(archive)
    install.unpack(archive, destination, sha)
    manifest = destination / ".aiserver-files.json"
    manifest.write_text(json.dumps(json.loads(manifest.read_text())["files"]))
    install.unpack(archive, destination, sha, include=["vendor/runtime.py"])
    assert (destination / "vendor/bench/report.txt").read_text() == "original"


def test_config_publish_rolls_back_and_refuses_concurrent_edit(tmp_path, monkeypatch):
    first, second = tmp_path / "a.json", tmp_path / "b.json"
    first.write_bytes(b"old")
    before = {first: b"old", second: None}
    changes = {first: b"new", second: b"new"}
    atomic = install.atomic_write
    def fail(path, data):
        if path == second:
            raise OSError("synthetic write failure")
        atomic(path, data)
    monkeypatch.setattr(install, "atomic_write", fail)
    with pytest.raises(OSError):
        install.commit_configs(changes, before)
    assert first.read_bytes() == b"old" and not second.exists()
    assert next(tmp_path.glob("a.json.*.bak")).read_bytes() == b"old"
    first.write_bytes(b"concurrent")
    with pytest.raises(ValueError, match="changed"):
        install.commit_configs(changes, before)
    assert first.read_bytes() == b"concurrent"


def test_source_lock_contains_full_shards_mtp_and_hashed_requirements():
    source = json.loads((ROOT / "config/models/strata.source.json").read_bytes())
    assert len(source["model_files"]) == 2 and len(source["mtp_tensor_sha256"]) == 31
    requirements = (ROOT / "scripts/inference/strata.requirements.txt").read_text()
    assert "--hash=sha256:" in requirements and "==" in requirements
    assert all(len(row["sha256"]) == 64 for row in source["model_files"])
