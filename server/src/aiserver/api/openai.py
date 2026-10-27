"""OpenAI Chat Completions facade over the same durable account/job scheduler."""
import asyncio
import base64
import hashlib
import json
import sqlite3
import time
from uuid import uuid4

from fastapi import Depends, Request
from jsonschema import Draft202012Validator
from starlette.responses import JSONResponse

from aiserver.api.body import read_json_body
from aiserver.api.errors import ApiError, safe_error
from aiserver.api.models import model_description
from aiserver.api.streaming import InferenceJSON, InferenceStream
from aiserver.api.validation import canonical, wire_json, decode_image, strict_json, MAX_TEXT_UTF8_BYTES
from aiserver.inference.contracts import TaskInput
from aiserver.storage.db import TERMINAL


def error_body(error):
    if error.code == "OUTPUT_TRUNCATED":
        limit = error.details.get("maxOutputTokens")
        message = safe_error(error.code)["message"]
        if limit is not None:
            message = f"本次输出上限为 {limit} token。" + message
        return {"error": {"message": "OUTPUT_TRUNCATED：" + message, "type": "invalid_request_error",
                          "param": "max_tokens", "code": error.code}}
    if error.code == "TOOLS_NOT_SUPPORTED":
        return {"error": {"message": "This model does not support tools. Select a tool-capable model for file operations, or use the model-bound text-only agent for conversation without tools.",
                          "type": "invalid_request_error", "param": "tools", "code": error.code}}
    if error.code == "CONTEXT_TOO_LARGE":
        details = error.details
        message = "Input exceeds the context window of this model. Please reduce the length of the messages."
        if details.get("contextTokens"):
            count = details.get("inputTokens")
            message = (f"This model's maximum context length is {details['contextTokens']} tokens. "
                       f"The requested output budget is {details['outputTokens']} tokens and "
                       f"{details['reservedTokens']} tokens are reserved; the input limit is "
                       f"{details['inputLimitTokens']} tokens. "
                       + (f"The input contains {count} tokens. " if count is not None else "")
                       + "Please reduce the length of the messages.")
        return {"error": {"message": message, "type": "invalid_request_error", "param": "messages",
                          "code": "context_length_exceeded"}}
    kind = ("authentication_error" if error.status == 401 else
            "rate_limit_error" if error.status == 429 else
            "server_error" if error.status >= 500 else "invalid_request_error")
    return {"error": {"message": safe_error(error.code)["message"], "type": kind,
                      "param": error.details.get("field"), "code": error.code}}


def error_status(error):
    return 400 if error.code == "CONTEXT_TOO_LARGE" else error.status


def stream_error_body(error):
    body = error_body(error)
    if error.code in ("CONTEXT_TOO_LARGE", "OUTPUT_TRUNCATED"):
        # The compatible SDK unwraps the outer error and strips extra fields.
        # OpenCode 1.19.x also decodes an error-event envelope from message; keep
        # the ordinary code/type fields for other Chat Completions consumers.
        detail = body["error"]
        compatible = detail
        if error.code == "OUTPUT_TRUNCATED":
            # OpenCode 1.19.x retries unknown stream codes indefinitely/backoff.
            # Its invalid_prompt classification is non-retryable. Preserve our
            # real code outside the compatibility envelope and in its metadata.
            compatible = {**detail, "code": "invalid_prompt", "aiserverCode": error.code}
        body = {"error": {**detail, "message": canonical({"type": "error", "error": compatible})}}
    return body


def invalid(field, reason="invalid_value"):
    raise ApiError(400, "INVALID_REQUEST", {"field": field, "reason": reason})


