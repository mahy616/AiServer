"""Target-machine preparation against synthetic Ollama metadata; no real model calls."""
from copy import deepcopy
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
from threading import Thread
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

ROOT = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location("additional_model_preparation", ROOT / "scripts/inference/Prepare-AdditionalModel.py")
provision = importlib.util.module_from_spec(spec)
spec.loader.exec_module(provision)


def dump(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def fixture(tmp_path):
    primary = json.loads((ROOT / "config/inference.5080.example.json").read_text(encoding="utf-8"))
    primary.update(BaseUrl="http://127.0.0.1:11555", ModelsPath="E:/installed-models",
                   ExecutablePath="E:/engine/ollama.exe", EngineVersion="fixture-version")
    server = {"provider": "Ollama", "inference_config": "config/existing-4b.local.json",
              "data_directory": "data-to-preserve", "additional_inference_configs": ["config/other.local.json"],
              "private_sentinel": "DO_NOT_COPY"}
    dump(tmp_path / "config/server.local.json", server)
    dump(tmp_path / server["inference_config"], primary)
    dump(tmp_path / "config/other.local.json", {"Model": "other-model"})
    dump(tmp_path / provision.TEMPLATE, json.loads((ROOT / provision.TEMPLATE).read_text(encoding="utf-8")))
    metadata = {"/api/version": {"version": "fixture-version"},
                "/api/tags": {"models": [{"name": "qwen3-vl:8b-instruct-q4_K_M", "digest": "a" * 64,
                                          "modified_at": "original", "details": {"quantization_level": "Q4_K_M"}}]},
                "/api/show": {"template": "实际模板\n{{ .Prompt }}", "capabilities": ["completion", "vision", "tools"],
                              "details": {"quantization_level": "Q4_K_M"}}}
    calls = []
    def fetch(endpoint, body=None):
        calls.append((endpoint, body))
        return deepcopy(metadata[endpoint])
    return primary, server, metadata, calls, fetch


def test_prepare_inherits_target_paths_preserves_active_files_and_records_actual_identity(tmp_path):
    primary, server, metadata, calls, fetch = fixture(tmp_path)
    before = (tmp_path / "config/server.local.json").read_bytes()
    result = provision.prepare(tmp_path, Path("config/server.local.json"), fetch=fetch)
    cfg = provision.read_json(Path(result["config"]))
    assert all(cfg[key] == primary[key] for key in provision.PROCESS_KEYS)
    assert (cfg["NumCtx"], cfg["NumPredict"], cfg["MaxInputTokens"]) == (32768, 4096, 28160)
    assert cfg["ImagePolicy"]["MaxImages"] == 1
    lock = provision.read_json(Path(result["lock"]))
    assert lock["model"] == metadata["/api/tags"]["models"][0]
    assert lock["template_sha256"] == hashlib.sha256(metadata["/api/show"]["template"].encode()).hexdigest()
    patch = provision.read_json(Path(result["patch"]))
    assert patch == {"inference_config": server["inference_config"],
                     "additional_inference_configs": ["config/other.local.json", provision.OUTPUT_CONFIG]}
    assert (tmp_path / "config/server.local.json").read_bytes() == before
    assert provision.read_json(tmp_path / server["inference_config"]) == primary
    assert "DO_NOT_COPY" not in "".join(Path(result[key]).read_text(encoding="utf-8") for key in ("config", "lock", "patch"))
    assert calls == [("/api/version", None), ("/api/tags", None),
                     ("/api/show", {"model": cfg["Model"]})]
    # Repeating preparation tolerates a changed timestamp and never rewrites the prior lock.
    previous = Path(result["lock"]).read_bytes()
    metadata["/api/tags"]["models"][0]["modified_at"] = "later"
    provision.prepare(tmp_path, Path("config/server.local.json"), fetch=fetch)
    assert Path(result["lock"]).read_bytes() == previous


@pytest.mark.parametrize("fault", ["missing", "version", "quantization", "vision", "template", "digest"])
def test_bad_metadata_leaves_no_prepared_files(tmp_path, fault):
    _, _, metadata, _, fetch = fixture(tmp_path)
    if fault == "missing": metadata["/api/tags"]["models"] = []
    if fault == "version": metadata["/api/version"]["version"] = "different"
    if fault == "quantization": metadata["/api/show"]["details"]["quantization_level"] = "BF16"
    if fault == "vision": metadata["/api/show"]["capabilities"] = ["completion"]
    if fault == "template": metadata["/api/show"]["template"] = ""
    if fault == "digest": metadata["/api/tags"]["models"][0]["digest"] = "incomplete"
    with pytest.raises(ValueError):
        provision.prepare(tmp_path, Path("config/server.local.json"), fetch=fetch)
    assert not (tmp_path / provision.OUTPUT_CONFIG).exists()
    assert not (tmp_path / provision.OUTPUT_PATCH).exists()
    assert not (tmp_path / "config/models/qwen3vl8b.local.json").exists()


@pytest.mark.parametrize("existing", ["config", "lock", "patch"])
def test_existing_different_destination_is_never_replaced(tmp_path, existing):
    _, _, _, _, fetch = fixture(tmp_path)
    paths = {"config": provision.OUTPUT_CONFIG, "lock": "config/models/qwen3vl8b.local.json",
             "patch": provision.OUTPUT_PATCH}
    destination = tmp_path / paths[existing]
    dump(destination, {"preserve": "existing-target-machine-file"})
    with pytest.raises(ValueError, match="Existing file differs"):
        provision.prepare(tmp_path, Path("config/server.local.json"), fetch=fetch)
    assert provision.read_json(destination) == {"preserve": "existing-target-machine-file"}
    for key, path in paths.items():
        if key != existing:
            assert not (tmp_path / path).exists()


def test_explicit_installed_model_name_and_quantization(tmp_path):
    _, _, metadata, _, fetch = fixture(tmp_path)
    metadata["/api/tags"]["models"][0].update(name="qwen3-vl:8b-instruct-q8_0")
    for record in (metadata["/api/tags"]["models"][0], metadata["/api/show"]):
        record["details"]["quantization_level"] = "Q8_0"
    result = provision.prepare(tmp_path, Path("config/server.local.json"), model="qwen3-vl:8b-instruct-q8_0",
                               quantization="Q8_0", fetch=fetch)
    assert provision.read_json(Path(result["config"]))["Model"] == "qwen3-vl:8b-instruct-q8_0"


@pytest.mark.parametrize("url", ["https://127.0.0.1:11434", "http://example.com", "http://127.0.0.1/private",
                                 "http://user:pass@127.0.0.1", "http://127.0.0.1/?secret=value"])
def test_transport_rejects_remote_or_credentialed_urls(url):
    with pytest.raises(ValueError, match="loopback HTTP"):
        provision.LocalOllama(url)


def test_cli_from_another_working_directory_uses_only_metadata_and_creates_utf8_lock(tmp_path):
    primary, server, metadata, _, _ = fixture(tmp_path)
    seen = []
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append(self.path)
            self.respond(metadata[self.path])

        def do_POST(self):
            seen.append(self.path)
            assert self.path == "/api/show"
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            assert body == {"model": "qwen3-vl:8b-instruct-q4_K_M"}
            self.respond(metadata[self.path])

        def respond(self, value):
            data = json.dumps(value, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = Thread(target=lambda: http.serve_forever(poll_interval=0.01), daemon=True)
    worker.start()
    try:
        primary["BaseUrl"] = f"http://127.0.0.1:{http.server_port}"
        dump(tmp_path / server["inference_config"], primary)
        script = tmp_path / "scripts/inference/Prepare-AdditionalModel.py"
        script.parent.mkdir(parents=True)
        script.write_bytes((ROOT / "scripts/inference/Prepare-AdditionalModel.py").read_bytes())
        before = (tmp_path / "config/server.local.json").read_bytes()
        listed = subprocess.run([sys.executable, str(script), "--list"], cwd=tmp_path.parent,
                                capture_output=True, text=True, timeout=15)
        assert listed.returncode == 0, listed.stderr
        assert json.loads(listed.stdout)["name"] == "qwen3-vl:8b-instruct-q4_K_M"
        assert not (tmp_path / provision.OUTPUT_CONFIG).exists()
        prepared = subprocess.run([sys.executable, str(script)], cwd=tmp_path.parent,
                                  capture_output=True, text=True, timeout=15)
        assert prepared.returncode == 0, prepared.stderr
        cfg = provision.read_json(tmp_path / provision.OUTPUT_CONFIG)
        assert cfg["BaseUrl"] == primary["BaseUrl"]
        lock = provision.read_json(tmp_path / cfg["ModelLockPath"])
        assert lock["template_sha256"] == hashlib.sha256(metadata["/api/show"]["template"].encode("utf-8")).hexdigest()
        assert (tmp_path / "config/server.local.json").read_bytes() == before
        assert seen == ["/api/tags", "/api/version", "/api/tags", "/api/show"]
    finally:
        http.shutdown()
        http.server_close()
        worker.join(timeout=1)
