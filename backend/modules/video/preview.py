from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Annotated

import requests
from fastapi import Header, HTTPException, Request, Response
from backend.infrastructure import video_network
from fastapi.responses import FileResponse, StreamingResponse
from fastapi import APIRouter

from backend.modules.accounts import service as accounts
from backend.modules.video import cache as video_cache
from backend.modules.video import history as video_history
from backend.modules.video import constants as video_constants
from backend.modules.video import schemas as video_schemas
from backend.modules.video import urls as video_urls
from backend.modules.video import preview_assets as assets
from backend.modules.video import preview_sources as sources


router = APIRouter()


@dataclass(frozen=True)
class PreviewSession:
    media_url: str
    referer: str
    expires_at: float
preview_sessions: dict[str, PreviewSession] = {}
preview_sessions_lock = threading.Lock()


def _cleanup_preview_sessions(now: float) -> None:
    expired = [token for token, session in preview_sessions.items() if session.expires_at <= now]
    for token in expired:
        preview_sessions.pop(token, None)


@router.post("/api/previews", response_model=video_schemas.PreviewResponse)
def create_preview(payload: video_schemas.PreviewRequest) -> video_schemas.PreviewResponse:
    page = video_urls.normalize_url(str(payload.url))
    video_history.validate_relation(payload.parse_record_id, page)
    if payload.parse_record_id and not payload.force_prepare:
        if reused := assets.reuse(page, payload.format_id, payload.parse_record_id):
            return video_schemas.PreviewResponse(**reused)
    source = sources.resolve(str(payload.url), payload.format_id)
    options = {"format_id": source.format_id}
    if payload.parse_record_id:
        options["selection"] = payload.format_id
    if payload.force_prepare:
        options["fast"] = False
    return video_schemas.PreviewResponse(**assets.create(source.media_url, source.audio_url, source.referer,
                                                        payload.parse_record_id, source.alternate, **options))


@router.post("/api/previews/reuse", response_model=video_schemas.PreviewResponse | None)
def reuse_preview(payload: video_schemas.PreviewReuseRequest):
    page = video_urls.normalize_url(str(payload.url))
    video_history.validate_relation(payload.parse_record_id, page)
    return assets.reuse(page, payload.format_id, payload.parse_record_id)


@router.get("/api/previews/{token}", response_model=video_schemas.PreviewResponse)
def preview_status(token: str):
    value = assets.get(token)
    if value is None:
        session = _get_preview_session(token)
        value = {"expires_at": session.expires_at}
    return video_schemas.PreviewResponse(**assets.response(token, value))


@router.delete("/api/previews/{token}", status_code=204)
def cancel_preview(token: str):
    value = assets.get(token)
    if value is None:
        _get_preview_session(token)
    assets.cancel(token)
    with preview_sessions_lock:
        preview_sessions.pop(token, None)


def _get_preview_session(token: str) -> PreviewSession:
    now = time.time()
    if accounts.principal.get():
        value = video_cache.get(video_cache.key("preview", token))
        if not value or value.get("user_id") != accounts.current().id:
            raise HTTPException(404, "预览会话不存在，请重新获取播放源。")
        from backend.infrastructure import platform_access
        platform_access.require_payload(value)
        if value.get("parse_record_id"):
            video_history.owned_record(value["parse_record_id"])
        if value["expires_at"] <= now:
            video_cache.remove(video_cache.key("preview", token))
            raise HTTPException(410, "预览地址已过期，请重新获取播放源。")
        return PreviewSession(value["media_url"], value["referer"], value["expires_at"])
    with preview_sessions_lock:
        session = preview_sessions.get(token)
        if not session:
            raise HTTPException(status_code=404, detail="预览会话不存在，请重新解析后播放。")
        if session.expires_at <= now:
            preview_sessions.pop(token, None)
            raise HTTPException(status_code=410, detail="预览地址已过期，请重新解析后播放。")
        return session


