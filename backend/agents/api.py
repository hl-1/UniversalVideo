from __future__ import annotations

import asyncio
from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from backend.agents import actions, config, runtime, store
from backend.infrastructure.database import database
from backend.modules.accounts import service as accounts
from backend.modules.knowledge.service import public_result

router = APIRouter(prefix="/api/agent", tags=["agents"], dependencies=[Depends(config.require_enabled)])


class CreateTask(BaseModel):
    kind: Literal["browser_playlist", "knowledge_query"]
    request: dict


class Version(BaseModel):
    revision: int = Field(ge=0)


class Decision(Version):
    payload_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    approved: bool


class ReconcileUsage(Version):
    actual_tokens: int = Field(ge=0, le=1_000_000_000)
    provider_reference: str = Field(min_length=3, max_length=200)


@router.post("/model-calls/{call_id}/reconcile")
def reconcile_usage(call_id: str, payload: ReconcileUsage):
    admin = accounts.admin()
    from backend.agents import gateway
    return gateway.reconcile(call_id, payload.revision, payload.actual_tokens, payload.provider_reference, admin.id)


@router.post("/tasks", status_code=202)
def create_task(payload: CreateTask, idempotency_key: str = Header(min_length=8, max_length=200)):
    uid = accounts.current().id
    if payload.kind == "browser_playlist":
        from backend.modules.browser_agent.service import BrowserRequest, require_enabled
        require_enabled()
        request = BrowserRequest.model_validate(payload.request).model_dump()
    else:
        from backend.modules.knowledge.api import QueryRequest
        from backend.modules.knowledge.service import allowed
        request = QueryRequest.model_validate(payload.request).model_dump()
        with database() as cursor:
            allowed(cursor, uid, request["kb_ids"])
    with database() as cursor:
        row, _ = store.create(cursor, uid, payload.kind, request, idempotency_key)
    runtime.wake()
    return {"task_id": row["id"], "task": store.public(row)}


@router.get("/tasks")
def list_tasks(status: str = "", kind: str = "", page: int = Query(1, ge=1)):
    with database() as cursor:
        where, args = "user_id=%s", [accounts.current().id]
        for field, value in (("status", status), ("kind", kind)):
            if value:
                where += f" AND {field}=%s"
                args.append(value)
        cursor.execute("SELECT COUNT(*) AS n FROM agent_tasks WHERE " + where, args)
        count = cursor.fetchone()["n"]
        cursor.execute("SELECT * FROM agent_tasks WHERE " + where + " ORDER BY created_at DESC LIMIT 20 OFFSET %s", (*args, (page - 1) * 20))
        rows = [store.decode(r) for r in cursor.fetchall()]
        items = []
        for row in rows:
            try:
                items.append(public_result(cursor, row))
            except HTTPException:
                items.append(store.public(row | {"result": {"unavailable": "知识权限或版本已变化"}}))
        return {"items": items, "total": count}


@router.get("/tasks/{task_id}")
def detail(task_id: str):
    with database() as cursor:
        row = store.get(cursor, task_id, accounts.current().id)
        result = public_result(cursor, row)
        cursor.execute("SELECT id,kind,tool,status,approval_state,arguments,result,payload_hash,approval_expires_at FROM agent_actions WHERE task_id=%s ORDER BY created_at", (task_id,))
        result["actions"] = [store.decode(r) for r in cursor.fetchall()]
        cursor.execute("SELECT id,kind,status,revision FROM agent_tasks WHERE parent_id=%s AND user_id=%s", (task_id, row["user_id"]))
        result["children"] = cursor.fetchall()
        cursor.execute("SELECT id,phase,provider,model,state,reserved_tokens,actual_tokens,`usage`,pricing,started_at,settled_at FROM agent_model_calls WHERE root_task_id=%s ORDER BY created_at", (row["root_id"] or task_id,))
        result["model_calls"] = [store.decode(r) for r in cursor.fetchall()]
        return result


@router.post("/tasks/{task_id}/cancel")
def cancel(task_id: str, payload: Version):
    uid = accounts.current().id
    with database() as cursor:
        row = store.get(cursor, task_id, uid, True)
        if row["status"] in config.TERMINAL:
            return store.public(row)
        if row["revision"] != payload.revision:
            raise HTTPException(409, "任务版本已变化")
        row = store.update(cursor, row, status="cancelled", worker_id=None, lease_until=None, finished_at=store.now())
        cursor.execute("SELECT id FROM agent_tasks WHERE root_id=%s AND status NOT IN ('succeeded','partial','failed','cancelled')", (row["id"],))
        for item in cursor.fetchall():
            store.update(cursor, store.get(cursor, item["id"], uid, True), status="cancelled", worker_id=None, lease_until=None, finished_at=store.now())
    if row["kind"] in {"music_scene", "music_profile"}:
        from backend.agents.music import cancel_business
        cancel_business(row)
    from backend.agents import gateway
    gateway.cancel(task_id)
    return store.public(row)


