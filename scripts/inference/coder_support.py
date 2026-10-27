"""Content-verified Coder downloads and tensor-preserving YaRN GGUF preparation."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import time

ROOT = Path(__file__).resolve().parents[2]
REGISTRY = "https://registry.ollama.ai/v2/library/qwen2.5-coder"
YARN = {"qwen2.context_length": 131072, "qwen2.rope.scaling.type": "yarn",
        "qwen2.rope.scaling.factor": 4.0,
        "qwen2.rope.scaling.original_context_length": 32768,
        "qwen2.rope.scaling.finetuned": False}


TOOL_TEMPLATE_VERSION = "qwen25-xml-tools-v2"


def legacy_json_template(original):
    """Identify the previous template exactly, only for a checked migration."""
    expected = "within <tool_call></tool_call> with NO other text. Do not include any backticks or ```json."
    if "{{ .Function }}" not in original or expected not in original:
        raise ValueError("Pinned source template shape changed")
    return (original.replace("{{ .Function }}", "{{ json .Function }}")
            .replace(expected, "with NO other text. Do not include XML tags, backticks or ```json.")
            .replace("<tool_call>", "").replace("</tool_call>", ""))


def compatible_template(original):
    """Keep Qwen's trained XML protocol and serialize typed 0.40.9 tools as JSON.

    The old bare-JSON parser stops recognizing calls after any prose/backticks.
    XML delimiters let Ollama distinguish a real call even after a text prefix.
    Render content AND calls in assistant history, with one wrapper per call.
    """
    legacy_json_template(original)  # Fail closed on a different pinned source.
    value = original.replace("{{ .Function }}", "{{ json .Function }}")
    value = value.replace("{{ if .Content }}{{ .Content }}\n{{- else if .ToolCalls }}<tool_call>\n",
                          "{{ if .Content }}{{ .Content }}{{ end }}\n{{ if .ToolCalls }}\n")
    value = value.replace('{{ range .ToolCalls }}{"name": "{{ .Function.Name }}", "arguments": {{ .Function.Arguments }}}\n{{ end }}</tool_call>',
                          '{{ range .ToolCalls }}<tool_call>\n{"name": {{ json .Function.Name }}, "arguments": {{ json .Function.Arguments }}}\n</tool_call>\n{{ end }}')
    return value


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def download(url, path, expected, size=None):
    import requests
    path = Path(path)
    if path.exists():
        if (size is None or path.stat().st_size == size) and digest(path) == expected:
            return path
        raise ValueError("Existing download differs from pinned SHA256: " + str(path))
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_suffix(path.suffix + ".download")
    # Resume a single contiguous download; always validate the final full hash.
    offset = partial.stat().st_size if partial.exists() else 0
    with requests.get(url, headers={"Range": f"bytes={offset}-"} if offset else {},
                      stream=True, timeout=(15, 90)) as response:
        response.raise_for_status()
        resume = offset and response.status_code == 206
        count = offset if resume else 0
        with partial.open("ab" if resume else "wb") as stream:
            last = time.monotonic()
            for block in response.iter_content(4 * 1024 * 1024):
                count += len(block)
                if size is not None and count > size:
                    raise ValueError("Download exceeded pinned length")
                stream.write(block)
                if time.monotonic() - last > 10:
                    print(f"Downloaded {count / 1024**2:.0f} MiB", flush=True)
                    last = time.monotonic()
    if (size is not None and count != size) or digest(partial) != expected:
        raise ValueError("Download SHA256/length mismatch: " + str(path))
    os.replace(partial, path)
    return path


def source_files(root=ROOT, *, source_gguf=None, variant="7b", models_path=None):
    root = Path(root)
    lock = json.loads((root / f"config/models/qwen25coder{variant}.source.json").read_text(encoding="utf-8"))
    work = root / (".local/coder" if variant == "7b" else f".local/coder{variant}") / "source"
    work.mkdir(parents=True, exist_ok=True)
    for name, expected in lock["tokenizer_files"].items():
        download(f'https://huggingface.co/{lock["tokenizer_repository"]}/resolve/'
                 f'{lock["tokenizer_revision"]}/{name}', work / name, expected)
    download(REGISTRY + "/blobs/sha256:" + lock["template_sha256"], work / "ollama.template",
             lock["template_sha256"], 1615)
    download(REGISTRY + "/blobs/sha256:" + lock["license_sha256"], work / "LICENSE.txt",
             lock["license_sha256"], 11343)
    filename = "qwen25coder7b-q5_k_m.gguf" if variant == "7b" else f"qwen25coder{variant}-q4_k_m.gguf"
    model = Path(source_gguf).resolve() if source_gguf else work / filename
    if source_gguf:
        if model.stat().st_size != lock["blob_size"] or digest(model) != lock["blob_sha256"]:
            raise ValueError("--source-gguf must match the pinned official GGUF: " + lock["model"])
    else:
        # Reuse this exact content-addressed blob from the normal portable store.
        existing = root / (models_path or ".local/ollama/models") / "blobs" / ("sha256-" + lock["blob_sha256"])
        if existing.is_file() and existing.stat().st_size == lock["blob_size"] and digest(existing) == lock["blob_sha256"]:
            model = existing
        else:
            if shutil.disk_usage(work).free < lock["blob_size"] * 2 + 256 * 1024**2:
                raise ValueError("Need space for source GGUF and independent YaRN copy")
            download(REGISTRY + "/blobs/sha256:" + lock["blob_sha256"], model,
                     lock["blob_sha256"], lock["blob_size"])
    return model, work, lock


def make_yarn(source, destination, *, chat_template, expected_layers=28):
    """Rewrite metadata only. Check every tensor hash before publishing a new file."""
    import gguf
    import numpy as np
    destination = Path(destination)
    if destination.exists():
        raise ValueError("YaRN destination already exists; use the matching recorded preparation")
    reader = gguf.GGUFReader(str(source))
    if reader.get_field("general.architecture").contents() != "qwen2":
        raise ValueError("Expected qwen2 GGUF")
    if reader.get_field("qwen2.block_count").contents() != expected_layers:
        raise ValueError(f"Expected Coder with {expected_layers} layers")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(".preparing")
    writer = gguf.GGUFWriter(str(temporary), "qwen2", endianess=reader.endianess)
    changes = {**YARN, "tokenizer.chat_template": chat_template}
    for field in reader.fields.values():
        if field.name.startswith("GGUF.") or field.name == "general.architecture" or field.name in changes:
            continue
        writer.add_key_value(field.name, field.contents(), field.types[0],
                             sub_type=field.types[-1] if field.types[0] == gguf.GGUFValueType.ARRAY else None)
    for name, value in changes.items():
        kind = (gguf.GGUFValueType.BOOL if type(value) is bool else
                gguf.GGUFValueType.UINT32 if type(value) is int else
                gguf.GGUFValueType.FLOAT32 if type(value) is float else gguf.GGUFValueType.STRING)
        writer.add_key_value(name, value, kind)
    for tensor in reader.tensors:
        writer.add_tensor_info(tensor.name, tensor.data.shape, tensor.data.dtype, tensor.n_bytes, tensor.tensor_type)
    try:
        writer.write_header_to_file()
        writer.write_kv_data_to_file()
        writer.write_ti_data_to_file()
        for tensor in reader.tensors:
            writer.write_tensor_data(tensor.data)
        writer.close()
        check = gguf.GGUFReader(str(temporary))
        if any(check.get_field(k).contents() != v for k, v in changes.items()):
            raise ValueError("YaRN metadata verification failed")
        if len(check.tensors) != len(reader.tensors):
            raise ValueError("Tensor inventory changed")
        for original, copied in zip(reader.tensors, check.tensors):
            if (original.name != copied.name or original.tensor_type != copied.tensor_type
                    or not np.array_equal(original.shape, copied.shape)
                    or hashlib.sha256(original.data).digest() != hashlib.sha256(copied.data).digest()):
                raise ValueError("Tensor contents changed during metadata preparation")
        # Release mmap views before renaming on Windows.
        del copied, original
        check.data._mmap.close()
        del check
        import gc
        gc.collect()
        os.rename(temporary, destination)
    finally:
        writer.close()
    return destination
