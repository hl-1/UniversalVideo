from __future__ import annotations

import json
import re
import mimetypes
import time
import tempfile
import zipfile
from collections import deque
from typing import Literal
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field, HttpUrl, field_validator

from backend.core import storage
from backend.modules.accounts import service as accounts
from backend.modules.delivery import service
from backend.modules.music import library, sources as music
from backend.modules.video import tasks

router = APIRouter(prefix="/api/downloads", tags=["downloads"])


class PlanRequest(BaseModel):
    url: str = Field(max_length=4096)

    @field_validator("url")
    @classmethod
    def validate_url(cls, value):
        if re.fullmatch(r"/api/music/site/[a-f0-9]{24}/content", value):
            return value
        return str(HttpUrl(value))
    format_id: str | None = Field(default=None, max_length=4096)
    parse_record_id: str | None = Field(default=None, max_length=64)
    title: str = Field(default="", max_length=255)
    media_type: Literal["video", "audio"] = "video"
    audio_format: Literal["original", "m4a", "mp3"] = "original"
    destination: Literal["device", "library"] = "device"
    compatibility: bool = False


class CompletionRequest(BaseModel):
    status: Literal["completed", "cancelled", "handed_off", "failed", "library_saved"]
    completion_source: Literal["filesystem", "browser", "none"] = "none"
    transferred_bytes: int = Field(default=0, ge=0)


class BatchRequest(BaseModel):
    tracks: list[library.TrackInput] = Field(min_length=1, max_length=100)
    audio_format: Literal["original", "m4a", "mp3"] = "original"


@router.post("/plan")
def create_plan(payload: PlanRequest, request: Request):
    return service.public(service.resolve(payload, origin=request.headers.get("origin", str(request.base_url).rstrip("/"))))


def disposition(name):
    return "attachment; filename*=UTF-8''" + quote(name, safe="")


@router.api_route("/{identity}/content", methods=["GET", "HEAD"])
def content(identity: str, request: Request):
    plan = service.load(identity)
    if plan["mode"] in {"file", "task"}:
        path = storage.resource_path("file", plan["source_filename"]) if plan["mode"] == "file" else service.task_path(plan["task_id"])
        return storage.media_response(path, download=True)
    headers = {"Cache-Control": "private, no-store", "X-Accel-Buffering": "no", "Content-Disposition": disposition(plan["filename"])}
    if plan.get("audio"):
        if request.headers.get("range"):
            raise HTTPException(416, "转换音轨需要从头下载。")
        headers["Accept-Ranges"] = "none"
        media_type = mimetypes.guess_type(plan["filename"])[0] or "application/octet-stream"
        return Response(headers=headers, media_type=media_type) if request.method == "HEAD" else StreamingResponse(service.audio_stream(plan), headers=headers, media_type=media_type)
    response = service.upstream(plan, range_header=request.headers.get("range"), if_range=request.headers.get("if-range"))
    for name in ("Content-Length", "Content-Type", "Content-Range", "Accept-Ranges", "ETag", "Last-Modified"):
        if name in response.headers:
            headers[name] = response.headers[name]
    if request.method == "HEAD":
        response.close()
        return Response(status_code=response.status_code, headers=headers)
    return StreamingResponse(service.relay(plan, response), status_code=response.status_code, headers=headers)


@router.get("/{identity}/state")
def download_state(identity: str):
    plan = service.load(identity)
    return service.state(plan)


@router.post("/{identity}/complete")
def complete(identity: str, payload: CompletionRequest):
    try:
        plan = service.load(identity, check_state=False)
    except HTTPException as exc:
        if exc.status_code != 410:
            raise
        row = accounts.resource("delivery", identity)
        if row["user_id"] != accounts.owner_id():
            raise HTTPException(404, "下载不存在。")
        old = json.loads(row["payload"])
        if old.get("origin_sid") != accounts.current().session_id or old.get("device_id") != accounts.current().device_id:
            raise HTTPException(404, "本设备没有此下载记录。")
        plan = old | {"uid": row["user_id"], "id": identity}
    if payload.status == "completed" and payload.completion_source != "filesystem":
        raise HTTPException(422, "浏览器接管下载不能确认为本机保存完成。")
    source = payload.completion_source
    if payload.status == "library_saved":
        if plan.get("destination") != "library" or not plan.get("task_id"):
            raise HTTPException(422, "该下载不是云端保存任务。")
        task = json.loads(accounts.resource("download", plan["task_id"])["payload"])
        if task.get("status") != "finished":
            raise HTTPException(409, "云端文件尚未保存完成。")
        source = "server"
    service.update(plan, status=payload.status, phase=payload.status, completion_source=source,
                   transferred_bytes=payload.transferred_bytes)
    if plan.get("destination") == "device" and plan.get("task_id") and payload.status in {"completed", "cancelled", "failed"}:
        service.cleanup_task(plan["task_id"], plan["uid"])
    return {"status": service.state(plan).get("status", payload.status)}


@router.api_route("/tasks/{task_id}/file", methods=["GET", "HEAD"])
def temporary_file(task_id: str):
    return storage.media_response(service.task_path(task_id), download=True)


