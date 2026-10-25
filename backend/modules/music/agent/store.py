from __future__ import annotations

import hashlib
import json
import secrets
import time
from contextvars import ContextVar
from contextlib import contextmanager

from fastapi import HTTPException

from backend.modules.accounts import service as accounts
from backend.modules.music import library
from backend.modules.music.agent.models import ACTIVE, MAX_SNAPSHOT_BYTES, POLICY_VERSION, ParsedNeed


execution_lease = ContextVar("scene_playlist_execution_lease",default=None)


@contextmanager
def worker_scope(run_id, worker):
    marker = execution_lease.set((run_id, worker))
    try:
        yield
    finally:
        execution_lease.reset(marker)


def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def tool_signature(name, arguments):
    return json.dumps([name, arguments], sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def decode(row):
    if row:
        for key in ("request", "context", "steps", "result"):
            if isinstance(row[key], str):
                row[key] = json.loads(row[key])
    return row


def get(cursor, run_id, uid=None, lock=False, include_deleted=False):
    cursor.execute("SELECT * FROM music_recommendation_runs WHERE run_id=%s" + (" FOR UPDATE" if lock else ""), (run_id,))
    row = decode(cursor.fetchone())
    if not row or (row["deleted"] and not include_deleted) or (uid is not None and row["user_id"] != uid):
        raise HTTPException(404, "场景歌单任务不存在")
    execution = execution_lease.get()
    if execution and execution[0]==run_id and (row["status"]!="running" or row["worker_id"]!=execution[1]
            or not row["lease_until"] or row["lease_until"]<=time.time()):
        error = HTTPException(409,"任务已停止")
        error.agent_policy_error = True
        raise error
    return row


def sync_resource(cursor, row):
    payload = {"kind": row["request"].get("kind", "scene_playlist"), "run_id": row["run_id"], "status": row["status"], "need": row["request"]["need"],
               "message": row["context"].get("message", ""),
               "result": {"title": row["result"].get("title", row["request"]["need"]),
                          "count": len(row["result"].get("tracks", []))},
               "saved_playlist_id": row["saved_playlist_id"]}
    cursor.execute("""INSERT INTO resources (kind,resource_key,user_id,payload,created_at,updated_at,deleted)
        VALUES ('music_recommendation',%s,%s,%s,%s,%s,%s) ON DUPLICATE KEY UPDATE
        payload=VALUES(payload),updated_at=VALUES(updated_at),deleted=VALUES(deleted)""",
        (row["run_id"], row["user_id"], encode(payload), row["created_at"], row["updated_at"], row["deleted"]))


def update(cursor, row, **changes):
    allowed = {"status", "request", "context", "steps", "result", "result_version", "saved_playlist_id",
               "saved_at", "worker_id", "lease_until", "deleted"}
    if not set(changes) <= allowed:
        raise ValueError("Invalid run update")
    merged = row | changes
    if row["status"] == "running" and merged["status"] != "running":
        context = dict(merged["context"])
        used = dict(context.get("budget", {}))
        stopped_at = min(time.time(), row["lease_until"] or time.time())
        elapsed = row["context"].get("base_seconds", 0) + max(0, stopped_at-row["context"].get("active_started_at", stopped_at))
        used["seconds"] = min(context["limits"]["seconds"], elapsed)
        merged["context"] = context | {"budget": used}
        changes["context"] = merged["context"]
        if merged["result"]:
            merged["result"] = merged["result"] | {"budget": used}
            changes["result"] = merged["result"]
    if len(encode({k: merged[k] for k in ("request", "context", "steps", "result")}).encode()) > MAX_SNAPSHOT_BYTES:
        raise HTTPException(413, "任务快照超过 512 KiB")
    merged["updated_at"] = time.time()
    fields = list(changes) + ["updated_at"]
    values = [encode(merged[k]) if k in {"request", "context", "steps", "result"} else merged[k] for k in fields]
    cursor.execute("UPDATE music_recommendation_runs SET " + ",".join(f"`{k}`=%s" for k in fields)
                   + ",revision=revision+1 WHERE run_id=%s AND revision=%s", (*values, row["run_id"], row["revision"]))
    if cursor.rowcount != 1:
        raise HTTPException(409, "任务已更新，请刷新")
    merged["revision"] += 1
    sync_resource(cursor, merged)
    return merged


def recover(cursor):
    from backend.modules.music.discovery import usage
    cursor.execute("SELECT run_id,user_id FROM music_recommendation_runs WHERE deleted=FALSE AND status IN ('pending','running') AND lease_until<%s ORDER BY user_id", (time.time(),))
    for candidate in cursor.fetchall():
        usage.lock(cursor,candidate['user_id'])
        row=get(cursor,candidate['run_id'],lock=True)
        if row['status'] not in {'pending','running'} or not row['lease_until'] or row['lease_until']>=time.time(): continue
        if row['status']=='pending' and row['context'].get('background'):
            update(cursor,row,lease_until=time.time()+60)
            continue
        context = row["context"]
        requests = [r | {"state":"unconfirmed", "error":"工作进程中断，用量待确认"}
                    if r["state"] == "in_flight" else r for r in context.get("model_requests", [])]
        if any(r["state"] == "unconfirmed" for r in requests):
            context = context | {"model_requests":requests, "model_stop":"执行中断，模型用量待确认，请手动重试新任务"}
        update(cursor, row, status="interrupted", worker_id=None, lease_until=None,
               context=context | {"message": "服务执行中断，请查看任务状态"})
        if row['request'].get('kind')=='daily_discovery': usage.sync_candidate(cursor,row['user_id'])


def create(request, key, skill_version, parent=None, seed=None):
    if "parsed" in request:
        request = request | {"parsed":ParsedNeed.model_validate(request["parsed"]).model_dump()}
    uid = accounts.current().id
    key_hash = hashlib.sha256(key.encode()).hexdigest()
    config = accounts.settings()
    with library.database() as cursor:
        # A user row serializes concurrent create/continue operations.
        from backend.modules.music.discovery import usage
        usage.lock(cursor,uid)
        recover(cursor)
        cursor.execute("SELECT * FROM music_recommendation_runs WHERE user_id=%s AND idempotency_key=%s", (uid, key_hash))
        existing = decode(cursor.fetchone())
        if existing:
            if existing["deleted"]:
                raise HTTPException(410, "此幂等请求已删除，请使用新的请求键")
            if existing["parent_run_id"] != parent or bool(existing["context"].get("fill_missing")) != (seed is not None) or existing["request"]["need"] != request["need"] or existing["request"].get("preferences", {}) != request.get("preferences", {}) or existing["request"].get("rule_version_id") != request.get("rule_version_id"):
                raise HTTPException(409, "幂等键已用于其他需求")
            return existing, False
        cursor.execute("SELECT COUNT(*) AS n FROM music_recommendation_runs WHERE user_id=%s AND deleted=FALSE AND status IN ('pending','running')", (uid,))
        if cursor.fetchone()["n"]:
            raise HTTPException(409, "已有场景歌单任务正在执行")
        now = time.time()
        row = dict(run_id=secrets.token_hex(16), user_id=uid, parent_run_id=parent, status="pending", request=request,
                   context={"session_id": accounts.current().session_id, "budget": {}, "message": "等待执行", "candidates": [],
                            "limits": {k.removeprefix("music_agent_"): v for k,v in config.items() if k.startswith("music_agent_")}},
                   steps=[], result={}, policy_version=POLICY_VERSION, skill_version=skill_version, idempotency_key=key_hash,
                   revision=0, result_version=0, saved_playlist_id=None, saved_at=None, worker_id=None,
                   lease_until=now+60, created_at=now, updated_at=now, deleted=False)
        if seed is not None:
            candidates = []
            for track in seed:
                track = dict(track)
                track['user_id'] = uid
                if now - (track.get('validation', {}).get('checked_at') or 0) > 3600:
                    track.pop('validation', None)
                analysis = track.get('analysis') or {}
                ttl = 604800 if (analysis.get('features') or {}).get('vocal_role', 'unknown') != 'unknown' else 86400
                if now - (analysis.get('checked_at') or 0) > ttl:
                    track.pop('analysis', None)
                candidates.append(track)
            row['context'].update(candidates=candidates, fill_missing=True)
        insert(cursor, row)
        return row, True


def insert(cursor, row):
    """Insert using the caller's transaction (daily quota and run are atomic)."""
    columns = list(row)
    cursor.execute("INSERT INTO music_recommendation_runs (" + ",".join(f"`{k}`" for k in columns) + ") VALUES (" + ",".join(["%s"]*len(columns)) + ")",
                   tuple(encode(row[k]) if k in {"request","context","steps","result"} else row[k] for k in columns))
    sync_resource(cursor, row)
    if row["request"].get("effective_rule"):
        rule = row["request"]["effective_rule"]
        cursor.execute("INSERT INTO social_rule_run_bindings (run_id,version_id,effective_rule,content_hash,created_at) VALUES (%s,%s,%s,%s,%s)",
            (row["run_id"], row["request"]["rule_version_id"], encode(rule), hashlib.sha256(encode(rule).encode()).hexdigest(), row["created_at"]))


def public(row):
    from backend.modules.music.agent import budget, service
    if row['request'].get('kind') == 'daily_discovery':
        from backend.modules.music.discovery import store as discovery
        with library.database() as cursor:
            cursor.execute('SELECT * FROM music_discovery_batches WHERE run_id=%s AND user_id=%s', (row['run_id'],row['user_id']))
            batch=cursor.fetchone()
            if batch:
                return discovery.public(cursor,batch)
            return {k:row[k] for k in ('run_id','status','created_at','updated_at')} | {
                'kind':'candidate_refill','budget':row['context'].get('budget',{}),
                'message':row['context'].get('message','')}
    context = row["context"]
    candidates = context.get("candidates", [])
    qualified = service.deduplicated(service.qualified(row))
    counts = {"candidates":len(candidates), "analyzed":sum(bool(t.get("analysis")) for t in candidates),
              "evaluated":sum(bool(t.get("analysis") and t.get("validation")) for t in candidates),
              "qualified":len(qualified)} if candidates else context.get("counts", {})
    preview = [{k:t.get(k) for k in ("key", "title", "artist", "thumbnail")} for t in qualified[:5]]
    return {k: row[k] for k in ("run_id", "parent_run_id", "status", "request", "steps", "result", "revision",
                               "result_version", "saved_playlist_id", "saved_at", "created_at", "updated_at")} | {
        "progress": {k: context.get(k) for k in ("message", "budget", "waiting_reason", "coverage", "stage", "current_song", "progress_at")} |
            {"usage":budget.usage(context) if row["policy_version"] == POLICY_VERSION else {
                "actual":context.get("actual_tokens", 0), "reserved":0, "unconfirmed":0, "legacy":True},
             "limits":{k:context["limits"].get(k) for k in ("tokens", "model_calls", "seconds")},
             "counts":counts, "preview":preview}}
