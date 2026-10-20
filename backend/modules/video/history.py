from __future__ import annotations

import hashlib
import json
import os
import socket
import threading
import time
import uuid
import zipfile
from pathlib import Path
from typing import Literal
from urllib.parse import unquote, urlparse

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field, HttpUrl

from backend.core.paths import ROOT_DIR as PROJECT_ROOT
from backend.core import paths
from backend.modules.accounts import service as accounts
from backend.infrastructure import platform_access
from backend.modules.video import cache as video_cache
from backend.modules.video import store as video_store
from backend.modules.video import timing as video_timing


router = APIRouter(prefix="/api/videos", tags=["video-history"])
WORKER_ID = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:12]}"
worker_stop = threading.Event()
worker_thread: threading.Thread | None = None
BACKUP_DIR = PROJECT_ROOT / ".run" / "video-history-backups"


class RecentMigration(BaseModel):
    links: list[HttpUrl] = Field(max_length=5)


class MapView(BaseModel):
    scale: float = Field(default=1, ge=0.2, le=1.6, allow_inf_nan=False)
    x: float = Field(default=0, ge=-100000, le=100000, allow_inf_nan=False)
    y: float = Field(default=0, ge=-100000, le=100000, allow_inf_nan=False)
    collapsed: list[str] = Field(default_factory=list, max_length=12000)
    layout: Literal["auto", "balanced", "right", "left"] = "auto"
    resolved_layout: Literal["balanced", "right", "left"] = "balanced"
    framing_valid: bool = True


class VideoView(BaseModel):
    tab: Literal["summary", "transcript", "mind-map"] = "summary"
    summary_task_id: str | None = Field(default=None, max_length=64)
    mind_map_id: str | None = Field(default=None, max_length=64)
    selected_format: str = Field(default="", max_length=128)
    playback_time: float = Field(default=0, ge=0, le=86400, allow_inf_nan=False)
    map_view: MapView | None = None


def decode(row: dict) -> dict:
    return json.loads(row["payload"])


def platform(url: str) -> str:
    host = (urlparse(url).hostname or "").removeprefix("www.")
    for suffix, label in (("bilibili.com", "B站"), ("b23.tv", "B站"), ("youtube.com", "YouTube"),
                          ("youtu.be", "YouTube"), ("douyin.com", "抖音"), ("xiaohongshu.com", "小红书"),
                          ("xhslink.com", "小红书"), ("weibo.com", "微博"), ("weibo.cn", "微博")):
        if host == suffix or host.endswith("." + suffix):
            return label
    return host


def owned_record(record_id: str) -> dict:
    return accounts.resource("video_parse", record_id)


def validate_relation(record_id: str | None, url: str) -> dict | None:
    if not record_id:
        return None
    row = owned_record(record_id)
    data = decode(row)
    if data["url"] != url and (data.get("result") or {}).get("webpage_url") != url:
        raise HTTPException(422, "视频链接与解析记录不匹配。")
    if data["status"] != "finished":
        raise HTTPException(409, "解析成功后才能创建任务。")
    return row


def _recent(cursor, uid: int, *, lock=False) -> list[dict]:
    return video_store.recent(cursor, uid, lock=lock)


def remember_link(url: str, cursor=None, uid: int | None = None) -> None:
    uid = uid or accounts.owner_id()
    if cursor is None:
        with accounts.database() as connection:
            remember_link(url, connection, uid)
        return
    now = time.time()
    cursor.execute("SELECT id FROM users WHERE id=%s FOR UPDATE", (uid,))
    if not cursor.fetchone():
        raise HTTPException(404, "账号不存在。")
    items = _recent(cursor, uid, lock=True)
    item = {"id": hashlib.sha256(url.encode()).hexdigest()[:24], "url": url, "submitted_at": now}
    items = [item] + [previous for previous in items if previous["url"] != url]
    video_store.save(cursor, "video_search", str(uid), uid, {"items": items[:5]}, updated=now)


@router.get("/recent-links")
def recent_links():
    with accounts.database() as cursor:
        return {"items": _recent(cursor, accounts.current().id)}


