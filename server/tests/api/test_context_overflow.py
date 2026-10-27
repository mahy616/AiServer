"""Both admission and engine overflow stay recoverable in OpenAI HTTP/SSE."""
import json

import pytest

from aiserver.inference.contracts import BudgetCheck, ModelError
from aiserver.inference.fake import FakeProvider
from test_fake_api import service, request
from test_openai import chat, frames, assert_openai


class OverflowBudget:
    accepted_input_types = ("text", "image")
    async def prepare(self, task, profile):
        return BudgetCheck(code="CONTEXT_TOO_LARGE", details={"contextTokens": 81920,
            "outputTokens": 1024, "reservedTokens": 512, "inputLimitTokens": 80384, "inputTokens": 80385})


@pytest.mark.parametrize("stream", [False, True])
def test_preflight_error_is_standard_http400_before_any_stream_or_job(tmp_path, stream):
    with service(tmp_path, budget=OverflowBudget()) as (client, store, scheduler, _):
        response = client.post("/v1/chat/completions", json=chat(stream=stream, max_tokens=1024))
        assert response.status_code == 400
        assert_openai("error", response.json())
        error = response.json()["error"]
        assert error["code"] == "context_length_exceeded" and error["param"] == "messages"
        assert "80384" in error["message"] and "80385" in error["message"]
        assert store.list("alice", 50, None)[0] == []
        native = client.post("/api/v1/jobs", json=request())
        assert native.status_code == 422 and native.json()["error"]["code"] == "CONTEXT_TOO_LARGE"
        assert native.json()["error"]["details"]["inputLimitTokens"] == 80384


class OverflowProvider(FakeProvider):
    fail = True
    async def generate(self, prepared, stop, on_delta):
        if self.fail:
            raise ModelError("CONTEXT_TOO_LARGE", request_finished=True)
        return await super().generate(prepared, stop, on_delta)


@pytest.mark.parametrize("stream", [False, True])
def test_engine_overflow_is_standard_and_does_not_poison_next_request(tmp_path, stream):
    provider = OverflowProvider(0)
    with service(tmp_path, provider=provider) as (client, store, scheduler, _):
        response = client.post("/v1/chat/completions", json=chat(stream=stream))
        if stream:
            assert response.status_code == 200
            received = frames(response)
            assert received[-1]["error"]["code"] == "context_length_exceeded"
            envelope = json.loads(received[-1]["error"]["message"])
            assert envelope["type"] == "error" and envelope["error"]["code"] == "context_length_exceeded"
            assert "[DONE]" not in received
        else:
            assert response.status_code == 400
            assert response.json()["error"]["code"] == "context_length_exceeded"
        provider.fail = False
        retry = client.post("/v1/chat/completions", json=chat())
        assert retry.status_code == 200
        assert scheduler.state != "paused"
