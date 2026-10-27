import base64
from contextlib import contextmanager
from io import BytesIO
import json
import time
from uuid import uuid4

from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator, FormatChecker
from PIL import Image
import pytest

from aiserver.app import create_app
from aiserver.api.validation import RequestValidator, canonical
from aiserver.inference.fake import FakeBudget, FakeProvider, UnverifiedBudget
from aiserver.settings import ROOT, Settings
from aiserver.storage.db import now_ms


def request(image=False):
    value = json.loads((ROOT/"contracts/examples"/("image-request.json" if image else "text-request.json")).read_text(encoding="utf-8"))
    value["clientRequestId"], value["model"] = str(uuid4()), "fake-v1"
    return value


def assert_schema(name, value):
    schema = json.loads((ROOT/"contracts/api/v1/responses.schema.json").read_text(encoding="utf-8"))
    schema["$ref"] = "#/$defs/"+name
    Draft202012Validator(schema, format_checker=FormatChecker()).validate(value)


@contextmanager
def service(tmp_path, *, provider=None, profile=None, budget=None, **overrides):
    overrides.setdefault("requests_per_minute", 10000)
    overrides.setdefault("submissions_per_minute", 1000)
    app = create_app(Settings(data_directory=tmp_path, **overrides), provider=provider, profile=profile, budget=budget)
    store = app.state.store
    store.initialize()
    store.user_add("alice", "Alice"); store.user_add("bob", "Bob")
    token, other = store.token_create("alice"), store.token_create("bob")
    with TestClient(app) as client:
        client.headers["Authorization"] = "Bearer "+token
        yield client, store, app.state.scheduler, {"Authorization":"Bearer "+other}


def events(response):
    kind, parts = "", []
    for line in response.iter_lines():
        if line.startswith("event:"):
            kind = line[6:].strip()
        elif line.startswith("data:"):
            parts.append(line[5:].lstrip())
        elif not line and parts:
            yield kind, json.loads("\n".join(parts))
            kind, parts = "", []


def terminal(client, job, timeout=5):
    until = time.monotonic()+timeout
    while time.monotonic() < until:
        response = client.get("/api/v1/jobs/"+job)
        assert response.status_code == 200, response.text
        value = response.json()
        if value["status"] in ("succeeded", "failed", "cancelled"):
            return value
        time.sleep(0.01)
    raise AssertionError("Task did not finish")


def post(client, data):
    # Scheduler-focused fixtures bypass the owning SSE connection intentionally.
    async def submit():
        scheduler = client.app.state.scheduler
        normalized, fingerprint, task = RequestValidator(scheduler.settings.contract_directory).validate(canonical(data).encode())
        row, _ = await scheduler.submit("alice", normalized, fingerprint, task)
        return row["job_id"]
    return client.portal.call(submit)


def test_text_image_sse_and_result_contract(tmp_path):
    with service(tmp_path, fake_delay_seconds=0.01) as (client, store, scheduler, other):
        assert_schema("models", client.get("/api/v1/models").json())
        assert_schema("ready", client.get("/api/v1/ready").json())
        for image in (False, True):
            data = request(image)
            response = client.post("/api/v1/jobs", json=data)
            assert response.status_code == 200 and response.headers["content-type"].startswith("text/event-stream")
            assert response.headers["x-accel-buffering"] == "no"
            received = list(events(response))
            assert received[0][0] == "accepted" and received[-1][0] == "completed"
            job = received[0][1]["jobId"]
            result = received[-1][1]
            assert_schema("result", result)
            assert result["output"]["isComplete"]
            assert "".join(value["text"] for kind,value in received if kind == "delta") == result["output"]["text"]
            assert_schema("status", client.get("/api/v1/jobs/"+job).json())
            assert client.get("/api/v1/jobs/"+job+"/result").json() == result
            assert client.get("/api/v1/jobs/by-request/"+data["clientRequestId"]).json()["jobId"] == job
            assert client.post("/api/v1/jobs", json=data).json()["error"]["code"] == "REQUEST_ALREADY_EXISTS"
            changed = json.loads(json.dumps(data)); changed["messages"][0]["content"][0]["text"] += "x"
            assert client.post("/api/v1/jobs", json=changed).json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
            assert "dataBase64" not in json.dumps(result)
            assert "validation" not in result and "source" not in result
        assert scheduler.provider.calls == 2
        assert_schema("list", client.get("/api/v1/jobs").json())


