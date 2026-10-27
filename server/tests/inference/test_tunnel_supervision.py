import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace

import httpx
import pytest

from aiserver.operations.engine import WindowsJob, process_identity
from aiserver.operations.processes import matching_processes, resume_suspended
from aiserver.operations.service import serve
from aiserver.operations.tunnel import ManagedTunnel
from aiserver.settings import Settings


def configured(tmp_path):
    exe = tmp_path/"natapp.exe"; exe.write_bytes(b"synthetic-not-executable")
    ini = tmp_path/"private.ini"; ini.write_text("[default]\nauthtoken=SYNTHETIC_ONLY\nlog=none\n")
    return Settings(data_directory=tmp_path/"data", natapp_executable=exe, natapp_config=ini)


def test_configuration_errors_do_not_echo_ini(tmp_path):
    settings = configured(tmp_path)
    manager = ManagedTunnel(settings)
    manager.validate()
    settings.natapp_config.write_text("[default]\nauthtoken=SYNTHETIC_ONLY\nlog=stdout\n")
    with pytest.raises(RuntimeError,match="NATAPP_LOG_MUST_BE_NONE"):
        manager.validate()
    settings.natapp_config.write_text("broken=SYNTHETIC_ONLY")
    with pytest.raises(RuntimeError,match="NATAPP_CONFIG_INVALID") as error:
        manager.validate()
    assert "SYNTHETIC_ONLY" not in str(error.value)


def test_hidden_launch_assigns_job_before_resume_and_uses_config_path(tmp_path,monkeypatch):
    from aiserver.operations import tunnel
    settings = configured(tmp_path)
    order, arguments, report = [], [], []
    class Child:
        pid=123456; returncode=None
        def poll(self): return self.returncode
        def wait(self,timeout): self.returncode=-1
        def terminate(self): self.returncode=-1
    child = Child()
    def launch(args, **kwargs):
        order.append("launch"); arguments.append((args,kwargs)); return child
    class Job:
        def __init__(self,process): order.append("assign")
        def terminate(self): order.append("terminate")
        def close(self): order.append("close")
    monkeypatch.setattr(tunnel.subprocess,"Popen",launch)
    monkeypatch.setattr(tunnel,"WindowsJob",Job)
    monkeypatch.setattr(tunnel,"matching_processes",lambda names:[])
    monkeypatch.setattr(tunnel,"process_identity",lambda pid:{"executable":str(settings.natapp_executable),"created":1})
    monkeypatch.setattr(tunnel,"resume_suspended",lambda proc:order.append("resume"))
    manager=ManagedTunnel(settings,report.append)
    async def check():
        await manager.start()
        assert order[:3] == ["launch","assign","resume"]
        assert arguments[0][0] == [str(settings.natapp_executable),"-config="+str(settings.natapp_config)]
        assert "SYNTHETIC_ONLY" not in json.dumps(arguments[0][0])
        flags = arguments[0][1]["creationflags"]
        assert flags & subprocess.CREATE_NO_WINDOW and flags & 0x4
        assert "public endpoint health is checked separately" in report[0]
        await manager.close()
        assert not manager.record.exists() and manager.lock is None
    asyncio.run(check())


def test_unmanaged_instance_and_failed_start_cleanup_never_kill_other_owner(tmp_path,monkeypatch):
    from aiserver.operations import tunnel
    manager=ManagedTunnel(configured(tmp_path))
    manager.directory.mkdir(parents=True)
    manager.record.write_text(json.dumps({"pid":987,"identity":{"created":1,"executable":"old"}}))
    monkeypatch.setattr(tunnel,"process_identity",lambda pid: {"created":2,"executable":"unrelated"})
    monkeypatch.setattr(tunnel,"matching_processes",lambda names:[987])
    monkeypatch.setattr(tunnel.subprocess,"run",lambda *args,**kwargs:pytest.fail("Must not kill unknown PID"))
    monkeypatch.setattr(tunnel.subprocess,"Popen",lambda *args,**kwargs:pytest.fail("Must not launch competing tunnel"))
    async def check():
        # Close without a lock must not even inspect another instance's record.
        await manager.close()
        assert manager.record.exists()
        with pytest.raises(RuntimeError,match="NATAPP_UNMANAGED_INSTANCE"):
            await manager.start()
        assert not manager.record.exists()  # Reused PID marker discarded; process left alone.
        await manager.close()
    asyncio.run(check())


