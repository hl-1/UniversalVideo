from __future__ import annotations

import hashlib
import json
import secrets
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException

from backend.infrastructure.database import database
from backend.agents import config

worker = ContextVar("agent_worker", default=None)


def now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(encode(value).encode()).hexdigest()


def decode(row):
    if row:
        for key in ("request", "context", "limits", "usage", "result", "arguments", "response", "pricing", "config", "chunk_config", "payload"):
            if isinstance(row.get(key), str):
                row[key] = json.loads(row[key])
    return row


def get(cursor, task_id, uid=None, lock=False):
    cursor.execute("SELECT * FROM agent_tasks WHERE id=%s" + (" AND user_id=%s" if uid is not None else "") +
                   (" FOR UPDATE" if lock else ""), (task_id, uid) if uid is not None else (task_id,))
    row = decode(cursor.fetchone())
    if not row:
        raise HTTPException(404, "任务不存在")
    return row


def event(cursor, row, event_type, payload):
    stamp = now()
    cursor.execute("UPDATE agent_tasks SET next_event_seq=next_event_seq+1 WHERE id=%s", (row["id"],))
    cursor.execute("SELECT next_event_seq FROM agent_tasks WHERE id=%s", (row["id"],))
    seq = cursor.fetchone()["next_event_seq"]
    cursor.execute("INSERT INTO agent_events VALUES (%s,%s,%s,%s,%s,%s)",
                   (row["id"], seq, event_type, encode(payload), stamp, stamp))


def update(cursor, row, **changes):
    allowed = {"status", "context", "usage", "result", "worker_id", "lease_until", "available_at", "started_at", "finished_at", "attempts", "business_ref"}
    if changes.keys() - allowed:
        raise ValueError("Unsupported task field")
    changes["updated_at"] = now()
    cursor.execute("UPDATE agent_tasks SET " + ",".join(f"`{k}`=%s" for k in changes) +
                   ",revision=revision+1 WHERE id=%s AND revision=%s",
                   tuple(encode(v) if k in {"context", "usage", "result"} and v is not None else v for k, v in changes.items()) + (row["id"], row["revision"]))
    if cursor.rowcount != 1:
        raise HTTPException(409, "任务版本已变化，请刷新")
    updated = row | changes | {"revision": row["revision"] + 1}
    event(cursor, updated, "status" if "status" in changes else "progress",
          {"status": updated["status"], "revision": updated["revision"], "node": updated["context"].get("node"),
           "message": updated["context"].get("message", "")})
    return updated


def create(cursor, uid, kind, request, key, *, task_id=None, skill_version="v1", context=None, parent=None, business_ref=None, task_limits=None):
    # The user row serializes creation and all model budget admissions.
    cursor.execute("SELECT id FROM users WHERE id=%s FOR UPDATE", (uid,))
    if not cursor.fetchone():
        raise HTTPException(401, "账号不存在")
    key_hash = digest(key)
    cursor.execute("SELECT * FROM agent_tasks WHERE user_id=%s AND idempotency_key=%s", (uid, key_hash))
    old = decode(cursor.fetchone())
    if old:
        if old["request"] != request or old["kind"] != kind:
            raise HTTPException(409, "幂等键已用于其他请求")
        return old, False
    parent_row = get(cursor, parent, uid, True) if parent else None
    if parent_row:
        root = get(cursor, parent_row["root_id"] or parent, uid, True)
        cursor.execute("SELECT COUNT(*) AS n FROM agent_tasks WHERE root_id=%s", (root["id"],))
        if cursor.fetchone()["n"] >= root["limits"]["children"]:
            raise HTTPException(409, "已达到子任务上限")
    stamp = now()
    row = {"id": task_id or secrets.token_hex(16), "parent_id": parent, "root_id": (parent_row["root_id"] or parent) if parent_row else None,
           "user_id": uid, "kind": kind, "engine": "langgraph", "graph_version": config.GRAPH_VERSION,
           "skill_version": skill_version, "business_ref": business_ref, "status": "queued", "worker_id": None,
           "idempotency_key": key_hash, "revision": 0, "next_event_seq": 0, "attempts": 0,
           "available_at": stamp, "lease_until": None, "started_at": None, "finished_at": None,
           "request": request, "context": context or {}, "limits": task_limits or config.limits(kind), "usage": {}, "result": None,
           "created_at": stamp, "updated_at": stamp}
    cursor.execute("INSERT INTO agent_tasks (" + ",".join(f"`{k}`" for k in row) + ") VALUES (" + ",".join(["%s"] * len(row)) + ")",
                   tuple(encode(v) if k in {"request", "context", "limits", "usage"} else v for k, v in row.items()))
    event(cursor, row, "created", {"kind": kind, "status": "queued"})
    return row, True


def check(task_id, uid=None, *, worker=None):
    with database() as cursor:
        row = get(cursor, task_id, uid)
    assert_running(row, worker)
    return row


def assert_running(row, owner=None):
    current = worker.get()
    owner = owner or (current[1] if current and current[0] == row["id"] else None)
    if row["status"] != "running" or (owner and row["worker_id"] != owner) or (row["lease_until"] and row["lease_until"] <= now()):
        raise HTTPException(409, "任务已停止或租约失效")
    if row["graph_version"] != config.GRAPH_VERSION:
        raise HTTPException(409, "任务图版本不兼容，请使用原版本恢复")
    if row["usage"].get("seconds", 0) > row["limits"]["seconds"]:
        raise HTTPException(409, "任务已达到执行时间上限")


def public(row):
    return {k: (str(v) if k == "user_id" else v.replace(tzinfo=timezone.utc).isoformat() if isinstance(v, datetime) else v)
            for k, v in row.items() if k not in {"request", "context", "idempotency_key", "worker_id", "lease_until"}} | {
                "message": row["context"].get("message", ""), "node": row["context"].get("node", ""),
                "observation": row["context"].get("observation"), "error": row["context"].get("error")}
