"""GPU admission and 14B preparation boundaries; no real model or target GPU."""
import asyncio
import importlib.util
import json

import httpx
import pytest

from aiserver.context.prompt_builder import prepare_messages
from aiserver.inference.configuration import runtime_options, validate_options
from aiserver.inference.gpu import require_gpu_status
from aiserver.inference.ollama import OllamaAdapter, ModelCallError
from aiserver.operations.diagnostics import diagnostic
from aiserver.settings import ROOT


def config():
    return json.loads((ROOT / "config/inference.coder14b.example.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("updates", [{"size_vram": 999}, {"size_vram": 1001}, {"context_length": 32768},
                                     {"size": 0}, {"size_vram": None}, {"context_length": True},
                                     {"name": "other-model"}])
def test_gpu_report_rejects_partial_unknown_or_reduced_context(updates):
    cfg = config()
    row = {"name": cfg["Model"], "size": 1000, "size_vram": 1000, "context_length": 81920, **updates}
    with pytest.raises(ValueError, match="FULL_GPU_REQUIRED"):
        require_gpu_status(cfg, {"models": [row]})


@pytest.mark.parametrize("full", [True, False])
def test_gpu_check_precedes_user_generation_and_requests_all_layers(full):
    async def run():
        cfg = config()
        events = []
        def handler(request):
            events.append(request.url.path)
            body = json.loads(request.content) if request.content else None
            if request.url.path == "/api/generate":
                assert body["prompt"] == "" and body["options"] == runtime_options(cfg)
                return httpx.Response(200, json={"done": True})
            if request.url.path == "/api/ps":
                return httpx.Response(200, json={"models": [{"name": cfg["Model"], "size": 1000,
                    "size_vram": 1000 if full else 900, "context_length": 81920}]})
            assert request.url.path == "/api/chat" and full
            assert body["options"]["num_gpu"] == 999
            assert body["options"]["num_predict"] == 128
            return httpx.Response(200, json={"model": cfg["Model"], "done": True, "done_reason": "stop",
                                             "message": {"content": "OK"}})
        adapter = OllamaAdapter(cfg)
        await adapter.client.aclose()
        adapter.client = httpx.AsyncClient(base_url=cfg["BaseUrl"], transport=httpx.MockTransport(handler))
        prompt = prepare_messages({"protocol": "openai-chat-v1", "messages": [{"role": "user", "content": [
            {"type": "text", "text": "private source"}]}], "options": {"num_gpu": 0, "num_predict": 128}}, (), cfg["ImagePolicy"])
        try:
            if full:
                assert (await adapter.chat(prompt)).text == "OK"
            else:
                with pytest.raises(ModelCallError, match="MODEL_UNAVAILABLE"):
                    await adapter.chat(prompt)
                assert events == ["/api/ps", "/api/generate", "/api/ps"]
        finally:
            await adapter.aclose()
    asyncio.run(run())


def test_gpu_option_validation_and_startup_diagnostic():
    cfg = config()
    with pytest.raises(ValueError, match="RequireFullGpu"):
        validate_options({**cfg, "RequireFullGpu": "true"})
    assert diagnostic(RuntimeError("FULL_GPU_REQUIRED"), "engine")["code"] == "FULL_GPU_REQUIRED"


def test_synthetic_input_construction_and_retrieval_check():
    spec = importlib.util.spec_from_file_location("capacity_check_test", ROOT / "scripts/validation/Check-CoderCapacity.py")
    checker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(checker)
    class Counter:
        async def count(self, prompt):
            return len(prompt.messages()[0]["content"])
    prompt, count, expected = asyncio.run(checker.target_prompt(config(), Counter(), 4096))
    assert 4032 <= count <= 4096
    assert all(prompt.messages()[0]["content"].count(value) == 1 for value in expected.values())
    assert checker.retrieved("```json\n" + json.dumps(expected) + "\n```", expected)
    assert not checker.retrieved(json.dumps({**expected, "CapacityMiddle": "wrong"}), expected)
