import argparse
import asyncio
from pathlib import Path
import sqlite3
import sys

from pydantic import ValidationError

from aiserver.operations.lock import InstanceLock
from aiserver.settings import ROOT, Settings
from aiserver.storage.db import Store


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", help="Absolute JSON settings path; AISERVER_* overrides")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("serve"); p.add_argument("--no-tunnel", action="store_true")
    p = sub.add_parser("user-add"); p.add_argument("user_id"); p.add_argument("--name", default="User")
    sub.add_parser("user-list")
    p = sub.add_parser("user-enable"); p.add_argument("user_id")
    p = sub.add_parser("token-create"); p.add_argument("user_id"); p.add_argument("--expires-at-ms", type=int)
    p = sub.add_parser("token-list"); p.add_argument("user_id")
    p = sub.add_parser("token-revoke"); p.add_argument("key_id")
    p = sub.add_parser("user-disable"); p.add_argument("user_id")
    p = sub.add_parser("recovery-confirm"); p.add_argument("--note", required=True)
    p = sub.add_parser("backup"); p.add_argument("destination", type=Path)
    p = sub.add_parser("restore"); p.add_argument("source", type=Path)
    sub.add_parser("cleanup")
    args = parser.parse_args(argv)
    if args.config and not Path(args.config).is_absolute():
        raise ValueError("CONFIG_PATH_MUST_BE_ABSOLUTE")
    default_config = ROOT / "config/server.local.json"
    if not default_config.exists():
        default_config = ROOT / "config/server.ollama.example.json"
    settings = Settings.load(args.config or str(default_config))
    if args.command == "serve":
        if args.no_tunnel:
            settings.manage_natapp = False
        from aiserver.operations.service import serve
        asyncio.run(serve(settings))
        return 0
    store = Store(settings)
    lock = None
    try:
        if args.command in ("recovery-confirm", "backup", "restore", "cleanup") or store.needs_migration():
            lock = InstanceLock(settings.data_directory).acquire()
        if args.command == "restore":
            store.restore(args.source)
            print("Restored into the new data directory; startup will migrate if required.")
            return 0
        store.initialize()
        if args.command == "user-add":
            store.user_add(args.user_id, args.name)
            print("Account created. Use token-create to issue an API key.")
        elif args.command == "user-list":
            for row in store.users():
                print(f'{row["user_id"]}\tenabled={bool(row["enabled"])}\t{row["display_name"]}')
        elif args.command == "user-enable":
            store.user_enable(args.user_id); print("Account enabled; old tasks are not resumed.")
        elif args.command == "token-create":
            # Only this explicit command prints a credential.
            print(store.token_create(args.user_id, args.expires_at_ms))
        elif args.command == "token-list":
            for row in store.tokens(args.user_id):
                print(f'{row["key_id"]}\t{row["status"]}\tcreatedAtMs={row["created_at_ms"]}\texpiresAtMs={row["expires_at_ms"]}')
        elif args.command == "token-revoke":
            if not store.token_revoke(args.key_id):
                raise ValueError("TOKEN_NOT_FOUND")
            print("Credential revoked.")
        elif args.command == "user-disable":
            store.user_disable(args.user_id); print("Account disabled; active work is being cancelled.")
        elif args.command == "recovery-confirm":
            store.confirm_recovery(args.note); print("Recovery confirmation recorded.")
        elif args.command == "backup":
            store.backup(args.destination); print("Consistent SQLite backup created.")
        elif args.command == "cleanup":
            store.cleanup(); print("Expired payload cleanup completed.")
        return 0
    finally:
        if lock:
            lock.release()


