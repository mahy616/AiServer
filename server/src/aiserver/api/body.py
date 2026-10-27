"""Bounded JSON transport for inference requests."""
import asyncio

from aiserver.api.errors import ApiError


async def read_json_body(request, max_bytes, timeout_seconds):
    if request.headers.get("content-type", "").split(";")[0].lower() != "application/json":
        raise ApiError(415, "UNSUPPORTED_MEDIA_TYPE")
    if request.headers.get("content-encoding", "identity").lower() != "identity":
        raise ApiError(415, "UNSUPPORTED_MEDIA_TYPE")
    length = request.headers.get("content-length")
    if length:
        try:
            size = int(length)
        except ValueError:
            raise ApiError(400, "INVALID_REQUEST") from None
        if size < 0:
            raise ApiError(400, "INVALID_REQUEST")
        if size > max_bytes:
            raise ApiError(413, "REQUEST_TOO_LARGE")
    body = bytearray()
    try:
        async with asyncio.timeout(timeout_seconds):
            async for chunk in request.stream():
                if len(body) + len(chunk) > max_bytes:
                    raise ApiError(413, "REQUEST_TOO_LARGE")
                body.extend(chunk)
    except TimeoutError:
        raise ApiError(408, "UPLOAD_TIMEOUT") from None
    return bytes(body)
