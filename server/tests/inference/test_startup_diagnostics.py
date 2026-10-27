import asyncio
import json
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys

from fastapi.testclient import TestClient
import pytest
import uvicorn

from aiserver.app import create_app
from aiserver.inference.fake import FakeProvider
from aiserver.operations.diagnostics import StartupFailure, diagnostic
from aiserver.operations.engine import ManagedEngine
from aiserver.operations.lock import InstanceLock
from aiserver.operations.service import serve
from aiserver.settings import ROOT, Settings


@pytest.mark.parametrize("cause,code",[("Engine executable missing","ENGINE_EXECUTABLE_MISSING"),
    ("Engine port is occupied by an unmanaged instance","ENGINE_PORT_IN_USE"),
    ("MODEL_IDENTITY_CHECK_FAILED","MODEL_IDENTITY_CHECK_FAILED")])
def test_uvicorn_lifespan_failure_keeps_startup_cause(tmp_path,cause,code):
    async def check():
        class Failing(FakeProvider):
            closed=False
            async def start(self): raise RuntimeError(cause)
            async def aclose(self): self.closed=True
        provider=Failing()
        settings=Settings(data_directory=tmp_path,manage_natapp=False)
        app=create_app(settings,provider=provider)
        server=uvicorn.Server(uvicorn.Config(app,log_level="critical",access_log=False,ws="none"))
        with pytest.raises(StartupFailure) as raised:
            await serve(settings,app=app,server=server,report=lambda text:None)
        problem=raised.value.diagnostic
        assert problem["code"]==code and problem["phase"]=="engine"
        assert provider.closed and not server.started
    asyncio.run(check())


def test_lock_failure_never_closes_other_owner_provider(tmp_path):
    class Owned(FakeProvider):
        closed=False
        async def aclose(self): self.closed=True
    provider=Owned()
    settings=Settings(data_directory=tmp_path)
    app=create_app(settings,provider=provider)
    lock=InstanceLock(tmp_path).acquire()
    try:
        with pytest.raises(RuntimeError,match="active instance"):
            with TestClient(app): pass
        assert not provider.closed
        assert app.state.startup_failure["code"]=="DATA_DIRECTORY_IN_USE"
    finally: lock.release()


def test_no_engine_ownership_never_cleans_recorded_tree(tmp_path,monkeypatch):
    manager=ManagedEngine({"ExecutablePath":str(tmp_path/"ollama.exe")},tmp_path/"logs")
    monkeypatch.setattr(manager,"_stop_recorded",lambda:pytest.fail("Non-owner must not stop another engine"))
    asyncio.run(manager.close())


def helper(action,config):
    return subprocess.run([sys.executable,"-X","utf8",str(ROOT/"scripts/server/ManagerActions.py"),action],
        input=json.dumps({"config":str(config)})+"\n",text=True,encoding="utf-8",capture_output=True,timeout=15,
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform=="win32" else 0)


def test_preflight_and_start_report_port_conflict_without_starting_model(tmp_path):
    with socket.socket() as occupied:
        occupied.bind(("127.0.0.1",0)); occupied.listen()
        config=tmp_path/"server.json"
        config.write_text(json.dumps({"provider":"Fake","data_directory":str(tmp_path/"data"),
                                      "port":occupied.getsockname()[1]}),encoding="utf-8")
        result=helper("startup-check",config)
        checks=json.loads(result.stdout)["result"]
        assert any(row["code"]=="API_PORT_IN_USE" and not row["ok"] for row in checks["checks"])
        assert not (tmp_path/"data/jobs.db").exists()
        started=helper("serve",config)
        assert started.returncode==1
        errors=[json.loads(line) for line in started.stdout.splitlines() if json.loads(line).get("event")=="error"]
        assert errors[-1]["code"]=="API_PORT_IN_USE"


def test_configuration_error_names_without_private_values(tmp_path):
    config=tmp_path/"bad.json"
    config.write_text(json.dumps({"provider":"Fake","port":"PRIVATE_SECRET_IN_INVALID_FIELD"}))
    result=helper("serve",config)
    assert result.returncode==1
    assert "PRIVATE_SECRET" not in result.stdout+result.stderr
    assert "CONFIG_FIELDS_INVALID" in result.stdout and "port" in result.stdout
    value=diagnostic(PermissionError("PRIVATE_SECRET"),"storage")
    assert "PRIVATE_SECRET" not in json.dumps(value)


def test_storage_startup_failure_survives_uvicorn_catch(tmp_path):
    data=tmp_path/"data"; data.mkdir()
    with sqlite3.connect(data/"jobs.db") as current: current.execute("PRAGMA user_version=99")
    config=tmp_path/"server.json"
    with socket.socket() as probe:
        probe.bind(("127.0.0.1",0)); port=probe.getsockname()[1]
    config.write_text(json.dumps({"provider":"Fake","manage_natapp":False,"data_directory":str(data),"port":port}))
    result=helper("serve",config)
    assert result.returncode==1
    errors=[json.loads(line) for line in result.stdout.splitlines() if json.loads(line).get("event")=="error"]
    assert errors[-1]["code"]=="STORAGE_SCHEMA_UNSUPPORTED"
    assert "数据库初始化" in errors[-1]["stage"]
