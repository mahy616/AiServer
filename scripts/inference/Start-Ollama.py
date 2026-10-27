"""Unique foreground engine launcher; uses process-local configuration only."""
from pathlib import Path
import argparse
import json
import os
import socket
import subprocess
import sys
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=ROOT / "config/inference.development.example.json")
    args = parser.parse_args()
    cfg = json.loads(args.config.read_text(encoding="utf-8-sig"))
    uri = urlsplit(cfg["BaseUrl"])
    if uri.scheme != "http" or uri.hostname != "127.0.0.1" or uri.path not in ("", "/"):
        raise ValueError("Only loopback HTTP is supported")
    with socket.socket() as probe:
        if probe.connect_ex((uri.hostname, uri.port)) == 0:
            raise RuntimeError("Port in use; inspect existing instance")
    engine = (ROOT / cfg["ExecutablePath"]).resolve()
    models = (ROOT / cfg["ModelsPath"]).resolve()
    models.mkdir(parents=True, exist_ok=True)
    sys.path.insert(0, str(ROOT / "server/src"))
    from aiserver.inference.configuration import engine_environment
    environment = engine_environment(cfg, ROOT)
    child = subprocess.Popen([str(engine), "serve"], env=environment, cwd=ROOT)
    pid_path = ROOT / ".local/locks/engine-process.json"
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.write_text(json.dumps({"pid": child.pid, "executable": str(engine)}), encoding="utf-8")
    try:
        return child.wait()
    except KeyboardInterrupt:
        if child.poll() is None:
            child.terminate()
        return child.wait(timeout=30)


if __name__ == "__main__":
    sys.exit(main())
