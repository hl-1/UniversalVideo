"""Inspect/apply/rollback scene-playlist schema; rollback always backs up first."""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from backend import library
import pymysql


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action",choices=["status","apply","rollback"])
    parser.add_argument("--apply",action="store_true",help="Required for destructive schema rollback")
    args = parser.parse_args()
    if args.action in {"status","apply"}:
        with library.database() as cursor:
            cursor.execute("SELECT * FROM account_migrations WHERE id='music-agent-v1'")
            print(json.dumps(cursor.fetchone(),ensure_ascii=True,default=str))
        return
    if not args.apply:
        parser.error("Rollback requires --apply. Stop the backend and use a compatible version before restarting.")
    connection = pymysql.connect(**library.database_config())
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT GET_LOCK('videodream-music-agent-migration',30) AS acquired")
            if cursor.fetchone()["acquired"]!=1:
                raise RuntimeError("Another migration is active")
            backup = library.backup_library(cursor)
            cursor.execute("SELECT config FROM system_settings WHERE id=1 FOR UPDATE")
            config = json.loads(cursor.fetchone()["config"])
            config["music_agent_enabled"] = False
            cursor.execute("UPDATE system_settings SET config=%s WHERE id=1",(json.dumps(config),))
            cursor.execute("UPDATE music_recommendation_runs SET status='cancelled',worker_id=NULL,lease_until=NULL,revision=revision+1,updated_at=%s WHERE status IN ('pending','running','waiting_auth','waiting_input','interrupted')",(time.time(),))
            cursor.execute("UPDATE resources SET deleted=TRUE,updated_at=%s WHERE kind='music_recommendation'",(time.time(),))
            connection.commit()
            # DDL commits independently in MySQL; the backup is the recovery point.
            cursor.execute("DROP TABLE IF EXISTS music_recommendation_runs")
            cursor.execute("DROP TABLE IF EXISTS music_track_features")
            cursor.execute("SHOW COLUMNS FROM music_playlist_tracks LIKE 'position'")
            if cursor.fetchone():
                cursor.execute("ALTER TABLE music_playlist_tracks DROP COLUMN position")
            cursor.execute("DELETE FROM account_migrations WHERE id='music-agent-v1'")
            connection.commit()
            print(json.dumps({"rollback":True,"backup":str(backup),"playlists_retained":True},ensure_ascii=True))
    finally:
        connection.close()


if __name__=="__main__":
    main()
