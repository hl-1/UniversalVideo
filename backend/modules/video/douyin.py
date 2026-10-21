from __future__ import annotations

import json
import html
from typing import Any
from urllib.parse import quote

import requests
from fastapi import HTTPException

from backend.modules.video import constants as video_constants
from backend.modules.video import schemas as video_schemas
from backend.modules.video import thumbnails as video_thumbnails
from backend.modules.video import urls as video_urls


def _pick_url_from_nested(data: dict[str, Any], *keys: str) -> str | None:
    node: Any = data
    for key in keys:
        if not isinstance(node, dict):
            return None
        node = node.get(key)
    if isinstance(node, dict):
        values = node.get("url_list")
        if isinstance(values, list):
            return next((item for item in values if isinstance(item, str) and item), None)
    return None


def _extract_router_data(html: str) -> dict[str, Any]:
    marker = "window._ROUTER_DATA = "
    start = html.find(marker)
    if start < 0:
        return {}

    index = start + len(marker)
    while index < len(html) and html[index].isspace():
        index += 1
    if index >= len(html) or html[index] != "{":
        return {}

    depth = 0
    in_string = False
    escaped = False
    for cursor in range(index, len(html)):
        char = html[cursor]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(html[index : cursor + 1])
                except ValueError:
                    return {}
    return {}


def _extract_item_from_router_data(router_data: dict[str, Any]) -> dict[str, Any]:
    loader_data = router_data.get("loaderData")
    if not isinstance(loader_data, dict):
        return {}
    for node in loader_data.values():
        if not isinstance(node, dict):
            continue
        video_info = node.get("videoInfoRes")
        if not isinstance(video_info, dict):
            continue
        items = video_info.get("item_list")
        if isinstance(items, list) and items and isinstance(items[0], dict):
            return items[0]
    return {}


def _fetch_douyin_item(url: str) -> tuple[str, dict[str, Any]]:
    resolved_url = video_urls._resolve_douyin_url(url)
    video_id = video_urls._extract_douyin_video_id(resolved_url)
    if not video_id:
        raise HTTPException(status_code=422, detail="无法从抖音链接中提取视频 ID，请粘贴单个公开视频链接。")

    session = requests.Session()
    session.trust_env = False
    session.headers.update(video_constants.DOUYIN_HEADERS)
    try:
        from backend.modules.video import parse_runtime
        response = session.get(video_constants.DOUYIN_API_URL, params={"item_ids": video_id}, timeout=(parse_runtime.seconds(3), parse_runtime.seconds(4)))
        if response.ok and response.content:
            data = response.json()
            item_list = data.get("item_list") or []
            if item_list and isinstance(item_list[0], dict):
                return video_id, item_list[0]
    except (requests.RequestException, ValueError):
        pass

    share_url = f"https://www.iesdouyin.com/share/video/{video_id}/"
    try:
        response = session.get(share_url, timeout=(parse_runtime.seconds(3), parse_runtime.seconds(4)))
        response.raise_for_status()
    except requests.RequestException as exc:
        raise HTTPException(
            status_code=422,
            detail="抖音公开解析失败：接口或分享页当前不可访问，请稍后重试或换一个公开视频链接。",
        ) from exc

    item = _extract_item_from_router_data(_extract_router_data(response.text or ""))
    if item:
        return video_id, item

    raise HTTPException(
        status_code=422,
        detail="抖音公开解析失败：没有拿到公开视频信息。该链接可能已失效、非公开视频，或平台接口已调整。",
    )


def _douyin_media_url(item: dict[str, Any]) -> str:
    video = item.get("video") if isinstance(item.get("video"), dict) else {}
    play_addr = video.get("play_addr") if isinstance(video.get("play_addr"), dict) else {}
    url_list = play_addr.get("url_list") if isinstance(play_addr, dict) else None
    if isinstance(url_list, list):
        for candidate in url_list:
            if isinstance(candidate, str) and candidate:
                return candidate.replace("playwm", "play")

    uri = play_addr.get("uri") if isinstance(play_addr, dict) else None
    if isinstance(uri, str) and uri:
        return f"https://aweme.snssdk.com/aweme/v1/play/?video_id={quote(uri)}&ratio=720p&line=0"

    raise HTTPException(status_code=422, detail="抖音公开解析成功，但没有找到可下载的视频地址。")


def parse_douyin_public(url: str) -> video_schemas.ParseResponse:
    video_id, item = _fetch_douyin_item(url)
    video = item.get("video") if isinstance(item.get("video"), dict) else {}
    cover = (
        _pick_url_from_nested(video, "cover")
        or _pick_url_from_nested(video, "origin_cover")
        or _pick_url_from_nested(video, "dynamic_cover")
    )
    title = str(item.get("desc") or f"抖音视频 {video_id}").strip()
    duration = video_thumbnails._safe_int(video.get("duration"))
    if duration and duration > 1000:
        duration = duration // 1000

    return video_schemas.ParseResponse(
        title=title,
        thumbnail=video_thumbnails._proxy_thumbnail(cover),
        duration=duration,
        uploader=(item.get("author") or {}).get("nickname") if isinstance(item.get("author"), dict) else None,
        webpage_url=f"https://www.douyin.com/video/{video_id}",
        formats=[
            video_schemas.FormatInfo(
                format_id="douyin-public",
                label="公开视频 MP4",
                ext="mp4",
                resolution="自动",
            )
        ],
    )