@router.post("/recent-links/migrate")
def migrate_recent(payload: RecentMigration):
    uid = accounts.current().id
    with accounts.database() as cursor:
        # Server history takes precedence when a second device imports old links.
        cursor.execute("SELECT id FROM users WHERE id=%s FOR UPDATE", (uid,))
        existing = _recent(cursor, uid, lock=True)
        for link in reversed(payload.links):
            remember_link(str(link), cursor, uid)
        for item in reversed(existing):
            remember_link(item["url"], cursor, uid)
        return {"items": _recent(cursor, uid)}


@router.delete("/recent-links")
@router.delete("/recent-links/{link_id}")
def remove_recent(link_id: str | None = None):
    uid = accounts.current().id
    with accounts.database() as cursor:
        cursor.execute("SELECT id FROM users WHERE id=%s FOR UPDATE", (uid,))
        items = [item for item in _recent(cursor, uid, lock=True) if item["id"] != link_id] if link_id else []
        video_store.save(cursor, "video_search", str(uid), uid, {"items": items})
    return {"items": items}


def create_record(url: str) -> str:
    uid = accounts.current().id
    record_id = uuid.uuid4().hex[:12]
    payload = {"record_id": record_id, "url": url, "platform": platform(url), "status": "running", "error": None,
               "result": None, "worker_id": WORKER_ID}
    now = time.time()
    with accounts.database() as cursor:
        remember_link(url, cursor, uid)
        video_store.save(cursor, "video_parse", record_id, uid, payload, created=now)
    return record_id


def durable_result(payload: dict) -> dict:
    result = dict(payload)
    result.pop("record_id", None)
    for field in ("thumbnail",):
        if isinstance(result.get(field), str) and not result[field].startswith("/api/thumbnails/"):
            result[field] = None
    formats = []
    for index, value in enumerate(payload.get("formats", [])):
        item = dict(value)
        identifier = item.get("format_id", "")
        if identifier.startswith(("sniff:", "sniffpair:")) or "http" in identifier or len(identifier) > 128:
            identifier = f"saved:{index}"
        item["format_id"] = identifier
        formats.append(item)
    result["formats"] = formats
    return result


def finish_record(record_id: str, result: dict | None = None, error: str | None = None) -> None:
    with accounts.database() as cursor:
        row = video_store.get(cursor, "video_parse", record_id, lock=True)
        if not row:
            raise HTTPException(409, "解析记录已删除。")
        data = decode(row)
        data.update(status="failed" if error else "finished", error=error, result=durable_result(result) if result else None)
        if result and result.get("webpage_url"):
            data["platform"] = platform(result["webpage_url"])
        video_store.save(cursor, "video_parse", record_id, row["user_id"], data)


def update_thumbnail(record_id: str, thumbnail: str) -> None:
    with accounts.database() as cursor:
        row = video_store.get(cursor, "video_parse", record_id, lock=True)
        if not row or row["user_id"] != accounts.owner_id():
            return
        data = decode(row)
        if data["status"] != "finished" or not data.get("result"):
            return
        data["result"]["thumbnail"] = thumbnail
        video_store.save(cursor, "video_parse", record_id, row["user_id"], data)


def link_legacy_results(record_id: str) -> None:
    row = owned_record(record_id)
    data = decode(row)
    urls = list({data["url"], (data.get("result") or {}).get("webpage_url") or data["url"]})
    uid = row["user_id"]
    placeholders = ",".join(["%s"] * len(urls))
    with accounts.database() as cursor:
        cursor.execute("SELECT deleted FROM video_parse_records WHERE record_id=%s AND user_id=%s FOR UPDATE", (record_id, uid))
        parent = cursor.fetchone()
        if not parent or parent["deleted"]:
            return
        cursor.execute("SELECT COUNT(*) AS n FROM video_parse_records WHERE user_id=%s AND deleted=FALSE AND (url IN (" + placeholders + ") OR webpage_url IN (" + placeholders + "))", (uid, *urls, *urls))
        if cursor.fetchone()["n"] != 1:
            return
        cursor.execute("SELECT * FROM video_summaries WHERE user_id=%s AND deleted=FALSE AND parse_record_id IS NULL AND webpage_url IN (" + placeholders + ") FOR UPDATE", (uid, *urls))
        summaries = [video_store.adapt("summary", row) for row in cursor.fetchall()]
        if not summaries:
            return
        summary_ids = [row["resource_key"] for row in summaries]
        cursor.execute("SELECT * FROM video_mind_maps WHERE user_id=%s AND deleted=FALSE AND summary_task_id IN (" + ",".join(["%s"] * len(summary_ids)) + ") FOR UPDATE", (uid, *summary_ids))
        affected = summaries + [video_store.adapt("mind_map", row) for row in cursor.fetchall()]
        folder = BACKUP_DIR
        folder.mkdir(parents=True, exist_ok=True)
        backup = folder / f"{int(time.time())}-{record_id}.json"
        backup.write_text(json.dumps(affected, ensure_ascii=False, default=str), encoding="utf-8")
        with zipfile.ZipFile(backup.with_suffix(".files.zip"), "w", compression=zipfile.ZIP_STORED) as archive:
            from backend.core import paths
            for summary in summaries:
                result = decode(summary).get("result") or {}
                for field in ("markdown_url", "json_url"):
                    path = paths.SUMMARY_DIR / unquote(Path(result.get(field) or "missing").name)
                    if path.is_file() and not path.is_symlink():
                        archive.write(path, arcname=path.name)
        for item in affected:
            payload = decode(item)
            payload["parse_record_id"] = record_id
            video_store.save(cursor, item["kind"], item["resource_key"], uid, payload, updated=item["updated_at"])


