"""Local metadata snapshots, read-only history, and best-effort host sampling.

No management HTTP routes. Snapshot/metrics contain no prompts, output, or credentials.
"""
import asyncio
from collections import deque
from contextlib import closing
import ctypes
from datetime import datetime, timedelta
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time

import httpx

DAY_MS = 86400000
STATUSES = ("queued", "running", "cancelling", "succeeded", "failed", "cancelled")
PUBLIC_CHECK_INTERVAL = 60


def now_ms():
    return time.time_ns() // 1000000


def settings_id(settings):
    return hashlib.sha256(settings.model_dump_json().encode("utf-8")).hexdigest()


def performance_row(row, at=None):
    at = now_ms() if at is None else at
    def difference(end, start):
        return max(0, end-start) if end is not None and start is not None else None
    received, created, started, first, finished = (row.get(key) for key in (
        "received_at_ms", "created_at_ms", "started_at_ms", "first_output_at_ms", "finished_at_ms"))
    end = finished if finished is not None else at
    row.update(prepare_ms=difference(created,received),
               queue_ms=difference(started if started is not None else end,created),
               execution_ms=difference(end,started),
               first_output_ms=difference(first,received if received is not None else created),
               total_ms=difference(end,received if received is not None else created),
               total_origin="received" if received is not None else "accepted")
    duration, count = row.get("eval_ms"), row.get("output_tokens")
    row["tokens_per_second"] = count*1000/duration if duration and duration > 0 and count is not None else None
    return row


