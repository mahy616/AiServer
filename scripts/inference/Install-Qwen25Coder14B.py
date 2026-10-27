"""Prepare official Q4_K_M 14B, Q4 KV and an 80K all-GPU window on the target host."""
import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location("coder14b_install_shared", Path(__file__).with_name("Install-Qwen25Coder7B.py"))
shared = importlib.util.module_from_spec(spec)
spec.loader.exec_module(shared)

if __name__ == "__main__":
    raise SystemExit(shared.main(variant="14b"))
