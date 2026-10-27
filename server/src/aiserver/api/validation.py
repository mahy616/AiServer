import base64
import binascii
import hashlib
from io import BytesIO
import json
from uuid import UUID
import warnings

from jsonschema import Draft202012Validator, FormatChecker
from PIL import Image, UnidentifiedImageError

from .errors import ApiError
from aiserver.inference.contracts import ImageInput, TaskInput

MAX_TEXT_UTF8_BYTES = 2097152


def request_text_bytes(request):
    total = sum(len(part["text"].encode("utf-8")) for message in request["messages"]
                for part in message["content"] if part["type"] == "text")
    if request.get("tools"):
        total += len(canonical(request["tools"]).encode("utf-8"))
    total += sum(len(canonical(message["tool_calls"]).encode("utf-8"))
                 for message in request["messages"] if message.get("tool_calls"))
    total += sum(len(message.get("reasoning_content", "").encode("utf-8")) for message in request["messages"])
    return total


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def wire_json(value):
    """Keep insertion order for model material; canonical JSON is for identity only."""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON property")
        result[key] = value
    return result


def strict_json(raw):
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=unique_pairs,
                          parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Nonfinite")))
        canonical(value).encode("utf-8")
        return value
    except (ValueError, UnicodeError, RecursionError):
        raise ApiError(400, "INVALID_REQUEST") from None


def decode_image(item, image_id):
    try:
        raw = base64.b64decode(item["dataBase64"], validate=True)
    except (ValueError, binascii.Error):
        raise ApiError(415, "IMAGE_INVALID") from None
    if not raw or len(raw) > 3145728:
        raise ApiError(422, "IMAGE_LIMIT_EXCEEDED")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(raw), formats=["PNG", "JPEG"]) as picture:
                width, height = picture.size
                if {"PNG": "image/png", "JPEG": "image/jpeg"}.get(picture.format) != item["mediaType"]:
                    raise ApiError(415, "IMAGE_INVALID")
                if max(width, height) > 1280 or width * height > 786432:
                    raise ApiError(422, "IMAGE_LIMIT_EXCEEDED")
                if getattr(picture, "n_frames", 1) != 1:
                    raise ApiError(415, "IMAGE_INVALID")
                picture.verify()
            with Image.open(BytesIO(raw), formats=["PNG", "JPEG"]) as picture:
                picture.load()
    except ApiError:
        raise
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError, Image.DecompressionBombError,
            Image.DecompressionBombWarning):
        raise ApiError(415, "IMAGE_INVALID") from None
    return ImageInput(image_id, raw, hashlib.sha256(raw).hexdigest(), width, height, item["mediaType"])


class RequestValidator:
    def __init__(self, directory):
        self.schema = json.loads((directory / "job-request.schema.json").read_text(encoding="utf-8"))
        self.validator = Draft202012Validator(self.schema, format_checker=FormatChecker())

    def validate(self, raw):
        request = strict_json(raw)
        error = next(self.validator.iter_errors(request), None)
        if error:
            raise ApiError(400, "INVALID_REQUEST", {"field": ".".join(map(str, error.absolute_path))})
        request["clientRequestId"] = str(UUID(request["clientRequestId"]))
        identity = json.loads(canonical(request))
        del identity["clientRequestId"]
        images, text_bytes, meaningful = [], 0, False
        for mi, message in enumerate(request["messages"]):
            if "reasoning_content" in message:
                if message["role"] != "assistant":
                    raise ApiError(400, "INVALID_REQUEST", {"field": "messages.reasoning_content"})
                text_bytes += len(message["reasoning_content"].encode("utf-8"))
            has_images = any(part["type"] == "image" for part in message["content"])
            if has_images and message["role"] != "user":
                raise ApiError(400, "INVALID_REQUEST", {"field": "messages", "reason": "images_require_user_role"})
            for pi, part in enumerate(message["content"]):
                if part["type"] == "text":
                    text_bytes += len(part["text"].encode("utf-8"))
                    meaningful = meaningful or bool(part["text"].strip())
                    if has_images and "[img]" in part["text"]:
                        raise ApiError(400, "INVALID_REQUEST", {"field": "messages", "reason": "reserved_image_marker"})
                else:
                    if len(images) >= 4:
                        raise ApiError(422, "IMAGE_LIMIT_EXCEEDED")
                    decoded = decode_image(part, f"{mi}:{pi}")
                    images.append(decoded)
                    part["dataBase64"] = base64.b64encode(decoded.data).decode("ascii")
                    hashed = identity["messages"][mi]["content"][pi]
                    del hashed["dataBase64"]
                    hashed["sha256"] = decoded.sha256
                    meaningful = True
        if not meaningful or not any(m["role"] == "user" for m in request["messages"]):
            raise ApiError(400, "INVALID_REQUEST")
        if text_bytes > MAX_TEXT_UTF8_BYTES:
            raise ApiError(413, "REQUEST_TOO_LARGE")
        fingerprint = hashlib.sha256(canonical(identity).encode("utf-8")).hexdigest()
        return request, fingerprint, TaskInput(wire_json(request), tuple(images))
