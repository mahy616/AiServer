import asyncio
import base64
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from io import BytesIO
import json
from pathlib import Path
import sqlite3
import time
from uuid import uuid4

from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator, FormatChecker
from PIL import Image
import pytest

from aiserver.app import create_app
from aiserver.inference.fake import FakeBudget, FakeProvider, UnverifiedBudget, fake_profile
from aiserver.settings import ROOT, Settings
from aiserver.storage.db import Store, now_ms

def request(image=False):
    data = json.loads((ROOT/"contracts/examples"/("image-request.json" if image else "text-request.json")).read_text(encoding="utf-8"))
    data["clientRequestId"] = str(uuid4())
    return data

def assert_schema(name, value):
    schema = json.loads((ROOT/"contracts/api/v1/responses.schema.json").read_text(encoding="utf-8"))
    schema["$ref"] = "#/$defs/"+name
    Draft202012Validator(schema,format_checker=FormatChecker()).validate(value)

@contextmanager
def service(tmp_path, *, provider=None, profile=None, budget=None, **overrides):
    settings = Settings(data_directory=tmp_path, **overrides)
    app = create_app(settings,provider=provider,profile=profile,budget=budget)
    app.state.store.initialize()
    store = app.state.store
    store.user_add("alice","Alice")
    store.user_add("bob","Bob")
    token = store.token_create("alice")
    other = store.token_create("bob")
    with TestClient(app) as client:
        client.headers["Authorization"] = "Bearer "+token
        yield client, store, app.state.scheduler, {"Authorization":"Bearer "+other}

def terminal(client, job, timeout=5):
    until = time.monotonic()+timeout
    while time.monotonic()<until:
        response = client.get("/api/v1/jobs/"+job)
        assert response.status_code == 200, response.text
        result = response.json()
        if result["status"] in ("succeeded","failed","cancelled"):
            return result
        time.sleep(0.01)
    raise AssertionError("Task did not finish")

def post(client, data):
    result = client.post("/api/v1/jobs",json=data)
    assert result.status_code in (200,202), result.text
    assert_schema("submission",result.json())
    return result.json()["jobId"]

def test_samples_and_text_image_full_loop(tmp_path):
    for filename in ("text-request.json","image-request.json"):
        data = json.loads((ROOT/"contracts/examples"/filename).read_text(encoding="utf-8"))
        schema = json.loads((ROOT/"contracts/api/v1/job-request.schema.json").read_text(encoding="utf-8"))
        Draft202012Validator(schema,format_checker=FormatChecker()).validate(data)
    with service(tmp_path,fake_delay_seconds=0.01) as (client,store,scheduler,other):
        ready = client.get("/api/v1/ready")
        assert ready.status_code == 200
        assert ready.json()["inferenceProvider"] == "Fake"
        assert_schema("ready",ready.json())
        for image in (False,True):
            data = request(image)
            data["instruction"] += "\n中文🙂e\u0301\r\n"
            job = post(client,data)
            state = terminal(client,job)
            assert state["status"] == "succeeded"
            assert_schema("status",state)
            result = client.get("/api/v1/jobs/"+job+"/result")
            assert result.status_code == 200
            assert_schema("result",result.json())
            assert result.json()["output"]["text"].endswith(data["instruction"])
            assert result.json()["generation"]["inferenceProvider"] == "Fake"
            assert result.json()["generation"]["inputTokens"] is None
            recovered = client.get("/api/v1/jobs/by-request/"+data["clientRequestId"])
            assert recovered.json()["jobId"] == job
            assert "dataBase64" not in json.dumps(state)
            assert "dataBase64" not in result.text
        listing = client.get("/api/v1/jobs").json()
        assert_schema("list",listing)
        assert len(listing["items"])==2

