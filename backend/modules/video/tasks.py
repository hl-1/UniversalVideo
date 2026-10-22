from __future__ import annotations

from backend.core import storage

import json
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Literal
from urllib.parse import quote

from fastapi import HTTPException

from backend.modules.video.constants import TaskStatus
from backend.modules.music import sources as music
from backend.modules.accounts import service as accounts
from backend.modules.video import history as video_history
from backend.modules.video import store as video_store
from backend.core import paths
from backend.modules.video import schemas as video_schemas
from backend.modules.video import urls as video_urls
from backend.modules.video import timing


class DownloadTask:
    def __init__(
        self,
        task_id: str,
        url: str,
        format_id: str | None,
        media_type: Literal["video", "audio"] = "video",
        audio_format: Literal["m4a", "mp3"] = "m4a",
    ) -> None:
        self.task_id = task_id
        self.url = video_urls.normalize_url(url)
        self.format_id = format_id
        self.media_type = media_type
        self.audio_format = audio_format
        self.status: TaskStatus = "pending"
        self.progress = 0.0
        self.message = "等待下载任务开始"
        self.filename: str | None = None
        self.error: str | None = None
        self.created_at = time.time()
        self.updated_at = time.time()
        self.music_track: music.Track | None = None
        self.user_id = accounts.owner_id() if accounts.principal.get() else 0
        self.cancel = threading.Event()
        self.process: subprocess.Popen | None = None
        self.parse_record_id: str | None = None
        self.destination = "library"
        self.device_id = accounts.current().device_id if accounts.principal.get() else None
        self.origin_sid = accounts.current().session_id if accounts.principal.get() else ""
        self.client_kind = accounts.current().client_kind if accounts.principal.get() else ""

    def directory(self, kind="file"):
        if self.destination == "library":
            return storage.directory(kind, uid=self.user_id or None)
        folder = storage.directory("temp", uid=self.user_id or None) / "delivery" / self.task_id / kind
        storage.contained(folder).mkdir(parents=True, exist_ok=True, mode=0o700)
        return folder

    def to_response(self) -> TaskResponse:
        download_url = f"/api/files/{quote(self.filename, safe='')}" if self.filename else None
        if self.filename and self.destination == "device":
            download_url = f"/api/downloads/tasks/{self.task_id}/file"
        return video_schemas.TaskResponse(
            task_id=self.task_id,
            status=self.status,
            progress=round(self.progress, 2),
            message=self.message,
            filename=self.filename,
            download_url=download_url,
            error=self.error,
            media_type=self.media_type,
            destination=self.destination,
        )


class SummaryTask:
    def __init__(self, task_id: str, url: str) -> None:
        self.task_id = task_id
        self.url = video_urls.normalize_url(url)
        self.status: TaskStatus = "pending"
        self.progress = 0.0
        self.message = "等待总结任务开始"
        self.error: str | None = None
        self.result: SummaryResult | None = None
        self.transcript: list[SubtitleSegment] = []
        self.parse_record_id: str | None = None
        self.created_at = time.time()
        self.updated_at = time.time()
        self.user_id = accounts.owner_id() if accounts.principal.get() else 0
        self.cancel = threading.Event()
        self.timing = timing.ExecutionTimer()
        self.model_info = None
        self.generate_mind_map = False
        self.bilibili_prepared = False
        self.prepared_source = None
        self.map_task = None
        self.map_future = None
        self.mind_map_task_id = None

    def to_response(self) -> SummaryTaskResponse:
        return video_schemas.SummaryTaskResponse(
            task_id=self.task_id,
            status=self.status,
            progress=round(self.progress, 2),
            message=self.message,
            error=self.error,
            result=self.result,
            timing=timing.snapshot(self.timing),
            model_info=self.model_info,
            mind_map_task_id=self.mind_map_task_id,
        )


class MindMapTask:
    def __init__(self, task_id: str, summary_task_id: str) -> None:
        self.user_id = accounts.owner_id() if accounts.principal.get() else 0
        self.cancel = threading.Event()
        self.task_id = task_id
        self.summary_task_id = summary_task_id
        self.status: TaskStatus = "pending"
        self.progress = 0.0
        self.message = "等待思维导图任务开始"
        self.error: str | None = None
        self.result: MindMapResult | None = None
        self.created_at = time.time()
        self.updated_at = time.time()
        self.version = 1
        self.parse_record_id: str | None = None
        self.timing = timing.ExecutionTimer()
        self.model_info = None

    def to_response(self) -> MindMapTaskResponse:
        return video_schemas.MindMapTaskResponse(
            task_id=self.task_id,
            summary_task_id=self.summary_task_id,
            status=self.status,
            progress=round(self.progress, 2),
            message=self.message,
            error=self.error,
            result=self.result,
            version=self.version,
            timing=timing.snapshot(self.timing),
            model_info=self.model_info,
        )
