"""Prepare pinned Strata Qwen IQ3_XXS or DeepSeek IQ2_XXS; keeps other model configurations."""
import argparse
from pathlib import Path
from strata_prepare import ROOT, run_with_diagnostics, enable_models


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--family", choices=("qwen", "deepseek"), default="qwen")
    parser.add_argument("--enable-models-only", action="store_true", help="Register both prepared models without downloading, repacking or loading them")
    parser.add_argument("--deepseek-layout", choices=("four-shard", "six-shard"),
                        help="DeepSeek GGUF layout; otherwise detect existing six-shard files or retain the saved layout")
    parser.add_argument("--server-config", default=str(ROOT / "config/server.local.json" if (ROOT / "config/server.local.json").exists()
                        else ROOT / "config/server.ollama.example.json"))
    parser.add_argument("--context", type=int, help="Explicit window; Qwen and DeepSeek first default to 128K, existing tuning is retained")
    parser.add_argument("--max-output", type=int, help="Explicit output ceiling; adjust this too when selecting a small window")
    parser.add_argument("--source-models", type=Path, help="Reuse exact pinned GGUF filenames from this folder")
    parser.add_argument("--storage-root", type=Path, help="Absolute SSD folder for downloads, models, MTP, prepared packs and runtime; saved per model after successful preparation")
    parser.add_argument("--install-dependencies", action="store_true")
    parser.add_argument("--offline", action="store_true", help="Use local files only; never download weights, runtime, dependencies or MTP")
    parser.add_argument("--pause", action="store_true")
    args = parser.parse_args()
    try:
        if args.enable_models_only:
            enable_models(args.server_config)
            return 0
        return run_with_diagnostics(args)
    finally:
        if args.pause:
            input("Press Enter to close...")


if __name__ == "__main__":
    raise SystemExit(main())
