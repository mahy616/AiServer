"""Bounded native Ollama NDJSON transport. No public API or task scheduling."""
import asyncio
from dataclasses import dataclass
import json
import math
from urllib.parse import urlsplit
from uuid import uuid4

import httpx

from aiserver.context.prompt_builder import PreparedPrompt
from aiserver.inference.configuration import runtime_options, chat_payload
from aiserver.inference.gpu import require_gpu_status
from aiserver.inference.diagnostics import record


class ModelCallError(Exception):
    def __init__(self, code: str, request_finished: bool = False, reason: str = ""):
        super().__init__(code)
        self.code, self.request_finished = code, request_finished
        self.reason = reason


@dataclass(frozen=True)
class Generation:
    text: str
    is_complete: bool
    finish_reason: str | None
    metrics: dict
    raw: dict
    tool_calls: tuple[dict, ...] = ()
    reasoning_text: str = ""


def engine_error(message):
    message = str(message).lower()
    return "CONTEXT_TOO_LARGE" if any(word in message for word in (
        "context length", "context window", "context_length_exceeded", "n_ctx", "prompt is too long",
        "input is too long", "input too long", "prompt too long", "input length exceeds",
        "too many tokens", "exceeds the available context size", "exceeds context size")) else "MODEL_PROTOCOL_ERROR"