tasks: dict[str, DownloadTask] = {}
tasks_lock = threading.Lock()
music_batches: dict[str, dict[str, Any]] = {}
music_batches_lock = threading.RLock()
music_batch_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="music-batch")
MUSIC_BATCH_INTERVAL = 2.0
summary_tasks: dict[str, SummaryTask] = {}
summary_tasks_lock = threading.Lock()
mind_map_tasks: dict[str, MindMapTask] = {}
mind_map_tasks_lock = threading.Lock()
executor = ThreadPoolExecutor(max_workers=2)
ai_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="video-generation")


def _set_summary_task(task: SummaryTask, **changes: Any) -> None:
    _checkpoint(task, changes)
    with summary_tasks_lock:
        for key, value in changes.items():
            setattr(task, key, value)
        task.updated_at = time.time()
        _time_status(task, changes)
    _persist_task(task, "summary")


def _set_mind_map_task(task: MindMapTask, **changes: Any) -> None:
    _checkpoint(task, changes)
    with mind_map_tasks_lock:
        for key, value in changes.items():
            setattr(task, key, value)
        task.updated_at = time.time()
        _time_status(task, changes)
    _persist_task(task, "mind_map")


def _time_status(task, changes):
    if isinstance(task.timing, timing.ExecutionTimer):
        if changes.get("status") == "running":
            task.timing.stop("queue")
            task.timing.stop("map_queue")
        if changes.get("status") in timing.TERMINAL:
            task.timing.finish(changes["status"])


def _checkpoint(task, changes):
    if not task.user_id:
        if task.cancel.is_set() and changes.get("status") not in {"failed", "cancelled"}:
            raise RuntimeError("任务已停止。")
        return
    if changes.get("status") in {"failed", "cancelled"}:
        if task.cancel.is_set():
            changes.update(status="cancelled", message="账号已停用或任务已取消", error=None)
        return
    try:
        if task.cancel.is_set():
            raise HTTPException(401, "任务已取消。")
        if time.monotonic() - getattr(task, "_checked_at", 0) >= 1:
            accounts.active_worker(task.user_id, source={"url": getattr(task, "url", None), "parse_record_id": getattr(task, "parse_record_id", None), "summary_task_id": getattr(task, "summary_task_id", None)})
            kind = "download" if isinstance(task, DownloadTask) else "summary" if isinstance(task, SummaryTask) else "mind_map"
            with accounts.database() as cursor:
                if kind in video_store.TABLES:
                    stored = video_store.get(cursor, kind, task.task_id, include_deleted=True)
                else:
                    cursor.execute("SELECT deleted,payload FROM resources WHERE kind=%s AND resource_key=%s", (kind, task.task_id))
                    stored = cursor.fetchone()
                if stored and (stored["deleted"] or json.loads(stored["payload"]).get("status") == "cancelled"):
                    raise HTTPException(409, "任务已取消或删除。")
            task._checked_at = time.monotonic()
    except HTTPException:
        task.cancel.set()
        if isinstance(task, DownloadTask) and task.process and task.process.poll() is None:
            task.process.kill()
        task.status, task.message = "cancelled", "账号已停用或任务已取消"
        _persist_task(task, "download" if isinstance(task, DownloadTask) else "summary" if isinstance(task, SummaryTask) else "mind_map", force=True)
        raise RuntimeError("任务已停止。")