def test_auth_owner_revoke_and_model(tmp_path):
    with service(tmp_path, fake_delay_seconds=0) as (client,store,scheduler,other):
        assert client.get("/api/v1/health", headers={"Authorization":""}).status_code == 200
        assert client.get("/api/v1/models", headers={"Authorization":""}).status_code == 401
        token = store.token_create("alice", now_ms()+60000)
        with store.connection(True) as conn:
            conn.execute("UPDATE api_keys SET expires_at_ms=? WHERE key_id=?", (now_ms()-1, token.split("_",2)[1]))
        assert client.get("/api/v1/ready", headers={"Authorization":"Bearer "+token}).status_code == 401
        token = store.token_create("alice"); store.token_revoke(token.split("_",2)[1])
        assert client.get("/api/v1/ready", headers={"Authorization":"Bearer "+token}).status_code == 401
        job = post(client, request()); terminal(client,job)
        for suffix in ("", "/result"):
            assert client.get("/api/v1/jobs/"+job+suffix, headers=other).status_code == 404
        assert client.post("/api/v1/jobs/"+job+"/cancel", headers=other).status_code == 404
        wrong = request(); wrong["model"] = "not-installed"
        assert client.post("/api/v1/jobs", json=wrong).json()["error"]["code"] == "MODEL_NOT_FOUND"


def test_transport_unknown_business_fields_and_parameters_rejected(tmp_path):
    with service(tmp_path, max_request_bytes=2000, fake_delay_seconds=0) as (client,store,scheduler,other):
        for body in ('{"x":1,"x":2}', '{"x":NaN}', '{"x":', '{"messages":"\\ud800"}'):
            assert client.post("/api/v1/jobs", content=body, headers={"Content-Type":"application/json"}).status_code == 400
        for field in ("operation", "context", "temperature", "max_tokens", "history"):
            data=request(); data[field]="not-supported"
            assert client.post("/api/v1/jobs", json=data).status_code == 400
        assert client.post("/api/v1/jobs", content="{}", headers={"Content-Type":"text/plain"}).status_code == 415
        assert client.post("/api/v1/jobs", content="{}", headers={"Content-Type":"application/json","Content-Encoding":"gzip"}).status_code == 415
        assert client.post("/api/v1/jobs", content=b"x"*2001, headers={"Content-Type":"application/json"}).status_code == 413
        assert scheduler.provider.calls == 0


def test_png_jpeg_four_images_and_identity(tmp_path):
    validator = RequestValidator(ROOT/"contracts/api/v1")
    data = request(True)
    content = data["messages"][0]["content"]
    buffer = BytesIO(); Image.new("RGB",(8,4),(12,34,56)).save(buffer, format="JPEG")
    jpeg = {"type":"image","mediaType":"image/jpeg","dataBase64":base64.b64encode(buffer.getvalue()).decode()}
    content.extend([{"type":"text","text":"中间文字"}, jpeg, content[1].copy(), jpeg.copy()])
    normalized, fingerprint, task = validator.validate(canonical(data).encode())
    assert len(task.images) == 4 and task.images[1].media_type == "image/jpeg"
    with service(tmp_path, fake_delay_seconds=0) as (client,store,scheduler,other):
        response = client.post("/api/v1/jobs", json=data)
        assert list(events(response))[-1][0] == "completed"
        changed = json.loads(json.dumps(data))
        changed["messages"][0]["content"][1],changed["messages"][0]["content"][3] = changed["messages"][0]["content"][3],changed["messages"][0]["content"][1]
        assert validator.validate(canonical(changed).encode())[1] != fingerprint
        too_many = json.loads(json.dumps(data)); too_many["messages"][0]["content"].append(jpeg)
        assert client.post("/api/v1/jobs", json=too_many).json()["error"]["code"] == "IMAGE_LIMIT_EXCEEDED"
        bad = request(True); bad["messages"][0]["content"][1]["mediaType"]="image/jpeg"
        assert client.post("/api/v1/jobs", json=bad).status_code == 415
        bad = request(True); bad["messages"][0]["content"][1]["dataBase64"]="bad"
        assert client.post("/api/v1/jobs", json=bad).status_code == 415


@pytest.mark.parametrize("scenario,code", [("truncated","OUTPUT_TRUNCATED"),
    ("failure","MODEL_UNAVAILABLE"),("protocol","MODEL_PROTOCOL_ERROR"),("connection","MODEL_UNAVAILABLE")])
def test_stream_failure_and_partial_output(tmp_path, scenario, code):
    with service(tmp_path, provider=FakeProvider(0,scenario)) as (client,store,scheduler,other):
        received=list(events(client.post("/api/v1/jobs",json=request())))
        assert received[-1][0] == "failed"
        assert received[-1][1]["error"]["code"] == code
        if scenario != "empty":
            assert received[-1][1]["output"]["isComplete"] is False
        assert scheduler.provider.calls == 1


def test_budget_unverified_and_small_limit(tmp_path):
    with service(tmp_path/"unverified", budget=UnverifiedBudget()) as (client,store,scheduler,other):
        assert client.get("/api/v1/ready").status_code == 503
        assert client.post("/api/v1/jobs",json=request()).status_code == 503
    with service(tmp_path/"small", budget=FakeBudget(1)) as (client,store,scheduler,other):
        assert client.post("/api/v1/jobs",json=request()).json()["error"]["code"] == "CONTEXT_TOO_LARGE"
