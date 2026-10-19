from __future__ import annotations

import re
import html
import time
from contextlib import contextmanager, nullcontext
from typing import Any
from urllib.parse import quote, urlparse

from fastapi import HTTPException
from playwright.sync_api import sync_playwright

from backend.modules.accounts import service as accounts
from backend.infrastructure.browser_sessions import needs_verification, verification_required
from backend.infrastructure import browser_sessions as browser_session_store
from backend.infrastructure import network
from backend.modules.video import constants as video_constants
from backend.modules.video import metadata as video_metadata
from backend.modules.video import parse_cache as video_parse_cache
from backend.modules.video import platforms as video_platforms
from backend.modules.video import resources as video_resources
from backend.modules.video import schemas as video_schemas
from backend.modules.video import sniffing as video_sniffing
from backend.modules.video import thumbnails as video_thumbnails
from backend.modules.video import urls as video_urls


@contextmanager
def _temporary_browser_page():
    with sync_playwright() as playwright:
        options = {
            "headless": True,
            "args": ["--disable-blink-features=AutomationControlled", "--autoplay-policy=no-user-gesture-required",
                     "--disable-extensions", "--disable-notifications", "--mute-audio"],
        }
        executable = video_resources._browser_executable()
        if executable:
            options["executable_path"] = str(executable)
        browser = playwright.chromium.launch(**options)
        try:
            yield browser.new_page(user_agent=video_constants.DEFAULT_HEADERS["User-Agent"], locale="zh-CN",
                                   viewport={"width": 1365, "height": 768})
        finally:
            browser.close()


def parse_browser_sniffed_page(url: str) -> video_schemas.ParseResponse:
    normalized = video_urls.normalize_url(url)
    cached = video_parse_cache._get_sniff_cache(normalized)
    if cached:
        return cached
    if video_urls._is_douyin_url(normalized) and accounts.principal.get():
        return browser_session_store.sessions.run(accounts.owner_id(), lambda page: _parse_browser_sniffed_page(normalized, page), url=normalized)
    return _parse_browser_sniffed_page(normalized)


