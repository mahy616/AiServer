"""Install GPT-OSS-20B and Gemma 4 12B/26B without replacing existing Qwen models."""
from pinned_model_install import main


if __name__ == "__main__":
    raise SystemExit(main(("gptoss", "gemma12", "gemma26")))
