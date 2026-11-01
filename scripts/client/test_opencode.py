"""Credential-boundary checks; real model/tool verification is a separate command."""
import json
from pathlib import Path
import sys
from unittest.mock import patch

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
import opencode_support as support

KEY = "ais_" + "1" * 32 + "_synthetic-not-a-real-key"


def remote(expires=None):
    return {
        "/api/v1/auth/me": {"user": {"userId": "test"}, "expiresAt": expires},
        "/api/v1/models": {"models": [{"id": "aiserver-qwen25-coder:test", "limits": {
            "contextTokens": 81920, "maxInputTokens": 65536, "maxOutputTokens": 8192}}]},
        "/api/v1/ready": {"canAcceptJobs": True},
    }


def locations(monkeypatch, tmp_path):
    monkeypatch.setattr(support, "WORK", tmp_path)
    monkeypatch.setattr(support, "VAULT", tmp_path / "api-key.dpapi")
    monkeypatch.setattr(support, "CONFIG", tmp_path / "opencode.json")


def test_failed_auth_preserves_existing_key(monkeypatch, tmp_path):
    locations(monkeypatch, tmp_path)
    support.VAULT.write_bytes(b"old-encrypted-key")
    with patch.object(support, "probe", side_effect=support.SetupError("HTTP 401")):
        with pytest.raises(support.SetupError):
            support.install_key(support.DEFAULT_SERVER, KEY)
    assert support.VAULT.read_bytes() == b"old-encrypted-key"
    assert not support.CONFIG.exists()


def test_valid_expiring_api_key_is_supported(monkeypatch, tmp_path):
    locations(monkeypatch, tmp_path)
    # A reversible stand-in isolates expiry handling from Windows DPAPI.
    with patch.object(support, "probe", return_value=remote("2030-11-23T00:00:00Z")), \
         patch.object(support, "dpapi", side_effect=lambda raw, protect: raw[::-1]):
        summary = support.install_key(support.DEFAULT_SERVER, KEY)
    assert summary["expiresAt"] == "2030-11-23T00:00:00Z"
    assert KEY.encode() not in support.VAULT.read_bytes()


def test_bad_endpoint_does_not_send_key():
    with patch.object(support, "probe") as probe:
        with pytest.raises(support.SetupError):
            support.install_key("http://untrusted.example", KEY)
        probe.assert_not_called()


def test_transport_exception_does_not_expose_secret():
    with patch.object(requests.Session, "get", side_effect=requests.ConnectionError("secret=" + KEY)):
        with pytest.raises(support.SetupError) as error:
            support.probe(support.key_record(support.DEFAULT_SERVER, KEY))
        assert KEY not in str(error.value)


def test_config_is_reference_only_and_child_gets_independent_key(monkeypatch, tmp_path):
    locations(monkeypatch, tmp_path)
    record = support.key_record(support.DEFAULT_SERVER, KEY)
    summary = support.configure(record, remote())
    monkeypatch.setattr(support, "saved_key", lambda: record)
    env = support.environment(True)
    assert env["AISERVER_API_KEY"] == KEY
    assert KEY not in support.CONFIG.read_text(encoding="utf-8")
    assert KEY not in json.dumps(summary)
    assert "{env:AISERVER_API_KEY}" in support.CONFIG.read_text(encoding="utf-8")
    assert json.loads(env["OPENCODE_CONFIG_CONTENT"])["permission"]["*"] == "deny"
    assert "AISERVER_API_KEY" not in __import__("os").environ


def test_missing_key_fails_without_client_vault_access(monkeypatch, tmp_path):
    locations(monkeypatch, tmp_path)
    with patch.object(support, "dpapi") as dpapi:
        with pytest.raises(support.SetupError, match="--set-key"):
            support.saved_key()
        dpapi.assert_not_called()


def test_window_budget_capabilities_refresh_without_reintroducing_64k_cap(monkeypatch, tmp_path):
    locations(monkeypatch, tmp_path)
    result = remote()
    limits = result["/api/v1/models"]["models"][0]["limits"]
    limits.update(maxInputTokens=81407, defaultMaxInputTokens=73216, reservedTokens=512,
                  inputBudgetPolicy="window-minus-output-v1")
    support.configure(support.key_record(support.DEFAULT_SERVER, KEY), result)
    cfg = json.loads(support.CONFIG.read_text(encoding="utf-8"))
    actual = cfg["provider"]["aiserver"]["models"]["aiserver-qwen25-coder:test"]["limit"]
    assert actual == {"context": 81920, "input": 73216, "output": 8192}
    assert cfg["compaction"] == {"auto": True, "prune": True, "reserved": 10000}


