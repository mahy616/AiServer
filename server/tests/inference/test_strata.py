"""Strata protocol and ownership checks. Synthetic data only; no GPU or model downloads."""
import asyncio
from dataclasses import asdict
import json
from unittest.mock import AsyncMock

import httpx
import pytest

from aiserver.context.prompt_builder import prepare_messages
from aiserver.context.window_budget import freeze_output
from aiserver.inference.contracts import ModelError, TaskInput
from aiserver.inference.strata import StrataAdapter, StrataBudget, StrataProvider, chat_payload
from aiserver.settings import ROOT, Settings


def config():
    return json.loads((ROOT / "config/inference.strata.example.json").read_text(encoding="utf-8"))


def prompt(cfg, *, thinking=True):
    request = {"protocol": "openai-chat-v1", "messages": [
        {"role": "user", "content": [{"type": "text", "text": "读取中文"}]}],
        "tools": [{"type": "function", "function": {"name": "read", "parameters": {"type": "object"}}}],
        "think": thinking}
    return freeze_output(prepare_messages(request, (), cfg["ImagePolicy"], provider="Strata"), cfg, supports_thinking=True)


class Chunks(httpx.AsyncByteStream):
    def __init__(self, body):
        self.body = body

    async def __aiter__(self):
        # Split JSON, UTF-8 and SSE delimiters across arbitrary byte boundaries.
        for i in range(0, len(self.body), 7):
            yield self.body[i:i+7]


def wire(rows, *, done=True):
    return (": keepalive\r\n\r\n" + "".join("data: " + json.dumps(row, ensure_ascii=False) + "\r\n\r\n" for row in rows)
            + ("data: [DONE]\r\n\r\n" if done else "")).encode("utf-8")


def run_chat(rows, *, done=True, mutate=None, parallel=True):
    async def run():
        cfg = config()
        prepared = prompt(cfg)
        if not parallel:
            from dataclasses import replace
            prepared = replace(prepared, parallel_tool_calls=False)
        adapter = StrataAdapter(cfg)
        await adapter.client.aclose()
        seen = []
        def handler(request):
            body = json.loads(request.content)
            assert body["reasoning_effort"] == "high"
            assert body["max_tokens"] == 16384
            assert "num_gpu" not in body
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=Chunks(wire(rows, done=done)))
        adapter.client = httpx.AsyncClient(base_url=cfg["BaseUrl"], transport=httpx.MockTransport(handler))
        async def delta(text, channel):
            seen.append((channel, text))
        try:
            if mutate:
                mutate(adapter)
            return await adapter.chat(prepared, delta), seen
        finally:
            await adapter.aclose()
    return asyncio.run(run())


