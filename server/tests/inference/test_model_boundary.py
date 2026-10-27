"""Model boundary checks using the current messages contract and mock engine."""
from pathlib import Path
from io import BytesIO
from PIL import Image
import asyncio
import base64
import json
import sys
import unittest

import httpx

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "server/src"))
from aiserver.api.errors import ApiError
from aiserver.api.validation import RequestValidator, canonical
from aiserver.context.prompt_builder import prepare_messages
from aiserver.context.input_budget import check_budget
from aiserver.inference.ollama import OllamaAdapter, ModelCallError


class ModelBoundaryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.cfg = json.loads((ROOT / "config/inference.development.example.json").read_text(encoding="utf-8"))
        image = BytesIO()
        Image.new("RGB", (32, 32), "white").save(image, format="PNG")
        self.code = "\r\n// 原代码\n"
        self.request = {"clientRequestId": "416417b5-e936-4cde-b0df-969281936318",
                        "model": self.cfg["Model"], "messages": [
            {"role": "system", "content": [{"type": "text", "text": "SDK"}]},
            {"role": "assistant", "content": [{"type": "text", "text": "资料"}]},
            {"role": "user", "content": [{"type": "text", "text": self.code},
                {"type": "image", "mediaType": "image/png",
                 "dataBase64": base64.b64encode(image.getvalue()).decode("ascii")}]}]}
        self.validator = RequestValidator(ROOT / "contracts/api/v1")
        normalized, _, task = self.validator.validate(canonical(self.request).encode("utf-8"))
        self.prepared = prepare_messages(normalized, task.images, self.cfg["ImagePolicy"])

    def test_materials_preserved_and_estimate_never_becomes_actual_count(self):
        messages = self.prepared.messages()
        self.assertEqual([message["role"] for message in messages], ["system", "assistant", "user"])
        self.assertEqual(messages[-1]["content"], self.code + "[img]")
        self.assertEqual(len(messages[-1]["images"]), 1)
        self.cfg["Budget"]["Verified"] = True
        result = check_budget(self.prepared, self.cfg)
        self.assertIsNone(result.input_tokens)
        self.assertGreater(result.estimated_input_tokens, 0)
        self.assertEqual(result.method, "conservative-estimate-v1")
        self.cfg.update(NumCtx=2048, NumPredict=1535, ReservedTokens=512)
        self.assertEqual(check_budget(self.prepared, self.cfg).code, "CONTEXT_TOO_LARGE")

    def test_corrupt_image_is_rejected(self):
        self.request["messages"][-1]["content"][-1]["dataBase64"] = base64.b64encode(b"not-png").decode("ascii")
        with self.assertRaises(ApiError) as raised:
            self.validator.validate(canonical(self.request).encode("utf-8"))
        self.assertEqual(raised.exception.code, "IMAGE_INVALID")

    async def test_length_termination_is_not_complete(self):
        adapter = OllamaAdapter(self.cfg)
        await adapter.client.aclose()
        adapter.client = httpx.AsyncClient(base_url=self.cfg["BaseUrl"], transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"model": self.cfg["Model"], "done": True,
                "done_reason": "length", "eval_count": 1024, "message": {"content": "unfinished code"}})))
        try:
            result = await adapter.chat(self.prepared)
            self.assertFalse(result.is_complete)
        finally:
            await adapter.aclose()

    async def test_oversized_response_is_rejected(self):
        self.cfg["MaxResponseBytes"] = 32
        adapter = OllamaAdapter(self.cfg)
        await adapter.client.aclose()
        adapter.client = httpx.AsyncClient(base_url=self.cfg["BaseUrl"], transport=httpx.MockTransport(
            lambda request: httpx.Response(200, content=b"x" * 33)))
        try:
            with self.assertRaises(ModelCallError) as raised:
                await adapter.chat(self.prepared)
            self.assertEqual(raised.exception.code, "MODEL_PROTOCOL_ERROR")
        finally:
            await adapter.aclose()

    async def test_cancelling_task_closes_mock_stream(self):
        started, closed = asyncio.Event(), asyncio.Event()

        class BlockingStream(httpx.AsyncByteStream):
            async def __aiter__(self):
                started.set()
                await asyncio.Event().wait()
                yield b""

            async def aclose(self):
                closed.set()

        adapter = OllamaAdapter(self.cfg)
        await adapter.client.aclose()
        adapter.client = httpx.AsyncClient(base_url=self.cfg["BaseUrl"], transport=httpx.MockTransport(
            lambda request: httpx.Response(200, stream=BlockingStream())))
        try:
            task = asyncio.create_task(adapter.chat(self.prepared))
            await asyncio.wait_for(started.wait(), 2)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertTrue(closed.is_set())
        finally:
            await adapter.aclose()


if __name__ == "__main__":
    unittest.main()
