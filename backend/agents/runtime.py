from __future__ import annotations

import logging
import secrets
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

from langgraph.types import Command

from backend.agents import config, gateway, store
from backend.infrastructure.database import database
from backend.modules.accounts import service as accounts

logger = logging.getLogger(__name__)
_stop, _wake = threading.Event(), threading.Event()
_thread = None
_pool = None
_inflight = set()
_lock = threading.Lock()
_next_retention = 0


def wake():
    _wake.set()


def graph_for(kind):
    if kind == "knowledge_ingest":
        from backend.modules.knowledge.service import ingest_graph
        return ingest_graph()
    if kind == "knowledge_query":
        from backend.modules.knowledge.service import answer_graph
        return answer_graph()
    if kind == "browser_playlist":
        from backend.modules.browser_agent.service import graph
        return graph()
    raise ValueError("Unsupported worker task")


def invoke_graph(graph, task_id, initial=None):
    row = store.check(task_id)
    cfg = {"configurable": {"thread_id": task_id, "graph_version": row["graph_version"]}, "recursion_limit": 150}
    snapshot = graph.get_state(cfg)
    value = initial or {"task_id": task_id}
    if snapshot.values:
        value = Command(resume=row["context"].get("resume", {})) if snapshot.interrupts else None
        if not snapshot.next:
            return snapshot.values
    token = gateway.current_task.set(task_id)
    try:
        return graph.invoke(value, cfg)
    finally:
        gateway.current_task.reset(token)


def claim(task_id):
    with database() as cursor:
        row = store.get(cursor, task_id, lock=True)
        if row["status"] != "queued" or row["available_at"] > store.now():
            return None
        worker = secrets.token_hex(16)
        return store.update(cursor, row, status="running", worker_id=worker,
                            lease_until=store.now() + timedelta(seconds=45), started_at=row["started_at"] or store.now(),
                            attempts=row["attempts"] + 1)


def heartbeat(task_id, worker, stop, clock):
    while not stop.wait(10):
        with database() as cursor:
            row = store.get(cursor, task_id, lock=True)
            if row["worker_id"] != worker or row["status"] != "running":
                return
            tick = time.monotonic()
            seconds = row["usage"].get("seconds", 0) + tick - clock[0]
            clock[0] = tick
            expired = seconds > row["limits"]["seconds"]
            store.update(cursor, row, lease_until=None if expired else store.now() + timedelta(seconds=45),
                         usage=row["usage"] | {"seconds": seconds},
                         **({"status": "failed", "finished_at": store.now(), "context": row["context"] | {"error": "执行超时", "message": "已达到自动执行时长上限"}} if expired else {}))
            if expired:
                return