@router.post("/tasks/{task_id}/resume", status_code=202)
def resume(task_id: str, payload: Version):
    with database() as cursor:
        row = store.get(cursor, task_id, accounts.current().id, True)
        if row["revision"] != payload.revision or row["status"] not in {"interrupted", "waiting_input", "waiting_auth"}:
            raise HTTPException(409, "当前状态或版本不允许恢复")
        if row["graph_version"] != config.GRAPH_VERSION:
            raise HTTPException(409, "须使用任务原图版本恢复")
        cursor.execute("SELECT 1 FROM agent_model_calls WHERE root_task_id=%s AND state IN ('unknown','dispatched','reserved') LIMIT 1", (row["root_id"] or row["id"],))
        if cursor.fetchone():
            raise HTTPException(409, "模型调用尚未核对，不能恢复")
        if row["kind"] in {"music_scene", "music_profile"}:
            raise HTTPException(409, "请通过原音乐或画像页面补充需求后继续")
        row = store.update(cursor, row, status="queued", available_at=store.now(), context=row["context"] | {"resume": {"continue": True}})
    runtime.wake()
    return {"task_id": task_id, "task": store.public(row)}


@router.post("/tasks/{task_id}/retry", status_code=202)
def retry(task_id: str, payload: Version):
    with database() as cursor:
        row = store.get(cursor, task_id, accounts.current().id, True)
        if row["revision"] != payload.revision or row["status"] != "failed" or row["kind"] in {"music_scene", "music_profile"}:
            raise HTTPException(409, "当前任务不能重试，请使用原业务入口")
        cursor.execute("SELECT 1 FROM agent_model_calls WHERE root_task_id=%s AND state<>'settled' LIMIT 1", (row["root_id"] or row["id"],))
        if cursor.fetchone():
            raise HTTPException(409, "调用用量待核对，禁止重试")
        row = store.update(cursor, row, status="queued", available_at=store.now(), finished_at=None,
                           context=row["context"] | {"error": None, "message": "等待恢复未完成节点"})
    runtime.wake()
    return {"task_id": task_id, "task": store.public(row)}


@router.post("/actions/{action_id}/decision")
def decision(action_id: str, payload: Decision):
    row = actions.decision(action_id, accounts.current().id, payload.revision, payload.payload_hash, payload.approved)
    runtime.wake()
    return store.public(row)


@router.get("/tasks/{task_id}/events")
async def events(task_id: str, request: Request, after: int = Query(0, ge=0), last_event_id: str | None = Header(None)):
    uid = accounts.current().id
    bearer = request.headers.get("authorization", "")[7:]
    with database() as cursor:
        store.get(cursor, task_id, uid)
    try:
        sequence = max(after, int(last_event_id or 0))
    except ValueError:
        raise HTTPException(422, "事件序号无效") from None
    def read_events(seq):
        user = accounts.authenticate(bearer)
        if user.id != uid:
            raise HTTPException(403, "会话已变化")
        with database() as cursor:
            row = store.get(cursor, task_id, uid)
            cursor.execute("SELECT * FROM agent_events WHERE task_id=%s AND sequence_no>%s ORDER BY sequence_no LIMIT 100", (task_id, seq))
            return row, cursor.fetchall()
    async def stream():
        nonlocal sequence
        while not await request.is_disconnected():
            try:
                row, items = await asyncio.to_thread(read_events, sequence)
            except Exception:
                return
            for item in items:
                sequence = item["sequence_no"]
                payload = item["payload"] if isinstance(item["payload"], str) else store.encode(item["payload"])
                yield f"id: {sequence}\nevent: {item['event_type']}\ndata: {payload}\n\n"
            if row["status"] in (config.TERMINAL | config.WAITING) and sequence >= row["next_event_seq"]:
                return
            if not items:
                yield ": keepalive\n\n"
            await asyncio.sleep(1)
    return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.post("/tasks/{task_id}/takeover")
def takeover(task_id: str, payload: dict):
    from backend.modules.browser_agent.takeover import handle
    return handle(task_id, accounts.current(), payload)
