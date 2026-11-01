"""Model-specific thinking controls; named levels never masquerade as an off mode."""
LEVELS = ("low", "medium", "high")


class ThinkingParameterError(ValueError):
    pass


def validate_thinking_config(cfg):
    levels = cfg.get("ThinkingLevels", [])
    default = cfg.get("DefaultThinkingLevel")
    if levels or default is not None:
        if levels != list(LEVELS) or default not in levels:
            raise ValueError("Invalid ThinkingLevels / DefaultThinkingLevel")


def resolve_thinking(cfg, requested, supported):
    validate_thinking_config(cfg)
    levels = cfg.get("ThinkingLevels", [])
    if levels:
        if not supported:
            raise ThinkingParameterError("thinking_levels_unverified")
        if requested is False:
            raise ThinkingParameterError("thinking_cannot_be_disabled")
        if requested is None or requested is True:
            return cfg["DefaultThinkingLevel"]
        if isinstance(requested, str) and requested in levels:
            return requested
        raise ThinkingParameterError("unsupported_thinking_level")
    if isinstance(requested, str):
        raise ThinkingParameterError("named_thinking_levels_not_supported")
    return (requested is True) if supported else None


def verify_thinking(cfg, show):
    """Require engine evidence before publishing named controls."""
    validate_thinking_config(cfg)
    levels = cfg.get("ThinkingLevels", [])
    if levels:
        metadata = show.get("thinking", {})
        if (not isinstance(metadata, dict) or
                not set(levels).issubset(metadata.get("values", []))):
            raise ValueError("Engine did not verify the configured thinking levels")
