import asyncio
from datetime import datetime
import json
import sqlite3
import time

import pytest

from aiserver.inference.contracts import Generation
from aiserver.inference.fake import FakeProvider
from aiserver.operations.monitoring import query_history, DAY_MS
from aiserver.settings import Settings
from aiserver.storage.db import Store, now_ms
from test_fake_api import service, request, events, post, terminal


def test_request_metrics_first_output_replay_presence_and_private_logs(tmp_path):
    class Timed(FakeProvider):
        stamps = []
        async def generate(self,prepared,stop,on_delta):
            for text in ("answer ","part ","three"):
                await asyncio.sleep(.025)
                await on_delta(text)
                with self.store.connection() as conn:
                    self.stamps.append(conn.execute("SELECT first_output_at_ms FROM task_performance").fetchone()[0])
            return Generation("answer part three",True,"stop",10,20,
                {"total_duration":2000,"load_duration":100,"prompt_eval_duration":200,"eval_duration":1000})
    provider=Timed()
    with service(tmp_path,provider=provider) as (client,store,scheduler,other):
        provider.store=store
        output=[]; monitor=client.app.state.monitor; monitor.report=output.append
        data=request(); data["messages"][0]["content"][0]["text"]="PRIVATE_MONITOR_INPUT"
        received=list(events(client.post("/api/v1/jobs",json=data)))
        assert received[-1][1]["status"]=="succeeded"
        assert client.post("/api/v1/jobs",json=data).status_code==409
        assert client.get("/api/v1/ready",headers=other).status_code==200
        result=query_history(store.path)
        row=result["items"][0]
        assert result["summary"]["total"]==1 and result["summary"]["succeeded"]==1
        assert row["received_at_ms"]<=row["created_at_ms"]<=row["started_at_ms"]<=row["first_output_at_ms"]<=row["finished_at_ms"]
        assert row["total_ms"]==row["prepare_ms"]+row["queue_ms"]+row["execution_ms"]
        assert row["tokens_per_second"]==20 and row["load_ms"]==100
        assert provider.stamps[0] is not None and len(set(provider.stamps))==1
        snapshot=client.portal.call(lambda:monitor.snapshot({},monitor.active_rows()))
        assert snapshot["active_users"]==2 and snapshot["active_tasks"]==0
        def expire():
            monitor.last_seen={"alice":time.monotonic()-301,"bob":time.monotonic()-301}
            return monitor.snapshot({},[])
        assert client.portal.call(expire)["active_users"]==0
        assert len(output)==3  # queued, running, succeeded; no replay event.
        text=json.dumps(result)+json.dumps(snapshot)+"".join(output)+(tmp_path/"logs/service.log").read_text(encoding="utf-8")
        assert "PRIVATE_MONITOR_INPUT" not in text and "answer part three" not in text and "Bearer" not in text


def test_cancel_timeout_and_active_task_users_survive_presence_timeout(tmp_path):
    with service(tmp_path,fake_delay_seconds=5,execution_timeout_seconds=.3) as (client,store,scheduler,other):
        first=post(client,request())
        second=post(client,request())
        monitor=client.app.state.monitor
        def snapshot():
            monitor.last_seen={}
            return monitor.snapshot({},monitor.active_rows())
        current=client.portal.call(snapshot)
        assert current["active_users"]==1 and current["active_tasks"]==2
        assert current["waiting_users"]==1
        assert client.post("/api/v1/jobs/"+second+"/cancel").status_code==200
        assert terminal(client,first)["error"]["code"]=="EXECUTION_TIMEOUT"
        result=query_history(store.path)
        assert result["summary"]["cancelled"]==1 and result["summary"]["failed"]==1
        assert result["summary"]["average_ms"] is None and result["summary"]["p95_ms"] is None
        assert all(row["tokens_per_second"] is None for row in result["items"])
        assert next(row for row in result["items"] if row["job_id"]==second)["first_output_ms"] is None
        assert next(row for row in result["items"] if row["job_id"]==first)["first_output_ms"] is not None


@pytest.mark.parametrize("scenario",["failure","truncated","protocol","connection"])
def test_failed_task_metrics_are_queryable(tmp_path,scenario):
    with service(tmp_path,fake_delay_seconds=0,fake_scenario=scenario) as (client,store,scheduler,other):
        response=list(events(client.post("/api/v1/jobs",json=request())))[-1][1]
        result=query_history(store.path,{"status":"failed"})
        assert response["status"]=="failed" and result["summary"]["failed"]==1
        assert result["items"][0]["error_code"]==response["error"]["code"]


