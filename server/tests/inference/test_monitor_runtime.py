import asyncio
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
from types import SimpleNamespace

import httpx
import pytest

from aiserver.operations import monitoring
from aiserver.operations.engine import ManagedEngine, process_identity
from aiserver.operations.diagnostics import StartupFailure
from aiserver.operations.processes import port_owners
from aiserver.operations.monitoring import Monitor, HostSampler, check_public, read_snapshot, now_ms
from aiserver.settings import ROOT, Settings
from aiserver.storage.db import Store


@pytest.mark.parametrize("kind,expected,reason",[
    ("ok","reachable",None),("http","unreachable","HTTP_503"),
    ("redirect","unreachable","HTTP_302"),("bad","unreachable","INVALID_HEALTH_RESPONSE"),
    ("large","unreachable","INVALID_HEALTH_RESPONSE"),("array","unreachable","INVALID_HEALTH_RESPONSE"),
    ("timeout","unreachable","TIMEOUT"),("tls","unreachable","NETWORK_OR_TLS_ERROR")])
def test_public_check_bounded_validated_and_credential_free(kind,expected,reason):
    async def run():
        async def handle(request):
            assert request.url=="https://public.example/api/v1/health"
            assert "authorization" not in request.headers and "cookie" not in request.headers
            if kind=="timeout": raise httpx.ReadTimeout("PRIVATE_SECRET_ERROR")
            if kind=="tls": raise httpx.ConnectError("PRIVATE_SECRET_CERTIFICATE")
            if kind=="http": return httpx.Response(503)
            if kind=="redirect": return httpx.Response(302,headers={"Location":"https://another.example"})
            if kind=="bad": return httpx.Response(200,content="not a health response")
            if kind=="large": return httpx.Response(200,content=b" "*4097+b'{"status":"ok"}')
            return httpx.Response(200,json=[] if kind=="array" else {"status":"ok"})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle),follow_redirects=False) as client:
            result=await check_public("https://public.example",client)
        assert result["state"]==expected and result["reason"]==reason
        assert result["checked_at_ms"] is not None
        assert "PRIVATE_SECRET" not in json.dumps(result)
        assert (await check_public(""))["state"]=="unconfigured"
    asyncio.run(run())


def test_public_loop_reports_only_state_changes(tmp_path,monkeypatch):
    async def run():
        settings=Settings(data_directory=tmp_path,public_base_url="https://public.example")
        monitor=Monitor(settings,Store(settings),SimpleNamespace(state="available"))
        monitor.local_ready=True; output=[]; monitor.report=output.append
        calls=[]
        async def check(url):
            calls.append(url)
            state="reachable" if len(calls)<3 else "unreachable"
            if len(calls)==3: monitor.stopping=True
            return {"state":state,"reason":None if state=="reachable" else "TIMEOUT","checked_at_ms":now_ms(),"latency_ms":1}
        monkeypatch.setattr(monitoring,"check_public",check)
        monkeypatch.setattr(monitoring,"PUBLIC_CHECK_INTERVAL",.001)
        await monitor.public_loop()
        assert len(calls)==3 and len(output)==2
    asyncio.run(run())


def test_resources_missing_timed_out_and_multi_gpu(tmp_path,monkeypatch):
    sampler=HostSampler(); sampler.smi=None
    assert sampler.sample()["gpu_error"]=="暂不可用"
    sampler.smi="fixture-smi"
    def timeout(*args,**kwargs):
        assert kwargs["timeout"]==1
        raise subprocess.TimeoutExpired("fixture",1)
    monkeypatch.setattr(subprocess,"run",timeout)
    assert sampler.sample()["gpus"]==[]
    monkeypatch.setattr(subprocess,"run",lambda *a,**kw:SimpleNamespace(returncode=0,stdout=
        "0, Fixture GPU, 60, 2000, 16000\n1, Other GPU, [N/A], 100, 8000\n"))
    result=sampler.sample()
    assert result["gpus"][0]["utilization_percent"]==60
    assert result["gpus"][1]["utilization_percent"] is None
    assert len(result["gpus"])==2 and result["gpu_error"] is None


def test_local_snapshot_identity_expiry_same_config_and_final_state(tmp_path,monkeypatch):
    async def run():
        settings=Settings(data_directory=tmp_path)
        store=Store(settings); store.initialize()
        monitor=Monitor(settings,store,SimpleNamespace(state="available"))
        monkeypatch.setattr(monitor.sampler,"sample",lambda:{"cpu_percent":1})
        monitor.local_ready=True; monitor.start()
        deadline=asyncio.get_running_loop().time()+3
        while not (tmp_path/"monitor-state.json").exists():
            assert asyncio.get_running_loop().time()<deadline
            await asyncio.sleep(.01)
        assert read_snapshot(settings)["fresh"]
        other=Settings(data_directory=tmp_path,port=18081)
        assert not read_snapshot(other)["fresh"]
        await monitor.close()
        value=read_snapshot(settings)["snapshot"]
        assert value["scheduler"]=="stopping" and not value["local_ready"]
        value["observed_at_ms"]-=10001
        monitor.write(value)
        assert not read_snapshot(settings)["fresh"]
        value["observed_at_ms"]=now_ms(); value["identity"]={"created":0,"executable":"unrelated"}
        monitor.write(value)
        assert not read_snapshot(settings)["fresh"]
    asyncio.run(run())


