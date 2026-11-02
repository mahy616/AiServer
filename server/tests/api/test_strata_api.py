"""Public API through real Strata mapping/budget/adapter, with synthetic upstream SSE."""
import asyncio
import json
from unittest.mock import AsyncMock

import httpx
import pytest

from aiserver.inference.contracts import Profile
from aiserver.inference.strata import StrataProvider, StrataBudget
from aiserver.settings import ROOT, Settings
from test_fake_api import service


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("family", ["qwen", "deepseek"])
def test_strata_tool_round_trip_preserves_protocol(tmp_path, stream, family):
    filename = "inference.strata.deepseek.example.json" if family == "deepseek" else "inference.strata.example.json"
    cfg = json.loads((ROOT / "config" / filename).read_bytes())
    profile = Profile("test-strata", "Strata", json.dumps({"config": cfg, "capacityMethod": "conservative-estimate-v1"}), ("text",))
    provider = StrataProvider(Settings(data_directory=tmp_path, provider="Strata"), cfg, {})
    provider.start = AsyncMock()
    provider.supports_tools = provider.supports_thinking = True
    asyncio.run(provider.adapter.client.aclose())
    captured = []
    def answer(request):
        payload = json.loads(request.content)
        captured.append(payload)
        assert payload["reasoning_effort"] == "high" and payload["max_tokens"] == 64
        if payload["messages"][-1]["role"] == "tool":
            assert payload["messages"][-1]["tool_call_id"] == "call_original"
            assert payload["messages"][-2]["tool_calls"][0]["id"] == "call_original"
            assert payload["messages"][-2]["reasoning_content"] == "先读文件"
            delta, finish = {"content": "文件已读取"}, "stop"
        else:
            delta, finish = {"reasoning_content": "先读文件", "tool_calls": [{"index": 0, "id": "call_original", "type": "function",
                "function": {"name": "read", "arguments": '{"path":"a.cs"}'}}]}, "tool_calls"
        rows = [{"model": cfg["Model"], "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]},
                {"choices": [], "usage": {"prompt_tokens": 42, "completion_tokens": 7}}]
        wire = "".join("data: " + json.dumps(row, ensure_ascii=False) + "\n\n" for row in rows) + "data: [DONE]\n\n"
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=wire.encode())
    provider.adapter.client = httpx.AsyncClient(base_url=cfg["BaseUrl"], transport=httpx.MockTransport(answer))
    with service(tmp_path, provider=provider, profile=profile, budget=StrataBudget(cfg)) as (client, *_):
        models = client.get("/api/v1/models").json()["models"]
        assert models[0]["id"] == cfg["Model"] and models[0]["thinking"] and models[0]["toolCall"]
        assert models[0]["capacityMethod"] == "conservative-estimate-v1"
        tool = {"type": "function", "function": {"name": "read", "parameters": {"type": "object"}}}
        payload = {"model": cfg["Model"], "messages": [{"role": "user", "content": "read a.cs"}],
                   "tools": [tool], "max_tokens": 64, "think": True, "stream": stream}
        response = client.post("/v1/chat/completions", json=payload)
        assert response.status_code == 200, response.text
        if stream:
            rows = [json.loads(line[6:]) for line in response.iter_lines() if line.startswith("data: ") and line != "data: [DONE]"]
            call = next(row["choices"][0]["delta"]["tool_calls"][0] for row in rows if row.get("choices") and row["choices"][0]["delta"].get("tool_calls"))
            assert call["id"] == "call_original"
        else:
            output = response.json()
            call = output["choices"][0]["message"]["tool_calls"][0]
            assert output["usage"]["completion_tokens"] == 7
        payload["messages"] += [{"role": "assistant", "content": "", "reasoning_content": "先读文件", "tool_calls": [
            {"id": call["id"], "type": "function", "function": call["function"]}]},
            {"role": "tool", "tool_call_id": call["id"], "content": "file contents"}]
        payload["stream"] = False
        response = client.post("/v1/chat/completions", json=payload)
        assert response.status_code == 200, response.text
        assert response.json()["choices"][0]["message"]["content"] == "文件已读取"
    assert len(captured) == 2
