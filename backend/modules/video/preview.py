from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass
from typing import Annotated
from urllib.parse import unquote, urlparse

import requests
from fastapi import Header, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from fastapi import APIRouter

from backend.modules.accounts import service as accounts
from backend.modules.video import cache as video_cache
from backend.modules.video import history as video_history
from backend.modules.video import constants as video_constants
from backend.modules.video import douyin as video_douyin
from backend.modules.video import parse_cache as video_parse_cache
from backend.modules.video import schemas as video_schemas
from backend.modules.video import urls as video_urls


router = APIRouter()


@dataclass(frozen=True)
class PreviewSession:
    media_url: str
    referer: str
    expires_at: float
preview_sessions: dict[str, PreviewSession] = {}
preview_sessions_lock = threading.Lock()


def _resolve_douyin_preview_source(url: str, format_id: str) -> tuple[str, str]:
    normalized_url = video_urls.normalize_url(url)
    if not video_urls._is_douyin_url(normalized_url):
        raise HTTPException(status_code=422, detail="首版仅支持抖音视频在线播放。")

    if format_id == "douyin-public":
        _, item = video_douyin._fetch_douyin_item(normalized_url)
        return video_douyin._douyin_media_url(item), normalized_url

    if not format_id.startswith("sniff:"):
        raise HTTPException(status_code=422, detail="该格式暂不支持在线播放。")

    cached = video_parse_cache._get_sniff_cache(normalized_url)
    if not cached:
        raise HTTPException(status_code=404, detail="解析结果已过期，请重新解析后播放。")
    matched = next((item for item in cached.formats if item.format_id == format_id), None)
    if not matched:
        raise HTTPException(status_code=404, detail="播放格式与最近解析结果不匹配。")
    if (matched.ext or "").lower() != "mp4":
        raise HTTPException(status_code=422, detail="该线路暂不支持在线播放，请选择 MP4 格式。")

    media_url = unquote(format_id.removeprefix("sniff:"))
    parsed_media = urlparse(media_url)
    if parsed_media.scheme not in {"http", "https"} or not parsed_media.netloc:
        raise HTTPException(status_code=422, detail="解析到的视频地址不合法。")
    return media_url, normalized_url


def _cleanup_preview_sessions(now: float) -> None:
    expired = [token for token, session in preview_sessions.items() if session.expires_at <= now]
    for token in expired:
        preview_sessions.pop(token, None)


@router.post("/api/previews", response_model=video_schemas.PreviewResponse)
def create_preview(payload: video_schemas.PreviewRequest) -> video_schemas.PreviewResponse:
    video_history.validate_relation(payload.parse_record_id, video_urls.normalize_url(str(payload.url)))
    media_url, referer = _resolve_douyin_preview_source(str(payload.url), payload.format_id)
    now = time.time()
    token = secrets.token_urlsafe(24)
    ttl = video_cache.media_ttl([media_url])
    if ttl <= 0:
        raise HTTPException(410, "播放源已过期，请重新解析。")
    if accounts.principal.get():
        value = {"user_id": accounts.owner_id(), "media_url": media_url, "referer": referer, "expires_at": now + ttl, "parse_record_id": payload.parse_record_id}
        if not video_cache.put(video_cache.key("preview", token), value, ttl):
            raise HTTPException(503, "预览缓存暂不可用，请稍后重试。")
        return video_schemas.PreviewResponse(preview_url=f"/api/previews/{token}/content", expires_in=ttl)
    with preview_sessions_lock:
        _cleanup_preview_sessions(now)
        preview_sessions[token] = PreviewSession(
            media_url=media_url,
            referer=referer,
            expires_at=now + video_constants.PREVIEW_TTL_SECONDS,
        )
    return video_schemas.PreviewResponse(
        preview_url=f"/api/previews/{token}/content",
        expires_in=video_constants.PREVIEW_TTL_SECONDS,
    )


def _get_preview_session(token: str) -> PreviewSession:
    now = time.time()
    if accounts.principal.get():
        value = video_cache.get(video_cache.key("preview", token))
        if not value or value.get("user_id") != accounts.current().id:
            raise HTTPException(404, "预览会话不存在，请重新获取播放源。")
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
) -> StreamingResponse:
    session = _get_preview_session(token)
    if range_header and not video_constants.PREVIEW_RANGE_PATTERN.fullmatch(range_header.strip()):
        raise HTTPException(status_code=416, detail="仅支持单段 bytes Range 请求。")

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
        fetch = requests.head if request and request.method == "HEAD" else requests.get
        upstream = fetch(
            session.media_url,
            headers=headers,
            stream=True,
            allow_redirects=True,
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
