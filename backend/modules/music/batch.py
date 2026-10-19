from __future__ import annotations

import re
import json
import threading
import uuid
import zipfile
from typing import Any

from fastapi import HTTPException
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask
from fastapi import APIRouter

from backend.modules.music import sources as music
from backend.modules.accounts import service as accounts
from backend.modules.music import library
from backend.modules.music import imports as music_imports
from backend.core import paths
from backend.modules.video import downloads as video_downloads
from backend.modules.video import schemas as video_schemas
from backend.modules.video import tasks as video_tasks


router = APIRouter()


def _music_batch_response(batch: dict[str, Any]) -> dict[str, Any]:
    items = []
    for entry in batch["items"]:
        with video_tasks.tasks_lock:
            result = entry["task"].to_response().model_dump()
        if entry["cancelled"]:
            result.update(status="cancelled", progress=0, message="已取消")
        track = entry["track"]
        result["playlist_pending"] = entry.get("playlist_pending", False)
        result.update(title=track.title, artist=track.artist, provider=track.provider, identifier=track.identifier, webpage_url=track.webpage_url)
        items.append(result)
    counts = {status: sum(item["status"] == status for item in items)
              for status in ("finished", "failed", "cancelled")}
    progress = sum(100 if item["status"] in counts else item["progress"] for item in items) / len(items)
    terminal = batch["status"] in {"finished", "failed", "cancelled"}
    return {"batch_id": batch["batch_id"], "status": batch["status"], "audio_format": batch["audio_format"],
            "playlist_id": batch.get("playlist_id"),
            "progress": round(progress, 2), "total": len(items), **counts, "items": items,
            "download_url": f"/api/music/batches/{batch['batch_id']}/archive" if terminal and counts["finished"] else None}


def _get_music_batch(batch_id: str) -> dict[str, Any]:
    batch = video_tasks.music_batches.get(batch_id)
    if not batch and accounts.principal.get():
        stored = accounts.resource("batch", batch_id)
        data = json.loads(stored["payload"])
        entries = []
        for item in data.get("items", []):
            track = music.Track(title=item["title"], artist=item.get("artist", ""), provider=item["provider"],
                                identifier=item.get("identifier", ""), webpage_url=item.get("webpage_url", "https://example.com"))
            task = video_tasks.DownloadTask(item["task_id"], track.webpage_url, "music:", media_type="audio", audio_format=data["audio_format"])
            task.user_id, task.music_track = stored["user_id"], track
            for field in ("status", "progress", "message", "filename", "error"):
                setattr(task, field, item.get(field))
            entries.append({"track": track, "task": task, "cancelled": item["status"] == "cancelled", "playlist_pending": item.get("playlist_pending", False)})
        batch = {"batch_id": batch_id, "user_id": stored["user_id"], "audio_format": data["audio_format"],
                 "status": data["status"], "playlist_id": data.get("playlist_id"), "stop": threading.Event(), "items": entries}
        video_tasks.music_batches[batch_id] = batch
    if not batch:
        raise HTTPException(404, "批量任务不存在或服务已重启。")
    return batch


def _new_batch_entry(track: music.Track, audio_format: str) -> dict[str, Any]:
    task = video_tasks.DownloadTask(uuid.uuid4().hex[:12], track.webpage_url, f"music:{track.token}",
                        media_type="audio", audio_format=audio_format)
    # The validated snapshot survives token expiry while waiting in the queue.
    task.music_track = track.model_copy(deep=True)
    with video_tasks.tasks_lock:
        video_tasks.tasks[task.task_id] = task
    video_tasks._persist_task(task, "download", force=True)
    return {"track": task.music_track, "task": task, "cancelled": False}


def _run_music_batch(batch: dict[str, Any]) -> None:
    with video_tasks.music_batches_lock:
        if not batch["stop"].is_set():
            batch["status"] = "running"
    attempted = False
    for entry in batch["items"]:
        if entry["task"].status != "pending" or entry["cancelled"]:
            continue
        if attempted:
            batch["stop"].wait(video_tasks.MUSIC_BATCH_INTERVAL)
        with video_tasks.music_batches_lock:
            if batch["stop"].is_set():
                entry["cancelled"] = True
                continue
            video_tasks._set_task(entry["task"], status="running", message="正在解析音轨")
        attempted = True
        try:
            if entry.get("playlist_pending") and entry["task"].filename and (paths.DOWNLOAD_DIR / entry["task"].filename).is_file():
                video_tasks._set_task(entry["task"], status="finished", progress=100, error=None)
            else:
                video_downloads._run_audio_download(entry["task"])
            if entry["task"].status == "finished" and batch.get("playlist_id"):
                try:
                    library.add_to_playlist(batch["playlist_id"], library.TrackInput.model_validate(entry["track"].model_dump()))
                    entry["playlist_pending"] = False
                except Exception as exc:
                    entry["playlist_pending"] = True
                    video_tasks._set_task(entry["task"], status="failed", message="文件已保存，但加入歌单失败，请重试", error=str(exc))
        except Exception as exc:
            video_tasks._set_task(entry["task"], status="failed", message="音轨下载失败", error=str(exc))
    with video_tasks.music_batches_lock:
        statuses = [entry["task"].status for entry in batch["items"]]
        batch["status"] = "cancelled" if batch["stop"].is_set() else "finished" if "finished" in statuses else "failed"
    accounts.register_resource("batch", batch["batch_id"], _music_batch_response(batch), uid=batch.get("user_id"))


