"""Window/output boundaries, immutable generation settings and legacy-profile migration."""
import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from aiserver.api.models import model_description
from aiserver.api.validation import canonical
from aiserver.context.prompt_builder import PreparedPrompt
from aiserver.context.window_budget import output_budget, POLICY
from aiserver.inference.contracts import Profile, TaskInput
from aiserver.inference.ollama import engine_error
from aiserver.inference.runtime import PracticalBudget, load_profile
from aiserver.settings import ROOT, Settings


def config():
    cfg = json.loads((ROOT / "config/inference.coder14b.example.json").read_text(encoding="utf-8"))
    cfg["Budget"] = {}  # Script the counter, never load a real model/tokenizer.
    cfg["MaxInputTokens"] = 16  # Legacy private limits have no admission authority.
    return cfg


@pytest.mark.parametrize("requested,limit,effective", [(None, 73216, 8192), (1024, 80384, 1024),
                                                       (1, 81407, 1)])
def test_admission_uses_this_requests_effective_output_and_freezes_it(requested, limit, effective):
    cfg = config()
    request = {"protocol": "openai-chat-v1", "messages": [{"role": "user", "content": [
        {"type": "text", "text": "source code"}]}], "options": {} if requested is None else {"num_predict": requested}}
    task = TaskInput(canonical(request), ())
    profile = Profile("window-test", "Ollama", "{}", ("text",))
    budget = PracticalBudget(cfg)
    budget.counter = type("Counter", (), {"count": AsyncMock(return_value=limit)})()
    result = asyncio.run(budget.prepare(task, profile))
    assert result.prepared is not None
    raw = json.loads(result.prepared.payload_json)
    raw["image_sha256"] = tuple(raw["image_sha256"])
    prompt = PreparedPrompt(**raw)
    assert json.loads(prompt.options_json)["num_predict"] == effective
    assert prompt.material_sha256() == result.prepared.material_sha256 == prompt.content_sha256
    budget.counter.count.return_value = limit + 1
    rejected = asyncio.run(budget.prepare(task, profile))
    assert rejected.code == "CONTEXT_TOO_LARGE"
    assert rejected.details == {"contextTokens": 81920, "outputTokens": effective,
                                "reservedTokens": 512, "inputLimitTokens": limit, "inputTokens": limit + 1}


def test_legacy_private_profile_does_not_keep_64k_cap_or_allow_cpu_offload(tmp_path):
    cfg = config()
    cfg.update(MaxInputTokens=65536, RequireFullGpu=False)
    identity = tmp_path / "identity.json"
    identity.write_text("{}")
    cfg["ModelLockPath"] = str(identity)
    path = tmp_path / "inference.json"
    path.write_text(json.dumps(cfg))
    loaded, _, profile = load_profile(Settings(provider="Ollama", inference_config=path))
    assert "MaxInputTokens" not in loaded
    assert loaded["RequireFullGpu"] is True
    assert json.loads(profile.snapshot_json)["config"]["RequireFullGpu"] is True
    limits = model_description(profile, Settings())["limits"]
    assert (limits["maxInputTokens"], limits["defaultMaxInputTokens"], limits["reservedTokens"]) == (81407, 73216, 512)
    assert limits["inputBudgetPolicy"] == POLICY
    assert json.loads(path.read_text())["MaxInputTokens"] == 65536  # No private-file rewrite.


@pytest.mark.parametrize("message,expected", [
    ("context_length_exceeded", "CONTEXT_TOO_LARGE"),
    ("prompt too long for context", "CONTEXT_TOO_LARGE"),
    ("input is too long", "CONTEXT_TOO_LARGE"),
    ("CUDA out of memory: allocation too large", "MODEL_PROTOCOL_ERROR"),
    ("tool name too long", "MODEL_PROTOCOL_ERROR"),
])
def test_engine_context_errors_are_not_confused_with_gpu_or_tool_errors(message, expected):
    assert engine_error(message) == expected


@pytest.mark.parametrize("value", [0, -1, True, "1024"])
def test_invalid_output_budget_cannot_enlarge_the_window(value):
    with pytest.raises(ValueError):
        output_budget(config(), {"num_predict": value})
