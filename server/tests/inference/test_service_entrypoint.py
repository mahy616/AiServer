"""Actual local HTTP through the unified runner, with Fake model/tunnel only."""
import asyncio
import socket

import httpx
import uvicorn

from aiserver.app import create_app
from aiserver.operations.service import serve
from aiserver.settings import Settings


def test_unified_runner_local_key_inference_and_graceful_stop(tmp_path):
    async def check():
        with socket.socket() as probe:
            probe.bind(("127.0.0.1",0))
            port = probe.getsockname()[1]
        settings = Settings(data_directory=tmp_path, port=port, manage_natapp=True, fake_delay_seconds=0)
        app = create_app(settings)
        app.state.store.initialize()
        app.state.store.user_add("engineer","Engineer")
        key = app.state.store.token_create("engineer")
        ready, closed = asyncio.Event(), asyncio.Event()
        class Tunnel:
            state="running"
            def __init__(self,*args): pass
            def validate(self): pass
            async def start(self): ready.set()
            async def watch(self,stopping): await stopping.wait()
            async def close(self): closed.set()
        server = uvicorn.Server(uvicorn.Config(app,host="127.0.0.1",port=port,
                                log_level="critical",access_log=False,proxy_headers=False,ws="none"))
        task = asyncio.create_task(serve(settings,app=app,server=server,tunnel_factory=Tunnel,report=lambda value:None))
        try:
            await asyncio.wait_for(ready.wait(),5)
            async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}",trust_env=False) as client:
                client.headers["Authorization"] = "Bearer " + key
                assert (await client.get("/api/v1/auth/me")).json()["user"]["userId"] == "engineer"
                answer = await client.post("/api/v1/jobs",json={
                    "clientRequestId":"416417b5-e936-4cde-b0df-969281936318", "model":"fake-v1",
                    "messages":[{"role":"user","content":[{"type":"text","text":"synthetic input"}]}]})
                assert "event: completed" in answer.text
                app.state.store.token_revoke(key.split("_")[1])
                assert (await client.get("/api/v1/auth/me")).status_code == 401
        finally:
            server.should_exit=True
            await asyncio.wait_for(task,10)
        assert closed.is_set() and app.state.scheduler.stopping
        assert app.state.scheduler.provider.active == 0
    asyncio.run(check())
