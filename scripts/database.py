"""Run explicit database operations. This command never starts the application."""
from __future__ import annotations

import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pymysql
from backend.core import configuration
from backend.infrastructure.database import database_config
from backend.migrations import check_database
from backend.migrations import runner


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("info", "validate", "migrate", "preflight", "baseline", "check", "legacy-data"))
    parser.add_argument("--engine", choices=("local", "docker"), default="local")
    parser.add_argument("--config", type=Path, help="dotenv configuration file; process environment still takes precedence")
    args = parser.parse_args()
    if args.config:
        configuration.LOCAL_CONFIG_PATH = args.config.resolve()
    config = database_config()
    print(f"Target: {config['host']}:{config['port']}/{config['database']}", flush=True)
    if args.action == "baseline":
        print("Database backup: " + str(runner.baseline(config, engine=args.engine)))
    elif args.action == "check":
        check_database()
        print("Application schema version is ready")
    elif args.action in {"preflight", "legacy-data"}:
        with pymysql.connect(**runner.migration_config(config)) as connection, connection.cursor() as cursor:
            runner.preflight(cursor)
            if args.action == "legacy-data":
                backup = runner.backup_database(cursor)
                from backend.modules.accounts import service as accounts
                from backend.modules.video import store as video_store
                from backend.migrations import device_music
                accounts.migrate(cursor)
                video_store.migrate(cursor)
                device_music.migrate(cursor)
                connection.commit()
                print("Legacy data migrated; backup: " + str(backup))
            else:
                print("V1 structure verified (tables, columns, defaults, indexes, foreign keys, collations)")
    else:
        runner.run_flyway(config, args.action, engine=args.engine, user_id_files=args.action == "migrate")


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, pymysql.MySQLError) as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
