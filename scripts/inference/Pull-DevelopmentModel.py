"""Explicit provisioning only; never called by the adapter or business jobs."""
from pathlib import Path
import hashlib
import json
import time
import requests

ROOT = Path(__file__).resolve().parents[2]
cfg = json.loads((ROOT / "config/inference.development.example.json").read_text(encoding="utf-8"))
session = requests.Session()
session.trust_env = False
url = cfg["BaseUrl"]
version = session.get(url + "/api/version", timeout=5).json()
if version["version"] != cfg["EngineVersion"]:
    raise RuntimeError("Engine version mismatch")
print(version, flush=True)
started = last = time.monotonic()
with session.post(url + "/api/pull", json={"model": cfg["Model"], "stream": True}, timeout=(5, 600)) as response:
    response.raise_for_status()
    final_status = None
    for line in response.iter_lines():
        if not line:
            continue
        item = json.loads(line)
        if "error" in item:
            raise RuntimeError(item["error"])
        final_status = item.get("status")
        if time.monotonic() - last > 15 or final_status == "success":
            print(json.dumps(item), flush=True)
            last = time.monotonic()
    if final_status != "success":
        raise RuntimeError("Pull did not complete")
tags = session.get(url + "/api/tags", timeout=10).json()
model = next(item for item in tags["models"] if item["name"] == cfg["Model"])
show = session.post(url + "/api/show", json={"model": cfg["Model"]}, timeout=15).json()
if model["details"]["quantization_level"] != cfg["ExpectedQuantization"]:
    raise RuntimeError("Unexpected quantization")
if not set(cfg["RequiredCapabilities"]).issubset(show.get("capabilities", [])):
    raise RuntimeError("Missing model capabilities")
identity = {"engine_version": version["version"], "model": model,
            "template_sha256": hashlib.sha256(show.get("template", "").encode()).hexdigest(),
            "capabilities": show["capabilities"], "image_policy": cfg["ImagePolicy"]}
path = ROOT / cfg["ModelLockPath"]
if path.exists() and json.loads(path.read_text(encoding="utf-8")) != identity:
    raise RuntimeError("Existing model lock differs; inspect before replacing")
path.parent.mkdir(parents=True, exist_ok=True)
path.write_text(json.dumps(identity, ensure_ascii=False, indent=2), encoding="utf-8")
evidence = ROOT / ".local/reports/model-provisioning"
evidence.mkdir(parents=True, exist_ok=True)
(evidence / "show.json").write_text(json.dumps(show, ensure_ascii=False, indent=2), encoding="utf-8")
(evidence / "tags.json").write_text(json.dumps(tags, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps({"pull_seconds": time.monotonic() - started, "identity": identity}), flush=True)
