"""Practical admission estimates; actual counts are supplied by the engine later."""
from dataclasses import dataclass
import base64
from io import BytesIO
from PIL import Image
from .prompt_builder import PreparedPrompt


@dataclass(frozen=True)
class BudgetDecision:
    ready: bool
    code: str
    method: str
    input_tokens: int | None
    limit_tokens: int
    estimated_input_tokens: int | None = None


def check_budget(prepared: PreparedPrompt, profile: dict) -> BudgetDecision:
    if profile["MaxInputTokens"] + profile["NumPredict"] + profile["ReservedTokens"] > profile["NumCtx"]:
        raise ValueError("Invalid profile token partition")
    text = "".join(item["content"] for item in prepared.messages())
    ascii_count = sum(ord(c) < 128 for c in text)
    estimate = (ascii_count + 1) // 2 + (len(text) - ascii_count) * 2 + 128
    for message in prepared.messages():
        for raw in message.get("images", []):
            with Image.open(BytesIO(base64.b64decode(raw))) as picture:
                width, height = picture.size
            estimate += max(1536, ((width + 31) // 32) * ((height + 31) // 32) + 512)
    ready = estimate <= profile["MaxInputTokens"]
    return BudgetDecision(ready, "OK" if ready else "CONTEXT_TOO_LARGE", "conservative-estimate-v1",
                          None, profile["MaxInputTokens"], estimate)
