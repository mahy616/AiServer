import hashlib
import json

from fastapi.testclient import TestClient
import httpx
import pytest

from aiserver.app import create_app
from aiserver.settings import ROOT, Settings
from aiserver.storage.db import Store
from test_fake_api import request, events, service, post, terminal


def real_app(tmp_path, scenario="success"):
    cfg=json.loads((ROOT/"config/inference.development.example.json").read_text())
    lock=json.loads((ROOT/"config/models/qwen3vl4b.lock.json").read_text())
    lock["template_sha256"]=hashlib.sha256(b"template").hexdigest()
    lock_file=tmp_path/"model-lock.json"; lock_file.write_text(json.dumps(lock))
    cfg["ModelLockPath"]=str(lock_file)
    cfg_file=tmp_path/"inference.json"; cfg_file.write_text(json.dumps(cfg))
    settings=Settings(provider="Ollama",environment="Production",manage_engine=False,
                      data_directory=tmp_path/"data",inference_config=cfg_file)
    app=create_app(settings)
    captured=[]
    async def handler(req):
        if req.url.path == "/api/version": return httpx.Response(200,json={"version":cfg["EngineVersion"]})
        if req.url.path == "/api/tags": return httpx.Response(200,json={"models":[lock["model"]]})
        if req.url.path == "/api/show":
            return httpx.Response(200,json={"details":{"quantization_level":cfg["ExpectedQuantization"]},
                                           "capabilities":["completion","vision"],"template":"template"})
        if req.url.path == "/api/generate":
            body=json.loads(req.content)
            assert body["prompt"]=="" and body["options"]["num_gpu"]==999
            assert body["options"]["num_ctx"]==cfg["NumCtx"]
            return httpx.Response(200,json={"done":True})
        if req.url.path == "/api/ps":
            return httpx.Response(200,json={"models":[{"name":cfg["Model"],"size":1000,
                "size_vram":1000,"context_length":cfg["NumCtx"]}]})
        captured.append(json.loads(req.content))
        if scenario == "context": return httpx.Response(413,json={"error":"context length"})
        frames=[{"model":cfg["Model"],"done":False,"message":{"content":"第一段🙂"}},
                {"model":cfg["Model"],"done":False,"message":{"content":"second"}},
                {"model":cfg["Model"],"done":True,"done_reason":"length" if scenario=="truncated" else "stop",
                 "eval_count":42,"prompt_eval_count":321,"message":{"content":""},
                 "total_duration":3500000000,"load_duration":500000000,
                 "prompt_eval_duration":1000000000,"eval_duration":2000000000}]
        if scenario == "eof": frames=frames[:2]
        return httpx.Response(200,content="\n".join(json.dumps(f,ensure_ascii=False) for f in frames).encode(),
                              headers={"Content-Type":"application/x-ndjson"})
    app.state.scheduler.provider.adapter.client=httpx.AsyncClient(base_url=cfg["BaseUrl"],transport=httpx.MockTransport(handler))
    store=app.state.store; store.initialize(); store.user_add("real","Fixture")
    return app,store.token_create("real"),captured,cfg


@pytest.mark.parametrize("scenario,status,code",[("success","succeeded",None),("context","failed","CONTEXT_TOO_LARGE"),
    ("truncated","failed","OUTPUT_TRUNCATED"),("eof","failed","MODEL_PROTOCOL_ERROR")])
def test_native_stream_roles_parameters_and_partial_failures(tmp_path,scenario,status,code):
    app,token,captured,cfg=real_app(tmp_path,scenario)
    with TestClient(app) as client:
        client.headers["Authorization"]="Bearer "+token
        data=request(True); data["model"]=cfg["Model"]
        data["messages"].insert(0,{"role":"system","content":[{"type":"text","text":"CUSTOM RULES"}]})
        received=list(events(client.post("/api/v1/jobs",json=data)))
        result=received[-1][1]
        assert result["status"] == status
        assert result["error"]["code"] == code if code else result["error"] is None
        assert captured[0]["stream"] is True and captured[0]["truncate"] is False and captured[0]["shift"] is False
        assert captured[0]["messages"][0] == {"role":"system","content":"CUSTOM RULES"}
        assert captured[0]["messages"][1]["content"].endswith("[img]")
        assert captured[0]["messages"][1]["images"][0] == data["messages"][1]["content"][1]["dataBase64"]
        assert captured[0]["options"]["num_predict"] == cfg["NumPredict"]
        assert captured[0]["options"]["num_ctx"] == cfg["NumCtx"]
        assert "SDK" not in json.dumps(captured[0]["messages"])
        if scenario == "success":
            assert result["generation"]["inputTokens"] == 321
            assert result["output"]["text"] == "第一段🙂second"
            from aiserver.operations.monitoring import query_history
            metrics=query_history(app.state.store.path)["items"][0]
            assert metrics["load_ms"]==500 and metrics["prompt_eval_ms"]==1000
            assert metrics["eval_ms"]==2000 and metrics["engine_total_ms"]==3500
            assert metrics["tokens_per_second"]==21
            long_input=request(); long_input["model"]=cfg["Model"]
            long_input["messages"][0]["content"][0]["text"]="中"*8000
            long_result=list(events(client.post("/api/v1/jobs",json=long_input)))[-1][1]
            assert long_result["status"] == "succeeded"
            assert captured[-1]["messages"][0]["content"] == "中"*8000
        if scenario == "eof":
            assert result["output"]["text"] == "第一段🙂second" and not result["output"]["isComplete"]
            assert client.get("/api/v1/ready").status_code == 503
        two=request(True); two["model"]=cfg["Model"]
        two["messages"][0]["content"].append(two["messages"][0]["content"][1].copy())
        assert client.post("/api/v1/jobs",json=two).json()["error"]["code"] == "IMAGE_LIMIT_EXCEEDED"
        if scenario != "eof":
            huge=request(); huge["model"]=cfg["Model"]; huge["messages"][0]["content"][0]["text"]="中"*(cfg["MaxInputTokens"]//2+1)
            assert client.post("/api/v1/jobs",json=huge).json()["error"]["code"] == "CONTEXT_TOO_LARGE"


def test_rate_limits_safe_logs_and_backup_restore(tmp_path):
    with service(tmp_path/"live",submissions_per_minute=1,fake_delay_seconds=0) as (client,store,scheduler,other):
        data=request(); data["messages"][0]["content"][0]["text"]="PRIVATE_PAYLOAD_MARKER"
        result=list(events(client.post("/api/v1/jobs",json=data)))[-1][1]
        assert client.post("/api/v1/jobs",json=request()).status_code == 429
        content=(tmp_path/"live/logs/service.log").read_text()
        assert "PRIVATE_PAYLOAD_MARKER" not in content and "Bearer" not in content
        assert "trace=" in content
    backup=tmp_path/"backup.db"; store.backup(backup)
    target=Store(Settings(data_directory=tmp_path/"restored")); target.restore(backup); target.initialize()
    assert target.get("alice",job=result["jobId"])["status"] == "succeeded"
