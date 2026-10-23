"""Small B-module checks; these do not certify real engine cancellation."""
from pathlib import Path
import asyncio
import json
import sys
import unittest

import httpx

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "server/src"))
from aiserver.context.prompt_builder import prepare_prompt
from aiserver.context.input_budget import check_budget
from aiserver.inference.ollama import OllamaAdapter, ModelCallError


class ModelBoundaryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.cfg = json.loads((ROOT / "config/inference.development.example.json").read_text(encoding="utf-8"))
        self.request = {"operation": "modify", "instruction": "保留完整材料", "currentCode": "\r\n// 原代码\n",
                        "context": {"sdkReferenceText": "SDK"}, "history": [{"role": "assistant", "content": "资料"}]}
        self.prepared = prepare_prompt(self.request, ROOT / "config/prompts/s01-bootstrap-vl-v1.txt",
                                       ((ROOT / "eval/cases/gap-threshold-v1/flow.png").read_bytes(),), self.cfg["ImagePolicy"])

    def test_materials_are_preserved_and_unknown_budget_refuses(self):
        messages = self.prepared.messages()
        self.assertEqual(json.loads(messages[1]["content"])["currentCode"], self.request["currentCode"])
        self.assertEqual(len(messages[1]["images"]), 1)
        self.cfg["Budget"]["Verified"] = True
        self.assertEqual(check_budget(self.prepared, self.cfg).code, "INPUT_BUDGET_UNVERIFIED")

    def test_corrupt_image_is_rejected(self):
        with self.assertRaises(Exception):
            prepare_prompt(self.request, ROOT / "config/prompts/s01-bootstrap-vl-v1.txt", (b"not-png",), self.cfg["ImagePolicy"])

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