class ChatValidator:
    def __init__(self, directory):
        self.schema = Draft202012Validator(json.loads(
            (directory / "openai-chat-request.schema.json").read_text(encoding="utf-8")))

    def validate(self, raw):
        value = strict_json(raw)
        error = next(self.schema.iter_errors(value), None)
        if error:
            invalid(".".join(str(x) for x in error.absolute_path) or "request")
        if "max_tokens" in value and "max_completion_tokens" in value:
            invalid("max_tokens", "conflicting_output_limits")
        tools = value.get("tools", [])
        names = [tool["function"]["name"] for tool in tools]
        if len(names) != len(set(names)):
            invalid("tools", "duplicate_names")
        for tool in tools:
            # Ollama has no strict-schema generation guarantee.
            if tool["function"].get("strict"):
                raise ApiError(400, "UNSUPPORTED_PARAMETER", {"field": "tools.function.strict"})
            try:
                Draft202012Validator.check_schema(tool["function"]["parameters"])
            except Exception:
                invalid("tools.function.parameters")
        choice = value.get("tool_choice", "auto")
        if isinstance(choice, dict):
            name = choice["function"]["name"]
            if name not in names:
                invalid("tool_choice", "unknown_tool")
            tools = [tool for tool in tools if tool["function"]["name"] == name]
            choice = "required"
        if choice == "required" and not tools:
            invalid("tool_choice", "tools_required")
        if choice == "none":
            tools = []

        messages, images, pending, used = [], [], {}, set()
        for mi, original in enumerate(value["messages"]):
            role = original["role"]
            if "name" in original:
                raise ApiError(400, "UNSUPPORTED_PARAMETER", {"field": f"messages.{mi}.name"})
            if "reasoning_content" in original and role != "assistant":
                invalid("messages.reasoning_content", "assistant_only")
            if pending and role != "tool":
                invalid("messages", "missing_tool_results")
            if "tool_calls" in original and role != "assistant":
                invalid("messages.tool_calls")
            if "tool_call_id" in original and role != "tool":
                invalid("messages.tool_call_id")
            content = original.get("content")
            if content is None and not (role == "assistant" and (original.get("tool_calls") or "reasoning_content" in original)):
                invalid("messages.content")
            parts = ([{"type": "text", "text": content}] if isinstance(content, str) else content or [])
            normalized = []
            for pi, part in enumerate(parts):
                if part["type"] == "text":
                    normalized.append(dict(part))
                    continue
                if role != "user":
                    invalid("messages.content", "images_require_user")
                if part["image_url"].get("detail", "auto") != "auto":
                    raise ApiError(400, "UNSUPPORTED_PARAMETER", {"field": "messages.content.image_url.detail"})
                url = part["image_url"]["url"]
                prefix, separator, encoded = url.partition(",")
                if not separator or prefix not in ("data:image/png;base64", "data:image/jpeg;base64"):
                    invalid("messages.content.image_url", "base64_png_jpeg_required")
                if len(images) >= 4:
                    raise ApiError(422, "IMAGE_LIMIT_EXCEEDED")
                image_part = {"type": "image", "mediaType": prefix[5:-7], "dataBase64": encoded}
                image = decode_image(image_part, f"{mi}:{pi}")
                images.append(image)
                image_part["dataBase64"] = base64.b64encode(image.data).decode("ascii")
                normalized.append(image_part)
            if any(p["type"] == "image" for p in normalized) and any(
                    "[img]" in p["text"] for p in normalized if p["type"] == "text"):
                invalid("messages.content", "reserved_image_marker")
            message = {"role": "system" if role == "developer" else role, "content": normalized}
            if "reasoning_content" in original:
                message["reasoning_content"] = original["reasoning_content"]
            if role == "tool":
                call_id = original.get("tool_call_id")
                if call_id not in pending:
                    invalid("messages.tool_call_id", "unmatched_tool_result")
                message.update(tool_call_id=call_id, tool_name=pending.pop(call_id))
            if original.get("tool_calls"):
                calls = []
                for call in original["tool_calls"]:
                    if call["id"] in used:
                        invalid("messages.tool_calls", "duplicate_call_id")
                    arguments = strict_json(call["function"]["arguments"].encode("utf-8"))
                    if not isinstance(arguments, dict):
                        invalid("messages.tool_calls", "arguments_require_object")
                    calls.append({"id": call["id"], "type": "function", "function": {
                        "name": call["function"]["name"], "arguments": arguments}})
                    pending[call["id"]] = call["function"]["name"]
                    used.add(call["id"])
                message["tool_calls"] = calls
            messages.append(message)
        if pending or not any(m["role"] == "user" for m in messages):
            invalid("messages", "incomplete_conversation")
        text_bytes = sum(len(p["text"].encode("utf-8")) for m in messages for p in m["content"] if p["type"] == "text")
        text_bytes += len(canonical(tools).encode("utf-8"))
        text_bytes += sum(len(canonical(m.get("tool_calls", [])).encode("utf-8")) for m in messages)
        text_bytes += sum(len(m.get("reasoning_content", "").encode("utf-8")) for m in messages)
        if text_bytes > MAX_TEXT_UTF8_BYTES:
            raise ApiError(413, "REQUEST_TOO_LARGE")
        options = {key: value[key] for key in ("temperature", "top_p", "top_k", "seed", "stop",
                                               "presence_penalty", "repeat_penalty") if key in value}
        if isinstance(options.get("stop"), str):
            options["stop"] = [options["stop"]]
        output_limit = value.get("max_completion_tokens", value.get("max_tokens"))
        if output_limit is not None:
            options["num_predict"] = output_limit
        normalized = {"clientRequestId": str(uuid4()), "model": value["model"], "messages": messages,
                      "protocol": "openai-chat-v1", "tools": tools, "tool_choice": choice,
                      "parallel_tool_calls": value.get("parallel_tool_calls", True), "options": options}
        if "think" in value:
            normalized["think"] = value["think"]
        raw_task = wire_json(normalized)
        return (value, normalized, hashlib.sha256(raw_task.encode()).hexdigest(),
                TaskInput(raw_task, tuple(images)))


