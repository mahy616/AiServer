"""Fail closed on Ollama's allocation report before admitting GPU-only inference."""


def require_gpu_status(cfg, status):
    rows = status.get("models", []) if isinstance(status, dict) else []
    row = next((item for item in rows if isinstance(item, dict) and
                item.get("name", item.get("model")) == cfg["Model"]), None) if isinstance(rows, list) else None
    if row is not None:
        size, vram, context = (row.get(key) for key in ("size", "size_vram", "context_length"))
        # Match the pinned Ollama CLI's "100% GPU" criterion exactly. A VRAM
        # value larger than total is inconsistent accounting, not stronger proof.
        if (type(size) is int and size > 0 and type(vram) is int and vram == size
                and type(context) is int and context >= cfg["NumCtx"]):
            return row
    raise ValueError("FULL_GPU_REQUIRED")
