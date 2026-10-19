from __future__ import annotations

from typing import Any

from fastapi import HTTPException
from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError

from backend.core import paths
from backend.infrastructure import runtime
from backend.modules.video import browser as video_browser
from backend.modules.video import douyin as video_douyin
from backend.modules.video import formats as video_formats
from backend.modules.video import schemas as video_schemas
from backend.modules.video import sniffing as video_sniffing
from backend.modules.video import thumbnails as video_thumbnails
from backend.modules.video import urls as video_urls


def _ydl_base_options(url: str | None = None) -> dict[str, Any]:
    url = video_urls.normalize_url(url) if url else url
    options: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "socket_timeout": 20,
        "retries": 2,
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/126.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        },
    }
    if (paths.FFMPEG_DIR / runtime.executable_name("ffmpeg")).is_file():
        options["ffmpeg_location"] = str(paths.FFMPEG_DIR)
    if paths.NODE_EXE and paths.NODE_EXE.is_file():
        options["js_runtimes"] = {"node": {"path": str(paths.NODE_EXE)}}
    if url and video_urls._is_bilibili_url(url):
        options["http_headers"].update(
            {
                "Origin": "https://www.bilibili.com",
                "Referer": "https://www.bilibili.com/",
            }
        )
    if url and video_urls._is_pornhub_url(url):
        options["http_headers"].update(
            {
                "Origin": "https://www.pornhub.com",
                "Referer": "https://www.pornhub.com/",
                "Accept-Language": "en-US,en;q=0.9",
            }
        )
    if url and video_urls._is_youtube_url(url):
        options["http_headers"].update(
            {
                "Origin": "https://www.youtube.com",
                "Referer": "https://www.youtube.com/",
                "Accept-Language": "en-US,en;q=0.9",
            }
        )
        options["extractor_args"] = {"youtube": {"player_client": ["web", "android"]}}
    if url and video_urls._is_douyin_url(url):
        options["http_headers"].update(
            {
                "Origin": "https://www.douyin.com",
                "Referer": "https://www.douyin.com/",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            }
        )
    return options


def parse_video(url: str) -> video_schemas.ParseResponse:
    url = video_urls.normalize_url(url)
    if video_urls._is_douyin_search_page(url):
        raise HTTPException(status_code=422, detail="这是抖音搜索结果页，请打开具体视频后复制视频地址，或使用该视频的分享链接。")
    if video_urls._is_douyin_url(url):
        try:
            return video_browser.parse_browser_sniffed_page(url)
        except HTTPException as exc:
            if isinstance(exc.detail, dict) and exc.detail.get("code") == "browser_verification_required":
                raise
            try:
                return video_douyin.parse_douyin_public(url)
            except HTTPException:
                pass

    options = {
        **_ydl_base_options(url),
        "skip_download": True,
    }
    try:
        if video_urls._is_pornhub_url(url):
            try:
                return video_browser.parse_browser_sniffed_page(url)
            except HTTPException:
                pass
        if video_urls._is_youtube_url(url):
            try:
                return video_browser.parse_browser_sniffed_page(url)
            except HTTPException:
                pass
        with YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=False)
    except DownloadError as exc:
        try:
            return video_sniffing.parse_sniffed_page(url)
        except HTTPException:
            try:
                return video_browser.parse_browser_sniffed_page(url)
            except HTTPException:
                raise HTTPException(status_code=422, detail=video_urls._clean_error(exc, url)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=video_urls._clean_error(exc, url)) from exc

    if not isinstance(info, dict):
        raise HTTPException(status_code=422, detail="无法解析该链接，请换一个公开视频链接。")

    return video_schemas.ParseResponse(
        title=info.get("title") or "未命名视频",
        thumbnail=video_thumbnails._proxy_thumbnail(info.get("thumbnail")),
        duration=video_thumbnails._safe_int(info.get("duration")),
        uploader=info.get("uploader") or info.get("channel"),
        webpage_url=info.get("webpage_url") or url,
        formats=video_formats._extract_formats(info),
    )
