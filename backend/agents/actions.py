from __future__ import annotations

import secrets
from datetime import timedelta

from fastapi import HTTPException

from backend.agents import config, store
from backend.infrastructure.database import database


def prepare(task_id, tool, arguments, *, step, approval=False, observation=None):
    with database() as cursor:
        task = store.get(cursor, task_id, lock=True)
        store.assert_running(task)
        if task["status"] != "running":
            raise HTTPException(409, "任务已停止")
        step_key, payload_hash = store.digest(step), store.digest(arguments)
        cursor.execute("SELECT * FROM agent_actions WHERE task_id=%s AND step_key=%s FOR UPDATE", (task_id, step_key))
        old = store.decode(cursor.fetchone())
        if old:
            if old["payload_hash"] != payload_hash or old["observation_hash"] != observation or old["tool"] != tool:
                raise HTTPException(409, "动作内容或观察版本已变化")
            return old
        stamp = store.now()
        row = {"id": secrets.token_hex(16), "task_id": task_id, "step_key": step_key, "payload_hash": payload_hash,
               "observation_hash": observation, "kind": "write" if approval else "read", "tool": tool,
               "status": "pending", "approval_state": "pending" if approval else "not_required",
               "arguments": arguments, "approval_expires_at": stamp + timedelta(hours=24) if approval else None,
               "created_at": stamp, "updated_at": stamp}
        cursor.execute("INSERT INTO agent_actions (" + ",".join(f"`{k}`" for k in row) + ") VALUES (" + ",".join(["%s"] * len(row)) + ")",
                       tuple(store.encode(v) if k == "arguments" else v for k, v in row.items()))
        return row


def execute(action, callback, reconcile=None):
    with database() as cursor:
        task = store.get(cursor, action["task_id"], lock=True)
        cursor.execute("SELECT * FROM agent_actions WHERE id=%s FOR UPDATE", (action["id"],))
        action = store.decode(cursor.fetchone())
        if action["status"] == "succeeded":
            return action["result"]
        store.assert_running(task)
        if task["status"] != "running":
            raise HTTPException(409, "任务已停止")
        if action["kind"] == "write" and (action["approval_state"] != "approved" or action["approved_by"] != task["user_id"] or action["approval_expires_at"] <= store.now()):
            raise HTTPException(409, "写入审批未通过或已过期")
        uncertain = action["status"] in {"executing", "unknown"}
        if not uncertain:
            cursor.execute("UPDATE agent_actions SET status='executing',attempts=attempts+1,started_at=%s,updated_at=%s WHERE id=%s", (store.now(), store.now(), action["id"]))
    if uncertain:
        result = reconcile(action) if reconcile else None
        if result is None:
            raise HTTPException(409, "动作结果不明，须先核对实际结果")
    else:
        try:
            result = callback(action["arguments"])
        except Exception:
            with database() as cursor:
                cursor.execute("UPDATE agent_actions SET status='unknown',error_code='execution_unknown',updated_at=%s WHERE id=%s", (store.now(), action["id"]))
            raise
    with database() as cursor:
        cursor.execute("UPDATE agent_actions SET status='succeeded',result=%s,finished_at=%s,updated_at=%s WHERE id=%s", (store.encode(result), store.now(), store.now(), action["id"]))
    store.check(action["task_id"])
    return result


def decision(action_id, uid, revision, payload_hash, approved):
    with database() as cursor:
        cursor.execute("SELECT task_id FROM agent_actions WHERE id=%s", (action_id,))
        found = cursor.fetchone()
        if not found:
            raise HTTPException(404, "审批不存在")
        task = store.get(cursor, found["task_id"], uid, True)
        cursor.execute("SELECT * FROM agent_actions WHERE id=%s FOR UPDATE", (action_id,))
        action = store.decode(cursor.fetchone())
        wanted = "approved" if approved else "rejected"
        if action["approval_state"] == wanted and action["payload_hash"] == payload_hash and action["approved_by"] == uid:
            return task
        if task["revision"] != revision or task["status"] != "waiting_approval" or action["approval_state"] != "pending" or action["payload_hash"] != payload_hash or action["approval_expires_at"] <= store.now():
            raise HTTPException(409, "审批内容、任务版本或有效期已变化")
        if action["observation_hash"] != task["context"].get("approval_observation"):
            raise HTTPException(409, "审批观察版本已变化")
        cursor.execute("UPDATE agent_actions SET approval_state=%s,approved_by=%s,updated_at=%s WHERE id=%s", (wanted, uid, store.now(), action_id))
        return store.update(cursor, task, status="queued" if approved else "cancelled", worker_id=None, lease_until=None,
                            context=task["context"] | {"resume": {"action_id": action_id, "approved": approved}},
                            finished_at=None if approved else store.now())
