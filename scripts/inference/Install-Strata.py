"""Prepare pinned Strata Qwen IQ3_XXS or DeepSeek IQ2_XXS; keeps other model configurations."""
import argparse
from pathlib import Path
from strata_prepare import ROOT, prepare


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--family", choices=("qwen", "deepseek"), default="qwen")
    parser.add_argument("--server-config", default=str(ROOT / "config/server.local.json" if (ROOT / "config/server.local.json").exists()
                        else ROOT / "config/server.ollama.example.json"))
    parser.add_argument("--context", type=int, help="Explicit window; first defaults Qwen 128K / DeepSeek 32K, existing tuning is retained")
    parser.add_argument("--max-output", type=int, help="Explicit output ceiling; adjust this too when selecting a small window")
    parser.add_argument("--source-models", type=Path, help="Reuse exact pinned GGUF filenames from this folder")
    parser.add_argument("--install-dependencies", action="store_true")
    parser.add_argument("--pause", action="store_true")
    args = parser.parse_args()
    try:
        prepare(args)
        return 0
    except Exception as error:
        print("Strata preparation failed: " + str(error), flush=True)
        return 1
    finally:
        if args.pause:
            input("Press Enter to close...")


if __name__ == "__main__":
    raise SystemExit(main())
