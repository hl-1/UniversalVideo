from __future__ import annotations

from backend.core import storage

import hashlib
import logging
import threading
from contextvars import copy_context
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse, parse_qs

from fastapi import HTTPException
from fastapi.responses import Response

from backend.modules.accounts import service as accounts
from backend.core import paths
from backend.infrastructure import network, video_network, video_proxy, platform_access
from backend.modules.video import urls as video_urls

slots = threading.BoundedSemaphore(2)


def schedule(record_id: str, thumbnail: str | None, source_url: str | None = None):
    if not thumbnail or thumbnail.startswith("/api/thumbnails/") or not slots.acquire(blocking=False):
        return
    image_url = parse_qs(urlparse(thumbnail).query).get("url", [thumbnail])[0]

    def work():
        from backend.modules.video import history, parse_runtime
        marker = parse_runtime.current.set(None)
        guard = network.request_guard.set(None)
        try:
            with video_proxy.scope(source_url):
                local = _cache_thumbnail(image_url)
            if platform_access.platform(source_url):
                accounts.register_resource("thumbnail", Path(local).name, {"source_url": source_url, "parse_record_id": record_id, "filename": Path(local).name})
            history.update_thumbnail(record_id, local)
        except Exception:
            # The current page can still lazy-load its original image proxy.
            logging.getLogger("videodream.video").info("Video thumbnail background cache unavailable")
        finally:
            network.request_guard.reset(guard)
            parse_runtime.current.reset(marker)
            slots.release()

    threading.Thread(target=copy_context().run, args=(work,), daemon=True, name="video-thumbnail").start()


def _safe_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _proxy_thumbnail(url: str | None) -> str | None:
    return f"/api/image-proxy?url={quote(url, safe='')}" if isinstance(url, str) and url else None


def _local_thumbnail(filename: str) -> str:
    return f"/api/thumbnails/{quote(Path(filename).name, safe='')}"


def _cache_thumbnail(url: str) -> str:
    prefix = f"video-{accounts.owner_id()}-{hashlib.sha256(url.encode()).hexdigest()[:24]}"
    for ext in ("jpg", "png", "webp", "gif"):
        cached = storage.owned_path("thumbnail", f"{prefix}.{ext}")
        if cached.is_file() and cached.stat().st_size:
            return _local_thumbnail(cached.name)
    response = fetch_image(url)
    ext = {"image/png": "png", "image/webp": "webp", "image/gif": "gif"}.get(response.media_type, "jpg")
    filename = f"{prefix}.{ext}"
    path = storage.path("thumbnail", filename)
    path.write_bytes(response.body)
    accounts.register_resource("thumbnail", filename, {"filename": filename, "size": path.stat().st_size, "source_url": url})
    return _local_thumbnail(filename)


def _thumbnail_url(url: str | None) -> str | None:
    if not isinstance(url, str) or not url:
        return None
    if url.startswith("/api/"):
        return url
    return _proxy_thumbnail(url)


def _thumbnail_referer(url: str) -> str:
    host = video_urls._host(url)
    if video_urls._is_pornhub_url(url):
        return "https://www.pornhub.com/"
    if video_urls._is_youtube_url(url) or "ytimg.com" in host or "googleusercontent.com" in host:
        return "https://www.youtube.com/"
    if video_urls._is_douyin_url(url) or any(token in host for token in ("byteimg.com", "douyinpic.com", "snssdk.com", "aweme")):
        return "https://www.douyin.com/"
    if video_urls._is_bilibili_url(url) or "hdslb.com" in host:
        return "https://www.bilibili.com/"
    return f"{urlparse(url).scheme}://{host}/"


def fetch_image(url: str) -> Response:
    try:
        with video_network.get(url, referer=_thumbnail_referer(url), headers={"User-Agent": "VideoDream/1.0", "Referer": _thumbnail_referer(url)}, stream=True, timeout=12) as result:
            result.raise_for_status()
            media_type = result.headers.get("Content-Type", "").split(";", 1)[0]
            if media_type not in {"image/jpeg", "image/png", "image/webp", "image/gif"}:
                raise HTTPException(422, "远程内容不是支持的图片。")
            content = bytearray()
            for chunk in result.iter_content(65536):
                content.extend(chunk)
                if len(content) > 10 * 1024 * 1024:
                    raise HTTPException(413, "图片超过 10 MB。")
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=video_urls._clean_error(exc, url)) from exc
    return Response(content=bytes(content), media_type=media_type)