def _related(cursor, record_id: str, uid: int) -> list[dict]:
    rows = []
    for kind in ("summary", "mind_map"):
        table = video_store.TABLES[kind][0]
        cursor.execute(f"SELECT * FROM {table} WHERE user_id=%s AND deleted=FALSE AND parse_record_id=%s", (uid, record_id))
        rows.extend(video_store.adapt(kind, row) for row in cursor.fetchall())
    cursor.execute("SELECT * FROM resources WHERE user_id=%s AND deleted=FALSE AND kind='download' AND JSON_UNQUOTE(JSON_EXTRACT(payload,'$.parse_record_id'))=%s", (uid, record_id))
    rows.extend(cursor.fetchall())
    return sorted(rows, key=lambda row: (row["created_at"], row["resource_key"]), reverse=True)


def task_reference(row: dict) -> dict:
    payload = decode(row)
    return {"kind": row["kind"], "task_id": row["resource_key"], "status": payload.get("status"),
            "version": payload.get("version", 1), "summary_task_id": payload.get("summary_task_id"),
            "filename": payload.get("filename"), "download_url": payload.get("download_url"), "created_at": row["created_at"]}


@router.get("/records")
def records(page: int = Query(default=1, ge=1), q: str = Query(default="", max_length=100),
            platform_filter: str = Query(default="", alias="platform", max_length=100),
            status: Literal["", "running", "finished", "failed", "cancelled"] = ""):
    uid = accounts.current().id
    where = " WHERE p.user_id=%s AND p.deleted=FALSE"
    args: tuple = (uid,)
    if q:
        where += " AND (p.url LIKE %s OR p.title LIKE %s)"
        args += ("%" + q + "%", "%" + q + "%")
    for field, value in (("platform", platform_filter), ("status", status)):
        if value:
            where += f" AND p.{field}=%s"
            args += (value,)
    with accounts.database() as cursor:
        cursor.execute("SELECT COUNT(*) AS n FROM video_parse_records p" + where, args)
        total = cursor.fetchone()["n"]
        cursor.execute("""SELECT p.*, EXISTS(SELECT 1 FROM video_summaries s WHERE s.user_id=p.user_id
            AND s.parse_record_id=p.record_id AND s.deleted=FALSE) AS has_summary,
            EXISTS(SELECT 1 FROM video_mind_maps m WHERE m.user_id=p.user_id AND m.parse_record_id=p.record_id
            AND m.deleted=FALSE AND m.status='finished') AS has_mind_map FROM video_parse_records p""" +
            where + " ORDER BY p.created_at DESC,p.record_id DESC" + (" LIMIT 20 OFFSET %s" if accounts.current().role == "super_admin" else ""), args + (((page - 1) * 20,) if accounts.current().role == "super_admin" else ()))
        rows = cursor.fetchall()
        if accounts.current().role != "super_admin":
            rows = [row for row in rows if platform_access.visible(decode(video_store.adapt("video_parse", row)))]
            total = len(rows)
            rows = rows[(page - 1) * 20:page * 20]
        items = []
        for row in rows:
            data = decode(video_store.adapt("video_parse", row))
            result = data.get("result") or {}
            items.append({"record_id": row["record_id"], "url": data["url"], "platform": data["platform"],
                          "status": data["status"], "error": data.get("error"), "title": result.get("title") or data["url"],
                          "thumbnail": result.get("thumbnail"), "duration": result.get("duration"), "created_at": row["created_at"],
                          "has_summary": bool(row["has_summary"]), "has_mind_map": bool(row["has_mind_map"])})
    return {"items": items, "total": total}


