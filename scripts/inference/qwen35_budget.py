"""Pinned tokenizer asset for counting the engine's complete Qwen3.5 prompt."""
import json
from pathlib import Path
from coder_support import download


def budget_config(source):
    return {"Method": "ollama-render-tokenizer-v1", "IsExact": False,
            "TokenizerPath": ".local/qwen35/source/tokenizer.json",
            "TokenizerSha256": source["tokenizer_files"]["tokenizer.json"]}


def prepare_tokenizer(root, source):
    budget = budget_config(source)
    path = Path(root) / budget["TokenizerPath"]
    download(f'https://huggingface.co/{source["tokenizer_repository"]}/resolve/'
             f'{source["tokenizer_revision"]}/tokenizer.json', path, budget["TokenizerSha256"])
    # Validate with the same library used at runtime before touching any config.
    from tokenizers import Tokenizer
    Tokenizer.from_file(str(path))
    return budget
