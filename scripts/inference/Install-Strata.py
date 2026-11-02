"""Prepare the pinned Windows Strata/IQ3_XXS deployment; keeps the Ollama config."""
import argparse
from pathlib import Path
from strata_prepare import ROOT, prepare


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server-config", default=str(ROOT / "config/server.local.json" if (ROOT / "config/server.local.json").exists()
                        else ROOT / "config/server.ollama.example.json"))
    parser.add_argument("--context", type=int, help="Explicit window; default 128K on first preparation, existing tuning is retained")
    parser.add_argument("--source-models", type=Path, help="Reuse the two exact GGUF shard filenames from this folder")
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
