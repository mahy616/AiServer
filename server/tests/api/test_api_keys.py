import hashlib
import json
from pathlib import Path
import sqlite3
import time
from uuid import uuid4

import pytest

from aiserver.__main__ import entrypoint
from aiserver.settings import Settings
from aiserver.storage.db import Store, now_ms
from test_fake_api import service, request, events, post, assert_schema


def bearer(key):
    return {"Authorization": "Bearer " + key}


def test_key_auth_me_inference_separate_revocation_and_retired_routes(tmp_path):
    with service(tmp_path, fake_delay_seconds=0) as (client, store, scheduler, other):
        first, second = store.token_create("alice"), store.token_create("alice")
        value = client.get("/api/v1/auth/me", headers=bearer(first)).json()
        assert_schema("me", value)
        assert value == {"user": {"userId": "alice", "displayName": "Alice"}, "expiresAt": None}
        assert list(events(client.post("/api/v1/jobs", json=request(), headers=bearer(first))))[-1][0] == "completed"
        assert store.token_revoke(first.split("_")[1])
        assert client.get("/api/v1/auth/me", headers=bearer(first)).status_code == 401
        assert client.get("/api/v1/auth/me", headers=bearer(second)).status_code == 200
        assert client.post("/api/v1/auth/login", json={"username": "alice", "password": "unused"}).status_code == 404
        assert client.post("/api/v1/auth/logout").status_code == 404
        listing = store.tokens("alice")
        assert {row["status"] for row in listing} == {"valid", "revoked"}
        text = json.dumps(listing) + (tmp_path / "logs/service.log").read_text()
        assert first not in text and second not in text and "token_sha256" not in text


def test_expired_unknown_and_disabled_keys_fail_and_issuance_is_validated(tmp_path):
    with service(tmp_path) as (client, store, scheduler, other):
        key = store.token_create("alice", now_ms() + 60000)
        with store.connection(True) as conn:
            conn.execute("UPDATE api_keys SET expires_at_ms=? WHERE key_id=?", (now_ms() - 1, key.split("_")[1]))
        assert client.get("/api/v1/auth/me", headers=bearer(key)).status_code == 401
        assert client.get("/api/v1/auth/me", headers=bearer("unknown")).status_code == 401
        assert store.tokens("alice")[0]["status"] == "expired"
        with pytest.raises(ValueError, match="TOKEN_EXPIRY_INVALID"):
            store.token_create("alice", now_ms() - 1)
        store.user_disable("alice")
        assert client.get("/api/v1/auth/me").status_code == 401
        with pytest.raises(ValueError, match="USER_DISABLED"):
            store.token_create("alice")
        assert scheduler.provider.calls == 0


def test_disable_cancels_work_and_enable_does_not_resume(tmp_path):
    with service(tmp_path, fake_delay_seconds=10) as (client, store, scheduler, other):
        first = post(client, request()); client.portal.call(scheduler.provider.started.wait)
        second = post(client, request())
        store.user_disable("alice")
        until = time.monotonic() + 3
        while store.get("alice", job=first)["status"] != "cancelled" and time.monotonic() < until:
            time.sleep(.01)
        assert store.get("alice", job=first)["status"] == "cancelled"
        assert store.get("alice", job=second)["status"] == "cancelled"
        store.user_enable("alice")
        assert client.get("/api/v1/auth/me").status_code == 200
        assert scheduler.provider.calls == 1


@pytest.mark.parametrize("version", [1, 2, 3, 4, 5])
def test_legacy_migration_backs_up_and_preserves_keys_accounts_and_expiry(tmp_path, version):
    store = Store(Settings(data_directory=tmp_path))
    migrations = sorted(Path(__import__("aiserver.storage.db", fromlist=["x"]).__file__).parent.glob("*.sql"))
    key_id = uuid4().hex
    key = "ais_" + key_id + "_synthetic-migration-secret"
    expires = now_ms() + 86400000
    with sqlite3.connect(store.path) as conn:
        for migration in migrations[:version]:
            conn.executescript(migration.read_text(encoding="utf-8"))
        conn.execute("INSERT INTO users(user_id,display_name,enabled,created_at_ms) VALUES('migrated','Existing',1,?)", (now_ms(),))
        if 3 <= version < 5:
            conn.execute("UPDATE users SET password_hash='old-hash-to-remove'")
        conn.execute("INSERT INTO api_keys VALUES(?,?,?,?,?,NULL)",
                     (key_id, "migrated", hashlib.sha256(key.encode()).hexdigest(), now_ms(), expires))
    store.initialize()
    assert store.authenticate(key) == "migrated"
    assert store.tokens("migrated")[0]["expires_at_ms"] == expires
    assert store.users() == [{"user_id": "migrated", "display_name": "Existing", "enabled": 1}]
    with store.connection() as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 6
        assert "password_hash" not in {row[1] for row in conn.execute("PRAGMA table_info(users)")}
    backups = list((tmp_path / "backups").glob(f"schema-v{version}-*.db"))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == version
        assert conn.execute("SELECT key_id FROM api_keys").fetchone()[0] == key_id
    store.initialize()
    assert len(list((tmp_path / "backups").glob("*.db"))) == 1