def test_all_models_and_each_capacity_survive_refresh(monkeypatch, tmp_path):
    locations(monkeypatch, tmp_path)
    result = remote()
    models = result["/api/v1/models"]["models"]
    models += [{"id": "aiserver-qwen25-coder:14b", "inputTypes": ["text"], "limits": {
        "contextTokens": 49152, "maxInputTokens": 48639, "maxOutputTokens": 8192}},
        {"id": "qwen3.5:9b-q4_K_M", "inputTypes": ["text", "image"], "limits": {
        "contextTokens": 81920, "maxInputTokens": 81407, "maxOutputTokens": 8192}}]
    record = support.key_record(support.DEFAULT_SERVER, KEY)
    support.configure(record, result)
    cfg = json.loads(support.CONFIG.read_text(encoding="utf-8"))
    assert list(cfg["provider"]["aiserver"]["models"]) == [m["id"] for m in models]
    assert cfg["provider"]["aiserver"]["models"][models[1]["id"]]["limit"]["context"] == 49152
    assert cfg["provider"]["aiserver"]["models"][models[2]["id"]]["modalities"]["input"] == ["text", "image"]
    cfg["model"] = "aiserver/" + models[1]["id"]
    support.CONFIG.write_text(json.dumps(cfg))
    support.configure(record, result)
    assert json.loads(support.CONFIG.read_text(encoding="utf-8"))["model"] == cfg["model"]
    result["/api/v1/models"]["models"] = [models[2]]
    support.configure(record, result)
    assert json.loads(support.CONFIG.read_text(encoding="utf-8"))["model"] == "aiserver/" + models[2]["id"]


def test_reasoning_is_opt_in_with_variants_only_for_capable_models(monkeypatch, tmp_path):
    locations(monkeypatch, tmp_path)
    result = remote()
    model = result["/api/v1/models"]["models"][0]
    model["thinking"] = True
    support.configure(support.key_record(support.DEFAULT_SERVER, KEY), result)
    cfg = json.loads(support.CONFIG.read_text(encoding="utf-8"))
    entry = cfg["provider"]["aiserver"]["models"][model["id"]]
    assert entry["reasoning"] is True
    assert entry["options"] == {"think": False}
    assert entry["variants"] == {"normal": {"think": False}, "thinking": {"think": True}}
    model["thinking"] = False
    support.configure(support.key_record(support.DEFAULT_SERVER, KEY), result)
    entry = json.loads(support.CONFIG.read_text(encoding="utf-8"))["provider"]["aiserver"]["models"][model["id"]]
    assert entry["options"] == {"think": False} and "variants" not in entry


def test_gptoss_levels_and_existing_selection_survive_refresh(monkeypatch, tmp_path):
    locations(monkeypatch, tmp_path)
    result = remote()
    models = result['/api/v1/models']['models']
    models.append({**models[0], 'id': 'gpt-oss:20b', 'thinking': True, 'toolCall': True,
                   'thinkingLevels': ['low', 'medium', 'high'], 'defaultThinkingLevel': 'medium',
                   'thinkingCanDisable': False})
    support.configure(support.key_record(support.DEFAULT_SERVER, KEY), result)
    cfg = json.loads(support.CONFIG.read_bytes())
    assert cfg['model'] == 'aiserver/' + models[0]['id']
    entry = cfg['provider']['aiserver']['models']['gpt-oss:20b']
    assert entry['options'] == {'think': 'medium'}
    assert entry['variants'] == {level: {'think': level} for level in ('low', 'medium', 'high')}
    assert entry['tool_call'] is True and 'normal' not in entry['variants']


@pytest.mark.parametrize("template,expected", [
    ("coder", 114176), ("coder14b", 65024), ("qwen35", 114176),
    ("qwen38", 65024), ("5080", 28160), ("5080.8b", 28160),
])
def test_each_model_reserves_default_output_even_with_older_server(template, expected):
    cfg = json.loads((support.ROOT / f"config/inference.{template}.example.json").read_bytes())
    limits = {"contextTokens": cfg["NumCtx"], "maxInputTokens": cfg["NumCtx"]-cfg["ReservedTokens"]-1,
              "maxOutputTokens": cfg["NumPredict"]}
    assert support.model_limits({"limits": limits})["input"] == expected
    limits.update(defaultMaxInputTokens=expected, reservedTokens=cfg["ReservedTokens"])
    assert support.model_limits({"limits": limits})["input"] == expected


def test_text_agent_is_model_bound_and_selected_only_for_text_model(monkeypatch, tmp_path):
    locations(monkeypatch, tmp_path)
    result = remote()
    native = result["/api/v1/models"]["models"][0]
    native["toolCall"] = True
    text = {**native, "id": "fixture-text-model", "toolCall": False}
    result["/api/v1/models"]["models"].append(text)
    record = support.key_record(support.DEFAULT_SERVER, KEY)
    support.configure(record, result)
    cfg = json.loads(support.CONFIG.read_bytes())
    assert cfg["default_agent"] == "build"
    agent = cfg["agent"][support.text_agent_name(text["id"])]
    assert agent["model"] == "aiserver/" + text["id"]
    assert agent["tools"] == {"*": False} and agent["permission"] == {"*": "deny"}
    cfg["model"] = agent["model"]
    support.CONFIG.write_text(json.dumps(cfg))
    support.configure(record, result)
    assert json.loads(support.CONFIG.read_bytes())["default_agent"] == support.text_agent_name(text["id"])
