import asyncio
from dataclasses import replace
import json
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from aiserver.api.validation import canonical, RequestValidator
from aiserver.inference.fake import FakeProvider, fake_profile
from aiserver.settings import Settings
from aiserver.storage.db import Store, now_ms
from test_fake_api import service, request, post, terminal


def test_limits_queue_cancel_and_late_output(tmp_path):
    provider = FakeProvider(10, "late", 0.05)
    with service(tmp_path, provider=provider) as (client,store,scheduler,other):
        first=post(client,request()); client.portal.call(provider.started.wait)
        second=post(client,request())
        with pytest.raises(Exception) as denied: post(client,request())
        assert denied.value.code == "USER_JOB_LIMIT"
        assert client.get("/api/v1/jobs/"+second+"/result").status_code == 409
        assert client.post("/api/v1/jobs/"+second+"/cancel").json()["status"] == "cancelled"
        cancelled = client.post("/api/v1/jobs/"+first+"/cancel").json()
        assert cancelled["inferenceStopConfirmed"] is False
        terminal(client,first)
        original=client.get("/api/v1/jobs/"+first+"/result").json()["output"]["text"]
        time.sleep(0.15)
        assert client.get("/api/v1/jobs/"+first+"/result").json()["output"]["text"] == original
        assert provider.calls == 1 and provider.max_active == 1


def test_concurrent_idempotency_one_generation(tmp_path):
    with service(tmp_path, fake_delay_seconds=0.05) as (client,store,scheduler,other):
        data=request()
        with ThreadPoolExecutor(max_workers=8) as pool:
            jobs=list(pool.map(lambda _:post(client,data),range(8)))
        assert len(set(jobs)) == 1
        terminal(client,jobs[0])
        assert scheduler.provider.calls == 1


def test_timeout_queue_expiry_and_24h_cleanup(tmp_path):
    with service(tmp_path, provider=FakeProvider(10), execution_timeout_seconds=0.2, queue_timeout_seconds=0.08) as (client,store,scheduler,other):
        first=post(client,request()); client.portal.call(scheduler.provider.started.wait)
        second=post(client,request())
        assert terminal(client,first)["error"]["code"] == "EXECUTION_TIMEOUT"
        assert terminal(client,second)["error"]["code"] == "QUEUE_TIMEOUT"
        row=store.get("alice",job=first)
        assert row["payload_expires_at_ms"] - row["finished_at_ms"] == 86400000
        assert row["output_json"] is not None
        with store.connection(True) as c:
            c.execute("UPDATE jobs SET payload_expires_at_ms=? WHERE job_id=?",(now_ms()-1,first))
        assert client.get("/api/v1/jobs/"+first+"/result").status_code == 410
        store.cleanup()
        with store.connection() as c:
            assert c.execute("SELECT count(*) FROM output_chunks WHERE job_id=?",(first,)).fetchone()[0] == 0
        assert store.get("alice",job=first)["request_json"] is None
        assert store.get("alice",job=first)["request_hash"]


def test_recovery_holds_execution_slot(tmp_path):
    class Recoverable(FakeProvider):
        recovered=False
        async def recover(self):
            assert self.active == 0
            await asyncio.sleep(0.1)
            self.recovered=True
            return True
    provider=Recoverable(10)
    profile=replace(fake_profile(Settings(data_directory=tmp_path)), automatic_cleanup_verified=False)
    with service(tmp_path,provider=provider,profile=profile) as (client,store,scheduler,other):
        first=post(client,request()); client.portal.call(provider.started.wait)
        second=post(client,request())
        client.post("/api/v1/jobs/"+first+"/cancel")
        provider.delay=0
        assert terminal(client,second)["status"] == "succeeded"
        assert provider.recovered and provider.max_active == 1


def test_sqlite_failure_does_not_accept_or_generate(tmp_path):
    with service(tmp_path) as (client,store,scheduler,other):
        with store.connection(True):
            response=client.post("/api/v1/jobs",json=request())
            assert response.status_code == 503
        assert scheduler.provider.calls == 0


def test_v1_migration_backup_preserves_existing_rows(tmp_path):
    settings=Settings(data_directory=tmp_path)
    tmp_path.mkdir(exist_ok=True)
    path=tmp_path/"jobs.db"
    original=(__import__("pathlib").Path(__file__).parents[2]/"src/aiserver/storage/001_initial.sql").read_text()
    with sqlite3.connect(path) as c:
        c.executescript(original)
        c.execute("INSERT INTO users VALUES('old','Old',1,1)")
    store=Store(settings); store.initialize()
    with store.connection() as c:
        assert c.execute("PRAGMA user_version").fetchone()[0] == 6
        assert c.execute("SELECT display_name FROM users WHERE user_id='old'").fetchone()[0] == "Old"
    assert len(list((tmp_path/"backups").glob("schema-v1-*.db"))) == 1
    store.initialize()
    assert len(list((tmp_path/"backups").glob("schema-v1-*.db"))) == 1
