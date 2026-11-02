"""Practical admission estimates; actual counts are supplied by the engine later."""
from dataclasses import dataclass
import base64
from io import BytesIO
import json
from PIL import Image
from .prompt_builder import PreparedPrompt
from .window_budget import validate_window, output_budget, input_budget


@dataclass(frozen=True)
class BudgetDecision:
    ready: bool
    code: str
    method: str
    input_tokens: int | None
    limit_tokens: int
    estimated_input_tokens: int | None = None


def check_budget(prepared: PreparedPrompt, profile: dict) -> BudgetDecision:
    validate_window(profile)
    limit = input_budget(profile, output_budget(profile, json.loads(prepared.options_json)))
    text = "".join(item["content"] + item.get("thinking", item.get("reasoning_content", "")) for item in prepared.messages())
    if prepared.prompt_version == "openai-chat-v1":
        text += prepared.tools_json
        text += "".join(json.dumps(m["tool_calls"], ensure_ascii=False) for m in prepared.messages() if m.get("tool_calls"))
    ascii_count = sum(ord(c) < 128 for c in text)
    estimate = (ascii_count + 1) // 2 + (len(text) - ascii_count) * 2 + 128 + 16 * len(prepared.messages())
    for message in prepared.messages():
        for raw in message.get("images", []):
            with Image.open(BytesIO(base64.b64decode(raw))) as picture:
                width, height = picture.size
            estimate += max(1536, ((width + 31) // 32) * ((height + 31) // 32) + 512)
    ready = estimate <= limit
    return BudgetDecision(ready, "OK" if ready else "CONTEXT_TOO_LARGE", "conservative-estimate-v1",
                          None, limit, estimate)
