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

def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)

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
        canonical(value).encode("utf-8")  # Reject unpaired Unicode surrogates anywhere.
        return value
    except (ValueError, UnicodeError, RecursionError):
        raise ApiError(400, "INVALID_REQUEST") from None

def decode_image(item):
    try:
        raw = base64.b64decode(item["dataBase64"], validate=True)
    except (ValueError, binascii.Error):
        raise ApiError(415, "IMAGE_INVALID") from None
    if not raw or len(raw) > 3145728:
        raise ApiError(422, "IMAGE_LIMIT_EXCEEDED")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(raw), formats=["PNG"]) as picture:
                width, height = picture.size
                if max(width, height) > 1280 or width * height > 786432:
                    raise ApiError(422, "IMAGE_LIMIT_EXCEEDED")
                if getattr(picture, "n_frames", 1) != 1:
                    raise ApiError(415, "IMAGE_INVALID")
                picture.verify()
            with Image.open(BytesIO(raw), formats=["PNG"]) as picture:
                picture.load()
    except ApiError:
        raise
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError, Image.DecompressionBombError,
            Image.DecompressionBombWarning):
        raise ApiError(415, "IMAGE_INVALID") from None
    return ImageInput(item["imageId"], raw, hashlib.sha256(raw).hexdigest(), width, height)

class RequestValidator:
    def __init__(self, directory):
        self.schema = json.loads((directory / "job-request.schema.json").read_text(encoding="utf-8"))
        self.validator = Draft202012Validator(self.schema, format_checker=FormatChecker())

    def validate(self, raw):
        request = strict_json(raw)
        error = next(self.validator.iter_errors(request), None)
        if error:
            path = list(error.absolute_path)
            if path and path[0] == "images":
                raise ApiError(422 if error.validator in ("maxItems", "maxLength") else 415,
                               "IMAGE_LIMIT_EXCEEDED" if error.validator in ("maxItems", "maxLength") else "IMAGE_INVALID")
            raise ApiError(400, "INVALID_REQUEST")
        if not request["instruction"].strip():
            raise ApiError(400, "INVALID_REQUEST", {"field": "instruction"})
        request["clientRequestId"] = str(UUID(request["clientRequestId"]))
        for key, default in (("currentCode", None), ("parentJobId", None), ("history", []), ("images", [])):
            request.setdefault(key, default)
        if request["parentJobId"]:
            request["parentJobId"] = str(UUID(request["parentJobId"]))
        if request["operation"] == "modify" and not (request["currentCode"] or "").strip():
            raise ApiError(400, "INVALID_REQUEST", {"field": "currentCode"})
        ctx = request["context"]
        for key in self.schema["properties"]["context"]["properties"]:
            ctx.setdefault(key, None)
        fields = [(request["instruction"], 8192), (request["currentCode"], 65536),
                  (ctx["sdkReferenceText"], 32768), (ctx["flowContextText"], 32768)]
        history_size = sum(len(x["content"].encode("utf-8")) for x in request["history"])
        if any(len((value or "").encode("utf-8")) > limit for value, limit in fields) or history_size > 16384:
            raise ApiError(413, "REQUEST_TOO_LARGE")
        text_size = sum(len((value or "").encode("utf-8")) for value, _ in fields) + history_size
        if text_size > 131072:
            raise ApiError(413, "REQUEST_TOO_LARGE")
        for item in request["images"]:
            item.setdefault("capturedAt", None)
            item.setdefault("snapshotRevision", None)
        if len({x["imageId"] for x in request["images"]}) != len(request["images"]):
            raise ApiError(400, "INVALID_REQUEST")
        images = tuple(decode_image(x) for x in request["images"])
        # Canonicalize Base64 without changing original decoded bytes.
        for item, decoded in zip(request["images"], images):
            item["dataBase64"] = base64.b64encode(decoded.png).decode("ascii")
        business = json.loads(canonical(request))
        del business["clientRequestId"]
        for item, decoded in zip(business["images"], images):
            del item["dataBase64"]
            item["sha256"] = decoded.sha256
        fingerprint = hashlib.sha256(canonical(business).encode("utf-8")).hexdigest()
        return request, fingerprint, TaskInput(canonical(request), images)
