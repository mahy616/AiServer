"""Download pinned Qwen3.8-27B UD-IQ3_XXS and append it after a full-GPU check."""
from pinned_model_install import ROOT, install as install_model, main as run

MODEL = "hf.co/unsloth/Qwen3.8-27B-GGUF:UD-IQ3_XXS"
TEMPLATE = "config/inference.qwen38.example.json"
OUTPUT_CONFIG = "config/inference.qwen38.local.json"


def install(root=ROOT, **kwargs):
    return install_model("qwen38", root, **kwargs)


if __name__ == "__main__":
    raise SystemExit(run(("qwen38",)))