ERRORS = {
    "USER_ALREADY_EXISTS": "Account already exists; use token-create to issue another API key.",
    "USER_NOT_FOUND": "Account does not exist; create it with user-add first.",
    "TOKEN_NOT_FOUND": "Credential keyId was not found; no credential was changed.",
    "ACCOUNT_FIELDS_INVALID": "Username must be 1-128 non-blank characters; display name 1-256.",
    "USER_DISABLED": "Account is disabled; enable it before issuing an API key.",
    "TOKEN_EXPIRY_INVALID": "API key expiry must be a future Unix timestamp in milliseconds.",
    "CONFIG_PATH_MUST_BE_ABSOLUTE": "Use an absolute --config path.",
    "NATAPP_EXECUTABLE_MISSING": "Set natapp_executable to your existing NATAPP executable, or use --no-tunnel for local development.",
    "NATAPP_CONFIG_MISSING": "Set natapp_config to your private INI; keep its authtoken local.",
    "NATAPP_CONFIG_INVALID": "NATAPP INI cannot be read or parsed; inspect the private file.",
    "NATAPP_TOKEN_NOT_CONFIGURED": "Configure NATAPP authtoken in the private INI.",
    "NATAPP_LOG_MUST_BE_NONE": "Set log=none in the private NATAPP INI.",
    "NATAPP_UNMANAGED_INSTANCE": "An existing NATAPP process was found and left untouched. Stop the old manual connector/task before unified startup.",
    "SERVICE_STARTUP_TIMEOUT": "Local API/model did not become ready within the startup deadline.",
    "API_STARTUP_FAILED": "API startup failed; inspect local instance, port and model configuration.",
    "NATAPP_WINDOWS_REQUIRED": "Managed NATAPP startup requires Windows.",
    "NATAPP_RECORD_INVALID": "Cannot verify the old owned tunnel record; inspect data/managed-natapp before retrying.",
    "NATAPP_STOP_FAILED": "Could not stop the verified owned tunnel tree; do not start another instance.",
    "Data directory already has an active instance": "This data directory/owned process already has an owner; stop the old service or maintenance operation before retrying.",
    "Unsupported schema": "Database schema is newer than this executable; use the matching version or restore a compatible backup into a new directory.",
    "Recovery note is required": "Provide a non-empty --note after verifying the old inference process is stopped.",
    "Archive active jobs before recovery": "Active jobs still exist; stop the service and inspect task state before recovery confirmation.",
    "Backup destination must be a new file": "Choose a new backup filename outside the live database file.",
    "Restore requires an existing backup and a new data directory": "Check the source backup and choose a fresh data_directory for restore.",
    "Invalid backup": "The backup failed integrity/schema validation; existing data was not overwritten.",
    "Storage integrity check failed": "Database integrity check failed; preserve it and restore a verified backup into a new directory.",
}


def entrypoint(argv=None):
    try:
        return main(argv)
    except (KeyboardInterrupt, EOFError):
        print("[CANCELLED] Operation cancelled.", file=sys.stderr)
        return 130
    except ModuleNotFoundError:
        print("[DEPENDENCY_MISSING] Install server/requirements.lock.txt using the server's Python interpreter.", file=sys.stderr)
        return 1
    except ValidationError:
        print("[CONFIG_INVALID] Configuration fields are invalid; inspect the selected JSON without printing secrets.", file=sys.stderr)
        return 1
    except FileNotFoundError:
        print("[FILE_NOT_FOUND] Check the selected configuration/source file and absolute paths.", file=sys.stderr)
        return 1
    except PermissionError:
        print("[ACCESS_DENIED] The running account needs configuration/model read and data-directory write permissions.", file=sys.stderr)
        return 1
    except sqlite3.Error:
        print("[STORAGE_ERROR] Check database integrity, permissions and locks; existing data was not replaced.", file=sys.stderr)
        return 1
    except (ValueError, RuntimeError, OSError) as error:
        from aiserver.operations.diagnostics import StartupFailure
        if isinstance(error,StartupFailure):
            problem=error.diagnostic
            print(f'[{problem["code"]}] {problem["stage"]}: {problem["message"]}',file=sys.stderr)
            return 1
        code = str(error)
        if code in ERRORS:
            print("[" + code + "] " + ERRORS[code], file=sys.stderr)
        else:
            print("[OPERATION_FAILED] Check configuration, owned process/instance state and local logs.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(entrypoint())
