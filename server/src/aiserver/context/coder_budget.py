"""Count the pinned tokenizer over Ollama's ACTUAL rendered prompt, without generation.

The pinned engine's render-only path uses no completion slot. It includes its own
system/template/tool handling, avoiding a second, subtly different template port.
These admission counts are never substituted for engine usage metrics.
"""
import asyncio
import hashlib
import json
import time
from pathlib import Path
import httpx

from aiserver.inference.ollama import ModelCallError, engine_error
from aiserver.inference.configuration import chat_payload
from aiserver.inference.diagnostics import record

METHOD = "ollama-render-tokenizer-v1"


class CoderCounter:
    def __init__(self, cfg, root):
        from tokenizers import Tokenizer
        budget = cfg["Budget"]
        path = (Path(root) / budget["TokenizerPath"]).resolve()
        if not path.is_file():
            raise ValueError("TOKENIZER_MISSING_OR_INVALID")
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != budget["TokenizerSha256"]:
            raise ValueError("TOKENIZER_MISSING_OR_INVALID: tokenizer identity mismatch")
        self.tokenizer = Tokenizer.from_str(data.decode("utf-8"))
        self.cfg = cfg

    async def count(self, prepared):
        cfg = self.cfg
        started = time.monotonic()
        record("render_count", model=cfg["Model"], context=cfg["NumCtx"], kv=cfg.get("KvCacheType", "f16"),
               flash=cfg.get("FlashAttention"), batch=cfg.get("NumBatch"), shift=False)
        payload = chat_payload(cfg, prepared, stream=False)
        payload["_debug_render_only"] = True
        try:
            async with httpx.AsyncClient(base_url=cfg["BaseUrl"], trust_env=False, follow_redirects=False,
                                          timeout=httpx.Timeout(cfg["GenerateTimeoutSeconds"],
                                                                connect=cfg["ConnectTimeoutSeconds"])) as client:
                async with client.stream("POST", "/api/chat", json=payload) as response:
                    raw = bytearray()
                    async for chunk in response.aiter_bytes():
                        raw.extend(chunk)
                        if len(raw) > 16 * 1024 * 1024:
                            raise ModelCallError("MODEL_PROTOCOL_ERROR")
                    if response.status_code != 200:
                        code = engine_error(raw.decode("utf-8", "replace"))
                        raise ModelCallError(code if code == "CONTEXT_TOO_LARGE" else "MODEL_UNAVAILABLE")
            value = json.loads(raw)
            prompt = value["_debug_info"]["rendered_template"]
            if value.get("model") != cfg["Model"] or not isinstance(prompt, str) or not prompt:
                raise ValueError("Invalid render-only response")
            encoded = await asyncio.to_thread(self.tokenizer.encode, prompt, add_special_tokens=False)
            record("render_count_result", model=cfg["Model"], counted_tokens=len(encoded.ids),
                   elapsed_ms=round((time.monotonic()-started)*1000, 2))
            return len(encoded.ids)
        except ModelCallError:
            raise
        except (httpx.HTTPError, TimeoutError):
            raise ModelCallError("MODEL_UNAVAILABLE") from None
        except (KeyError, TypeError, ValueError):
            raise ModelCallError("MODEL_PROTOCOL_ERROR") from None
