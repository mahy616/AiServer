"""Internal local-engine transport, reused by experiments and the future API.

No task queue, public HTTP routes, model downloads, or process management here.
The caller owns readiness/budget checks and the execution slot. Async task
cancellation closes the response; it does not certify engine/GPU termination.
"""
import asyncio
from dataclasses import dataclass
import json
from urllib.parse import urlsplit

import httpx

from aiserver.context.prompt_builder import PreparedPrompt


class ModelCallError(Exception):
    def __init__(self, code: str, request_finished: bool = False):
        super().__init__(code)
        self.code = code
        self.request_finished = request_finished


@dataclass(frozen=True)
class Generation:
    text: str
    is_complete: bool
    finish_reason: str | None
    metrics: dict
    raw: dict


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

    async def chat(self, prepared: PreparedPrompt) -> Generation:
        cfg = self.profile
        payload = {"model": cfg["Model"], "messages": prepared.messages(), "stream": False,
                   "truncate": False, "shift": False,
                   "keep_alive": cfg["KeepAlive"],
                   "options": {"num_ctx": cfg["NumCtx"], "num_predict": cfg["NumPredict"],
                               "temperature": cfg["Temperature"], "top_p": cfg["TopP"],
                               "top_k": cfg["TopK"], "seed": cfg["Seed"]}}
        try:
            async with asyncio.timeout(cfg["GenerateTimeoutSeconds"]):
                async with self.client.stream("POST", "/api/chat", json=payload) as response:
                    if response.status_code != 200:
                        error_body = bytearray()
                        async for chunk in response.aiter_bytes():
                            error_body.extend(chunk)
                            if len(error_body) > 8192:
                                break
                        message = error_body.decode("utf-8", "replace").lower()
                        if response.status_code == 413 or (response.status_code == 400 and any(
                                word in message for word in ("context length", "context window", "too long", "too large"))):
                            raise ModelCallError("CONTEXT_TOO_LARGE", request_finished=True)
                        code = {404: "MODEL_CONFIG_ERROR", 400: "MODEL_CONFIG_ERROR",
                                503: "MODEL_BUSY"}.get(response.status_code, "MODEL_UNAVAILABLE")
                        raise ModelCallError(code)
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > cfg.get("MaxResponseBytes", 1048576):
                            raise ModelCallError("MODEL_PROTOCOL_ERROR")
            data = json.loads(body)
        except (TimeoutError, httpx.TimeoutException) as error:
            raise ModelCallError("EXECUTION_TIMEOUT") from error
        except httpx.HTTPError as error:
            raise ModelCallError("MODEL_UNAVAILABLE") from error
        except (ValueError, UnicodeError) as error:
            raise ModelCallError("MODEL_PROTOCOL_ERROR") from error
        if isinstance(data, dict) and "error" in data:
            message = str(data["error"]).lower()
            if any(word in message for word in ("context length", "context window", "too long", "too large")):
                raise ModelCallError("CONTEXT_TOO_LARGE", request_finished=True)
            raise ModelCallError("MODEL_PROTOCOL_ERROR")
        if not isinstance(data, dict) or data.get("done") is not True or data.get("model") != cfg["Model"]:
            raise ModelCallError("MODEL_PROTOCOL_ERROR")
        message = data.get("message")
        text = message.get("content") if isinstance(message, dict) else None
        if not isinstance(text, str) or not text.strip():
            raise ModelCallError("MODEL_EMPTY_OUTPUT", request_finished=True)
        reason = data.get("done_reason")
        count = data.get("eval_count")
        complete = reason == "stop" and isinstance(count, int)
        if reason == "length" or (reason is None and isinstance(count, int) and count >= cfg["NumPredict"]):
            complete = False
        metrics = {key: data.get(key) for key in ("model", "total_duration", "load_duration",
                   "prompt_eval_count", "prompt_eval_duration", "eval_count", "eval_duration")}
        return Generation(text, complete, reason, metrics, data)