@router.get("/last-record")
def last_record():
    uid = accounts.current().id
    with accounts.database() as cursor:
        cursor.execute("""SELECT p.* FROM video_view_states v JOIN video_parse_records p
            ON p.record_id=v.record_id AND p.user_id=v.user_id AND p.deleted=FALSE
            WHERE v.user_id=%s AND v.deleted=FALSE ORDER BY v.updated_at DESC""", (uid,))
        rows = cursor.fetchall()
    row = next((row for row in rows if platform_access.visible(decode(video_store.adapt("video_parse", row)))), None)
    return {"record_id": row["record_id"] if row else None}


@router.get("/records/{record_id}")
def record_detail(record_id: str):
    row = owned_record(record_id)
    data = decode(row)
    uid = row["user_id"]
    with accounts.database() as cursor:
        related = _related(cursor, record_id, uid)
        view = video_store.get(cursor, "video_view", f"{accounts.current().id}:{record_id}")
    return data | {"created_at": row["created_at"], "tasks": [task_reference(item) for item in related], "view": decode(view) if view else None}


@router.put("/records/{record_id}/view")
def save_view(record_id: str, payload: VideoView):
    record = owned_record(record_id)
    value = payload.model_dump()
    with accounts.database() as cursor:
        cursor.execute("SELECT deleted FROM video_parse_records WHERE record_id=%s FOR UPDATE", (record_id,))
        if cursor.fetchone()["deleted"]:
            raise HTTPException(404, "解析记录已删除。")
        for kind, field in (("summary", "summary_task_id"), ("mind_map", "mind_map_id")):
            if value[field]:
                task = video_store.get(cursor, kind, value[field])
                if not task or task["user_id"] != record["user_id"] or decode(task).get("parse_record_id") != record_id:
                    raise HTTPException(422, "成果与解析记录不匹配。")
        if value["map_view"] and value["mind_map_id"]:
            valid = set()
            def visit(nodes):
                for node in nodes:
                    valid.add(node["id"])
                    visit(node.get("children", []))
            visit((decode(task).get("result") or {}).get("nodes", []))
            valid.add("video-mind-map-root")
            value["map_view"]["collapsed"] = [node for node in value["map_view"]["collapsed"] if node in valid]
        value["record_id"] = record_id
        now = time.time()
        uid = accounts.current().id
        video_store.save(cursor, "video_view", f"{uid}:{record_id}", uid, value, updated=now)
    return value


def delete_record(record_id: str, cascade: bool = False):
    record = owned_record(record_id)
    uid = record["user_id"]
    files = []
    with accounts.database() as cursor:
        video_store.get(cursor, "video_parse", record_id, lock=True, include_deleted=True)
        related = _related(cursor, record_id, uid)
        for task in related:
            data = decode(task)
            if not cascade:
                if data.get("status") in {"pending", "running"}:
                    data.update(status="cancelled", message="解析记录已删除，任务已取消")
                    if task["kind"] in video_store.KINDS:
                        video_store.save(cursor, task["kind"], task["resource_key"], uid, data)
                    else:
                        cursor.execute("UPDATE resources SET payload=%s,updated_at=%s WHERE kind=%s AND resource_key=%s", (json.dumps(data, ensure_ascii=False), time.time(), task["kind"], task["resource_key"]))
                continue
            if task["kind"] == "download" and data.get("filename"):
                files.append(("file", data["filename"]))
                cursor.execute("DELETE FROM music_downloads WHERE filename=%s AND user_id=%s", (data["filename"], uid))
            if task["kind"] == "summary":
                for field in ("markdown_url", "json_url"):
                    value = (data.get("result") or {}).get(field)
                    if value:
                        files.append(("summary_file", unquote(Path(value).name)))
            video_store.tombstone(cursor, task["kind"], task["resource_key"])
        video_store.tombstone(cursor, "video_parse", record_id)
        cursor.execute("SELECT view_key FROM video_view_states WHERE record_id=%s", (record_id,))
        for view in cursor.fetchall():
            video_store.tombstone(cursor, "video_view", view["view_key"])
        for kind, filename in files:
            cursor.execute("UPDATE resources SET deleted=TRUE WHERE kind=%s AND resource_key=%s AND user_id=%s", (kind, filename, uid))
    for task in related:
        for callback in accounts.cancel_callbacks:
            callback(uid, task["resource_key"])
    from backend.modules.admin.api import remove_file
    for kind, filename in files:
        remove_file(kind, filename)
    data = decode(record)
    from backend.modules.video import preview_assets
    preview_assets.cancel_record(uid, record_id)
    for url in {data["url"], (data.get("result") or {}).get("webpage_url") or data["url"]}:
        video_cache.remove(video_cache.parse_key(url, uid))
    accounts.audit("data_delete", f"video_parse:{record_id}")
    return {"ok": True}


