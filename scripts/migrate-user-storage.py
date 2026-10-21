"""Offline user storage migration. Run --help for the supported commands."""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pymysql

from backend.core import paths
from backend.infrastructure.database import database_config
from backend.infrastructure import storage_migration as migration


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("plan", "apply", "status", "rollback"))
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--offline-confirmed", action="store_true", help="Confirm all app processes and writers are stopped")
    options = parser.parse_args()
    manifest = options.manifest.resolve()
    if manifest.is_relative_to(paths.DOWNLOAD_DIR.resolve()):
        parser.error("Manifest must be outside downloads")
    if options.command == "status":
        data = json.loads(manifest.read_text(encoding="utf-8"))
        print(json.dumps({"state": data["state"], "files": len(data["entries"]),
                          "statuses": {status: sum(e["status"] == status for e in data["entries"]) for status in ("pending", "moved", "missing")},
                          "conflicts": data["conflicts"], "unmapped": data["unmapped"],
                          "backup_path": data.get("backup_path")}, ensure_ascii=False, indent=2))
        return
    if options.command in {"apply", "rollback"} and not options.offline_confirmed:
        parser.error("Stop the application and all writers, then supply --offline-confirmed")
    with pymysql.connect(**database_config()) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT GET_LOCK(CONCAT(DATABASE(),':user-storage'),0) AS acquired")
            if cursor.fetchone()["acquired"] != 1:
                raise RuntimeError("Another storage migration is running")
            cursor.execute("SELECT * FROM resources")
            resources = cursor.fetchall()
            cursor.execute("SELECT filename,user_id FROM music_downloads")
            downloads = cursor.fetchall()
            if options.command == "plan":
                if manifest.exists():
                    raise ValueError("Manifest already exists; use a new filename")
                data = migration.plan(paths.DOWNLOAD_DIR, resources, downloads)
                migration.save(manifest, data)
            else:
                data = json.loads(manifest.read_text(encoding="utf-8"))
                if data["root"] != str(paths.DOWNLOAD_DIR.resolve()):
                    raise ValueError("Manifest belongs to another downloads directory")
                if options.command == "rollback":
                    data = migration.rollback_plan(paths.DOWNLOAD_DIR)
                    manifest = manifest.with_name(manifest.stem + ".rollback.json")
                else:
                    owners = {(row["kind"], row["resource_key"]): row["user_id"] for row in resources}
                    for entry in data["entries"]:
                        if owners.get(tuple(entry["authority"])) != entry["uid"]:
                            raise ValueError("Resource ownership changed after planning")
                    fresh = migration.plan(paths.DOWNLOAD_DIR, resources, downloads)
                    if fresh["conflicts"]:
                        raise ValueError("Current data has ownership conflicts; regenerate the plan")
                migration.validate(data)
                backup = manifest.parent / (manifest.stem + ".backup-" + str(time.time_ns()))
                migration.backup(paths.DOWNLOAD_DIR, backup, cursor)
                data["backup_path"] = str(backup)
                active = paths.RUN_DIR / "storage-migrations" / "active.json"
                migration.move_files(data, manifest, active=active if options.command == "apply" else None)
                if options.command == "apply":
                    cursor.execute("INSERT INTO account_migrations (id,completed_at,backup_path) VALUES (%s,%s,%s) "
                                   "ON DUPLICATE KEY UPDATE completed_at=VALUES(completed_at),backup_path=VALUES(backup_path)",
                                   (migration.VERSION, time.time(), str(backup)))
                else:
                    cursor.execute("DELETE FROM account_migrations WHERE id=%s", (migration.VERSION,))
                connection.commit()
                data["state"] = "complete" if options.command == "apply" else "rolled_back"
                migration.save(manifest, data)
                active.unlink(missing_ok=True)
            print(json.dumps({"manifest": str(manifest), "state": data["state"], "files": len(data["entries"]),
                              "conflicts": data["conflicts"], "unmapped": data["unmapped"]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
