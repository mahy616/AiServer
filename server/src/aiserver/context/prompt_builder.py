"""Shared production material preparation; the HTTP contract remains separate."""
from dataclasses import dataclass
import base64
import hashlib
import json

@dataclass(frozen=True)
class PreparedPrompt:
    messages_json: str
    content_sha256: str
    prompt_version: str
    image_sha256: tuple[str, ...]
    text_utf8_bytes: int
    tools_json: str = "[]"
    options_json: str = "{}"
    tool_choice: str = "auto"
    parallel_tool_calls: bool = True
    think: bool | str | None = None
    think_provided: bool = False

    def material_sha256(self):
        material = json.dumps([self.messages_json, self.tools_json, self.options_json,
                               self.tool_choice, self.parallel_tool_calls, self.think, self.think_provided], ensure_ascii=False)
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    def messages(self):
        return json.loads(self.messages_json)


def prepare_messages(request: dict, images, image_policy: dict, *, provider="Ollama") -> PreparedPrompt:
    """Freeze client roles and ordered parts without injecting a system prompt.

    Ollama's native API uses a string plus an images array. [img] preserves each
    image's position in that string; original image bytes are passed unchanged.
    """
    if len(images) > image_policy["MaxImages"]:
        raise ValueError("IMAGE_LIMIT_EXCEEDED")
    messages, hashes = [], []
    decoded = iter(images)
    for original in request["messages"]:
        pieces, encoded = [], []
        for part in original["content"]:
            if part["type"] == "text":
                pieces.append(part["text"])
            else:
                image = next(decoded)
                if (len(image.data) > image_policy["MaxBytesPerImage"] or
                        max(image.width, image.height) > image_policy["MaxLongEdge"] or
                        image.width * image.height > image_policy["MaxPixelsPerImage"]):
                    raise ValueError("IMAGE_LIMIT_EXCEEDED")
                pieces.append("[img]")
                encoded.append(base64.b64encode(image.data).decode("ascii"))
                hashes.append(image.sha256)
        message = {"role": original["role"], "content": "".join(pieces)}
        if "reasoning_content" in original:
            message["reasoning_content" if provider == "Strata" else "thinking"] = original["reasoning_content"]
        if original.get("tool_calls"):
            if provider == "Strata":
                message["tool_calls"] = [{"id": call["id"], "type": "function", "function": {
                    "name": call["function"]["name"], "arguments": json.dumps(call["function"]["arguments"],
                        ensure_ascii=False, separators=(",", ":"), allow_nan=False)}} for call in original["tool_calls"]]
            else:
                message["tool_calls"] = [{"type": "function", "function": call["function"]}
                                         for call in original["tool_calls"]]
        if original["role"] == "tool":
            if provider == "Strata":
                message["tool_call_id"] = original["tool_call_id"]
            else:
                message["tool_name"] = original["tool_name"]
        if encoded:
            message["images"] = encoded
        messages.append(message)
    serialized = json.dumps(messages, ensure_ascii=False, separators=(",", ":"))
    tools = [{"type": "function", "function": {k: v for k, v in tool["function"].items() if k != "strict"}}
             for tool in request.get("tools", [])]
    prepared = PreparedPrompt(serialized, "", request.get("protocol", "client-messages-v1"), tuple(hashes),
                              sum(len((m["content"] + m.get("thinking", m.get("reasoning_content", ""))).encode("utf-8")) for m in messages),
                              json.dumps(tools, ensure_ascii=False, separators=(",", ":")),
                              json.dumps(request.get("options", {})),
                              request.get("tool_choice", "auto"), request.get("parallel_tool_calls", True),
                              request.get("think"), "think" in request)
    from dataclasses import replace
    return replace(prepared, content_sha256=prepared.material_sha256())
