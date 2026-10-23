import asyncio
import base64
import hashlib
from io import BytesIO
import json
from pathlib import Path
import sqlite3
from uuid import uuid4

from fastapi.testclient import TestClient
import httpx
from PIL import Image
import pytest

from aiserver.app import create_app
from aiserver.settings import ROOT, Settings
from aiserver.storage.db import Store
from aiserver.operations.lock import InstanceLock
from test_fake_api import service, request, post, terminal


def real_app(tmp_path, scenario="success"):
    cfg = json.loads((ROOT / "config/inference.development.example.json").read_text())
    lock = json.loads((ROOT / "config/models/qwen3vl4b.lock.json").read_text())
    lock["template_sha256"] = hashlib.sha256(b"template").hexdigest()
    lock_file = tmp_path / "model-lock.json"
    lock_file.write_text(json.dumps(lock))
    cfg["ModelLockPath"] = str(lock_file)
    cfg_file = tmp_path / "inference.json"
    cfg_file.write_text(json.dumps(cfg))
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("Return complete C# code or ask for required SDK facts.")
    settings = Settings(provider="Ollama", environment="Production", manage_engine=False,
                        data_directory=tmp_path / "data", inference_config=cfg_file, system_prompt=prompt)
    app = create_app(settings)
    captured = []
    async def handler(req):
        if req.url.path == "/api/version":
            return httpx.Response(200, json={"version":cfg["EngineVersion"]})
        if req.url.path == "/api/tags":
            return httpx.Response(200, json={"models":[lock["model"]]})
        if req.url.path == "/api/show":
            return httpx.Response(200, json={"details":{"quantization_level":cfg["ExpectedQuantization"]},
                                           "capabilities":["completion","vision"], "template":"template"})
        captured.append(json.loads(req.content))
        if scenario == "context":
            return httpx.Response(413, json={"error":"input exceeds context length"})
        return httpx.Response(200, json={"model":cfg["Model"], "done":True,
            "done_reason":"length" if scenario == "truncated" else "stop",
            "eval_count":42, "prompt_eval_count":321, "message":{"content":"```csharp\n// complete preview\n```"}})
    app.state.scheduler.provider.adapter.client = httpx.AsyncClient(base_url=cfg["BaseUrl"], transport=httpx.MockTransport(handler))
    store = app.state.store
    store.initialize()
    store.user_add("real", "Real provider fixture")
    return app, store.token_create("real"), captured


@pytest.mark.parametrize("scenario,expected", [("success","succeeded"),("context","failed"),("truncated","failed")])
def test_real_assembly_wire_capacity_and_result(tmp_path, scenario, expected):
    app, token, captured = real_app(tmp_path, scenario)
    with TestClient(app) as client:
        client.headers["Authorization"] = "Bearer " + token
        ready = client.get("/api/v1/ready").json()
        assert ready["inferenceProvider"] == "Ollama" and ready["capacityMethod"] == "conservative-estimate-v1"
        data = request(True)
        job = post(client, data)
        assert terminal(client, job)["status"] == expected
        result = client.get(f"/api/v1/jobs/{job}/result").json()
        assert captured[0]["truncate"] is False and captured[0]["shift"] is False
        assert captured[0]["messages"][1]["images"][0] == data["images"][0]["dataBase64"]
        assert result["generation"]["estimatedInputTokens"] > 0
        if scenario == "success":
            assert result["generation"]["inputTokens"] == 321
            followup = request(); followup["operation"] = "modify"; followup["currentCode"] = "// previous candidate"
            followup["parentJobId"] = job; followup["instruction"] = "Fix compiler error while preserving the original task"
            assert terminal(client, post(client, followup))["status"] == "succeeded"
        elif scenario == "context":
            assert result["error"]["code"] == "CONTEXT_TOO_LARGE"
        else:
            assert result["output"]["isComplete"] is False
        huge = request(); huge["instruction"] = "x" * 8000
        assert client.post("/api/v1/jobs", json=huge).json()["error"]["code"] == "CONTEXT_TOO_LARGE"
        assert result["validation"]["compilation"] == "not_run"


def test_user_rate_limits_and_safe_logs(tmp_path):
    with service(tmp_path, requests_per_minute=50, submissions_per_minute=1, fake_delay_seconds=0) as (client,store,scheduler,other):
        data = request(); data["instruction"] = "PRIVATE_PAYLOAD_MARKER"
        terminal(client, post(client, data))
        second = client.post("/api/v1/jobs", json=request())
        assert second.status_code == 429 and second.json()["error"]["code"] == "RATE_LIMITED"
        assert client.post("/api/v1/jobs", json=request(), headers=other).status_code == 202
        content = (tmp_path / "logs/service.log").read_text()
        assert "PRIVATE_PAYLOAD_MARKER" not in content and "Bearer" not in content
        assert "trace=" in content


def test_backup_restore_and_instance_exclusion(tmp_path):
    with service(tmp_path / "live", fake_delay_seconds=0) as (client,store,scheduler,other):
        job = post(client, request()); terminal(client, job)
        with pytest.raises(RuntimeError):
            InstanceLock(store.path.parent).acquire()
    backup = tmp_path / "backups" / "snapshot.db"
    store.backup(backup)
    target = Store(Settings(data_directory=tmp_path / "restored"))
    target.restore(backup)
    target.initialize()
    assert target.get("alice", job=job)["status"] == "succeeded"
    with pytest.raises(ValueError):
        target.restore(backup)
    with pytest.raises(ValueError):
        store.backup(backup)
    corrupt = tmp_path / "bad.db"; corrupt.write_text("not a database")
    bad = Store(Settings(data_directory=tmp_path / "bad-target"))
    with pytest.raises(sqlite3.Error):
        bad.restore(corrupt)
    assert not bad.path.exists()


def test_automatic_recovery_holds_slot_until_engine_reset(tmp_path):
    from aiserver.inference.fake import FakeProvider, fake_profile
    from dataclasses import replace
    class Recoverable(FakeProvider):
        recovered = False
        async def recover(self):
            assert self.active == 0
            await asyncio.sleep(0.05)
            self.recovered = True
            return True
    provider = Recoverable(10)
    cfg = Settings(data_directory=tmp_path)
    profile = replace(fake_profile(cfg), automatic_cleanup_verified=False)
    with service(tmp_path, provider=provider, profile=profile) as (client,store,scheduler,other):
        first = post(client, request()); client.portal.call(provider.started.wait)
        client.post(f"/api/v1/jobs/{first}/cancel")
        terminal(client, first)
        provider.delay = 0
        # Recovering may briefly reject submissions; the existing queue still resumes.
        import time
        until = time.monotonic() + 3
        while not provider.recovered and time.monotonic() < until:
            time.sleep(0.01)
        assert provider.recovered
        second = post(client, request())
        assert terminal(client, second)["status"] == "succeeded"
        assert provider.max_active == 1


def test_transient_result_write_failure_is_retried(tmp_path, monkeypatch):
    with service(tmp_path, fake_delay_seconds=0) as (client,store,scheduler,other):
        original = store.finish
        attempts = []
        def flaky(*args):
            attempts.append(1)
            if len(attempts) == 1:
                raise sqlite3.OperationalError("injected result write failure")
            return original(*args)
        monkeypatch.setattr(store,"finish",flaky)
        job = post(client,request())
        assert terminal(client,job)["status"] == "succeeded"
        assert len(attempts) == 2
        assert terminal(client,post(client,request()))["status"] == "succeeded"