def test_auth_owner_expiry_revoke_and_parent(tmp_path):
    with service(tmp_path,fake_delay_seconds=0.01) as (client,store,scheduler,other):
        assert client.get("/api/v1/health",headers={"Authorization":""}).json()=={"status":"ok"}
        denied = client.get("/api/v1/ready",headers={"Authorization":"Bearer invalid"})
        assert denied.status_code==401
        assert_schema("errorResponse",denied.json())
        expired = store.token_create("alice",now_ms()-1)
        assert client.get("/api/v1/ready",headers={"Authorization":"Bearer "+expired}).status_code==401
        revoked = store.token_create("alice")
        store.token_revoke(revoked.split("_",2)[1])
        assert client.get("/api/v1/ready",headers={"Authorization":"Bearer "+revoked}).status_code==401
        data=request(); job=post(client,data); terminal(client,job)
        for suffix in ("","/result"):
            assert client.get("/api/v1/jobs/"+job+suffix,headers=other).status_code==404
        assert client.post("/api/v1/jobs/"+job+"/cancel",headers=other).status_code==404
        assert client.get("/api/v1/jobs/by-request/"+data["clientRequestId"],headers=other).status_code==404
        assert client.get("/api/v1/jobs",headers=other).json()["items"]==[]
        parent=request(); parent["parentJobId"]=job
        assert client.post("/api/v1/jobs",json=parent,headers=other).status_code==404
        renewed=store.token_create("alice")
        assert client.get("/api/v1/jobs/"+job,headers={"Authorization":"Bearer "+renewed}).status_code==200

def test_concurrent_idempotency_and_normalization(tmp_path):
    provider=FakeProvider(0.1)
    with service(tmp_path,provider=provider) as (client,store,scheduler,other):
        data=request()
        with ThreadPoolExecutor(max_workers=8) as pool:
            replies=list(pool.map(lambda _:client.post("/api/v1/jobs",json=data),range(8)))
        assert all(x.status_code in (200,202) for x in replies)
        assert sum(x.status_code==202 for x in replies)==1
        job=replies[0].json()["jobId"]
        assert all(x.json()["jobId"]==job for x in replies)
        minimal=json.loads(json.dumps(data))
        for key in ("currentCode","parentJobId","images","history"):
            del minimal[key]
        for key in ("csharpLanguageVersion","sdkContractVersion"):
            del minimal["context"][key]
        assert client.post("/api/v1/jobs",content=json.dumps(minimal,sort_keys=True),
                           headers={"Content-Type":"application/json"}).json()["jobId"]==job
        changed=json.loads(json.dumps(data)); changed["instruction"]+="x"
        assert client.post("/api/v1/jobs",json=changed).status_code==409
        terminal(client,job)
        assert provider.calls==1 and provider.max_active==1

def test_queue_cancel_limit_and_completed_cancel(tmp_path):
    provider=FakeProvider(30)
    with service(tmp_path,provider=provider) as (client,store,scheduler,other):
        first=post(client,request())
        client.portal.call(provider.started.wait)
        data=request(); second=post(client,data)
        denied=client.post("/api/v1/jobs",json=request())
        assert denied.status_code==429 and denied.json()["error"]["code"]=="USER_JOB_LIMIT"
        assert client.get("/api/v1/jobs/"+second+"/result").status_code==409
        cancel=client.post("/api/v1/jobs/"+second+"/cancel")
        assert cancel.status_code==200 and cancel.json()["status"]=="cancelled"
        assert_schema("cancel",cancel.json())
        client.post("/api/v1/jobs/"+first+"/cancel")
        assert terminal(client,first)["status"]=="cancelled"
        assert provider.calls==1
        assert client.post("/api/v1/jobs/"+second+"/cancel").json()["cancelAccepted"] is True
    with service(tmp_path/"finished",fake_delay_seconds=0) as (client,store,scheduler,other):
        job=post(client,request()); terminal(client,job)
        assert client.post("/api/v1/jobs/"+job+"/cancel").json()["cancelAccepted"] is False
        assert client.get("/api/v1/jobs/"+job).json()["status"]=="succeeded"

def test_late_completion_discarded_and_next_no_overlap(tmp_path):
    provider=FakeProvider(10,"late",0.1)
    with service(tmp_path,provider=provider) as (client,store,scheduler,other):
        first=post(client,request()); client.portal.call(provider.started.wait)
        response=client.post("/api/v1/jobs/"+first+"/cancel")
        assert response.status_code==202
        assert terminal(client,first)["status"]=="cancelled"
        second=post(client,request())
        provider.delay=0
        assert terminal(client,second)["status"]=="succeeded"
        result=client.get("/api/v1/jobs/"+first+"/result").json()
        assert result["output"] is None
        assert provider.max_active==1 and provider.calls==2
        assert terminal(client,first)["status"]=="cancelled"