@router.post("/batches")
def create_batch(payload: BatchRequest):
    unique = list({(track.provider, track.identifier or track.webpage_url): track.model_dump() for track in payload.tracks}.values())
    # Validate ownership/access before returning a native browser download URL.
    from backend.infrastructure import platform_access
    for track in unique:
        platform_access.require_payload(track)
    plan = service.save_plan({"destination": "device", "mode": "batch", "filename": "music.zip",
                              "tracks": unique, "audio_format": payload.audio_format})
    return {"batch_id": plan["id"], "download_id": plan["id"], "url": f"/api/downloads/batches/{plan['id']}/archive", "filename": "music.zip", "total": len(unique)}


class ZipSink:
    def __init__(self):
        self.chunks = deque()
        self.position = 0

    def write(self, data):
        self.position += len(data)
        if data:
            self.chunks.append(data)
        return len(data)

    def tell(self):
        return self.position

    def flush(self):
        pass

    def drain(self):
        while self.chunks:
            yield self.chunks.popleft()


def media_chunks(plan):
    if plan.get("audio"):
        yield from service.audio_stream(plan)
    elif plan["mode"] in {"relay", "direct"}:
        yield from service.relay(plan, service.upstream(plan))
    else:
        if plan["mode"] == "task":
            deadline = time.monotonic() + 900
            while True:
                service.check(plan)
                task = tasks.tasks.get(plan["task_id"])
                if task and task.status == "finished":
                    path = service.task_path(task.task_id)
                    break
                if not task or task.status in {"failed", "cancelled"} or time.monotonic() > deadline:
                    raise RuntimeError("歌曲处理失败，请重新下载。")
                time.sleep(.2)
        else:
            path = storage.resource_path("file", plan["source_filename"])
        with path.open("rb") as stream:
            while chunk := stream.read(service.CHUNK):
                service.check(plan)
                yield chunk


def zip_chunks(plan):
    sink, children, items, failures = ZipSink(), [], [], []
    try:
        service.update(plan, status="running", phase="transferring")
        with zipfile.ZipFile(sink, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as archive:
            for index, data in enumerate(plan["tracks"], 1):
                service.check(plan)
                child = None
                # A song must finish validation before any of its ZIP entry
                # is written. Only this one song uses temporary disk space.
                with tempfile.TemporaryFile(dir=storage.directory("temp", uid=plan["uid"])) as verified:
                    try:
                        track = music.parse_url(data["webpage_url"])
                        child = service.resolve(PlanRequest(url=track.webpage_url, title=track.title, media_type="audio",
                            format_id="music:" + track.token, audio_format=plan["audio_format"]), allow_direct=False)
                        child["parent_id"] = plan["id"]
                        children.append(child)
                        size = 0
                        for chunk in media_chunks(child):
                            service.check(plan)
                            size += len(chunk)
                            if size > 1024 * 1024 * 1024:
                                raise ValueError("单曲超过 1 GB。")
                            verified.write(chunk)
                        if not size:
                            raise ValueError("歌曲没有有效音频。")
                    except Exception:
                        # Cancellation, revoked sessions and deleted parent
                        # records still stop the request. A failed song does not.
                        service.check(plan)
                        failures.append({"title": data["title"], "artist": data.get("artist", ""),
                                         "status": "failed", "error": "音频获取或校验失败，已跳过，可单独重试。"})
                    else:
                        # Once an entry starts, output errors must propagate;
                        # they cannot safely be treated as a skipped song.
                        verified.seek(0)
                        name = f"{index:03d}. " + service.filename(f"{track.artist} - {track.title}", child["filename"].rsplit(".", 1)[-1])
                        with archive.open(name, "w", force_zip64=True) as entry:
                            while chunk := verified.read(service.CHUNK):
                                service.check(plan)
                                entry.write(chunk)
                                yield from sink.drain()
                        items.append({"title": track.title, "artist": track.artist, "provider": track.provider,
                                      "identifier": track.identifier, "webpage_url": track.webpage_url, "status": "finished"})
                if child and child.get("task_id"):
                    service.cleanup_task(child["task_id"], child["uid"])
                    children.remove(child)
                service.update(plan, items=items + failures, finished=len(items), failed=len(failures), transferred_bytes=sink.position)
            if failures:
                archive.writestr("下载结果.txt", "\n".join([f"成功 {len(items)} 首，失败 {len(failures)} 首。失败歌曲未写入压缩包。", *[f"{item['title']} — {item['error']}" for item in failures]]).encode("utf-8"))
                yield from sink.drain()
        yield from sink.drain()
        service.update(plan, phase="delivered", finished=len(items), failed=len(failures), transferred_bytes=sink.position)
    except GeneratorExit:
        service.update(plan, status="cancelled", phase="cancelled")
        raise
    except Exception:
        service.update(plan, status="failed", phase="failed", error="批量下载中断，请重试；未完成的 ZIP 不可作为成功文件。")
        raise
    finally:
        for child in children:
            if child.get("task_id"):
                service.cleanup_task(child["task_id"], child["uid"])


@router.api_route("/batches/{identity}/archive", methods=["GET", "HEAD"])
def archive(identity: str, request: Request):
    plan = service.load(identity)
    if plan["mode"] != "batch":
        raise HTTPException(404, "批量下载不存在。")
    headers = {"Content-Disposition": disposition(plan["filename"]), "Cache-Control": "private, no-store", "X-Accel-Buffering": "no"}
    return Response(headers=headers, media_type="application/zip") if request.method == "HEAD" else StreamingResponse(zip_chunks(plan), headers=headers, media_type="application/zip")