def data_event(value):
    return ("data: " + canonical(value) + "\n\n").encode("utf-8")


def completion(row):
    output = json.loads(row["output_json"]) if row["output_json"] else {}
    metrics = json.loads(row["metrics_json"]) if row["metrics_json"] else {}
    if row["error_code"] == "OUTPUT_TRUNCATED":
        effective = json.loads(row["effective_settings_json"])
        cfg = json.loads(effective["profile"]["snapshot_json"]).get("config", {})
        # Real jobs freeze the per-request budget; Fake/legacy jobs may not.
        prepared = json.loads(effective["prepared"]["payload_json"])
        options = json.loads(prepared.get("options_json", "{}"))
        limit = options.get("num_predict", cfg.get("MaxOutputTokens", cfg.get("NumPredict")))
        raise ApiError(422, "OUTPUT_TRUNCATED", {"maxOutputTokens": limit})
    if row["status"] != "succeeded":
        raise ApiError(503 if row["error_code"] in ("MODEL_UNAVAILABLE", "SERVICE_STOPPED") else 500,
                       row["error_code"] or "GENERATION_CANCELLED")
    reason = metrics.get("finishReason", "stop")
    message = {"role": "assistant", "content": output.get("text") or None}
    if "reasoningText" in output:
        message["reasoning_content"] = output["reasoningText"]
    if output.get("toolCalls") and reason != "length":
        message["tool_calls"] = output["toolCalls"]
        reason = "tool_calls"
    input_tokens, output_tokens = metrics.get("inputTokens"), metrics.get("outputTokens")
    # Never substitute admission estimates for actual engine usage.
    usage = None if input_tokens is None or output_tokens is None else {
        "prompt_tokens": input_tokens, "completion_tokens": output_tokens, "total_tokens": input_tokens + output_tokens}
    return {"id": "chatcmpl-" + row["job_id"], "object": "chat.completion",
            "created": row["created_at_ms"] // 1000, "model": json.loads(row["request_json"])["model"],
            "choices": [{"index": 0, "message": message, "finish_reason": reason}], "usage": usage}