def frame(delta, finish=None):
    return {"choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}


def test_fragmented_reasoning_tools_and_usage():
    rows = [frame({"reasoning_content": "思考"}), frame({"content": "读取"}),
            frame({"tool_calls": [{"index": 0, "id": "call_a", "type": "function", "function": {"name": "re", "arguments": '{"pa'}}]}),
            frame({"tool_calls": [{"index": 0, "function": {"name": "ad", "arguments": 'th":"中文"}'}}]}),
            frame({}, "tool_calls"), {"choices": [], "usage": {"prompt_tokens": 12, "completion_tokens": 8}}]
    result, seen = run_chat(rows)
    assert seen == [("reasoning_content", "思考"), ("content", "读取")]
    assert result.input_tokens == 12 and result.output_tokens == 8
    assert result.finish_reason == "tool_calls" and result.is_complete
    assert result.tool_calls[0]["id"] == "call_a"
    assert json.loads(result.tool_calls[0]["function"]["arguments"]) == {"path": "中文"}


@pytest.mark.parametrize("rows,done", [
    ([frame({"content": "partial"})], True),
    ([frame({}, "stop")], False),
    ([frame({}, "tool_calls")], True),
    ([frame({}, "stop"), {"choices": [], "usage": {"completion_tokens": -1}}], True),
    ([frame({"content": {"invalid": True}}, "stop")], True),
    ([frame({"tool_calls": [{"index": 0, "id": "a", "function": {"name": "unknown", "arguments": "{}"}}]}, "tool_calls")], True),
    ([frame({"tool_calls": [{"index": 0, "id": "a", "function": {"name": "read", "arguments": "[]"}}]}, "tool_calls")], True),
    ([frame({"tool_calls": [{"index": 0, "id": "a", "function": {"name": "read", "arguments": "{bad"}}]}, "tool_calls")], True),
])
def test_incomplete_or_invalid_streams_fail(rows, done):
    with pytest.raises(ModelError) as error:
        run_chat(rows, done=done)
    assert error.value.code == "MODEL_PROTOCOL_ERROR"


def test_truncated_thinking_keeps_partial_result_and_drops_tools():
    result, _ = run_chat([frame({"reasoning_content": "未完成"}, "length")])
    assert not result.is_complete and result.finish_reason == "length"
    assert result.reasoning_text == "未完成" and result.tool_calls == ()
    result, _ = run_chat([frame({"tool_calls": [{"index": 0, "id": "partial", "function": {
        "name": "read", "arguments": '{"unfinished":'}}]}, "length")])
    assert not result.is_complete and result.tool_calls == ()


def test_history_preserves_ids_arguments_and_thinking():
    cfg = config()
    request = {"messages": [{"role": "assistant", "content": [], "reasoning_content": "之前的思考",
        "tool_calls": [{"id": "original-id", "function": {"name": "read", "arguments": {"path": "a.cs"}}}]},
        {"role": "tool", "content": [{"type": "text", "text": "body"}], "tool_call_id": "original-id", "tool_name": "read"}]}
    prepared = prepare_messages(request, (), cfg["ImagePolicy"], provider="Strata")
    first, second = prepared.messages()
    assert first["tool_calls"][0]["id"] == second["tool_call_id"] == "original-id"
    assert first["reasoning_content"] == "之前的思考"
    assert json.loads(first["tool_calls"][0]["function"]["arguments"]) == {"path": "a.cs"}
    assert "tool_name" not in second
    frozen = freeze_output(prepared, cfg, supports_thinking=True)
    assert chat_payload(cfg, frozen)["reasoning_effort"] == "none"


def test_budget_counts_tool_history_and_rejects_limits():
    async def run():
        cfg = config()
        budget = StrataBudget(cfg)
        profile = type("Profile", (), {"profile_id": "test"})()
        for options, code in (({"num_predict": 16385}, "OUTPUT_LIMIT_EXCEEDED"), ({}, "CONTEXT_TOO_LARGE")):
            task = TaskInput(json.dumps({"messages": [{"role": "user", "content": [{"type": "text", "text": "中" * 90000}]}], "options": options}), ())
            assert (await budget.prepare(task, profile)).code == code
    asyncio.run(run())


def test_recovery_requires_idle_engine_or_managed_restart():
    async def run():
        cfg = config()
        settings = Settings(provider="Strata")
        provider = StrataProvider(settings, cfg, {})
        await provider.adapter.client.aclose()
        provider.adapter.client = httpx.AsyncClient(base_url=cfg["BaseUrl"], transport=httpx.MockTransport(
            lambda r: httpx.Response(200, json={"busy": False, "queued": 0} if r.url.path == "/status" else [{"is_processing": False}])))
        provider.verify_identity = AsyncMock()
        provider.engine.restart = AsyncMock()
        try:
            assert await provider.recover()
            provider.engine.restart.assert_not_awaited()
            await provider.adapter.client.aclose()
            provider.adapter.client = httpx.AsyncClient(base_url=cfg["BaseUrl"], transport=httpx.MockTransport(
                lambda r: httpx.Response(500)))
            assert await provider.recover()
            provider.engine.restart.assert_awaited_once()
            provider.engine.restart.side_effect = RuntimeError()
            assert not await provider.recover()
        finally:
            await provider.aclose()
    asyncio.run(run())


def test_strata_rejects_multiple_models_and_external_ownership():
    with pytest.raises(ValueError):
        Settings(provider="Strata", additional_inference_configs=["config/inference.qwen35.example.json"])
    with pytest.raises(ValueError):
        Settings(provider="Strata", manage_engine=False)
