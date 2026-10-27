"""Public capability projection; never expose local paths or engine configuration."""
import json
from aiserver.context.window_budget import POLICY, input_budget, max_output_tokens


def model_description(profile, settings):
    snapshot = json.loads(profile.snapshot_json)
    cfg = snapshot.get("config", {})
    policy = cfg.get("ImagePolicy", {})
    vision = "image" in profile.accepted_input_types
    window = all(key in cfg for key in ("NumCtx", "ReservedTokens")) and max_output_tokens(cfg) is not None
    return {
        "id": cfg.get("Model", "fake-v1"),
        "inputTypes": list(profile.accepted_input_types),
        "outputTypes": ["text"],
        "limits": {
            "maxRequestBytes": settings.max_request_bytes,
            "maxTextUtf8Bytes": cfg.get("MaxTextUtf8Bytes", 131072),
            "maxMessages": 128,
            "maxImages": min(4, policy.get("MaxImages", 4)) if vision else 0,
            "imageMediaTypes": ["image/png", "image/jpeg"] if vision else [],
            "maxImageBytes": policy.get("MaxBytesPerImage", 3145728) if vision else 0,
            "maxImageLongEdge": policy.get("MaxLongEdge", 1280) if vision else 0,
            "maxImagePixels": policy.get("MaxPixelsPerImage", 786432) if vision else 0,
            "contextTokens": cfg.get("NumCtx"),
            "maxInputTokens": input_budget(cfg, 1) if window else cfg.get("MaxInputTokens"),
            "defaultMaxInputTokens": input_budget(cfg) if window else cfg.get("MaxInputTokens"),
            "reservedTokens": cfg.get("ReservedTokens"),
            "inputBudgetPolicy": POLICY if window else "provider-defined",
            "maxOutputTokens": max_output_tokens(cfg),
        },
        "capacityMethod": snapshot.get("capacityMethod", snapshot.get("budgetMethod", "unknown")),
        "generationParameters": "request-or-model-default",
        "outputChannels": ["content", "reasoning_content", "tool_calls"],
        "roleMappings": {"developer": "system"},
        "outputLimitPolicy": "reject-above-limit",
    }