class OllamaAdapter:
    def __init__(self, profile: dict):
        parsed = urlsplit(profile["BaseUrl"])
        if (parsed.scheme != "http" or parsed.hostname != "127.0.0.1"
                or parsed.username or parsed.password or parsed.query or parsed.fragment
                or parsed.path not in ("", "/")):
            raise ValueError("Only local loopback HTTP is supported")
        self.profile = dict(profile)
        self.client = httpx.AsyncClient(base_url=profile["BaseUrl"], trust_env=False,
                                        follow_redirects=False,
                                        timeout=httpx.Timeout(profile["GenerateTimeoutSeconds"],
                                                              connect=profile["ConnectTimeoutSeconds"]))

    async def aclose(self):
        await self.client.aclose()

    async def ensure_full_gpu(self):
        cfg = self.profile
        if not cfg.get("RequireFullGpu"):
            return None
        # Empty prompt loads the requested window without evaluating user text.
        try:
            # A resident runner needs verification, not a second load with
            # different defaults. In particular /generate defaults shift=true.
            response = await self.client.get("/api/ps", timeout=10)
            response.raise_for_status()
            try:
                return require_gpu_status(cfg, response.json())
            except ValueError:
                pass
            record("load", model=cfg["Model"], reason="resident_model_or_window_missing",
                   context=cfg["NumCtx"], kv=cfg.get("KvCacheType", "f16"),
                   flash=cfg.get("FlashAttention"), shift=False, batch=cfg.get("NumBatch"))
            loaded = await self.client.post("/api/generate", json={
                "model": cfg["Model"], "prompt": "", "stream": False,
                "truncate": False, "shift": False,
                "keep_alive": cfg["KeepAlive"], "options": runtime_options(cfg)})
            loaded.raise_for_status()
            body = loaded.json()
            if body.get("done") is not True or body.get("error"):
                raise ValueError("FULL_GPU_REQUIRED")
            response = await self.client.get("/api/ps", timeout=10)
            response.raise_for_status()
            return require_gpu_status(cfg, response.json())
        except (httpx.HTTPError, ValueError, TypeError, AttributeError):
            raise RuntimeError("FULL_GPU_REQUIRED") from None

    async def chat(self, prepared: PreparedPrompt, on_delta=None) -> Generation:
        cfg = self.profile
        try:
            await self.ensure_full_gpu()
        except RuntimeError as error:
            raise ModelCallError("MODEL_UNAVAILABLE", request_finished=True) from error
        payload = chat_payload(cfg, prepared, stream=True)
        tools = payload.get("tools", [])
        parts, total, pending = [], 0, bytearray()
        reasoning_parts = []
        calls = []
        allowed = {tool["function"]["name"] for tool in tools}
        thinking_characters, native_call_count, tool_error = 0, 0, None

        def protocol(reason, finished=False):
            record("response_validation", model=cfg["Model"], code="MODEL_PROTOCOL_ERROR", reason=reason,
                   request_finished=finished, response_bytes=total)
            return ModelCallError("MODEL_PROTOCOL_ERROR", finished, reason)

        async def consume(line):
            nonlocal thinking_characters, native_call_count, tool_error
            data = json.loads(line)
            if not isinstance(data, dict):
                raise protocol("ndjson_not_object")
            if "error" in data:
                code = engine_error(data["error"])
                record("generation", model=cfg["Model"], code=code, reason="engine_error_frame")
                raise ModelCallError(code, request_finished=code == "CONTEXT_TOO_LARGE", reason="engine_error_frame")
            if data.get("model") != cfg["Model"] or type(data.get("done")) is not bool:
                raise protocol("model_or_done_invalid")
            message = data.get("message", {})
            if not isinstance(message, dict) or not isinstance(message.get("content", ""), str):
                raise protocol("message_or_content_invalid")
            if "thinking" in message and not isinstance(message["thinking"], str):
                raise protocol("thinking_not_string")
            reasoning = message.get("thinking", "")
            thinking_characters += len(reasoning)
            if reasoning:
                if on_delta is not None:
                    await on_delta(reasoning, "reasoning_content")
                reasoning_parts.append(reasoning)
            delta = message.get("content", "")
            if delta:
                if on_delta is not None:
                    await on_delta(delta)
                parts.append(delta)
            native_calls = message.get("tool_calls", [])
            if not isinstance(native_calls, list):
                tool_error = tool_error or "tool_calls_not_array"
                native_calls = []
            for call in native_calls:
                native_call_count += 1
                function = call.get("function", {}) if isinstance(call, dict) else {}
                if (not isinstance(function, dict) or not isinstance(function.get("name"), str)
                        or function.get("name") not in allowed):
                    tool_error = tool_error or "tool_name_not_allowed"
                    continue
                if not isinstance(function.get("arguments"), dict):
                    tool_error = tool_error or "tool_arguments_not_object"
                    continue
                if len(calls) >= 128:
                    tool_error = tool_error or "too_many_tool_calls"
                    continue
                if not prepared.parallel_tool_calls and calls:
                    tool_error = tool_error or "parallel_tool_calls_disallowed"
                    continue
                try:
                    arguments = json.dumps(function["arguments"], ensure_ascii=False, separators=(",", ":"), allow_nan=False)
                except ValueError:
                    tool_error = tool_error or "tool_arguments_nonfinite"
                    continue
                calls.append({"id": "call_" + uuid4().hex, "type": "function", "function": {
                    "name": function["name"], "arguments": arguments}})
            return data if data["done"] else None

        try:
            async with asyncio.timeout(cfg["GenerateTimeoutSeconds"]):
                async with self.client.stream("POST", "/api/chat", json=payload) as response:
                    if response.status_code != 200:
                        error_body = bytearray()
                        async for chunk in response.aiter_bytes():
                            error_body.extend(chunk[:8192-len(error_body)])
                            if len(error_body) >= 8192:
                                break
                        code = engine_error(error_body.decode("utf-8", "replace"))
                        record("generation", model=cfg["Model"], http_status=response.status_code,
                               code=code if code == "CONTEXT_TOO_LARGE" else "ENGINE_HTTP_ERROR",
                               reason="engine_http_rejection")
                        if response.status_code == 413 or code == "CONTEXT_TOO_LARGE":
                            raise ModelCallError("CONTEXT_TOO_LARGE", request_finished=True)
                        raise ModelCallError({404: "MODEL_CONFIG_ERROR", 400: "MODEL_CONFIG_ERROR",
                                              503: "MODEL_BUSY"}.get(response.status_code, "MODEL_UNAVAILABLE"))
                    final = None
                    async for chunk in response.aiter_bytes():
                        total += len(chunk)
                        if total > cfg.get("MaxResponseBytes", 1048576):
                            raise protocol("response_size_limit")
                        pending.extend(chunk)
                        while b"\n" in pending:
                            line, _, rest = pending.partition(b"\n")
                            pending = bytearray(rest)
                            if line.strip():
                                final = await consume(line)
                                if final is not None:
                                    break
                        if final is not None:
                            break
                    if final is None and pending.strip():
                        final = await consume(pending)
                    if final is None:
                        raise protocol("missing_final_frame")
        except (TimeoutError, httpx.TimeoutException) as error:
            raise ModelCallError("EXECUTION_TIMEOUT") from error
        except httpx.HTTPError as error:
            raise ModelCallError("MODEL_UNAVAILABLE") from error
        except (ValueError, UnicodeError) as error:
            raise protocol("invalid_json_or_encoding") from error
        text = "".join(parts)
        record("generation_result", model=cfg["Model"], response_bytes=total,
               content_characters=len(text), thinking_characters=thinking_characters,
               native_tool_calls=native_call_count, accepted_tool_calls=len(calls),
               accepted_tool_names=[c["function"]["name"] for c in calls], requested_think=prepared.think,
               done_reason=final.get("done_reason") if final.get("done_reason") in ("stop", "length") else "invalid",
               **{key: final[key] for key in ("prompt_eval_count", "eval_count", "total_duration",
                  "load_duration", "prompt_eval_duration", "eval_duration")
                  if type(final.get(key)) in (int, float) and math.isfinite(final[key]) and final[key] >= 0})
        # Drain through the final frame before rejecting semantic tool errors.
        # The runner has finished; restarting the whole engine is unnecessary.
        if tool_error:
            raise protocol(tool_error, True)
        reason = final.get("done_reason")
        if reason not in ("stop", "length"):
            raise protocol("done_reason_invalid", True)
        if reason == "stop" and prepared.tool_choice == "required" and not calls:
            raise ModelCallError("TOOL_CHOICE_NOT_SATISFIED", request_finished=True)
        metrics = {key: final.get(key) for key in ("model", "total_duration", "load_duration",
                   "prompt_eval_count", "prompt_eval_duration", "eval_count", "eval_duration")}
        for key in ("prompt_eval_count", "eval_count"):
            value = metrics[key]
            if value is not None and (type(value) is not int or value < 0):
                raise protocol("usage_count_invalid", True)
        return Generation(text, reason == "stop", "tool_calls" if calls and reason == "stop" else reason,
                          metrics, final, tuple(calls), "".join(reasoning_parts))
