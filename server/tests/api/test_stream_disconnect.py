"""Real loopback sockets with Fake only; never start Ollama or use public URLs."""
import asyncio
from contextlib import contextmanager
import socket
import threading
import time

import httpx
import uvicorn

from aiserver.app import create_app
from aiserver.api.streaming import InferenceStream, event
from aiserver.inference.fake import FakeProvider
from aiserver.settings import Settings
from test_fake_api import request, events


@contextmanager
def live(tmp_path):
    provider = FakeProvider(30)
    app = create_app(Settings(data_directory=tmp_path, requests_per_minute=10000,
                              submissions_per_minute=1000), provider=provider)
    store = app.state.store
    store.initialize(); store.user_add("alice", "Alice")
    token = store.token_create("alice")
    listener = socket.socket(); listener.bind(("127.0.0.1", 0)); listener.listen()
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, access_log=False, log_level="critical", proxy_headers=False))
    thread = threading.Thread(target=server.run, kwargs={"sockets":[listener]}, daemon=True)
    thread.start()
    until = time.monotonic()+5
    while not server.started and time.monotonic() < until:
        time.sleep(0.01)
    assert server.started
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", trust_env=False,
                          headers={"Authorization":"Bearer "+token}, timeout=5) as client:
            yield client, store, provider
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        listener.close()
        assert not thread.is_alive()


def wait_state(store, job, expected):
    until = time.monotonic()+5
    while time.monotonic() < until:
        value = store.get("alice", job=job)
        if value["status"] == expected:
            return value
        time.sleep(0.02)
    raise AssertionError("Missing terminal state")


def test_incremental_delivery_disconnect_partial_and_next_job(tmp_path):
    with live(tmp_path) as (client,store,provider):
        data = request()
        started = time.monotonic()
        with client.stream("POST", "/api/v1/jobs", json=data) as response:
            received = events(response)
            kind, accepted = next(received)
            assert kind == "accepted"
            job = accepted["jobId"]
            partial = next(value for kind,value in received if kind == "delta")
            assert partial["text"] and time.monotonic()-started < 3
            assert store.get("alice",job=job)["status"] == "running"
            assert client.post("/api/v1/jobs",json=data).json()["error"]["code"] == "REQUEST_ALREADY_EXISTS"
        wait_state(store,job,"cancelled")
        result = client.get("/api/v1/jobs/"+job+"/result").json()
        assert result["stopReason"] == "client_disconnected"
        assert result["output"]["text"] == partial["text"] and not result["output"]["isComplete"]
        until = time.monotonic()+5
        while provider.active and time.monotonic() < until:
            time.sleep(0.01)
        provider.delay = 0
        followup = list(events(client.post("/api/v1/jobs",json=request())))
        assert followup[-1][0] == "completed"
        assert provider.calls == 2 and provider.max_active == 1


def test_disconnect_while_queued_and_explicit_cancel(tmp_path):
    with live(tmp_path) as (client,store,provider):
        with client.stream("POST","/api/v1/jobs",json=request()) as first:
            first_events = events(first)
            job = next(first_events)[1]["jobId"]
            next(value for kind,value in first_events if kind == "delta")
            with client.stream("POST","/api/v1/jobs",json=request()) as second:
                second_events = events(second)
                waiting = next(second_events)[1]["jobId"]
                status = next(value for kind,value in second_events if kind == "status")
                assert status["status"] == "queued" and status["queue"]["activeAhead"] == 1
            wait_state(store,waiting,"cancelled")
            cancel = client.post("/api/v1/jobs/"+job+"/cancel")
            assert cancel.status_code == 202
            assert list(first_events)[-1][0] == "cancelled"
        assert provider.calls == 1


def test_slow_network_send_timeout_runs_cleanup():
    async def check():
        closed = asyncio.Event()
        async def body():
            yield event("accepted", {})
        async def send(message):
            if message["type"] == "http.response.body":
                await asyncio.Event().wait()
        async def receive():
            await asyncio.Event().wait()
        async def close():
            closed.set()
        response = InferenceStream(body(), close, 0.02)
        await asyncio.wait_for(response({}, receive, send), 1)
        assert closed.is_set()
    asyncio.run(check())