def run(task_id):
    marker = None
    worker_marker = None
    row = None
    stop = threading.Event()
    thread = None
    started = time.monotonic()
    clock = [started]
    try:
        row = claim(task_id)
        if not row:
            return
        worker_marker = store.worker.set((task_id, row["worker_id"]))
        user = accounts.get_user(row["user_id"])
        marker = accounts.principal.set(accounts.Principal(user["id"], user["username"], user["role"]))
        thread = threading.Thread(target=heartbeat, args=(task_id, row["worker_id"], stop, clock), daemon=True)
        thread.start()
        result = invoke_graph(graph_for(row["kind"]), task_id)
        with database() as cursor:
            current = store.get(cursor, task_id, lock=True)
            if current["status"] != "running" or current["worker_id"] != row["worker_id"]:
                return
            if result.get("__interrupt__"):
                raise RuntimeError("Graph interrupted without a persisted waiting state")
            if row["kind"] == "knowledge_query":
                from backend.modules.knowledge.service import validate_evidence
                validate_evidence(cursor, row["user_id"], result.get("result", {}).get("citations", []))
            store.update(cursor, current, status="succeeded", result=result.get("result", {}), worker_id=None,
                         lease_until=None, finished_at=store.now(), context=current["context"] | {"message": "任务已完成"})
    except Exception as exc:
        logger.warning("Agent stopped: task=%s type=%s", task_id, type(exc).__name__)
        if row:
            with database() as cursor:
                current = store.get(cursor, task_id, lock=True)
                if current["status"] == "running" and current["worker_id"] == row["worker_id"]:
                    waiting = isinstance(exc, gateway.UsagePending)
                    message = "模型用量待核对" if waiting else "任务执行失败，请检查配置或重试"
                    from fastapi import HTTPException
                    if isinstance(exc, HTTPException):
                        message = str(exc.detail)[:300]
                    store.update(cursor, current, status="waiting_usage" if waiting else "failed", worker_id=None, lease_until=None,
                                 finished_at=None if waiting else store.now(), context=current["context"] | {"error": message, "message": message})
                    if row["kind"] == "knowledge_ingest":
                        cursor.execute("UPDATE knowledge_document_versions SET status='failed',error=%s,updated_at=%s WHERE document_id=%s AND version=%s AND status<>'published'",
                                       (message, store.now(), row["request"]["document_id"], row["request"]["version"]))
                        cursor.execute("UPDATE knowledge_documents SET status=IF(active_version IS NULL,'failed','published'),updated_at=%s WHERE id=%s AND status<>'deleted'", (store.now(), row["request"]["document_id"]))
    finally:
        stop.set()
        if thread:
            thread.join(timeout=1)
        if row:
            with database() as cursor:
                current = store.get(cursor, task_id, lock=True)
                # Record the last partial heartbeat interval, excluding human waits.
                seconds = current["usage"].get("seconds", 0) + time.monotonic() - clock[0]
                if current["attempts"] == row["attempts"]:
                    store.update(cursor, current, usage=current["usage"] | {"seconds": round(seconds, 3)})
        if marker is not None:
            accounts.principal.reset(marker)
        if worker_marker is not None:
            store.worker.reset(worker_marker)
        with _lock:
            _inflight.discard(task_id)
        wake()


def maintenance():
    global _next_retention
    with database() as cursor:
        cursor.execute("SELECT id FROM agent_tasks WHERE status='running' AND lease_until<%s AND kind NOT IN ('music_scene','music_profile')", (store.now(),))
        for item in cursor.fetchall():
            row = store.get(cursor, item["id"], lock=True)
            cursor.execute("UPDATE agent_model_calls SET state=IF(dispatched,'unknown','settled'),actual_tokens=IF(dispatched,NULL,0),updated_at=%s WHERE task_id=%s AND state IN ('reserved','dispatched')", (store.now(), row["id"]))
            cursor.execute("SELECT 1 FROM agent_model_calls WHERE task_id=%s AND state='unknown' LIMIT 1", (row["id"],))
            unknown = bool(cursor.fetchone())
            store.update(cursor, row, status="waiting_usage" if unknown else "interrupted", worker_id=None, lease_until=None,
                         context=row["context"] | {"message": "服务中断，等待恢复"})
        cursor.execute("SELECT id FROM agent_tasks WHERE status IN ('waiting_input','waiting_auth','waiting_approval') AND updated_at<%s", (store.now() - timedelta(hours=24),))
        for item in cursor.fetchall():
            row = store.get(cursor, item["id"], lock=True)
            store.update(cursor, row, status="cancelled", finished_at=store.now(), context=row["context"] | {"message": "人工等待已超时"})
    from backend.modules.knowledge import storage
    with database() as cursor:
        cursor.execute("SELECT id,kb_id FROM knowledge_documents WHERE status='deleted' AND EXISTS (SELECT 1 FROM knowledge_document_versions v WHERE v.document_id=knowledge_documents.id AND v.status<>'deleted') LIMIT 5")
        deleted = cursor.fetchall()
    for doc in deleted:
        try:
            storage.remove_document(doc["kb_id"], doc["id"])
            with database() as cursor:
                cursor.execute("UPDATE knowledge_document_versions SET status='deleted',updated_at=%s WHERE document_id=%s", (store.now(), doc["id"]))
        except Exception:
            logger.warning("Knowledge cleanup deferred: document=%s", doc["id"])
    with database() as cursor:
        cursor.execute("SELECT b.id FROM knowledge_bases b WHERE b.status='deleted' AND NOT EXISTS (SELECT 1 FROM knowledge_documents d JOIN knowledge_document_versions v ON v.document_id=d.id WHERE d.kb_id=b.id AND v.status<>'deleted') LIMIT 5")
        for kb in cursor.fetchall():
            cursor.execute("DELETE v FROM knowledge_document_versions v JOIN knowledge_documents d ON d.id=v.document_id WHERE d.kb_id=%s", (kb["id"],))
            cursor.execute("DELETE FROM knowledge_documents WHERE kb_id=%s", (kb["id"],))
            cursor.execute("DELETE FROM knowledge_members WHERE kb_id=%s", (kb["id"],))
            cursor.execute("DELETE FROM knowledge_bases WHERE id=%s AND status='deleted'", (kb["id"],))
    if time.monotonic() >= _next_retention:
        retention()
        _next_retention = time.monotonic() + 3600


