"""Installer safety with tiny local artifacts; never downloads engines or weights."""
import hashlib
import json
from pathlib import Path
import sys
import zipfile

import pytest
import requests

from aiserver.settings import ROOT

sys.path.insert(0, str(ROOT / "scripts/inference"))
import strata_prepare as install


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