def query_history(database, filters=None):
    """Never initialize, migrate or create a database from the monitoring reader."""
    filters = filters or {}
    at = now_ms()
    clauses, values = ["(p.finished_at_ms IS NULL OR p.finished_at_ms>?)"], [at-7*DAY_MS]
    for key, column in (("user","user_id"),("status","status")):
        value = filters.get(key)
        if value:
            if key == "status" and value not in STATUSES:
                raise ValueError("任务状态无效。")
            clauses.append("p."+column+"=?"); values.append(value)
    for key, operator in (("from",">="),("to","<")):
        if filters.get(key):
            try:
                date = datetime.strptime(filters[key], "%Y-%m-%d")
            except (TypeError,ValueError):
                raise ValueError("日期须为 YYYY-MM-DD。") from None
            if key == "to": date += timedelta(days=1)
            clauses.append("p.created_at_ms"+operator+"?"); values.append(int(date.timestamp()*1000))
    if filters.get("from") and filters.get("to") and filters["from"] > filters["to"]:
        raise ValueError("开始日期不能晚于结束日期。")
    # This expression is shared by the slow filter, averages and percentile.
    elapsed = f"max(0,coalesce(p.finished_at_ms,{at})-coalesce(p.received_at_ms,p.created_at_ms))"
    if filters.get("slow"):
        clauses.append(elapsed+">20000")
    where = " AND ".join(clauses)
    page = max(1,int(filters.get("page",1)))
    database = Path(database).resolve()
    with closing(sqlite3.connect(database.as_uri()+"?mode=ro",uri=True,timeout=1)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        if conn.execute("PRAGMA user_version").fetchone()[0] not in (4, 5, 6):
            raise ValueError("监控历史需要schema v4至v6；请从正式入口完成升级。")
        conn.execute("BEGIN")  # All counts and the page refer to one read snapshot.
        summary = dict(conn.execute(f"""SELECT count(*) total,
            coalesce(sum(status='succeeded'),0) succeeded,coalesce(sum(status='failed'),0) failed,
            coalesce(sum(status='cancelled'),0) cancelled,
            avg(CASE WHEN status='succeeded' THEN {elapsed} END) average_ms
            FROM task_performance p WHERE {where}""",values).fetchone())
        pages = max(1,math.ceil(summary["total"]/50))
        page = min(page,pages)
        p95 = conn.execute(f"""SELECT {elapsed} FROM task_performance p
            WHERE {where} AND status='succeeded' ORDER BY {elapsed} LIMIT 1 OFFSET ?""",
            [*values,max(0,math.ceil(summary["succeeded"]*.95)-1)]).fetchone()
        summary["p95_ms"] = p95[0] if p95 else None
        rows = conn.execute(f"""SELECT p.*,u.display_name FROM task_performance p
            LEFT JOIN users u USING(user_id) WHERE {where}
            ORDER BY p.created_at_ms DESC,p.job_id DESC LIMIT 50 OFFSET ?""",[*values,(page-1)*50]).fetchall()
    return {"summary":summary,"items":[performance_row(dict(row),at) for row in rows],"page":page,"pages":pages}


class HostSampler:
    def __init__(self):
        self.previous_cpu = None
        self.smi = shutil.which("nvidia-smi.exe") or shutil.which("nvidia-smi")

    def sample(self):
        result = {"cpu_percent":None,"memory_used_bytes":None,"memory_total_bytes":None,
                  "gpus":[],"host_error":None,"gpu_error":None}
        try:
            if os.name != "nt":
                raise OSError("Windows host counters unavailable")
            from ctypes import wintypes
            kernel = ctypes.WinDLL("kernel32",use_last_error=True)
            idle, system, user = (wintypes.FILETIME() for _ in range(3))
            if not kernel.GetSystemTimes(ctypes.byref(idle),ctypes.byref(system),ctypes.byref(user)):
                raise OSError("CPU counters unavailable")
            def ticks(value): return value.dwHighDateTime*4294967296+value.dwLowDateTime
            current = ticks(idle),ticks(system)+ticks(user)
            if self.previous_cpu:
                idle_delta,total_delta = (value-old for value,old in zip(current,self.previous_cpu))
                if total_delta > 0: result["cpu_percent"] = min(100,max(0,100*(1-idle_delta/total_delta)))
            self.previous_cpu = current
            class Memory(ctypes.Structure):
                _fields_ = [("length",wintypes.DWORD),("load",wintypes.DWORD)]+[
                    (key,ctypes.c_ulonglong) for key in ("total","available","page_total","page_available","virtual_total","virtual_available","extended")]
            memory = Memory(); memory.length = ctypes.sizeof(memory)
            if not kernel.GlobalMemoryStatusEx(ctypes.byref(memory)): raise OSError("Memory unavailable")
            result.update(memory_used_bytes=memory.total-memory.available,memory_total_bytes=memory.total)
        except (OSError,AttributeError):
            result["host_error"] = "暂不可用"
        try:
            if not self.smi: raise FileNotFoundError()
            response = subprocess.run([self.smi,"--query-gpu=index,name,utilization.gpu,memory.used,memory.total",
                "--format=csv,noheader,nounits"],capture_output=True,text=True,encoding="utf-8",errors="replace",timeout=1,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name=="nt" else 0)
            if response.returncode: raise OSError("GPU counters unavailable")
            import csv
            for fields in csv.reader(response.stdout.splitlines(),skipinitialspace=True):
                if len(fields) != 5: raise ValueError()
                def number(value):
                    try:
                        parsed = float(value)
                        return parsed if math.isfinite(parsed) and parsed >= 0 else None
                    except ValueError: return None
                result["gpus"].append({"index":fields[0],"name":fields[1],"utilization_percent":number(fields[2]),
                    "memory_used_mib":number(fields[3]),"memory_total_mib":number(fields[4])})
            if not result["gpus"]: result["gpu_error"] = "暂不可用"
        except (OSError,subprocess.SubprocessError,ValueError):
            result["gpus"] = []; result["gpu_error"] = "暂不可用"
        return result


async def check_public(base_url, client=None):
    result = {"state":"unconfigured","checked_at_ms":None,"latency_ms":None,"reason":None}
    if not base_url: return result
    result["checked_at_ms"] = now_ms()
    start = time.monotonic()
    owned = client is None
    client = client or httpx.AsyncClient(trust_env=False,follow_redirects=False)
    try:
        async with asyncio.timeout(5):
            async with client.stream("GET",base_url.rstrip("/")+"/api/v1/health",timeout=5) as response:
                if response.status_code != 200:
                    result.update(state="unreachable",reason="HTTP_"+str(response.status_code))
                else:
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        body.extend(chunk[:4097-len(body)])
                        if len(body)>4096: break
                    try:
                        valid = len(body)<=4096 and json.loads(body).get("status")=="ok"
                    except (ValueError,AttributeError): valid = False
                    result.update(state="reachable" if valid else "unreachable",reason=None if valid else "INVALID_HEALTH_RESPONSE")
        result["latency_ms"] = round((time.monotonic()-start)*1000)
    except (TimeoutError,httpx.TimeoutException):
        result.update(state="unreachable",reason="TIMEOUT")
    except httpx.HTTPError:
        result.update(state="unreachable",reason="NETWORK_OR_TLS_ERROR")
    finally:
        if owned: await client.aclose()
    return result


class TaskReporter(logging.Handler):
    def __init__(self, monitor):
        super().__init__(); self.monitor = monitor

    def emit(self, record):
        if not getattr(record,"job_event",False): return
        message = record.getMessage()
        with self.monitor.event_lock:
            self.monitor.sequence += 1
            self.monitor.events.append({"sequence":self.monitor.sequence,"message":message})
        if self.monitor.report:
            try: self.monitor.report(message)
            except Exception: pass  # An unavailable console must not break a committed task.


class Monitor:
    def __init__(self, settings, store, scheduler):
        self.settings,self.store,self.scheduler = settings,store,scheduler
        self.report = None
        self.local_ready = False
        self.tunnel = None
        self.last_seen = {}
        self.public = {"state":"pending" if settings.public_base_url else "unconfigured",
                       "checked_at_ms":None,"latency_ms":None,"reason":None}
        self.events = deque(maxlen=100)
        self.event_lock = threading.Lock()
        self.sequence = 0
        self.handler = TaskReporter(self)
        self.sampler = HostSampler()
        self.task = self.public_task = None
        self.stopping = False
        self.identity = None
        self.io_task = None
        self.last_snapshot = None

    def seen(self, user):
        self.last_seen[user] = time.monotonic()
        if len(self.last_seen)>10000:
            oldest = min(self.last_seen,key=self.last_seen.get)
            self.last_seen.pop(oldest,None)

    def start(self):
        from aiserver.operations.engine import process_identity
        try: self.identity = process_identity(os.getpid())
        except OSError: pass
        self.task = asyncio.create_task(self.run(),name="local-monitor")
        self.public_task = asyncio.create_task(self.public_loop(),name="public-health-check")

    async def public_loop(self):
        last_state = (self.public["state"],None)
        while not self.stopping:
            if not self.local_ready:
                await asyncio.sleep(.2); continue
            try:
                self.public = await check_public(self.settings.public_base_url)
            except Exception:
                self.public = {"state":"unreachable","reason":"CHECK_UNAVAILABLE","checked_at_ms":now_ms(),"latency_ms":None}
            current = (self.public["state"],self.public["reason"])
            if current != last_state and self.report:
                try: self.report("[public] state="+current[0]+" reason="+str(current[1]))
                except Exception: pass
            last_state = current
            await asyncio.sleep(PUBLIC_CHECK_INTERVAL)

    def snapshot(self, resources, recent):
        # last_seen is accessed on the event-loop thread only.
        cutoff = time.monotonic()-300
        self.last_seen = {user:seen for user,seen in self.last_seen.items() if seen>cutoff}
        with self.event_lock: events = list(self.events)
        active = set(self.last_seen)
        generating,waiting = set(),set()
        for row in recent:
            active.add(row["user_id"])
            (waiting if row["status"]=="queued" else generating).add(row["user_id"])
        return {"version":1,"observed_at_ms":now_ms(),"pid":os.getpid(),"identity":self.identity,
            "settings_id":settings_id(self.settings),"scheduler":self.scheduler.state,
            "local_ready":self.local_ready and not self.stopping,
            "tunnel":self.tunnel.state if self.tunnel else "disabled","public":dict(self.public),
            "active_users":len(active),"generating_users":len(generating),"waiting_users":len(waiting),
            "waiting_tasks":sum(row["status"]=="queued" for row in recent),"active_tasks":len(recent),
            "active_accounts":sorted(active),"resources":resources,"events":events}

    def active_rows(self):
        with self.store.connection() as conn:
            return [dict(row) for row in conn.execute("SELECT user_id,status FROM task_performance WHERE finished_at_ms IS NULL")]

    def write(self, snapshot):
        path = self.settings.data_directory/"monitor-state.json"
        descriptor,temp = tempfile.mkstemp(prefix="monitor-",suffix=".tmp",dir=path.parent)
        try:
            with os.fdopen(descriptor,"w",encoding="utf-8") as stream:
                json.dump(snapshot,stream,ensure_ascii=False,allow_nan=False)
            os.replace(temp,path)
        finally:
            Path(temp).unlink(missing_ok=True)

    async def run(self):
        while not self.stopping:
            start = time.monotonic()
            try:
                async def read():
                    return await asyncio.to_thread(lambda:(self.sampler.sample(),self.active_rows()))
                self.io_task = asyncio.create_task(read())
                resources,rows = await asyncio.shield(self.io_task)
                self.last_snapshot = self.snapshot(resources,rows)
                self.io_task = asyncio.create_task(asyncio.to_thread(self.write,self.last_snapshot))
                await asyncio.shield(self.io_task)
            except Exception:
                # Readers mark an old snapshot stale; inference remains available.
                pass
            await asyncio.sleep(max(.05,2-(time.monotonic()-start)))

    async def close(self):
        self.stopping = True
        for task in (self.task,self.public_task):
            if task: task.cancel()
        await asyncio.gather(*(task for task in (self.task,self.public_task) if task),return_exceptions=True)
        if self.io_task: await asyncio.gather(self.io_task,return_exceptions=True)
        if self.last_snapshot:
            self.last_snapshot.update(local_ready=False,scheduler="stopping",observed_at_ms=now_ms())
            try: await asyncio.to_thread(self.write,self.last_snapshot)
            except Exception: pass


def read_snapshot(settings):
    from aiserver.operations.engine import process_identity
    try:
        value = json.loads((settings.data_directory/"monitor-state.json").read_text(encoding="utf-8"))
        fresh = value.get("version")==1 and value.get("settings_id")==settings_id(settings)
        fresh = fresh and 0<=now_ms()-value["observed_at_ms"]<=10000
        fresh = fresh and process_identity(value["pid"])==value["identity"]
        return {"fresh":bool(fresh),"snapshot":value if fresh else None}
    except (OSError,ValueError,KeyError,TypeError):
        return {"fresh":False,"snapshot":None}
