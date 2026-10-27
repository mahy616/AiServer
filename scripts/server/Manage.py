"""Administrative CLI without installing the source package or setting PYTHONPATH."""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "server/src"))

if __name__ == "__main__":
    from aiserver.__main__ import entrypoint
    raise SystemExit(entrypoint())
