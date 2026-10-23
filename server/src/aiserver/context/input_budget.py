"""Fail closed until the actual Ollama renderer/vision budget is calibrated."""
from dataclasses import dataclass
from .prompt_builder import PreparedPrompt


@dataclass(frozen=True)
class BudgetDecision:
    ready: bool
    code: str
    method: str
    input_tokens: int | None
    limit_tokens: int


def check_budget(prepared: PreparedPrompt, profile: dict) -> BudgetDecision:
    if profile["MaxInputTokens"] + profile["NumPredict"] + profile["ReservedTokens"] > profile["NumCtx"]:
        raise ValueError("Invalid profile token partition")
    # A JSON flag or post-generation token count cannot certify this algorithm.
    # No calibrated counting implementation exists yet, including for text-only.
    return BudgetDecision(False, "INPUT_BUDGET_UNVERIFIED", "unverified-qwen3vl-v1",
                          None, profile["MaxInputTokens"])