@pytest.mark.parametrize("scenario,code",[
    ("empty","MODEL_EMPTY_OUTPUT"),("truncated","OUTPUT_TRUNCATED"),
    ("failure","MODEL_UNAVAILABLE"),("protocol","MODEL_PROTOCOL_ERROR"),("connection","MODEL_UNAVAILABLE")])
def test_fake_failure_mapping_no_retry(tmp_path,scenario,code):
    provider=FakeProvider(0,scenario)
    with service(tmp_path,provider=provider) as (client,store,scheduler,other):
        job=post(client,request())
        final=terminal(client,job)
        assert final["status"]=="failed" and final["error"]["code"]==code
        result=client.get("/api/v1/jobs/"+job+"/result")
        assert result.status_code==200
        if scenario=="truncated":
            assert result.json()["output"]["isComplete"] is False
        assert provider.calls==1

def test_execution_timeout_and_queue_wait(tmp_path):
    provider=FakeProvider(30)
    with service(tmp_path,provider=provider,execution_timeout_seconds=0.15,queue_timeout_seconds=0.05) as (client,store,scheduler,other):
        first=post(client,request()); client.portal.call(provider.started.wait)
        second=post(client,request())
        assert terminal(client,first)["error"]["code"]=="EXECUTION_TIMEOUT"
        assert terminal(client,second)["error"]["code"]=="QUEUE_TIMEOUT"
        client.post("/api/v1/jobs/"+first+"/cancel")
        assert client.get("/api/v1/jobs/"+first).json()["error"]["code"]=="EXECUTION_TIMEOUT"
        assert provider.calls==1

def test_request_transport_and_strict_json(tmp_path):
    with service(tmp_path,max_request_bytes=2000,fake_delay_seconds=0) as (client,store,scheduler,other):
        for body in ('{"instruction":"a","instruction":"b"}','{"instruction":NaN}', '{"instruction":'):
            result=client.post("/api/v1/jobs",content=body,headers={"Content-Type":"application/json"})
            assert result.status_code==400
        data=request(); data["instruction"]="\ud800"
        assert client.post("/api/v1/jobs",content=json.dumps(data),headers={"Content-Type":"application/json"}).status_code==400
        data=request(); data["model"]="forbidden"
        assert client.post("/api/v1/jobs",json=data).status_code==400
        data=request(); data["history"]=[{"role":"system","content":"x"}]
        assert client.post("/api/v1/jobs",json=data).status_code==400
        data=request(); data["operation"]="modify"
        assert client.post("/api/v1/jobs",json=data).status_code==400
        assert client.post("/api/v1/jobs",content="{}",headers={"Content-Type":"text/plain"}).status_code==415
        assert client.post("/api/v1/jobs",content="{}",headers={"Content-Type":"application/json","Content-Encoding":"gzip"}).status_code==415
        assert client.post("/api/v1/jobs",content=b"x"*2001,headers={"Content-Type":"application/json"}).status_code==413
        def chunks():
            yield b"x"*1500
            yield b"x"*501
        assert client.post("/api/v1/jobs",content=chunks(),headers={"Content-Type":"application/json"}).status_code==413
        assert scheduler.provider.calls==0

def test_png_decode_identity_no_echo(tmp_path):
    with service(tmp_path,fake_delay_seconds=0) as (client,store,scheduler,other):
        data=request(True); job=post(client,data); terminal(client,job)
        changed=json.loads(json.dumps(data))
        picture=Image.new("RGB",(2,1),(123,45,6)); buffer=BytesIO(); picture.save(buffer,format="PNG")
        changed["images"][0]["dataBase64"]=base64.b64encode(buffer.getvalue()).decode()
        assert client.post("/api/v1/jobs",json=changed).status_code==409
        bad=request(True); bad["images"][0]["dataBase64"]="not base64"
        assert client.post("/api/v1/jobs",json=bad).json()["error"]["code"]=="IMAGE_INVALID"
        bad=request(True); bad["images"][0]["dataBase64"]=base64.b64encode(b"not png").decode()
        assert client.post("/api/v1/jobs",json=bad).status_code==415
        bad=request(True); bad["images"][0]["dataBase64"]=data["images"][0]["dataBase64"][:-16]
        assert client.post("/api/v1/jobs",json=bad).status_code==415
        picture=Image.new("RGB",(1281,1)); buffer=BytesIO(); picture.save(buffer,format="PNG")
        bad=request(True); bad["images"][0]["dataBase64"]=base64.b64encode(buffer.getvalue()).decode()
        assert client.post("/api/v1/jobs",json=bad).json()["error"]["code"]=="IMAGE_LIMIT_EXCEEDED"
        effective=json.loads(store.get("alice",job=job)["effective_settings_json"])
        assert effective["images"][0]["receivedSha256"]==effective["images"][0]["sentSha256"]

