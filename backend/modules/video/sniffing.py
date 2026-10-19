from __future__ import annotations

import re
import html
from pathlib import Path
from urllib.parse import quote, urljoin, urlparse

import requests
from fastapi import HTTPException

from backend.modules.video import constants as video_constants
from backend.modules.video import schemas as video_schemas


def _looks_like_media_url(url: str) -> bool:
    path = urlparse(url).path.lower()
    return path.endswith(video_constants.MEDIA_EXTENSIONS)


def _media_ext(url: str) -> str:
    path = urlparse(url).path.lower()
    for ext in video_constants.MEDIA_EXTENSIONS:
        if path.endswith(ext):
            return ext.removeprefix(".")
    return "mp4"


def _fetch_html(url: str) -> str:
    try:
        response = requests.get(url, headers=video_constants.DEFAULT_HEADERS, timeout=(10, 25))
        response.raise_for_status()
    except requests.RequestException as exc:
        raise HTTPException(status_code=422, detail="网页访问失败，无法嗅探视频资源。") from exc
    content_type = response.headers.get("Content-Type", "").lower()
    if "text/html" not in content_type and "application/xhtml" not in content_type:
        return ""
    return response.text or ""


def _sniff_media_urls(page_url: str, html_text: str) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()

    candidates = [match.group(1) for match in video_constants.MEDIA_SRC_PATTERN.finditer(html_text)]
    candidates.extend(match.group(0) for match in video_constants.MEDIA_URL_PATTERN.finditer(html_text))

    for raw in candidates:
        cleaned = html.unescape(raw).replace("\\/", "/").strip()
        absolute = urljoin(page_url, cleaned)
        if not _looks_like_media_url(absolute) or absolute in seen:
            continue
        seen.add(absolute)
        found.append(absolute)
    return found[:12]


def parse_sniffed_page(url: str) -> video_schemas.ParseResponse:
    if _looks_like_media_url(url):
        ext = _media_ext(url)
        return video_schemas.ParseResponse(
            title=Path(urlparse(url).path).name or "嗅探视频",
            thumbnail=None,
            duration=None,
            uploader="网页直链",
            webpage_url=url,
            formats=[
                video_schemas.FormatInfo(
                    format_id=f"sniff:{quote(url, safe='')}",
                    label=f"直链 {ext.upper()}",
                    ext="mp4" if ext == "m3u8" else ext,
                    resolution="自动",
                )
            ],
        )

    html_text = _fetch_html(url)
    media_urls = _sniff_media_urls(url, html_text)
    if not media_urls:
        raise HTTPException(
            status_code=422,
            detail="没有在页面中嗅探到公开视频资源。该页面可能使用了动态播放器、加密流、DRM，或需要登录后才会加载视频。",
        )

    title_match = re.search(r"<title[^>]*>(.*?)</title>", html_text, re.IGNORECASE | re.DOTALL)
    title = html.unescape(re.sub(r"\s+", " ", title_match.group(1)).strip()) if title_match else "嗅探视频"
    formats = []
    for index, media_url in enumerate(media_urls, start=1):
        ext = _media_ext(media_url)
        label = "HLS 流 MP4" if ext == "m3u8" else f"直链 {ext.upper()}"
        formats.append(
            video_schemas.FormatInfo(
                format_id=f"sniff:{quote(media_url, safe='')}",
                label=f"{label} · 资源 {index}",
                ext="mp4" if ext == "m3u8" else ext,
                resolution="自动",
            )
        )

    return video_schemas.ParseResponse(
        title=title,
        thumbnail=None,
        duration=None,
        uploader=urlparse(url).netloc,
        webpage_url=url,
        formats=formats,
    )
