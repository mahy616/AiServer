"""One console starts API/model and optional managed NATAPP."""
import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "server/src"))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path)
    parser.add_argument("--no-tunnel", action="store_true", help="Local development only")
    args = parser.parse_args()
    config = args.config or ROOT / "config/server.local.json"
    if not config.exists() and args.config is None:
        config = ROOT / "config/server.ollama.example.json"
    from aiserver.__main__ import entrypoint
    command = ["--config", str(config.resolve()), "serve"]
    if args.no_tunnel:
        command.append("--no-tunnel")
    raise SystemExit(entrypoint(command))
