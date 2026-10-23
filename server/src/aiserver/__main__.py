import argparse
from pathlib import Path
import sqlite3
import sys

from aiserver.operations.lock import InstanceLock
from aiserver.settings import Settings
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
    args = parser.parse_args()
    if args.config and not Path(args.config).is_absolute():
        parser.error("--config must be absolute")
    settings = Settings.load(args.config)
    if args.command == "serve":
        import uvicorn
        from aiserver.app import create_app
        uvicorn.run(create_app(settings),host="127.0.0.1",port=settings.port,workers=1,
                    access_log=False,proxy_headers=False,timeout_graceful_shutdown=45)
        return
    store = Store(settings)
    lock = None
    try:
        if args.command == "recovery-confirm":
            lock = InstanceLock(settings.data_directory).acquire()
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
    finally:
        if lock:
            lock.release()

if __name__ == "__main__":
    try:
        main()
    except (ValueError, RuntimeError, sqlite3.Error):
        print("Operation failed; check local configuration/storage/instance state.",file=sys.stderr)
        sys.exit(1)