def test_v4_migration_preserves_completed_payload_history_and_restore(tmp_path):
    data = tmp_path / "active"
    with service(data, fake_delay_seconds=0) as (client, store, scheduler, other):
        job = list(events(client.post("/api/v1/jobs", json=request())))[-1][1]["jobId"]
        key = store.token_create("alice")
        before = store.get("alice", job=job)
    with store.connection(True) as conn:
        conn.execute("ALTER TABLE users ADD COLUMN password_hash TEXT")
        conn.execute("UPDATE users SET password_hash='old-hash'")
        conn.execute("ALTER TABLE output_chunks DROP COLUMN channel")
        conn.execute("PRAGMA user_version=4")
    store.initialize()
    assert store.authenticate(key) == "alice"
    after = store.get("alice", job=job)
    for field in ("request_json", "output_json", "payload_expires_at_ms", "request_hash"):
        assert before[field] == after[field]
    target = Store(Settings(data_directory=tmp_path / "restored"))
    target.restore(next((data / "backups").glob("schema-v4-*.db")))
    target.initialize()
    assert target.authenticate(key) == "alice" and target.get("alice", job=job)["output_json"] == before["output_json"]


def test_old_private_login_settings_are_ignored_without_exposing_controls():
    settings = Settings(login_token_days=30, login_attempts_per_minute=20, login_attempts_per_username_per_minute=5)
    assert not any(key.startswith("login_") for key in settings.model_dump())
    with pytest.raises(ValueError):
        Settings(unknown_new_field=True)


def test_failed_v4_migration_rolls_back_and_keeps_backup(tmp_path, monkeypatch):
    store = Store(Settings(data_directory=tmp_path))
    store.initialize(); store.user_add("existing", "Existing")
    key = store.token_create("existing")
    with store.connection(True) as conn:
        conn.execute("ALTER TABLE users ADD COLUMN password_hash TEXT")
        conn.execute("UPDATE users SET password_hash='old-hash'")
        conn.execute("ALTER TABLE output_chunks DROP COLUMN channel")
        conn.execute("PRAGMA user_version=4")
    original = Path.read_text
    def broken(path, *args, **kwargs):
        if path.name == "005_api_keys_only.sql":
            return "ALTER TABLE users DROP COLUMN password_hash; SELECT * FROM missing_migration_table; PRAGMA user_version=5;"
        return original(path, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(Path, "read_text", broken)
        with pytest.raises(sqlite3.OperationalError):
            store.initialize()
    with store.connection() as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 4
        assert conn.execute("SELECT password_hash FROM users").fetchone()[0] == "old-hash"
    assert store.authenticate(key) == "existing"
    backup = next((tmp_path / "backups").glob("schema-v4-*.db"))
    with sqlite3.connect(backup) as conn:
        assert conn.execute("SELECT password_hash FROM users").fetchone()[0] == "old-hash"
    store.initialize()
    assert not store.needs_migration() and store.authenticate(key) == "existing"


def test_admin_cli_account_key_list_and_revoke(tmp_path, capsys):
    config = tmp_path / "server.json"
    config.write_text(json.dumps({"provider": "Fake", "data_directory": str(tmp_path / "data")}))
    prefix = ["--config", str(config)]
    assert entrypoint(prefix + ["user-add", "engineer"]) == 0
    assert entrypoint(prefix + ["user-add", "engineer"]) == 1
    assert entrypoint(prefix + ["token-create", "missing"]) == 1
    capsys.readouterr()
    assert entrypoint(prefix + ["token-create", "engineer"]) == 0
    key = capsys.readouterr().out.strip()
    assert key.startswith("ais_")
    key_id = key.split("_")[1]
    assert entrypoint(prefix + ["token-list", "engineer"]) == 0
    output = capsys.readouterr().out
    assert key_id in output and key not in output
    assert entrypoint(prefix + ["token-revoke", key_id]) == 0
    assert entrypoint(prefix + ["token-list", "engineer"]) == 0
    assert "revoked" in capsys.readouterr().out
    with pytest.raises(SystemExit) as error:
        entrypoint(prefix + ["password-set", "engineer"])
    assert error.value.code == 2