def register_openai(app, settings, scheduler, store, user):
    validator = ChatValidator(settings.contract_directory)

    @app.get("/v1/models")
    async def models(user_id=Depends(user)):
        return {"object": "list", "data": [{"id": model_description(p, settings)["id"], "object": "model",
                "created": 0, "owned_by": "aiserver"} for p in scheduler.profiles.values()]}

    @app.post("/v1/chat/completions")
    async def chat(request: Request, user_id=Depends(user)):
        body = await read_json_body(request, settings.max_request_bytes, settings.upload_timeout_seconds)
        value, normalized, fingerprint, task = await asyncio.to_thread(validator.validate, body)
        try:
            row, _ = await scheduler.submit(user_id, normalized, fingerprint, task, request.state.received_at_ms)
        except ApiError as error:
            if error.code == "OUTPUT_LIMIT_EXCEEDED":
                error.details["field"] = "max_completion_tokens" if "max_completion_tokens" in value else "max_tokens"
            raise
        job = row["job_id"]

        async def close():
            try:
                await scheduler.cancel(user_id, job, "client_disconnected")
            except (ApiError, sqlite3.Error):
                scheduler.fatal, scheduler.state = True, "paused"

        async def current():
            try:
                return await scheduler.db(store.get, user_id, job)
            except sqlite3.Error:
                scheduler.fatal, scheduler.state = True, "paused"
                raise ApiError(503, "STORAGE_UNAVAILABLE") from None

        async def complete():
            while True:
                result = await current()
                if result["status"] in TERMINAL:
                    try:
                        return JSONResponse(completion(result))
                    except ApiError as error:
                        # Keep the job/result link on errors as on successful replies.
                        return JSONResponse(error_body(error), status_code=error_status(error))
                await asyncio.sleep(0.05)

        async def events():
            base = {"id": "chatcmpl-" + job, "object": "chat.completion.chunk", "created": row["created_at_ms"] // 1000,
                    "model": normalized["model"]}
            def chunk(delta, reason=None):
                return {**base, "choices": [{"index": 0, "delta": delta, "finish_reason": reason}]}
            yield data_event(chunk({"role": "assistant", "content": ""}))
            sequence, heartbeat = 0, time.monotonic()
            try:
                while True:
                    result = await current()
                    chunks = await scheduler.db(store.chunks, job, sequence)
                    for part in chunks:
                        sequence = part["sequence"]
                        yield data_event(chunk({part["channel"]: part["text"]}))
                    if result["status"] in TERMINAL:
                        if len(chunks) == 64:
                            continue
                        final = completion(result)
                        message = final["choices"][0]["message"]
                        # Only publish complete, committed calls after the generation succeeds.
                        for index, call in enumerate(message.get("tool_calls", [])):
                            yield data_event(chunk({"tool_calls": [{"index": index, **call}]}))
                        yield data_event(chunk({}, final["choices"][0]["finish_reason"]))
                        if value.get("stream_options", {}).get("include_usage"):
                            yield data_event({**base, "choices": [], "usage": final["usage"]})
                        yield b"data: [DONE]\n\n"
                        return
                    if time.monotonic() - heartbeat >= settings.stream_heartbeat_seconds:
                        yield b": keep-alive\n\n"
                        heartbeat = time.monotonic()
                    await asyncio.sleep(0.05)
            except (ApiError, sqlite3.Error) as error:
                if isinstance(error, sqlite3.Error):
                    scheduler.fatal, scheduler.state = True, "paused"
                    error = ApiError(503, "STORAGE_UNAVAILABLE")
                yield data_event(stream_error_body(error))
                # No successful finish or [DONE] after an error.

        headers = {"X-Job-Id": job, "X-Accel-Buffering": "no"}
        if value.get("stream", False):
            return InferenceStream(events(), close, settings.stream_send_timeout_seconds, headers=headers)
        return InferenceJSON(complete, close, settings.stream_send_timeout_seconds, headers=headers)
