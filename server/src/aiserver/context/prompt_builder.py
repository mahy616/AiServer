"""B-owned preparation; A owns the eventual public Protocol and business DTOs."""
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
import base64
import hashlib
import json

from PIL import Image

@dataclass(frozen=True)
class PreparedPrompt:
    messages_json: str
    content_sha256: str
    prompt_version: str
    image_sha256: tuple[str, ...]
    text_utf8_bytes: int

    def messages(self):
        return json.loads(self.messages_json)


def prepare_prompt(request: dict, system_path: Path, image_bytes: tuple[bytes, ...],
                   image_policy: dict) -> PreparedPrompt:
    """Consume validated business materials, preserving all text and image bytes.

    This supplements (does not replace) A's transport/schema/ownership checks.
    Expected answers from eval case.json are never supplied to this function.
    """
    if len(image_bytes) > image_policy["MaxImages"]:
        raise ValueError("IMAGE_LIMIT_EXCEEDED")
    images, hashes = [], []
    for raw in image_bytes:
        if len(raw) > image_policy["MaxBytesPerImage"]:
            raise ValueError("IMAGE_LIMIT_EXCEEDED")
        with Image.open(BytesIO(raw)) as picture:
            width, height = picture.size
            if picture.format != "PNG":
                raise ValueError("IMAGE_INVALID")
            if max(width, height) > image_policy["MaxLongEdge"] or width * height > image_policy["MaxPixelsPerImage"]:
                raise ValueError("IMAGE_LIMIT_EXCEEDED")
            picture.verify()
        with Image.open(BytesIO(raw)) as picture:
            picture.load()
        images.append(base64.b64encode(raw).decode("ascii"))
        hashes.append(hashlib.sha256(raw).hexdigest())
    # History remains untrusted material inside user content, never system roles.
    order = (("context", "currentCode", "history", "operation", "instruction")
             if system_path.stem == "s01-bootstrap-vl-v2" else
             ("operation", "instruction", "currentCode", "context", "history"))
    material = {key: request.get(key) for key in order}
    user = {"role": "user", "content": json.dumps(material, ensure_ascii=False, separators=(",", ":"))}
    if images:
        user["images"] = images
    messages = [{"role": "system", "content": system_path.read_text(encoding="utf-8-sig")}, user]
    serialized = json.dumps(messages, ensure_ascii=False, separators=(",", ":"))
    return PreparedPrompt(serialized, hashlib.sha256(serialized.encode()).hexdigest(),
                          system_path.stem, tuple(hashes),
                          sum(len(item["content"].encode("utf-8")) for item in messages))