def test_budget_unverified_and_fake_limit(tmp_path):
    with service(tmp_path,budget=UnverifiedBudget()) as (client,store,scheduler,other):
        assert client.get("/api/v1/ready").status_code == 503
        assert client.get("/api/v1/ready").json()["acceptedInputTypes"] == []
        result=client.post("/api/v1/jobs",json=request(True))
        assert result.status_code==503 and result.json()["error"]["code"]=="INPUT_BUDGET_UNVERIFIED"
        assert scheduler.provider.calls==0
    cfg=Settings(data_directory=tmp_path/"no-types")
    profile=replace(fake_profile(cfg),accepted_input_types=())
    with service(tmp_path/"no-types",profile=profile) as (client,store,scheduler,other):
        ready=client.get("/api/v1/ready")
        assert ready.status_code==503 and ready.json()["acceptedInputTypes"]==[]
        assert client.post("/api/v1/jobs",json=request()).status_code==503
    with service(tmp_path/"limited",budget=FakeBudget(1)) as (client,store,scheduler,other):
        result=client.post("/api/v1/jobs",json=request())
        assert result.status_code==422 and result.json()["error"]["code"]=="CONTEXT_TOO_LARGE"

def test_expiry_tombstone_replay_without_budget(tmp_path):
    with service(tmp_path,fake_delay_seconds=0) as (client,store,scheduler,other):
        data=request(True); job=post(client,data); terminal(client,job)
        with store.connection(True) as c:
            c.execute("UPDATE jobs SET payload_expires_at_ms=? WHERE job_id=?", (now_ms()-1,job))
        assert client.get("/api/v1/jobs/"+job+"/result").status_code==410
        store.cleanup()
        assert store.get("alice",job=job)["request_json"] is None
        scheduler.budget=UnverifiedBudget()
        replay=client.post("/api/v1/jobs",json=data)
        assert replay.status_code==200 and replay.json()["jobId"]==job
        assert client.get("/api/v1/jobs/"+job).json()["resultAvailable"] is False

def test_crash_residual_recovery_and_single_instance(tmp_path):
    with service(tmp_path,fake_delay_seconds=0) as (client,store,scheduler,other):
        data=request(); job=post(client,data); terminal(client,job)
        with pytest.raises(RuntimeError):
            with TestClient(create_app(Settings(data_directory=tmp_path))):
                pass
    # Simulate a process crash leaving a running durable record (no real engine).
    with store.connection(True) as c:
        c.execute("UPDATE jobs SET status='running',finished_at_ms=NULL WHERE job_id=?", (job,))
    app=create_app(Settings(data_directory=tmp_path))
    with TestClient(app) as client:
        client.headers["Authorization"]="Bearer "+store.token_create("alice")
        assert client.get("/api/v1/ready").status_code==503
        assert client.get("/api/v1/jobs/"+job).json()["error"]["code"]=="SERVER_RESTARTED"
        assert client.post("/api/v1/jobs",json=data).status_code==200
        assert client.post("/api/v1/jobs",json=request()).json()["error"]["code"]=="ENGINE_RECOVERING"
    store.confirm_recovery("Fake-only crash fixture; no external inference process")
    with TestClient(create_app(Settings(data_directory=tmp_path))) as client:
        client.headers["Authorization"]="Bearer "+store.token_create("alice")
        assert client.get("/api/v1/ready").status_code==200

def test_production_fake_forbidden(tmp_path):
    with pytest.raises(ValueError):
        Settings(data_directory=tmp_path,environment="Production")
