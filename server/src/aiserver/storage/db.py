from contextlib import contextmanager, closing
import hashlib
import hmac
import json
from pathlib import Path
import secrets
import sqlite3
import time
from uuid import uuid4

from aiserver.api.errors import ApiError
from aiserver.api.validation import canonical

TERMINAL = ("succeeded", "failed", "cancelled")
ACTIVE_SQL = "('queued','running','cancelling')"

def now_ms():
    return time.time_ns() // 1_000_000

class Store:
    def __init__(self, settings):
        self.settings = settings
        self.path = settings.data_directory / "jobs.db"

    @contextmanager
    def connection(self, write=False):
        conn = sqlite3.connect(self.path, timeout=3, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA synchronous=FULL")
            if write:
                conn.execute("BEGIN IMMEDIATE")
            yield conn
            if write:
                conn.commit()
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
            if version not in (0, 1):
                raise RuntimeError("Unsupported schema")
            c.execute("PRAGMA journal_mode=WAL")
            if version == 0:
                # Initial migration only; future migrations must back up first.
                sql = Path(__file__).with_name("001_initial.sql").read_text(encoding="utf-8")
                c.executescript("BEGIN IMMEDIATE;\n" + sql + "\nCOMMIT;")

    def event(self, c, job, kind, reason=None):
        c.execute("INSERT INTO job_events(job_id,at_ms,event_type,reason) VALUES(?,?,?,?)",
                  (job, now_ms(), kind, reason))

    def _terminal(self, c, job, status, code=None, output=None, metrics=None):
        at = now_ms()
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
                if backup.execute("PRAGMA integrity_check").fetchone()[0] != "ok" or backup.execute("PRAGMA user_version").fetchone()[0] != 1:
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
        with self.connection(True) as c:
            c.execute("INSERT INTO users VALUES(?,?,1,?)", (user_id, name, now_ms()))

    def token_create(self, user, expires_at=None):
        key = uuid4().hex
        token = "ais_" + key + "_" + secrets.token_urlsafe(32)
        with self.connection(True) as c:
            c.execute("INSERT INTO api_keys VALUES(?,?,?,?,?,NULL)",
                      (key, user, hashlib.sha256(token.encode()).hexdigest(), now_ms(), expires_at))
        return token

    def token_revoke(self, key):
        with self.connection(True) as c:
            c.execute("UPDATE api_keys SET revoked_at_ms=? WHERE key_id=?", (now_ms(), key))

    def user_disable(self, user):
        with self.connection(True) as c:
            c.execute("UPDATE users SET enabled=0 WHERE user_id=?", (user,))
            for row in c.execute("SELECT job_id,status FROM jobs WHERE user_id=? AND status IN " + ACTIVE_SQL, (user,)).fetchall():
                if row["status"] == "queued":
                    self._terminal(c, row["job_id"], "cancelled")
                elif row["status"] == "running":
                    c.execute("UPDATE jobs SET status='cancelling',stop_reason='user_disabled' WHERE job_id=?", (row["job_id"],))
                    self._recovery(c, row["job_id"], "DRAINING")

    def authenticate(self, token):
        parts = token.split("_", 2)
        key = parts[1] if len(parts) == 3 and parts[0] == "ais" else ""
        with self.connection() as c:
            row = c.execute("""SELECT k.*,u.enabled FROM api_keys k JOIN users u USING(user_id)
                WHERE key_id=?""", (key,)).fetchone()
        digest = hashlib.sha256(token.encode()).hexdigest()
        expected = row["token_sha256"] if row else "0"*64
        valid = hmac.compare_digest(digest, expected)
        if not row or not valid or not row["enabled"] or row["revoked_at_ms"] is not None or (
            row["expires_at_ms"] is not None and row["expires_at_ms"] <= now_ms()):
            raise ApiError(401, "UNAUTHORIZED")
        return row["user_id"]

    def _lookup(self, c, user, request_id, fingerprint):
        row = c.execute("SELECT * FROM jobs WHERE user_id=? AND client_request_id=?", (user, request_id)).fetchone()
        if row and row["request_hash"] != fingerprint:
            raise ApiError(409, "IDEMPOTENCY_CONFLICT")
        return dict(row) if row else None

    def replay(self, user, request_id, fingerprint):
        with self.connection() as c:
            return self._lookup(c, user, request_id, fingerprint)

    def insert(self, user, request, fingerprint, effective, allow_draining):
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
            parent = request["parentJobId"]
            if parent and not c.execute("SELECT 1 FROM jobs WHERE job_id=? AND user_id=?", (parent, user)).fetchone():
                raise ApiError(404, "JOB_NOT_FOUND")
            if c.execute("SELECT count(*) FROM jobs WHERE user_id=? AND status IN " + ACTIVE_SQL, (user,)).fetchone()[0] >= self.settings.max_outstanding_per_user:
                raise ApiError(429, "USER_JOB_LIMIT")
            if c.execute("SELECT count(*) FROM jobs WHERE status IN " + ACTIVE_SQL).fetchone()[0] >= self.settings.max_outstanding_global:
                raise ApiError(429, "QUEUE_FULL")
            job = str(uuid4())
            c.execute("""INSERT INTO jobs(job_id,user_id,client_request_id,request_hash,hash_version,
                operation,status,created_at_ms,profile_id) VALUES(?,?,?,?,'business-v1',?,'queued',?,?)""",
                (job,user,request["clientRequestId"],fingerprint,request["operation"],now_ms(),effective["profile"]["profile_id"]))
            c.execute("INSERT INTO job_payloads VALUES(?,?,?,NULL,NULL)", (job,canonical(request),canonical(effective)))
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

    def cancel(self, user, job):
        with self.connection(True) as c:
            row = c.execute("SELECT * FROM jobs WHERE user_id=? AND job_id=?", (user,job)).fetchone()
            if not row:
                raise ApiError(404, "JOB_NOT_FOUND")
            old = row["status"]
            accepted = old not in ("succeeded","failed")
            if old == "queued":
                self._terminal(c,job,"cancelled")
                c.execute("UPDATE jobs SET stop_reason='user_cancel' WHERE job_id=?", (job,))
            elif old == "running":
                c.execute("UPDATE jobs SET status='cancelling',stop_reason='user_cancel',inference_stop_confirmed=0 WHERE job_id=?", (job,))
                self._recovery(c,job,"DRAINING")
                self.event(c,job,"cancelling","user_cancel")
            status = c.execute("SELECT status FROM jobs WHERE job_id=?", (job,)).fetchone()[0]
            return status, accepted, old == "running", None if row["started_at_ms"] is None else False

    def finish(self, job, generation=None, code=None):
        with self.connection(True) as c:
            row = c.execute("SELECT status FROM jobs WHERE job_id=?", (job,)).fetchone()
            if row["status"] != "running":
                return False
            output, metrics = None, None
            if generation:
                if not generation.text.strip():
                    code = "MODEL_EMPTY_OUTPUT"
                elif not generation.is_complete:
                    code = "OUTPUT_TRUNCATED"
                output = {"text":generation.text,"isComplete":not bool(code),"kind":"unclassified"} if generation.text else None
                metrics = {"inputTokens":generation.input_tokens,"outputTokens":generation.output_tokens,
                           "finishReason":generation.finish_reason}
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
