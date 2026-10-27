"""One window/output policy shared by admission, generation and capabilities."""
from dataclasses import replace
import json
from aiserver.inference.configuration import generation_defaults

POLICY = "window-minus-output-v1"


class OutputLimitExceeded(ValueError):
    pass


def max_output_tokens(cfg):
    # NumPredict remains the portable legacy name for this resource limit.
    return cfg.get("MaxOutputTokens", cfg.get("NumPredict"))


def validate_window(cfg):
    context, output, reserved = cfg["NumCtx"], max_output_tokens(cfg), cfg["ReservedTokens"]
    if (any(type(value) is not int for value in (context, output, reserved))
            or output < 1 or reserved < 0 or context <= output + reserved):
        raise ValueError("Invalid context/output partition")


def output_budget(cfg, options=None):
    limit = max_output_tokens(cfg)
    requested = (options or {}).get("num_predict", limit)
    if type(requested) is not int or requested < 1:
        raise ValueError("Invalid output token budget")
    if requested > limit:
        raise OutputLimitExceeded("OUTPUT_LIMIT_EXCEEDED")
    return requested


def input_budget(cfg, output_tokens=None):
    return cfg["NumCtx"] - (max_output_tokens(cfg) if output_tokens is None else output_tokens) - cfg["ReservedTokens"]


def freeze_output(prepared, cfg, *, supports_thinking=False):
    defaults = generation_defaults(cfg)
    # Thinking is request opt-in, including with older private admin defaults.
    # Unsupported models receive no think field at all (Ollama may reject it).
    defaults.pop("think", None)
    options = {**defaults, **json.loads(prepared.options_json)}
    options["num_predict"] = output_budget(cfg, options)
    prepared = replace(prepared, options_json=json.dumps(options),
                       think=(prepared.think is True) if supports_thinking else None)
    return replace(prepared, content_sha256=prepared.material_sha256())


def budget_details(cfg, prepared, count=None):
    output = output_budget(cfg, json.loads(prepared.options_json))
    return {"contextTokens": cfg["NumCtx"], "outputTokens": output,
            "reservedTokens": cfg["ReservedTokens"], "inputLimitTokens": input_budget(cfg, output),
            "inputTokens": count}
