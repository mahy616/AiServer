from contextlib import contextmanager, closing
import hashlib
import hmac
import json
import logging
from pathlib import Path
import secrets
import sqlite3
import time
from uuid import uuid4

from aiserver.api.errors import ApiError
from aiserver.api.validation import canonical

TERMINAL = ("succeeded", "failed", "cancelled")
ACTIVE_SQL = "('queued','running','cancelling')"


class StoreConnection(sqlite3.Connection):
    """Publish safe lifecycle summaries only after their transaction commits."""
    pass

def now_ms():
    return time.time_ns() // 1_000_000

class Store:
    def __init__(self, settings):
        self.settings = settings
        self.path = settings.data_directory / "jobs.db"

    @contextmanager
    def connection(self, write=False):
        conn = sqlite3.connect(self.path, timeout=3, isolation_level=None, factory=StoreConnection)
        conn.task_events = []
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA synchronous=FULL")
            if write:
                conn.execute("BEGIN IMMEDIATE")
            yield conn
            if write:
                conn.commit()
                logger = logging.getLogger("aiserver." + str(self.settings.data_directory))
                for metadata in conn.task_events:
                    logger.info("task %s", canonical(metadata), extra={"job_event":True})
        except BaseException:
            if write:
                conn.rollback()
            raise
        finally:
            conn.close()

    def initialize(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as c:
            if c.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise RuntimeError("Storage integrity check failed")
            version = c.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1, 2, 3, 4, 5, 6):
                raise RuntimeError("Unsupported schema")
            c.execute("PRAGMA journal_mode=WAL")
            if version in (1, 2, 3, 4, 5):
                destination = self.path.parent / "backups" / (f"schema-v{version}-" + str(now_ms()) + "-" + uuid4().hex[:8] + ".db")
                self.backup(destination)
            if version == 0:
                # Initial migration only; future migrations must back up first.
                sql = Path(__file__).with_name("001_initial.sql").read_text(encoding="utf-8")
                c.executescript("BEGIN IMMEDIATE;\n" + sql + "\nCOMMIT;")
            if version in (0, 1):
                sql = Path(__file__).with_name("002_output_chunks.sql").read_text(encoding="utf-8")
                c.executescript("BEGIN IMMEDIATE;\n" + sql + "\nCOMMIT;")
            if version in (0, 1, 2):
                sql = Path(__file__).with_name("003_password_auth.sql").read_text(encoding="utf-8")
                c.executescript("BEGIN IMMEDIATE;\n" + sql + "\nCOMMIT;")
            if version in (0, 1, 2, 3):
                sql = Path(__file__).with_name("004_monitoring.sql").read_text(encoding="utf-8")
                c.executescript("BEGIN IMMEDIATE;\n" + sql + "\nCOMMIT;")
            if version in (0, 1, 2, 3, 4):
                sql = Path(__file__).with_name("005_api_keys_only.sql").read_text(encoding="utf-8")
                c.executescript("BEGIN IMMEDIATE;\n" + sql + "\nCOMMIT;")
            if version in (0, 1, 2, 3, 4, 5):
                sql = Path(__file__).with_name("006_output_channels.sql").read_text(encoding="utf-8")
                c.executescript("BEGIN IMMEDIATE;\n" + sql + "\nCOMMIT;")

    def needs_migration(self):
        if not self.path.exists():
            return True
        with self.connection() as c:
            return c.execute("PRAGMA user_version").fetchone()[0] != 6

    def event(self, c, job, kind, reason=None):
        c.execute("INSERT INTO job_events(job_id,at_ms,event_type,reason) VALUES(?,?,?,?)",
                  (job, now_ms(), kind, reason))
        c.execute("""UPDATE task_performance SET
            status=(SELECT status FROM jobs WHERE job_id=?),
            error_code=(SELECT error_code FROM jobs WHERE job_id=?),
            started_at_ms=(SELECT started_at_ms FROM jobs WHERE job_id=?),
            finished_at_ms=(SELECT finished_at_ms FROM jobs WHERE job_id=?) WHERE job_id=?""",
            (job,job,job,job,job))
        from aiserver.operations.monitoring import performance_row
        row = c.execute("SELECT * FROM task_performance WHERE job_id=?", (job,)).fetchone()
        if row:
            item = performance_row(dict(row))
            c.task_events.append({key:item[key] for key in ("job_id","user_id","status","error_code",
                "prepare_ms","queue_ms","execution_ms","first_output_ms","total_ms","input_tokens","output_tokens","tokens_per_second")})

    def _terminal(self, c, job, status, code=None, output=None, metrics=None):
        at = now_ms()
        previous = c.execute("SELECT output_json,metrics_json FROM job_payloads WHERE job_id=?", (job,)).fetchone()
        if output is None and previous and previous["output_json"]:
            output = json.loads(previous["output_json"])
            output["isComplete"] = False
        if metrics is None and previous and previous["metrics_json"]:
            metrics = json.loads(previous["metrics_json"])
        c.execute("""UPDATE jobs SET status=?,error_code=?,finished_at_ms=?,
            payload_expires_at_ms=?,tombstone_expires_at_ms=? WHERE job_id=?""",
            (status, code, at, at+self.settings.payload_days*86400000,
             at+self.settings.tombstone_days*86400000, job))
        c.execute("UPDATE job_payloads SET output_json=?,metrics_json=? WHERE job_id=?",
                  (canonical(output) if output is not None else None,
                   canonical(metrics) if metrics is not None else None, job))
        self.event(c, job, status, code)

    def _recovery(self, c, job, reason):
        c.execute("""UPDATE service_state SET recovery_required=1,recovery_reason=?,
            recovery_job_id=?,updated_at_ms=? WHERE id=1""", (reason, job, now_ms()))

    def startup(self):
        with self.connection(True) as c:
            rows = c.execute("SELECT job_id,status FROM jobs WHERE status IN " + ACTIVE_SQL).fetchall()
            for row in rows:
                if row["status"] in ("running", "cancelling"):
                    self._recovery(c, row["job_id"], "SERVER_RESTARTED")
                self._terminal(c, row["job_id"], "cancelled" if row["status"] == "cancelling" else "failed",
                               None if row["status"] == "cancelling" else "SERVER_RESTARTED")

    def recovery(self):
        with self.connection() as c:
            return dict(c.execute("SELECT * FROM service_state WHERE id=1").fetchone())

    def clear_cleanup(self, job):
        with self.connection(True) as c:
            c.execute("""UPDATE service_state SET recovery_required=0,recovery_reason=NULL,
                recovery_job_id=NULL,updated_at_ms=? WHERE id=1 AND recovery_job_id=?
                AND recovery_reason='DRAINING'""", (now_ms(), job))

    def mark_recovery(self, job, reason):
        with self.connection(True) as c:
            self._recovery(c, job, reason)

    def engine_recovered(self, job):
        """Called only after a managed engine restart and identity check succeed."""
        with self.connection(True) as c:
            c.execute("""UPDATE service_state SET recovery_required=0,recovery_reason=NULL,
                recovery_job_id=NULL,recovered_at_ms=?,updated_at_ms=?,recovery_note='Managed engine restarted'
                WHERE id=1 AND recovery_job_id IS ?""", (now_ms(), now_ms(), job))
            if job:
                c.execute("UPDATE jobs SET inference_stop_confirmed=1 WHERE job_id=?", (job,))

    def backup(self, destination):
        """SQLite backup API includes committed WAL pages; offline instance lock is held by CLI."""
        destination = Path(destination)
        if destination.exists() or destination.resolve() == self.path.resolve():
            raise ValueError("Backup destination must be a new file")
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            with self.connection() as source, closing(sqlite3.connect(destination)) as target:
                source.backup(target)
                if target.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise RuntimeError("Backup integrity check failed")
        except BaseException:
            destination.unlink(missing_ok=True)
            raise

    def restore(self, source):
        """Never overwrite an existing database; choose a new data directory."""
        source = Path(source).resolve()
        if self.path.exists() or not source.is_file():
            raise ValueError("Restore requires an existing backup and a new data directory")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as backup, closing(sqlite3.connect(self.path)) as target:
                if backup.execute("PRAGMA integrity_check").fetchone()[0] != "ok" or backup.execute("PRAGMA user_version").fetchone()[0] not in (1, 2, 3, 4, 5, 6):
                    raise ValueError("Invalid backup")
                backup.backup(target)
        except BaseException:
            self.path.unlink(missing_ok=True)
            raise

    def confirm_recovery(self, note):
        if not note.strip():
            raise ValueError("Recovery note is required")
        with self.connection(True) as c:
            if c.execute("SELECT 1 FROM jobs WHERE status IN " + ACTIVE_SQL).fetchone():
                raise ValueError("Archive active jobs before recovery")
            c.execute("""UPDATE service_state SET recovery_required=0,recovery_reason=NULL,
                recovery_job_id=NULL,recovered_at_ms=?,updated_at_ms=?,recovery_note=? WHERE id=1""",
                (now_ms(), now_ms(), note))

    def user_add(self, user_id, name):
        if not user_id.strip() or len(user_id) > 128 or not name.strip() or len(name) > 256:
            raise ValueError("ACCOUNT_FIELDS_INVALID")
        with self.connection(True) as c:
            if c.execute("SELECT 1 FROM users WHERE user_id=?", (user_id,)).fetchone():
                raise ValueError("USER_ALREADY_EXISTS")
            c.execute("INSERT INTO users(user_id,display_name,enabled,created_at_ms) VALUES(?,?,1,?)",
                      (user_id, name, now_ms()))

    def user_enable(self, user_id):
        with self.connection(True) as c:
            if c.execute("UPDATE users SET enabled=1 WHERE user_id=?", (user_id,)).rowcount != 1:
                raise ValueError("USER_NOT_FOUND")

    def users(self):
        with self.connection() as c:
            return [dict(row) for row in c.execute("SELECT user_id,display_name,enabled FROM users ORDER BY user_id")]

    def tokens(self, user):
        with self.connection() as c:
            if not c.execute("SELECT 1 FROM users WHERE user_id=?", (user,)).fetchone():
                raise ValueError("USER_NOT_FOUND")
            rows = [dict(row) for row in c.execute("""SELECT key_id,user_id,created_at_ms,expires_at_ms,revoked_at_ms
                FROM api_keys WHERE user_id=? ORDER BY created_at_ms DESC,key_id""", (user,))]
        at = now_ms()
        for row in rows:
            row["status"] = ("revoked" if row["revoked_at_ms"] is not None else
                             "expired" if row["expires_at_ms"] is not None and row["expires_at_ms"] <= at else "valid")
        return rows

    def token_create(self, user, expires_at=None):
        if expires_at is not None and (type(expires_at) is not int or expires_at <= now_ms()):
            raise ValueError("TOKEN_EXPIRY_INVALID")
        key = uuid4().hex
        token = "ais_" + key + "_" + secrets.token_urlsafe(32)
        with self.connection(True) as c:
            row = c.execute("SELECT enabled FROM users WHERE user_id=?", (user,)).fetchone()
            if not row:
                raise ValueError("USER_NOT_FOUND")
            if not row["enabled"]:
                raise ValueError("USER_DISABLED")
            c.execute("INSERT INTO api_keys VALUES(?,?,?,?,?,NULL)",
                      (key, user, hashlib.sha256(token.encode()).hexdigest(), now_ms(), expires_at))
        return token

    def token_revoke(self, key, user=None):
        with self.connection(True) as c:
            return bool(c.execute("UPDATE api_keys SET revoked_at_ms=COALESCE(revoked_at_ms,?) WHERE key_id=? AND (? IS NULL OR user_id=?)",
                                  (now_ms(), key, user, user)).rowcount)

    def user_disable(self, user):
        with self.connection(True) as c:
            if c.execute("UPDATE users SET enabled=0 WHERE user_id=?", (user,)).rowcount != 1:
                raise ValueError("USER_NOT_FOUND")
            for row in c.execute("SELECT job_id,status FROM jobs WHERE user_id=? AND status IN " + ACTIVE_SQL, (user,)).fetchall():
                if row["status"] == "queued":
                    self._terminal(c, row["job_id"], "cancelled")
                elif row["status"] == "running":
                    c.execute("UPDATE jobs SET status='cancelling',stop_reason='user_disabled' WHERE job_id=?", (row["job_id"],))
                    self._recovery(c, row["job_id"], "DRAINING")

    def authenticate_details(self, token):
        parts = token.split("_", 2)
        key = parts[1] if len(parts) == 3 and parts[0] == "ais" else ""
        with self.connection() as c:
            row = c.execute("""SELECT k.*,u.enabled,u.display_name FROM api_keys k JOIN users u USING(user_id)
                WHERE key_id=?""", (key,)).fetchone()
        digest = hashlib.sha256(token.encode()).hexdigest()
        expected = row["token_sha256"] if row else "0"*64
        valid = hmac.compare_digest(digest, expected)
        if not row or not valid or not row["enabled"] or row["revoked_at_ms"] is not None or (
            row["expires_at_ms"] is not None and row["expires_at_ms"] <= now_ms()):
            raise ApiError(401, "UNAUTHORIZED")
        return {"userId":row["user_id"], "displayName":row["display_name"],
                "keyId":row["key_id"], "expiresAtMs":row["expires_at_ms"]}

    def authenticate(self, token):
        return self.authenticate_details(token)["userId"]

    def _lookup(self, c, user, request_id, fingerprint):
        row = c.execute("SELECT * FROM jobs WHERE user_id=? AND client_request_id=?", (user, request_id)).fetchone()
        if row and row["request_hash"] != fingerprint:
            raise ApiError(409, "IDEMPOTENCY_CONFLICT")
        return dict(row) if row else None

    def replay(self, user, request_id, fingerprint):
        with self.connection() as c:
            return self._lookup(c, user, request_id, fingerprint)

    def insert(self, user, request, fingerprint, effective, allow_draining, received_at_ms=None):
        with self.connection(True) as c:
            existing = self._lookup(c, user, request["clientRequestId"], fingerprint)
            if existing:
                return existing, True
            enabled = c.execute("SELECT enabled FROM users WHERE user_id=?", (user,)).fetchone()
            if not enabled or not enabled[0]:
                raise ApiError(401, "UNAUTHORIZED")
            state = c.execute("SELECT * FROM service_state WHERE id=1").fetchone()
            if state["recovery_required"] and not (allow_draining and state["recovery_reason"] == "DRAINING"):
                raise ApiError(503, "ENGINE_RECOVERING")
            if c.execute("SELECT count(*) FROM jobs WHERE user_id=? AND status IN " + ACTIVE_SQL, (user,)).fetchone()[0] >= self.settings.max_outstanding_per_user:
                raise ApiError(429, "USER_JOB_LIMIT")
            if c.execute("SELECT count(*) FROM jobs WHERE status IN " + ACTIVE_SQL).fetchone()[0] >= self.settings.max_outstanding_global:
                raise ApiError(429, "QUEUE_FULL")
            job = str(uuid4())
            c.execute("""INSERT INTO jobs(job_id,user_id,client_request_id,request_hash,hash_version,
                operation,status,created_at_ms,profile_id) VALUES(?,?,?,?,'messages-v1','chat','queued',?,?)""",
                (job,user,request["clientRequestId"],fingerprint,now_ms(),effective["profile"]["profile_id"]))
            c.execute("INSERT INTO job_payloads VALUES(?,?,?,NULL,NULL)", (job,canonical(request),canonical(effective)))
            c.execute("""INSERT INTO task_performance(job_id,user_id,status,received_at_ms,created_at_ms)
                SELECT job_id,user_id,status,?,created_at_ms FROM jobs WHERE job_id=?""", (received_at_ms,job))
            self.event(c, job, "queued")
            return dict(c.execute("SELECT * FROM jobs WHERE job_id=?", (job,)).fetchone()), False

    def get(self, user, job=None, request_id=None):
        with self.connection() as c:
            where, value = ("j.job_id", job) if job else ("j.client_request_id", request_id)
            row = c.execute("""SELECT j.*,p.request_json,p.effective_settings_json,p.output_json,p.metrics_json
                FROM jobs j LEFT JOIN job_payloads p USING(job_id) WHERE j.user_id=? AND """+where+"=?", (user,value)).fetchone()
            if not row:
                raise ApiError(404, "JOB_NOT_FOUND")
            return dict(row)

    def by_job(self, job):
        with self.connection() as c:
            row = c.execute("SELECT user_id FROM jobs WHERE job_id=?", (job,)).fetchone()
        return self.get(row["user_id"], job=job)

    def list(self, user, limit, before):
        with self.connection() as c:
            rows = c.execute("""SELECT * FROM jobs WHERE user_id=? AND sequence<?
                ORDER BY sequence DESC LIMIT ?""", (user,before or 9223372036854775807,limit+1)).fetchall()
        return [dict(x) for x in rows[:limit]], len(rows)>limit

    def queue(self, sequence):
        with self.connection() as c:
            waiting = c.execute("SELECT count(*) FROM jobs WHERE status='queued' AND sequence<?", (sequence,)).fetchone()[0]
            active = c.execute("SELECT count(*) FROM jobs WHERE status IN ('running','cancelling')").fetchone()[0]
        return waiting, active

    def expire_waiting(self):
        with self.connection(True) as c:
            rows = c.execute("SELECT job_id FROM jobs WHERE status='queued' AND created_at_ms<=?",
                             (now_ms()-self.settings.queue_timeout_seconds*1000,)).fetchall()
            for row in rows:
                self._terminal(c, row["job_id"], "failed", "QUEUE_TIMEOUT")

    def claim(self):
        with self.connection(True) as c:
            if c.execute("SELECT recovery_required FROM service_state WHERE id=1").fetchone()[0]:
                return None
            row = c.execute("SELECT * FROM jobs WHERE status='queued' ORDER BY sequence LIMIT 1").fetchone()
            if not row:
                return None
            c.execute("UPDATE jobs SET status='running',started_at_ms=? WHERE job_id=? AND status='queued'", (now_ms(),row["job_id"]))
            self.event(c, row["job_id"], "running")
        return self.by_job(row["job_id"])

    def cancel(self, user, job, reason="user_cancel"):
        with self.connection(True) as c:
            row = c.execute("SELECT * FROM jobs WHERE user_id=? AND job_id=?", (user,job)).fetchone()
            if not row:
                raise ApiError(404, "JOB_NOT_FOUND")
            old = row["status"]
            accepted = old not in ("succeeded","failed")
            if old == "queued":
                self._terminal(c,job,"cancelled")
                c.execute("UPDATE jobs SET stop_reason=? WHERE job_id=?", (reason,job))
            elif old == "running":
                c.execute("UPDATE jobs SET status='cancelling',stop_reason=?,inference_stop_confirmed=0 WHERE job_id=?", (reason,job))
                self._recovery(c,job,"DRAINING")
                self.event(c,job,"cancelling",reason)
            current = c.execute("SELECT status,inference_stop_confirmed FROM jobs WHERE job_id=?", (job,)).fetchone()
            confirmed = current["inference_stop_confirmed"]
            return current["status"], accepted, old in ("running", "cancelling"), None if confirmed is None else bool(confirmed)

    def append_output(self, job, text, channel="content"):
        """Commit before publishing. Conditional writes discard all late output."""
        if not text:
            return
        if channel not in ("content", "reasoning_content"):
            raise ValueError("Invalid output channel")
        with self.connection(True) as c:
            row = c.execute("""SELECT j.status,p.output_json FROM jobs j JOIN job_payloads p USING(job_id)
                WHERE j.job_id=?""", (job,)).fetchone()
            if not row or row["status"] != "running":
                return
            sequence = c.execute("SELECT coalesce(max(sequence),0)+1 FROM output_chunks WHERE job_id=?", (job,)).fetchone()[0]
            c.execute("INSERT INTO output_chunks(job_id,sequence,text,channel) VALUES(?,?,?,?)", (job,sequence,text,channel))
            output = json.loads(row["output_json"]) if row["output_json"] else {"text":"", "isComplete":False}
            key = "text" if channel == "content" else "reasoningText"
            output[key] = output.get(key, "") + text
            c.execute("UPDATE job_payloads SET output_json=? WHERE job_id=?",
                      (canonical(output),job))
            if sequence == 1:
                c.execute("UPDATE task_performance SET first_output_at_ms=? WHERE job_id=? AND first_output_at_ms IS NULL", (now_ms(),job))

    def chunks(self, job, after):
        with self.connection() as c:
            return [dict(row) for row in c.execute("SELECT sequence,text,channel FROM output_chunks WHERE job_id=? AND sequence>? ORDER BY sequence LIMIT 64", (job,after))]

    def finish(self, job, generation=None, code=None):
        with self.connection(True) as c:
            row = c.execute("SELECT status FROM jobs WHERE job_id=?", (job,)).fetchone()
            if row["status"] != "running":
                return False
            output, metrics = None, None
            if generation:
                if not generation.is_complete:
                    code = "OUTPUT_TRUNCATED"
                output = {"text":generation.text,"isComplete":not bool(code)}
                if generation.reasoning_text:
                    output["reasoningText"] = generation.reasoning_text
                if generation.tool_calls:
                    output["toolCalls"] = list(generation.tool_calls)
                    c.execute("UPDATE task_performance SET first_output_at_ms=? WHERE job_id=? AND first_output_at_ms IS NULL", (now_ms(),job))
                metrics = {"inputTokens":generation.input_tokens,"outputTokens":generation.output_tokens,
                           "finishReason":generation.finish_reason}
                engine = generation.engine_metrics or {}
                c.execute("""UPDATE task_performance SET input_tokens=?,output_tokens=?,engine_total_ms=?,
                    load_ms=?,prompt_eval_ms=?,eval_ms=? WHERE job_id=?""",
                    (generation.input_tokens,generation.output_tokens,engine.get("total_duration"),
                     engine.get("load_duration"),engine.get("prompt_eval_duration"),engine.get("eval_duration"),job))
            self._terminal(c,job,"failed" if code else "succeeded",code,output,metrics)
            return True

    def stop_running(self, job, reason):
        with self.connection(True) as c:
            row = c.execute("SELECT status FROM jobs WHERE job_id=?", (job,)).fetchone()
            if row["status"] in TERMINAL:
                return
            if row["status"] == "cancelling":
                self._terminal(c,job,"cancelled")
            else:
                self._terminal(c,job,"failed",reason)
                c.execute("UPDATE jobs SET stop_reason=? WHERE job_id=?", (reason,job))
            self._recovery(c,job,"DRAINING")

    def shutdown_waiting(self):
        with self.connection(True) as c:
            for row in c.execute("SELECT job_id FROM jobs WHERE status='queued'").fetchall():
                self._terminal(c,row["job_id"],"failed","SERVICE_STOPPED")

    def cleanup(self):
        with self.connection(True) as c:
            at = now_ms()
            rows = c.execute("SELECT job_id FROM jobs WHERE payload_expires_at_ms<=? AND payload_purged_at_ms IS NULL LIMIT 100", (at,)).fetchall()
            for row in rows:
                c.execute("DELETE FROM job_payloads WHERE job_id=?", (row["job_id"],))
                c.execute("UPDATE jobs SET payload_purged_at_ms=? WHERE job_id=?", (at,row["job_id"]))
            c.execute("DELETE FROM jobs WHERE job_id IN (SELECT job_id FROM jobs WHERE tombstone_expires_at_ms<=? LIMIT 100)", (at,))
            c.execute("DELETE FROM task_performance WHERE job_id IN (SELECT job_id FROM task_performance WHERE finished_at_ms<=? LIMIT 1000)", (at-604800000,))
