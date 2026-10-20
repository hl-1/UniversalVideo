from __future__ import annotations

from typing import Any

from fastapi import HTTPException
from yt_dlp.utils import DownloadError

from backend.core import paths
from backend.infrastructure import runtime
from backend.infrastructure import video_network, video_proxy, platform_access
from backend.infrastructure.ytdlp import YoutubeDL, is_bilibili_video_url
from backend.modules.video import browser as video_browser
from backend.modules.video import douyin as video_douyin
from backend.modules.video import formats as video_formats
from backend.modules.video import schemas as video_schemas
from backend.modules.video import sniffing as video_sniffing
from backend.modules.video import thumbnails as video_thumbnails
from backend.modules.video import urls as video_urls
from backend.modules.video import parse_runtime
from backend.modules.video import parse_adapters


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
    if url and video_urls._is_douyin_url(url):
        options["http_headers"].update(
            {
                "Origin": "https://www.douyin.com",
                "Referer": "https://www.douyin.com/",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            }
        )
    if video_network.domestic(url):
        options.update(proxy="", concurrent_fragment_downloads=4, hls_prefer_native=True,
                       retries=3, fragment_retries=3, skip_unavailable_fragments=False)
    elif chosen := video_proxy.selected(url):
        options.update(proxy=chosen["url"], concurrent_fragment_downloads=4, hls_prefer_native=True,
                       retries=3, fragment_retries=3, skip_unavailable_fragments=False)
    return options


def parse_video(url: str) -> video_schemas.ParseResponse:
    platform_access.require(url)
    if platform_access.platform(url) and video_proxy._active.get() is None:
        with video_proxy.scope(url):
            return parse_video(url)
    if parse_runtime.current.get() is None:
        with parse_runtime.scope(), video_proxy.scope(url):
            return parse_video(url)
    url = video_urls.normalize_url(url)
    parse_runtime.current.get().source_url = url
    if video_urls._is_douyin_search_page(url):
        raise HTTPException(status_code=422, detail="这是抖音搜索结果页，请打开具体视频后复制视频地址，或使用该视频的分享链接。")
    adapter = parse_adapters.select(url, _extract_metadata)
    failure = None
    for name, operation in adapter.routes:
        try:
            with parse_runtime.stage(name):
                result = operation(url)
            parse_runtime.check()
            return result
        except HTTPException as exc:
            if exc.status_code in {401, 403, 429, 503, 504} or (isinstance(exc.detail, dict) and exc.detail.get("code") == "browser_verification_required"):
                raise
            failure = failure or exc
        except DownloadError as exc:
            failure = HTTPException(422, video_urls._clean_error(exc, url))
            if adapter.definitive_extractor_error:
                raise failure from exc
        except Exception as exc:
            failure = failure or HTTPException(422, video_urls._clean_error(exc, url))
        parse_runtime.check()
    raise failure or HTTPException(422, "没有获取到可用的视频资源。")


def _extract_metadata(url):
    options = {
        **_ydl_base_options(url),
        "skip_download": True,
        "socket_timeout": parse_runtime.seconds(6),
        "retries": 0,
        "extractor_retries": 0,
    }
    with YoutubeDL(options) as ydl:
        info = ydl.extract_info(url, download=False)

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
