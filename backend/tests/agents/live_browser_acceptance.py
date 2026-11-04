"""Explicit third-party browser test; all playlist writes stay in a disposable DB."""
from __future__ import annotations

import argparse
from contextlib import ExitStack
import json
import os
from pathlib import Path
import secrets
import time
from unittest.mock import patch

from dotenv import dotenv_values
import pymysql


def main(args):
    os.environ.update({k: v for k, v in dotenv_values(args.test_env, interpolate=False).items() if v is not None})
    from backend.agents import actions, runtime, store
    from backend.infrastructure import database as db
    from backend.modules.accounts import service as accounts
    from backend.tests.support import migrate_test_database

    tag = secrets.token_hex(6)
    cfg = db.database_config()
    name = "videodream_browser_test_" + tag
    administrator = pymysql.connect(**{k: v for k, v in cfg.items() if k != "database"})
    with administrator.cursor() as cursor:
        cursor.execute(f"CREATE DATABASE `{name}` CHARACTER SET utf8mb4")
    report = {"site": "https://music.163.com", "test_user_approval": True, "writes": "disposable database only", "cases": []}
    with ExitStack() as stack:
        stack.enter_context(patch.object(db, "database_config", return_value=cfg | {"database": name}))
        try:
            migrate_test_database(cfg | {"database": name})
            db.schema_ready = False
            uid = accounts.create_user(accounts.Credentials(username="browser_acceptance", password=secrets.token_urlsafe(24)))
            principal = accounts.Principal(uid, "browser_acceptance", "user", "acceptance")
            marker = accounts.principal.set(principal)
            stack.callback(accounts.principal.reset, marker)
            goals = ["纯音乐", "钢琴", "轻音乐", "阅读", "古风", "爵士", "民谣", "摇滚", "运动", "睡眠"][:args.limit]
            for i, goal in enumerate(goals):
                started = time.perf_counter()
                with db.database() as cursor:
                    task, _ = store.create(cursor, uid, "browser_playlist", {"goal": goal, "playlist_name": "测试歌单" + str(i), "site": "netease"}, tag + str(i))
                runtime.run(task["id"])
                with db.database() as cursor:
                    task = store.get(cursor, task["id"])
                    cursor.execute("SELECT * FROM agent_actions WHERE task_id=%s AND kind='write'", (task["id"],))
                    action = store.decode(cursor.fetchone())
                if task["status"] == "waiting_approval" and action and action["arguments"]["tracks"]:
                    # Fixture user explicitly approves exactly the extracted payload.
                    actions.decision(action["id"], uid, task["revision"], action["payload_hash"], True)
                    runtime.run(task["id"])
                with db.database() as cursor:
                    task = store.get(cursor, task["id"])
                with db.database() as cursor:
                    cursor.execute("SELECT tool,arguments,status,error_code FROM agent_actions WHERE task_id=%s ORDER BY created_at", (task["id"],))
                    action_log = [store.decode(a) for a in cursor.fetchall()]
                item = {"actions": action_log, "elements": task["context"].get("observation", {}).get("elements", []), "goal": goal, "status": task["status"], "result": task["result"], "message": task["context"].get("message"), "seconds": round(time.perf_counter()-started, 3), "usage": task["usage"]}
                report["cases"].append(item)
                print(goal, item["status"], item["message"], flush=True)
                Path(args.output).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        finally:
            with administrator.cursor() as cursor:
                cursor.execute(f"DROP DATABASE `{name}`")
            administrator.close()
            db.schema_ready = False
            report["cleanup"] = {"database_removed": name, "browsers_closed": True}
            Path(args.output).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--test-env", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--limit", type=int, default=10)
    main(parser.parse_args())