@router.delete("/records/{record_id}")
def remove_record(record_id: str, cascade: bool = False):
    return delete_record(record_id, cascade)


def start_worker():
    global worker_thread, worker_stop
    from backend.infrastructure import video_transfer
    video_transfer.cleanup(paths.DOWNLOAD_DIR / ".transfers")
    video_cache.put(accounts.redis_prefix() + "video:worker:" + WORKER_ID, {"alive": True}, 45)
    if worker_thread and worker_thread.is_alive():
        return
    worker_stop = threading.Event()
    def heartbeat():
        while not worker_stop.wait(10):
            video_transfer.cleanup(paths.DOWNLOAD_DIR / ".transfers")
            video_cache.put(accounts.redis_prefix() + "video:worker:" + WORKER_ID, {"alive": True}, 45)
            try:
                restore_interrupted(include_legacy=False)
            except Exception:
                video_cache.logger.warning("Could not reconcile interrupted video tasks")
    worker_thread = threading.Thread(target=heartbeat, name="video-heartbeat", daemon=True)
    worker_thread.start()


def stop_worker():
    worker_stop.set()
    if worker_thread:
        worker_thread.join(timeout=1)
    video_cache.remove(accounts.redis_prefix() + "video:worker:" + WORKER_ID)


def restore_interrupted(include_legacy: bool = True):
    accounts.redis_client().ping()
    with accounts.database() as cursor:
        cursor.execute("SELECT kind,resource_key,payload,updated_at FROM resources WHERE kind IN ('download','watermark','batch','music_import') AND deleted=FALSE AND JSON_UNQUOTE(JSON_EXTRACT(payload,'$.status')) IN ('pending','running','cancelling','recognizing','searching')")
        rows = list(cursor.fetchall())
        for kind in ("video_parse", "summary", "mind_map"):
            table = video_store.TABLES[kind][0]
            cursor.execute(f"SELECT * FROM {table} WHERE deleted=FALSE AND status IN ('pending','running')")
            rows.extend(video_store.adapt(kind, row) for row in cursor.fetchall())
        for row in rows:
            value = decode(row)
            owner = value.get("worker_id")
            if not owner and (not include_legacy or time.time() - row["updated_at"] < 86400):
                continue
            if owner and accounts.redis_client().exists(accounts.redis_prefix() + "video:worker:" + owner):
                continue
            observed_status = value["status"]
            value.update(status="failed", message="服务重启，任务已中断", error="服务中断，请主动重试。")
            if value.get("timing"):
                value["timing"] = video_timing.freeze_saved(value["timing"])
            if row["kind"] in video_store.TABLES:
                table, key, content = video_store.TABLES[row["kind"]]
                now = time.time()
                cursor.execute(f"UPDATE {table} SET {content}=%s,status='failed',updated_at=%s WHERE {key}=%s AND deleted=FALSE AND updated_at=%s AND status=%s",
                               (json.dumps(value, ensure_ascii=False), now, row["resource_key"], row["updated_at"], observed_status))
                if cursor.rowcount:
                    video_store.catalogue(cursor, row["kind"], row["resource_key"], row["user_id"], value, row["created_at"], now)
            else:
                cursor.execute("UPDATE resources SET payload=%s,updated_at=%s WHERE kind=%s AND resource_key=%s AND deleted=FALSE AND updated_at=%s AND JSON_UNQUOTE(JSON_EXTRACT(payload,'$.status'))=%s", (json.dumps(value, ensure_ascii=False), time.time(), row["kind"], row["resource_key"], row["updated_at"], observed_status))
