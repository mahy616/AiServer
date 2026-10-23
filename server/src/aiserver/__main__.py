import argparse
from pathlib import Path
import sqlite3
import sys

from aiserver.operations.lock import InstanceLock
from aiserver.settings import ROOT, Settings
from aiserver.storage.db import Store

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config",help="Absolute JSON settings path; AISERVER_* overrides")
    sub = parser.add_subparsers(dest="command",required=True)
    sub.add_parser("serve")
    p = sub.add_parser("user-add"); p.add_argument("user_id"); p.add_argument("--name",default="Test user")
    p = sub.add_parser("token-create"); p.add_argument("user_id"); p.add_argument("--expires-at-ms",type=int)
    p = sub.add_parser("token-revoke"); p.add_argument("key_id")
    p = sub.add_parser("user-disable"); p.add_argument("user_id")
    p = sub.add_parser("recovery-confirm"); p.add_argument("--note",required=True)
    p = sub.add_parser("backup"); p.add_argument("destination", type=Path)
    p = sub.add_parser("restore"); p.add_argument("source", type=Path)
    sub.add_parser("cleanup")
    args = parser.parse_args()
    if args.config and not Path(args.config).is_absolute():
        parser.error("--config must be absolute")
    default_config = ROOT / "config/server.local.json"
    if not default_config.exists():
        default_config = ROOT / "config/server.ollama.example.json"
    settings = Settings.load(args.config or str(default_config))
    if args.command == "serve":
        import uvicorn
        from aiserver.app import create_app
        uvicorn.run(create_app(settings),host="127.0.0.1",port=settings.port,workers=1,
                    access_log=False,proxy_headers=False,timeout_graceful_shutdown=45)
        return
    store = Store(settings)
    lock = None
    try:
        if args.command in ("recovery-confirm", "backup", "restore", "cleanup"):
            lock = InstanceLock(settings.data_directory).acquire()
        if args.command == "restore":
            store.restore(args.source)
            return
        store.initialize()
        if args.command == "user-add":
            store.user_add(args.user_id,args.name)
        elif args.command == "token-create":
            # Only explicit token-create prints a secret; never redirect to logs.
            print(store.token_create(args.user_id,args.expires_at_ms))
        elif args.command == "token-revoke":
            store.token_revoke(args.key_id)
        elif args.command == "user-disable":
            store.user_disable(args.user_id)
        elif args.command == "recovery-confirm":
            store.confirm_recovery(args.note)
        elif args.command == "backup":
            store.backup(args.destination)
        elif args.command == "cleanup":
            store.cleanup()
    finally:
        if lock:
            lock.release()

if __name__ == "__main__":
    try:
        main()
    except (ValueError, RuntimeError, sqlite3.Error):
        print("Operation failed; check local configuration/storage/instance state.",file=sys.stderr)
        sys.exit(1)