def _parse_browser_sniffed_page(url: str, persistent_page=None) -> video_schemas.ParseResponse:

    captured: list[str] = []
    page_meta: dict[str, Any] = {}
    pornhub_player_data: dict[str, Any] = {}
    page_html = ""
    normalized_url = video_urls.normalize_url(url)
    cached = video_parse_cache._get_sniff_cache(normalized_url)
    if cached:
        return cached
    is_youtube_page = video_urls._is_youtube_url(normalized_url)
    is_douyin_page = video_urls._is_douyin_url(normalized_url)
    is_pornhub_page = video_urls._is_pornhub_url(normalized_url)
    douyin_meta: dict[str, Any] = {}

    verification_detected = False
    with nullcontext(persistent_page) if persistent_page is not None else _temporary_browser_page() as page:

        def remember(candidate: str) -> None:
            cleaned = html.unescape(candidate).replace("&amp;", "&")
            if video_resources._is_probable_video_resource(cleaned) and cleaned not in captured:
                captured.append(cleaned)

        def route_request(route: Any) -> None:
            request = route.request
            if accounts.principal.get() and request.url.startswith(("http://", "https://")):
                try:
                    network.public_url(request.url)
                except HTTPException:
                    route.abort()
                    return
            remember(request.url)
            if (request.resource_type in {"image", "font", "stylesheet"} and not is_douyin_page) or (
                request.resource_type == "media" and not (is_youtube_page or is_douyin_page or is_pornhub_page)
            ):
                route.abort()
                return
            route.continue_()

        page.route("**/*", route_request)
        def record_request(request: Any) -> None:
            remember(request.url)
        page.on("request", record_request)
        def record_response(response: Any) -> None:
            remember(response.url)
            if is_douyin_page:
                douyin_meta.update(video_platforms._extract_douyin_response_meta(response, video_urls._extract_douyin_video_id(normalized_url)))
        page.on("response", record_response)
        try:
            if persistent_page is None or page.url != normalized_url:
                page.goto(normalized_url, wait_until="commit", timeout=8000 if is_douyin_page else 12000)
            elif is_douyin_page and not needs_verification(page):
                # Reuse media already loaded during verification before navigating again.
                resources = page.evaluate("""() => [...performance.getEntriesByType('resource').map(item => item.name), ...Array.from(document.querySelectorAll('video')).map(item => item.currentSrc)]""")
                for resource in resources:
                    remember(resource)
                if not captured:
                    page.reload(wait_until="commit", timeout=8000)
            deadline = time.time() + (8 if is_douyin_page else 12 if (is_youtube_page or is_pornhub_page) else 4)
            while time.time() < deadline:
                if is_youtube_page:
                    if any(video_resources._is_youtube_progressive_stream(item) for item in captured) or (
                        any(video_resources._is_youtube_video_stream(item) for item in captured)
                        and any(video_resources._is_youtube_audio_stream(item) for item in captured)
                    ):
                        break
                elif is_douyin_page:
                    has_video = any(("douyinvod.com" in item.lower() or "mime_type=video" in item.lower()) and not video_resources._is_douyin_audio_stream(item) for item in captured)
                    has_audio = any(video_resources._is_douyin_audio_stream(item) for item in captured)
                    if has_video and has_audio:
                        break
                    if has_video and time.time() > deadline - 3:
                        break
                elif is_pornhub_page:
                    if any(video_resources._is_signed_pornhub_media_url(item) for item in captured):
                        break
                elif captured and (
                    any(video_resources._is_high_confidence_video(item) for item in captured)
                    or time.time() > deadline - 2
                ):
                    break
                page.wait_for_timeout(500)
            if is_douyin_page:
                verification_detected = needs_verification(page)
                if verification_detected:
                    raise verification_required(normalized_url)
                try:
                    page.evaluate("""() => { const video = Array.from(document.querySelectorAll('video')).find(item => item.currentSrc.startsWith('blob:') || item.currentSrc.includes('.douyinvod.com/')); if (video) { video.muted = true; video.play().catch(() => {}); } }""")
                    page.wait_for_function("""() => { const video = Array.from(document.querySelectorAll('video')).find(item => item.currentSrc.startsWith('blob:') || item.currentSrc.includes('.douyinvod.com/')); return video?.readyState >= 2 && video.videoWidth > 0 && video.currentTime > 0.05; }""", timeout=3000)
                except Exception:
                    pass
            page_meta = video_metadata._extract_browser_page_meta(page, normalized_url)
            if video_urls._is_douyin_url(normalized_url):
                page_meta.update(douyin_meta)
                if not douyin_meta.get("thumbnail"):
                    local_thumbnail = video_metadata._capture_page_thumbnail(page, normalized_url)
                    if local_thumbnail:
                        page_meta["thumbnail"] = local_thumbnail
            if is_pornhub_page:
                pornhub_player_data = video_platforms._extract_pornhub_player_data(page)
            page_html = page.content()
        finally:
            page.remove_listener("request", record_request)
            page.remove_listener("response", record_response)
            page.unroute("**/*", route_request)

    if is_pornhub_page:
        player_response = video_platforms._pornhub_player_response(normalized_url, page_meta, pornhub_player_data)
        if player_response:
            video_parse_cache._set_sniff_cache(normalized_url, player_response)
            return player_response

    for match in re.finditer(r"https?://[^\"'<>\\\s]+", page_html):
        remember(match.group(0))
    for match in re.finditer(r'(?:videoUrl|qualityUrl|defaultQuality)[^"\']*["\'](https?://[^"\']+)["\']', page_html):
        remember(match.group(1).encode("utf-8").decode("unicode_escape"))

    ranked = sorted(captured, key=video_resources._score_video_resource, reverse=True)
    if not ranked:
        if is_douyin_page:
            raise verification_required(normalized_url, detected=verification_detected)
        raise HTTPException(status_code=422, detail="浏览器嗅探没有发现可下载的视频资源。")

    formats = []
    if video_urls._is_youtube_url(normalized_url):
        video_stream = next((item for item in ranked if video_resources._is_youtube_video_stream(item)), None)
        audio_stream = next((item for item in ranked if video_resources._is_youtube_audio_stream(item)), None)
        if video_stream and audio_stream:
            formats.append(
                video_schemas.FormatInfo(
                    format_id=f"sniffpair:{quote(video_stream, safe='')}|{quote(audio_stream, safe='')}",
                    label="MP4 · 自动合并音频",
                    ext="mp4",
                    resolution="自动",
                )
            )

    for index, media_url in enumerate(ranked[:8], start=1):
        if is_douyin_page and video_resources._is_douyin_audio_stream(media_url):
            continue
        ext = video_sniffing._media_ext(media_url)
        if video_urls._is_youtube_url(normalized_url):
            if video_resources._is_youtube_progressive_stream(media_url):
                label = "MP4 · 含音频"
            else:
                continue
        elif video_urls._is_douyin_url(normalized_url):
            label = "HLS · 高清线路" if ext == "m3u8" else "MP4 · 高清线路"
        elif video_urls._is_pornhub_url(normalized_url):
            label = "HLS · 播放器线路" if ext == "m3u8" else "MP4 · 播放器线路"
        elif "mime_type=video" in media_url.lower() and ext == "mp4":
            label = "MP4 · 视频"
        elif ext == "m3u8":
            label = "HLS · 视频"
        else:
            label = f"{ext.upper()} · 视频"
        formats.append(
            video_schemas.FormatInfo(
                format_id=f"sniff:{quote(media_url, safe='')}",
                label=f"{label} · 线路 {index}",
                ext="mp4" if ext == "m3u8" else ext,
                resolution="自动",
            )
        )

    if is_douyin_page:
        audio_stream = next((item for item in ranked if video_resources._is_douyin_audio_stream(item)), None)
        video_stream = next((item for item in ranked if not video_resources._is_douyin_audio_stream(item)), None)
        if audio_stream and video_stream:
            formats.append(video_schemas.FormatInfo(
                format_id=f"sniffpair:{quote(video_stream, safe='')}|{quote(audio_stream, safe='')}",
                label="MP4 · 完整音视频", ext="mp4", resolution="自动",
            ))

    if video_urls._is_youtube_url(normalized_url) and not formats:
        raise HTTPException(status_code=422, detail="浏览器嗅探没有捕获可直接下载的 YouTube 音视频组合。")
    if is_douyin_page and not formats:
        raise verification_required(normalized_url, detected=False)

    default_title = "YouTube 视频" if video_urls._is_youtube_url(normalized_url) else "抖音视频" if video_urls._is_douyin_url(normalized_url) else "网页视频"
    default_uploader = "YouTube" if video_urls._is_youtube_url(normalized_url) else "抖音" if video_urls._is_douyin_url(normalized_url) else urlparse(url).netloc
    response = video_schemas.ParseResponse(
        title=page_meta.get("title") or default_title,
        thumbnail=video_thumbnails._thumbnail_url(page_meta.get("thumbnail")),
        duration=page_meta.get("duration"),
        uploader=page_meta.get("author") or default_uploader,
        webpage_url=normalized_url,
        formats=formats,
    )
    video_parse_cache._set_sniff_cache(normalized_url, response)
    return response