@router.post("/api/music/batches")
def create_music_batch(payload: video_schemas.MusicBatchRequest) -> dict[str, Any]:
    tracks, identities = [], set()
    for item in payload.tracks:
        track = music.get_track(item.token, str(item.webpage_url))
        identity = (track.provider, track.identifier)
        if identity not in identities:
            identities.add(identity)
            tracks.append(track)
    with video_tasks.music_batches_lock:
        if any(batch["status"] in {"pending", "running", "cancelling"} and batch.get("user_id") == (accounts.owner_id() if accounts.principal.get() else 0) for batch in video_tasks.music_batches.values()):
            raise HTTPException(409, "已有批量任务正在处理，请等待完成或停止后再添加。")
        playlist_id = library.download_playlist(payload.playlist_id, payload.playlist_name)
        batch_id = uuid.uuid4().hex[:12]
        batch = {"batch_id": batch_id, "status": "pending", "audio_format": payload.audio_format, "playlist_id": playlist_id,
                 "user_id": accounts.owner_id() if accounts.principal.get() else 0,
                 "stop": threading.Event(), "items": [_new_batch_entry(track, payload.audio_format) for track in tracks]}
        video_tasks.music_batches[batch_id] = batch
        response = _music_batch_response(batch)
        accounts.register_resource("batch", batch_id, response, uid=batch["user_id"])
        accounts.submit(video_tasks.music_batch_executor, _run_music_batch, batch)
    return response


@router.get("/api/music/batches/{batch_id}")
def music_batch_status(batch_id: str) -> dict[str, Any]:
    with video_tasks.music_batches_lock:
        return _music_batch_response(_get_music_batch(batch_id))


@router.post("/api/music/imports/{import_id}/download")
def download_music_import(import_id: str, payload: music_imports.ImportDownload):
    tracks = music_imports.selected_tracks(import_id, payload)
    return create_music_batch(video_schemas.MusicBatchRequest(
        tracks=[video_schemas.BatchTrackRequest(token=track.token, webpage_url=track.webpage_url) for track in tracks],
        audio_format=payload.audio_format,
    ))


@router.post("/api/music/batches/{batch_id}/cancel")
def cancel_music_batch(batch_id: str) -> dict[str, Any]:
    with video_tasks.music_batches_lock:
        batch = _get_music_batch(batch_id)
        if batch["status"] in {"pending", "running", "cancelling"}:
            batch["stop"].set()
            batch["status"] = "cancelling"
            for entry in batch["items"]:
                if entry["task"].status == "pending":
                    entry["cancelled"] = True
        return _music_batch_response(batch)


@router.post("/api/music/batches/{batch_id}/retry")
def retry_music_batch(batch_id: str) -> dict[str, Any]:
    with video_tasks.music_batches_lock:
        batch = _get_music_batch(batch_id)
        if any(item["status"] in {"pending", "running", "cancelling"} and item.get("user_id") == batch.get("user_id") for item in video_tasks.music_batches.values()):
            raise HTTPException(409, "请等待当前批量任务停止或完成。")
        failed = [entry for entry in batch["items"] if entry["task"].status == "failed"]
        if not failed:
            raise HTTPException(409, "没有需要重试的失败歌曲。")
        owner_marker = accounts.data_owner.set(batch.get("user_id") or None)
        try:
            for entry in failed:
                if entry.get("playlist_pending"):
                    video_tasks._set_task(entry["task"], status="pending", error=None)
                else:
                    entry.update(_new_batch_entry(entry["track"], batch["audio_format"]))
            batch["stop"].clear()
            batch["status"] = "pending"
            response = _music_batch_response(batch)
            accounts.register_resource("batch", batch_id, response, uid=batch.get("user_id"))
            accounts.submit(video_tasks.music_batch_executor, _run_music_batch, batch)
        finally:
            accounts.data_owner.reset(owner_marker)
        return response


@router.get("/api/music/batches/{batch_id}/archive")
def music_batch_archive(batch_id: str) -> FileResponse:
    with video_tasks.music_batches_lock:
        batch = _get_music_batch(batch_id)
        if batch["status"] in {"pending", "running", "cancelling"}:
            raise HTTPException(409, "批量任务仍在处理中。")
        entries = [(index, entry["track"], entry["task"].filename) for index, entry in enumerate(batch["items"], 1)
                   if entry["task"].status == "finished" and entry["task"].filename]
    if not entries:
        raise HTTPException(409, "没有成功下载的音频可供打包。")
    archive_dir = paths.DOWNLOAD_DIR / "_batches"
    archive_dir.mkdir(exist_ok=True)
    archive = archive_dir / f"{uuid.uuid4().hex}.zip"
    try:
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_STORED) as bundle:
            for index, track, filename in entries:
                if accounts.principal.get():
                    accounts.resource("file", filename)
                file_path = (paths.DOWNLOAD_DIR / filename).resolve()
                if file_path.parent != paths.DOWNLOAD_DIR.resolve() or not file_path.is_file():
                    raise HTTPException(404, f"音频文件不存在：{track.title}")
                label = re.sub(r'[\x00-\x1f\\/:*?"<>|]+', " ", f"{track.artist} - {track.title}").strip(" .")[:120] or "audio"
                bundle.write(file_path, arcname=f"{index:03d}. {label}{file_path.suffix}")
    except Exception:
        archive.unlink(missing_ok=True)
        raise
    return FileResponse(archive, media_type="application/zip", filename=f"music-{batch_id}.zip",
                        background=BackgroundTask(archive.unlink, missing_ok=True))
