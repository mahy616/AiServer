"""Validated process settings shared by every engine launch path."""
import os
import math
import json
from urllib.parse import urlsplit

PROCESS_KEYS = ("BaseUrl", "ExecutablePath", "ModelsPath", "EngineVersion",
                "MaxLoadedModels", "NumParallel", "EngineMaxQueue")

# Only an explicit GenerationDefaults object supplies sampling defaults.
# Its legacy think key is still validated/readable, but thinking is request opt-in.
# Retired top-level Temperature/TopP/TopK/Seed/Think fields remain readable but
# never override the installed model's own defaults.
GENERATION_KEYS = {"temperature", "top_p", "top_k", "seed", "presence_penalty",
                   "repeat_penalty", "stop", "think"}


def generation_defaults(cfg):
    values = cfg.get("GenerationDefaults", {})
    if not isinstance(values, dict) or values.keys() - GENERATION_KEYS:
        raise ValueError("Invalid GenerationDefaults")
    for key, value in values.items():
        if key == "think":
            valid = value is None or type(value) is bool
        elif key == "stop":
            valid = (isinstance(value, list) and len(value) <= 4
                     and all(isinstance(s, str) and 1 <= len(s) <= 256 for s in value))
        elif key in ("top_k", "seed"):
            valid = type(value) is int and ((0 <= value <= 100000) if key == "top_k"
                                           else -2147483648 <= value <= 2147483647)
        else:
            bounds = {"temperature": (0, 2), "top_p": (0, 1),
                      "presence_penalty": (-2, 2), "repeat_penalty": (0, 10)}
            low, high = bounds[key]
            valid = (type(value) in (int, float) and math.isfinite(value)
                     and low <= value <= high and (key != "top_p" or value > 0))
        if not valid:
            raise ValueError("Invalid GenerationDefaults field: " + key)
    return dict(values)


def validate_options(cfg):
    generation_defaults(cfg)
    flash = cfg.get("FlashAttention")
    cache = cfg.get("KvCacheType", "f16")
    if "Think" in cfg and type(cfg["Think"]) is not bool:
        raise ValueError("Think must be a boolean")
    if "PresencePenalty" in cfg and (type(cfg["PresencePenalty"]) not in (int, float) or not -2 <= cfg["PresencePenalty"] <= 2):
        raise ValueError("PresencePenalty must be between -2 and 2")
    if type(cfg.get("RequireFullGpu", False)) is not bool:
        raise ValueError("RequireFullGpu must be a boolean")
    if flash is not None and type(flash) is not bool:
        raise ValueError("FlashAttention must be a boolean")
    if cache not in ("f16", "q8_0", "q4_0"):
        raise ValueError("KvCacheType must be f16, q8_0 or q4_0")
    if cache != "f16" and flash is not True:
        raise ValueError("Quantized KV requires FlashAttention=true")
    if "NumBatch" in cfg and (type(cfg["NumBatch"]) is not int or not 1 <= cfg["NumBatch"] <= 2048):
        raise ValueError("NumBatch must be between 1 and 2048")
    size = cfg.get("MaxTextUtf8Bytes", 131072)
    if type(size) is not int or not 1 <= size <= 2097152:
        raise ValueError("MaxTextUtf8Bytes must be between 1 and 2097152")


def process_signature(cfg):
    validate_options(cfg)
    return {**{k: cfg[k] for k in PROCESS_KEYS},
            "FlashAttention": cfg.get("FlashAttention"), "KvCacheType": cfg.get("KvCacheType", "f16")}


def runtime_options(cfg, output_tokens=None):
    """The same allocation options for preload, rendering and generation."""
    options = {"num_ctx": cfg["NumCtx"], "num_predict": cfg.get("MaxOutputTokens", cfg.get("NumPredict")) if output_tokens is None else output_tokens}
    if "NumBatch" in cfg:
        options["num_batch"] = cfg["NumBatch"]
    if cfg.get("RequireFullGpu"):
        options["num_gpu"] = 999  # Explicitly request every layer, including output.
    return options


def chat_payload(cfg, prepared, *, stream):
    """Counting and generation share exactly the same frozen model inputs."""
    from aiserver.context.window_budget import output_budget
    options = json.loads(prepared.options_json)
    payload = {"model": cfg["Model"], "messages": prepared.messages(), "stream": stream,
               "truncate": False, "shift": False, "keep_alive": cfg["KeepAlive"],
               "options": {**options, **runtime_options(cfg, output_budget(cfg, options))}}
    if prepared.think is not None:
        payload["think"] = prepared.think
    tools = json.loads(prepared.tools_json)
    if tools:
        payload["tools"] = tools
    return payload


def engine_environment(cfg, root):
    validate_options(cfg)
    environment = os.environ.copy()
    environment.update({"OLLAMA_HOST": urlsplit(cfg["BaseUrl"]).netloc,
                        "OLLAMA_MODELS": str((root / cfg["ModelsPath"]).resolve()),
                        "OLLAMA_CONTEXT_LENGTH": str(cfg["NumCtx"]),
                        "OLLAMA_NUM_PARALLEL": "1", "OLLAMA_MAX_LOADED_MODELS": "1", "OLLAMA_MAX_QUEUE": "1",
                        "OLLAMA_KV_CACHE_TYPE": cfg.get("KvCacheType", "f16"),
                        "OLLAMA_KEEP_ALIVE": cfg["KeepAlive"], "OLLAMA_NO_CLOUD": "1", "OLLAMA_DEBUG": "0"})
    # Missing legacy option retains Ollama's own automatic attention selection.
    environment.pop("OLLAMA_FLASH_ATTENTION", None)
    if "FlashAttention" in cfg:
        environment["OLLAMA_FLASH_ATTENTION"] = "1" if cfg["FlashAttention"] else "0"
    return environment