def test_monitor_capture_failure_does_not_escape_or_publish_false_freshness(tmp_path,monkeypatch):
    async def run():
        settings=Settings(data_directory=tmp_path); store=Store(settings); store.initialize()
        monitor=Monitor(settings,store,SimpleNamespace(state="available"))
        monkeypatch.setattr(monitor.sampler,"sample",lambda:(_ for _ in ()).throw(OSError("unavailable")))
        monitor.start()
        await asyncio.sleep(.05)
        assert not monitor.task.done() and not read_snapshot(settings)["fresh"]
        await monitor.close()
    asyncio.run(run())


@pytest.mark.parametrize("url",["http://example.test","https://name:password@example.test","https://example.test/path",
    "https://example.test?token=secret","https://example.test/#fragment","https://example.test:99999"])
def test_public_url_rejects_credentials_http_and_non_origin(url):
    with pytest.raises(ValueError): Settings(public_base_url=url)


def test_public_url_normalizes_and_legacy_config_keeps_empty_default():
    assert Settings().public_base_url==""
    assert Settings(public_base_url=" https://public.example/ ").public_base_url=="https://public.example"


def test_monitor_reader_never_creates_or_migrates_database(tmp_path):
    sys.path.insert(0,str(ROOT/"scripts/server"))
    from ManagerActions import perform
    config=tmp_path/"config.json"
    config.write_text(json.dumps({"provider":"Fake","data_directory":str(tmp_path/"absent")}))
    result=perform("monitor",{"config":str(config)})
    assert not result["fresh"] and result["history"] is None
    assert not (tmp_path/"absent").exists()


@pytest.mark.skipif(os.name!="nt",reason="Windows listener inventory")
def test_listener_pid_paths_and_external_ownership_are_read_only(tmp_path):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1",0)); listener.listen()
        port=listener.getsockname()[1]
        owners=port_owners(port)
        current=next(item for item in owners if item["pid"]==os.getpid())
        assert current["executable"]==process_identity(os.getpid())["executable"] and not current["owned"]
        manager=ManagedEngine({"ExecutablePath":sys.executable,"BaseUrl":f"http://127.0.0.1:{port}"},tmp_path/"logs")
        manager.lock_directory=tmp_path/"engine"; manager.record=manager.lock_directory/"process.json"
        try:
            with pytest.raises(StartupFailure) as failure: manager._launch()
            assert failure.value.diagnostic["code"]=="ENGINE_PORT_IN_USE"
            assert str(os.getpid()) in failure.value.diagnostic["message"]
            assert not manager.child
        finally: asyncio.run(manager.close())
        assert listener.fileno()>=0  # Unmanaged process/socket left untouched.


def test_exited_and_reused_engine_records_are_removed_without_termination(tmp_path,monkeypatch):
    from aiserver.operations import engine
    manager=ManagedEngine({"ExecutablePath":sys.executable},tmp_path/"logs")
    manager.record=tmp_path/"process.json"
    manager.record.write_text(json.dumps({"pid":42,"identity":{"executable":"old","created":1}}))
    monkeypatch.setattr(engine,"process_identity",lambda pid:(_ for _ in ()).throw(ProcessLookupError(pid)))
    manager._stop_recorded(); assert not manager.record.exists()
    manager.record.write_text(json.dumps({"pid":42,"identity":{"executable":"old","created":1}}))
    monkeypatch.setattr(engine,"process_identity",lambda pid:{"executable":"unrelated","created":2})
    monkeypatch.setattr(subprocess,"run",lambda *a,**kw:pytest.fail("Must not terminate reused PID"))
    manager._stop_recorded(); assert not manager.record.exists()


@pytest.mark.skipif(os.name!="nt",reason="Windows suspended launch order")
def test_engine_is_suspended_then_assigned_then_resumed(tmp_path,monkeypatch):
    import io
    from aiserver.operations import engine,processes
    events=[]
    class Child:
        pid=4242
        stdout=io.BytesIO(b"")
        def wait(self,timeout=None): return 0
    class Job:
        def __init__(self,child): events.append("assign")
        def terminate(self): events.append("terminate")
        def close(self): events.append("close")
    def launch(args,**kwargs):
        assert kwargs["creationflags"] & 0x4
        events.append("suspended"); return Child()
    def resume(child):
        assert manager.record.exists()
        events.append("resume")
    monkeypatch.setattr(engine.subprocess,"Popen",launch)
    monkeypatch.setattr(engine,"WindowsJob",Job)
    monkeypatch.setattr(engine,"process_identity",lambda pid:{"executable":sys.executable,"created":1})
    monkeypatch.setattr(processes,"resume_suspended",resume)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1",0)); port=probe.getsockname()[1]
    manager=ManagedEngine({"ExecutablePath":sys.executable,"BaseUrl":f"http://127.0.0.1:{port}",
        "ModelsPath":str(tmp_path),"NumCtx":1024,"KeepAlive":"5m"},tmp_path/"logs")
    manager.lock_directory=tmp_path/"engine"; manager.record=manager.lock_directory/"process.json"
    try:
        manager._launch()
        assert events[:3]==["suspended","assign","resume"]
    finally: asyncio.run(manager.close())