def _persist_task(task, kind, force=False):
    if isinstance(getattr(task, "timing", None), timing.ExecutionTimer) and task.status in timing.TERMINAL:
        task.timing.finish(task.status)
    if task.user_id and (force or time.monotonic() - getattr(task, "_saved_at", 0) >= 1 or task.status in {"finished", "failed", "cancelled"}):
        snapshot = task.to_response().model_dump()
        if kind == "download" and getattr(task, "destination", None) == "device":
            snapshot.update(device_id=getattr(task, "device_id", None), origin_sid=getattr(task, "origin_sid", ""), client_kind=getattr(task, "client_kind", ""))
        if isinstance(task, SummaryTask):
            snapshot["transcript"] = [segment.model_dump() for segment in task.transcript]
        snapshot.update(parse_record_id=getattr(task, "parse_record_id", None), url=getattr(task, "url", None), worker_id=video_history.WORKER_ID)
        if getattr(task, "route_revision", None):
            snapshot.update(route_profile="overseas-proxy", route_revision=task.route_revision)
        with accounts.database() as cursor:
            record_id = getattr(task, "parse_record_id", None)
            if record_id:
                cursor.execute("SELECT deleted FROM video_parse_records WHERE record_id=%s FOR UPDATE", (record_id,))
                parent = cursor.fetchone()
            else:
                parent = None
            if kind in video_store.TABLES:
                stored = video_store.get(cursor, kind, task.task_id, lock=True, include_deleted=True)
            else:
                cursor.execute("SELECT deleted,payload FROM resources WHERE kind=%s AND resource_key=%s FOR UPDATE", (kind, task.task_id))
                stored = cursor.fetchone()
            if stored and kind == "download":
                previous = json.loads(stored["payload"])
                snapshot.update({name: previous[name] for name in ("device_id", "origin_sid", "client_kind") if name in previous})
            if stored and stored["deleted"]:
                task.cancel.set()
                if isinstance(task, SummaryTask):
                    files_to_remove = list(storage.directory("summary_file", uid=task.user_id).glob(f"{task.task_id}.*.summary.*"))
                    file_kind = "summary_file"
                else:
                    files_to_remove = [storage.owned_path("file", task.filename, uid=task.user_id)] if getattr(task, "filename", None) else []
                    file_kind = "file"
                for path in files_to_remove:
                    path.unlink(missing_ok=True)
                    cursor.execute("UPDATE resources SET deleted=TRUE WHERE kind=%s AND resource_key=%s AND user_id=%s", (file_kind, path.name, task.user_id))
                return
            if stored and json.loads(stored["payload"]).get("status") == "cancelled":
                task.cancel.set()
                task.status = "cancelled"
                snapshot.update(status="cancelled", message="任务已取消")
            if record_id and not stored and (not parent or parent["deleted"]):
                raise HTTPException(409, "解析记录已删除，无法创建任务。")
            now = time.time()
            if kind in video_store.TABLES:
                video_store.save(cursor, kind, task.task_id, task.user_id, snapshot, updated=now)
            else:
                cursor.execute("""INSERT INTO resources (kind,resource_key,user_id,payload,created_at,updated_at,device_id) VALUES (%s,%s,%s,%s,%s,%s,%s)
                    ON DUPLICATE KEY UPDATE payload=VALUES(payload),updated_at=VALUES(updated_at)""",
                               (kind, task.task_id, task.user_id, json.dumps(snapshot, ensure_ascii=False), now, now, snapshot.get("device_id")))
        if task.status == "finished" and getattr(task, "filename", None) and getattr(task, "destination", "library") == "library":
            path = storage.owned_path("file", task.filename, uid=task.user_id)
            if path.is_file() and not getattr(task, "_shared_published", False):
                from backend.core import blobs
                blobs.publish(path)
                task._shared_published = True
            accounts.register_resource("file", task.filename, {"filename": task.filename, "size": path.stat().st_size if path.is_file() else 0,
                "source_url": getattr(task, "url", None), "source_task_id": task.task_id,
                "parse_record_id": getattr(task, "parse_record_id", None), "route_revision": getattr(task, "route_revision", None)}, uid=task.user_id)
        task._saved_at = time.monotonic()


def _set_task(task: DownloadTask, **changes: Any) -> None:
    _checkpoint(task, changes)
    with tasks_lock:
        for key, value in changes.items():
            setattr(task, key, value)
        task.updated_at = time.time()
    _persist_task(task, "download")


def cancel_user_tasks(uid: int, key: str | None = None):
    cancelled = []
    for collection, lock in ((tasks, tasks_lock), (summary_tasks, summary_tasks_lock), (mind_map_tasks, mind_map_tasks_lock)):
        with lock:
            for task in collection.values():
                if task.user_id == uid and (key is None or task.task_id == key or getattr(task, "summary_task_id", None) == key) and task.status in {"pending", "running"}:
                    task.cancel.set()
                    task.status, task.message = "cancelled", "账号已停用或任务已取消"
                    if isinstance(task, DownloadTask) and task.process and task.process.poll() is None:
                        task.process.kill()
                    cancelled.append(task)
    with music_batches_lock:
        for batch in music_batches.values():
            if batch.get("user_id") == uid and (key is None or batch["batch_id"] == key):
                batch["stop"].set()
                batch["status"] = "cancelled"
                for entry in batch["items"]:
                    if entry["task"].status in {"pending", "running"}:
                        entry["cancelled"] = True
                        entry["task"].cancel.set()
    for task in cancelled:
        _persist_task(task, "download" if isinstance(task, DownloadTask) else "summary" if isinstance(task, SummaryTask) else "mind_map", force=True)
    # In-flight transfers remove their parts in finally; failed tasks have no
    # running worker, so their retained parts are removed here on delete/cancel.
    from backend.infrastructure import video_transfer
    video_transfer.cleanup(storage.directory("temp", uid=uid, create=False) / "transfers", owner=uid, task_id=key)