def test_v3_backup_backfill_and_seven_day_retention_independent_of_payload(tmp_path):
    with service(tmp_path) as (client,store,scheduler,other):
        response=list(events(client.post("/api/v1/jobs",json=request())))[-1][1]
    job=response["jobId"]
    with sqlite3.connect(store.path) as conn:
        conn.execute("ALTER TABLE users ADD COLUMN password_hash TEXT")
        conn.execute("DROP TABLE task_performance")
        conn.execute("ALTER TABLE output_chunks DROP COLUMN channel")
        conn.execute("PRAGMA user_version=3")
    store.initialize()
    backups=list((tmp_path/"backups").glob("schema-v3-*.db"))
    assert len(backups)==1
    with sqlite3.connect(backups[0]) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0]==3
        assert conn.execute("SELECT count(*) FROM api_keys").fetchone()[0]==2
    row=query_history(store.path)["items"][0]
    assert row["total_origin"]=="accepted" and row["prepare_ms"] is None and row["first_output_ms"] is None
    assert row["input_tokens"]==response["generation"]["inputTokens"]
    with store.connection(True) as conn:
        conn.execute("UPDATE jobs SET payload_expires_at_ms=?,tombstone_expires_at_ms=?",(now_ms()-1,now_ms()-1))
        conn.execute("UPDATE task_performance SET finished_at_ms=?",(now_ms()-2*DAY_MS,))
    store.cleanup()
    assert query_history(store.path)["summary"]["total"]==1
    with store.connection() as conn:
        assert conn.execute("SELECT count(*) FROM jobs").fetchone()[0]==0
        assert conn.execute("SELECT count(*) FROM job_payloads").fetchone()[0]==0
    with store.connection(True) as conn:
        conn.execute("UPDATE task_performance SET finished_at_ms=?",(now_ms()-8*DAY_MS,))
    assert query_history(store.path)["summary"]["total"]==0  # hidden even before physical cleanup.
    store.cleanup()
    with store.connection() as conn:
        assert conn.execute("SELECT count(*) FROM task_performance").fetchone()[0]==0
    target=Store(Settings(data_directory=tmp_path/"restored"))
    backup=tmp_path/"v4.db"; store.backup(backup); target.restore(backup); target.initialize()
    assert not target.needs_migration()


def test_history_filters_pagination_average_and_p95(tmp_path):
    store=Store(Settings(data_directory=tmp_path)); store.initialize()
    created=now_ms()-DAY_MS
    with store.connection(True) as conn:
        for index in range(1,54):
            conn.execute("""INSERT INTO task_performance(job_id,user_id,status,received_at_ms,created_at_ms,started_at_ms,finished_at_ms)
                VALUES(?,?,'succeeded',?,?,?,?)""",(str(index),"even" if index%2==0 else "odd",created,created,created,created+index*1000))
    result=query_history(store.path)
    assert result["summary"]["average_ms"]==27000 and result["summary"]["p95_ms"]==51000
    assert result["pages"]==2 and len(result["items"])==50
    assert len(query_history(store.path,{"page":2})["items"])==3
    assert query_history(store.path,{"slow":True})["summary"]["total"]==33
    assert query_history(store.path,{"user":"even"})["summary"]["total"]==26
    date=datetime.fromtimestamp(created/1000).strftime("%Y-%m-%d")
    assert query_history(store.path,{"from":date,"to":date})["summary"]["total"]==53
    for filters in ({"from":"2026-99-99"},{"status":"unsafe' SQL"},{"from":"2026-10-24","to":"2026-10-23"}):
        with pytest.raises(ValueError): query_history(store.path,filters)


def test_rolled_back_lifecycle_does_not_publish_events(tmp_path):
    with service(tmp_path,fake_delay_seconds=0) as (client,store,scheduler,other):
        result=list(events(client.post("/api/v1/jobs",json=request())))[-1][1]
        output=[]; client.app.state.monitor.report=output.append
        with pytest.raises(RuntimeError):
            with store.connection(True) as conn:
                conn.execute("UPDATE jobs SET status='failed' WHERE job_id=?",(result["jobId"],))
                store.event(conn,result["jobId"],"failed")
                raise RuntimeError("rollback")
        assert output==[]
        assert query_history(store.path)["items"][0]["status"]=="succeeded"