@router.api_route("/api/previews/{token}/content", methods=["GET", "HEAD"])
def preview_content(
    token: str,
    range_header: Annotated[str | None, Header(alias="Range")] = None,
    request: Request = None,
):
    if range_header and not video_constants.PREVIEW_RANGE_PATTERN.fullmatch(range_header.strip()):
        raise HTTPException(status_code=416, detail="仅支持单段 bytes Range 请求。")

    value = assets.get(token)
    if value and value.get("version") == 2:
        if value["status"] != "ready":
            raise HTTPException(409, value.get("message") or "预览尚未准备完成。")
        if value.get("delivery") == "proxy":
            return _proxy_content(token, value, range_header, request)
        if value.get("delivery") == "fragmented" and not value.get("complete"):
            if request and request.method == "HEAD":
                response = Response(media_type="video/mp4", headers={"Cache-Control": "private, no-store"})
                del response.headers["content-length"]
                return response
            return _fragmented_content(token, value, range_header)
        path = assets.acquire(token, value)
        class PreviewFileResponse(FileResponse):
            async def __call__(self, scope, receive, send):
                try:
                    return await super().__call__(scope, receive, send)
                finally:
                    assets.release(token)
        return PreviewFileResponse(path, media_type="video/mp4", headers={"Cache-Control": "private, no-store"})
    session = _get_preview_session(token)

    headers = {
        **video_constants.DOUYIN_HEADERS,
        "Referer": session.referer,
        "Accept-Encoding": "identity",
    }
    if range_header:
        headers["Range"] = range_header.strip()

    if request and request.headers.get("if-range"):
        headers["If-Range"] = request.headers["if-range"]
    try:
        upstream = video_network.get(
            session.media_url,
            referer=session.referer,
            headers=headers,
            stream=True,
            timeout=(10, 60),
        )
    except requests.RequestException as exc:
        raise HTTPException(status_code=502, detail="连接抖音视频源失败，请重新解析后重试。") from exc

    content_type = upstream.headers.get("Content-Type", "").split(";", 1)[0].lower()
    if upstream.status_code == 416:
        content_range = upstream.headers.get("Content-Range")
        upstream.close()
        raise HTTPException(416, "播放位置超出视频范围。", headers={"Content-Range": content_range} if content_range else None)
    if upstream.status_code not in {200, 206} or not (
        content_type.startswith("video/") or content_type == "application/octet-stream"
    ):
        upstream.close()
        if accounts.principal.get():
            video_cache.remove(video_cache.key("preview", token))
            video_cache.remove(video_cache.parse_key(session.referer))
        raise HTTPException(status_code=502, detail="抖音视频源返回了无效内容，请重新解析后重试。")

    allowed_headers = {"content-type", "content-length", "content-range", "accept-ranges"}
    response_headers = {
        name: value for name, value in upstream.headers.items() if name.lower() in allowed_headers
    }

    response_headers["Cache-Control"] = "private, no-store"
    if request and request.method == "HEAD":
        upstream.close()
        return Response(status_code=upstream.status_code, headers=response_headers)

    def body():
        try:
            for chunk in upstream.iter_content(chunk_size=1024 * 256):
                if chunk:
                    yield chunk
        finally:
            upstream.close()

    return StreamingResponse(
        body(),
        status_code=upstream.status_code,
        headers=response_headers,
    )


class ManagedStream(StreamingResponse):
    def __init__(self, body, finish, **kwargs):
        self.finish = finish
        super().__init__(body, **kwargs)

    async def __call__(self, scope, receive, send):
        try:
            return await super().__call__(scope, receive, send)
        finally:
            self.finish()


def _proxy_content(token, value, range_header, request: Request = None):
    headers = {**sources.headers(value["referer"]), "Accept-Encoding": "identity"}
    if range_header:
        headers["Range"] = range_header.strip()
    if request and request.headers.get("if-range"):
        headers["If-Range"] = request.headers["if-range"]
    try:
        upstream = sources.retry_network(lambda: video_network.get(value["media_url"], referer=value["referer"],
            headers=headers, stream=True, timeout=(5, 10)), lambda: assets.get(token))
    except sources.PreparationError as exc:
        raise HTTPException(502, exc.message) from exc
    kind = upstream.headers.get("Content-Type", "").split(";", 1)[0].lower()
    if upstream.status_code == 416:
        content_range = upstream.headers.get("Content-Range")
        upstream.close()
        raise HTTPException(416, "请求范围超出视频文件。", headers={"Content-Range": content_range} if content_range else None)
    if upstream.status_code not in {200, 206} or kind not in {"video/mp4", "application/mp4", "application/octet-stream", "binary/octet-stream"}:
        upstream.close()
        raise HTTPException(502, "视频源返回了无效内容，请重新解析。")
    try:
        job = assets.hold(token, value)
    except Exception:
        upstream.close()
        raise
    finished = False

    def finish():
        nonlocal finished
        if not finished:
            finished = True
            upstream.close()
            assets.release(token)

    def body():
        try:
            for chunk in upstream.iter_content(65536):
                if job.cancelled.is_set() or job.value["expires_at"] <= time.time():
                    return
                with assets.lock:
                    job.received += len(chunk)
                    if job.received > assets.MAX_INPUT:
                        raise sources.PreparationError("size", "视频超过预览大小限制。")
                if chunk:
                    yield chunk
        finally:
            finish()

    allowed = {"content-type", "content-length", "content-range", "accept-ranges"}
    response_headers = {k: v for k, v in upstream.headers.items() if k.lower() in allowed}
    response_headers.update({"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"})
    if request and request.method == "HEAD":
        finish()
        return Response(status_code=upstream.status_code, headers=response_headers)
    return ManagedStream(body(), finish, status_code=upstream.status_code, headers=response_headers)


def _fragmented_content(token, value, range_header):
    # Only the initial sequential read is available while the file is growing.
    if range_header and range_header.strip() != "bytes=0-":
        raise HTTPException(416, "视频仍在准备，完成后可跳转到任意位置。")
    path = assets.acquire(token, value)
    finished = False

    def finish():
        nonlocal finished
        if not finished:
            finished = True
            assets.release(token)

    def body():
        try:
            with path.open("rb") as stream:
                while True:
                    state = assets.get(token)
                    if not state or state.get("status") != "ready":
                        return
                    chunk = stream.read(65536)
                    if chunk:
                        yield chunk
                    elif state.get("complete"):
                        return
                    else:
                        time.sleep(0.1)
        finally:
            finish()

    return ManagedStream(body(), finish, media_type="video/mp4", headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"})