def test_tunnel_retry_keeps_api_separate_and_respects_stop(tmp_path,monkeypatch):
    settings=configured(tmp_path)
    settings.natapp_restart_initial_seconds=0.01
    manager=ManagedTunnel(settings,lambda text:None)
    manager.child=SimpleNamespace(poll=lambda:1)
    calls=[]
    async def check():
        stopping=asyncio.Event()
        async def restart():
            calls.append("restart"); stopping.set()
        monkeypatch.setattr(manager,"start",restart)
        await asyncio.wait_for(manager.watch(stopping),2)
        assert calls == ["restart"]
    asyncio.run(check())


def test_service_waits_for_http_model_readiness_and_stops_owned_tunnel(tmp_path,monkeypatch):
    from aiserver.operations import service as service_module
    settings=Settings(data_directory=tmp_path,manage_natapp=True)
    order=[]
    async def check():
        launched=asyncio.Event()
        class Scheduler:
            stopping=False
            wake=asyncio.Event()
            async def can_accept(self): return True,None
        app=SimpleNamespace(state=SimpleNamespace(scheduler=Scheduler()))
        class Server:
            started=False; should_exit=False
            async def serve(self):
                order.append("api"); self.started=True
                while not self.should_exit: await asyncio.sleep(0.01)
        server=Server()
        class Tunnel:
            def __init__(self,*args): pass
            def validate(self): order.append("validate")
            async def start(self):
                assert server.started
                order.append("tunnel"); launched.set()
            async def watch(self,stopping): await stopping.wait()
            async def close(self): order.append("close")
        original=httpx.AsyncClient
        monkeypatch.setattr(service_module.httpx,"AsyncClient",lambda **kwargs:original(
            transport=httpx.MockTransport(lambda req:httpx.Response(200,json={"status":"ok"})),**kwargs))
        task=asyncio.create_task(serve(settings,app=app,server=server,tunnel_factory=Tunnel,report=lambda msg:None))
        await asyncio.wait_for(launched.wait(),2)
        server.should_exit=True
        await asyncio.wait_for(task,2)
        assert order[:3] == ["validate","api","tunnel"]
        assert "close" in order and app.state.scheduler.stopping
    asyncio.run(check())


@pytest.mark.skipif(os.name != "nt",reason="Windows process primitives")
def test_suspended_process_and_descendant_die_when_job_handle_closes(tmp_path):
    marker=tmp_path/"child.pid"
    code = ("import pathlib,subprocess,sys,time; "
            "c=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'],creationflags=subprocess.CREATE_NO_WINDOW); "
            "pathlib.Path(sys.argv[1]).write_text(str(c.pid)); time.sleep(60)")
    process=subprocess.Popen([sys.executable,"-c",code,str(marker)],
                             creationflags=subprocess.CREATE_NO_WINDOW | 0x4)
    job=None
    try:
        assert not marker.exists()
        job=WindowsJob(process)
        resume_suspended(process)
        until=time.monotonic()+5
        while not marker.exists() and time.monotonic()<until: time.sleep(0.02)
        assert marker.exists()
        child=int(marker.read_text())
        assert child in matching_processes({Path(sys.executable).name})
        job.close()  # Simulates owner handle closure, not process-name termination.
        process.wait(timeout=5)
        until=time.monotonic()+5
        while time.monotonic()<until:
            try: process_identity(child)
            except ProcessLookupError: break
            time.sleep(0.02)
        else: pytest.fail("Owned descendant survived Job closure")
    finally:
        if job: job.close()
        if process.poll() is None:
            process.terminate(); process.wait(timeout=5)