def retention():
    """Bounded project-only cleanup; keep unresolved calls and dependencies."""
    with database() as cursor:
        cursor.execute("DELETE FROM agent_events WHERE created_at<%s LIMIT 1000", (store.now() - timedelta(days=30),))
        cursor.execute("DELETE c FROM agent_checkpoints c JOIN agent_tasks t ON t.id=c.task_id WHERE t.status IN ('succeeded','failed','cancelled') AND t.finished_at<%s", (store.now() - timedelta(days=7),))
        cursor.execute("SELECT t.id FROM agent_tasks t WHERE t.status IN ('succeeded','failed','cancelled') AND t.finished_at<%s AND t.business_ref IS NULL AND NOT EXISTS (SELECT 1 FROM agent_tasks child WHERE child.parent_id=t.id OR child.root_id=t.id) AND NOT EXISTS (SELECT 1 FROM agent_model_calls m WHERE m.root_task_id=COALESCE(t.root_id,t.id) AND m.state<>'settled') LIMIT 50", (store.now() - timedelta(days=90),))
        for item in cursor.fetchall():
            row = store.get(cursor, item["id"], lock=True)
            for table in ("agent_checkpoints", "agent_events", "agent_actions", "agent_model_calls"):
                cursor.execute(f"DELETE FROM {table} WHERE task_id=%s", (row["id"],))
            cursor.execute("DELETE FROM agent_tasks WHERE id=%s", (row["id"],))


def loop():
    next_maintenance = 0
    while not _stop.is_set():
        try:
            if time.monotonic() >= next_maintenance:
                maintenance()
                next_maintenance = time.monotonic() + 30
            with database() as cursor:
                cursor.execute("SELECT id FROM agent_tasks WHERE status='queued' AND available_at<=%s AND kind NOT IN ('music_scene','music_profile') ORDER BY created_at LIMIT 20", (store.now(),))
                ids = [r["id"] for r in cursor.fetchall()]
            with _lock:
                for task_id in ids:
                    if task_id not in _inflight and len(_inflight) < config.integer("VIDEODREAM_AGENT_CONCURRENCY", 2, maximum=8):
                        _inflight.add(task_id)
                        _pool.submit(run, task_id)
        except Exception:
            logger.warning("Agent scheduler unavailable; retry deferred")
        _wake.wait(2)
        _wake.clear()


def start():
    global _thread, _pool
    if not config.enabled():
        return
    if _thread and _thread.is_alive():
        return
    with database() as cursor:
        if not config.available(cursor):
            return
    _stop.clear()
    _pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="agent")
    _thread = threading.Thread(target=loop, name="agent-scheduler", daemon=True)
    _thread.start()


def stop():
    global _thread
    _stop.set()
    wake()
    with _lock:
        running = list(_inflight)
    for task_id in running:
        with database() as cursor:
            row = store.get(cursor, task_id, lock=True)
            if row["status"] == "running":
                store.update(cursor, row, status="interrupted", worker_id=None, lease_until=None,
                             context=row["context"] | {"message": "服务停止，等待恢复"})
        gateway.cancel(task_id)
    if _thread:
        _thread.join(timeout=3)
        _thread = None
    if _pool:
        _pool.shutdown(wait=False, cancel_futures=True)


def cancel_user(uid, key=None):
    if key is not None:
        return
    with database() as cursor:
        cursor.execute("SELECT id FROM agent_tasks WHERE user_id=%s AND status NOT IN ('succeeded','partial','failed','cancelled')", (uid,))
        tasks = cursor.fetchall()
        for item in tasks:
            row = store.get(cursor, item["id"], lock=True)
            store.update(cursor, row, status="cancelled", lease_until=None, worker_id=None, finished_at=store.now())
    for item in tasks:
        gateway.cancel(item["id"])
