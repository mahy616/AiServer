"""Portable foreground entry. The API owns the only Ollama lifecycle."""
import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "server" / "src"))

from aiserver.__main__ import main

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path)
    args = parser.parse_args()
    config = args.config or ROOT / "config" / "server.local.json"
    if not config.exists() and args.config is None:
        config = ROOT / "config" / "server.ollama.example.json"
    sys.argv = [sys.argv[0], "--config", str(config.resolve()), "serve"]
    main()
