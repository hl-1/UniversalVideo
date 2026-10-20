from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse

import requests

from backend.modules.video import constants as video_constants
from backend.infrastructure import video_network


def _host(url: str) -> str:
    return urlparse(url).netloc.lower()


def _is_bilibili_url(url: str) -> bool:
    host = _host(url)
    return video_network.matches(url, ("bilibili.com", "b23.tv"))


def _is_pornhub_url(url: str) -> bool:
    return video_network.matches(url, ("pornhub.com",))


def _is_youtube_url(url: str) -> bool:
    host = _host(url)
    return video_network.matches(url, ("youtube.com", "youtu.be"))


def _extract_youtube_video_id(url: str) -> str | None:
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    if "youtu.be" in host:
        video_id = parsed.path.strip("/").split("/")[0]
        return video_id or None

    query_id = parse_qs(parsed.query).get("v")
    if query_id and query_id[0]:
        return query_id[0]

    match = re.search(r"/(?:embed|shorts)/([A-Za-z0-9_-]{6,})", parsed.path)
    return match.group(1) if match else None


def _is_douyin_url(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return any(host == domain or host.endswith("." + domain) for domain in ("douyin.com", "iesdouyin.com", "amemv.com"))


def _is_douyin_search_page(url: str) -> bool:
    path = urlparse(url).path
    return _is_douyin_url(url) and (path.rstrip("/") == "/search" or path.startswith("/search/"))


def _extract_douyin_video_id(url: str) -> str | None:
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    for key in ("modal_id", "item_ids", "group_id", "aweme_id"):
        values = query.get(key)
        if values:
            match = re.search(r"(\d{8,24})", values[0])
            if match:
                return match.group(1)

    if _is_douyin_search_page(url):
        return None

    for pattern in (r"/video/(\d{8,24})", r"/note/(\d{8,24})", r"/(\d{8,24})(?:/|$)"):
        match = re.search(pattern, parsed.path)
        if match:
            return match.group(1)

    fallback = re.search(r"(?<!\d)(\d{8,24})(?!\d)", url)
    return fallback.group(1) if fallback else None


def _resolve_douyin_url(url: str) -> str:
    if _extract_douyin_video_id(url):
        return url
    try:
        response = video_network.get(
            url,
            headers=video_constants.DOUYIN_HEADERS,
            timeout=(10, 20),
        )
        try:
            response.raise_for_status()
            return response.url or url
        finally:
            response.close()
    except requests.RequestException:
        return url


def normalize_url(url: str) -> str:
    parsed = urlparse(url)
    if _is_pornhub_url(url) and parsed.path.strip("/") and "viewkey=" not in parsed.query:
        viewkey = parsed.path.strip("/").split("/")[-1]
        if re.fullmatch(r"[A-Za-z0-9]+", viewkey):
            return f"https://www.pornhub.com/view_video.php?viewkey={viewkey}"
    if _is_douyin_url(url):
        video_id = _extract_douyin_video_id(url)
        if video_id:
            return f"https://www.douyin.com/video/{video_id}"
    return url


def _clean_error(error: Exception, url: str | None = None) -> str:
    from backend.infrastructure.ytdlp import network_failure
    if failure := network_failure(error):
        return failure.message.replace("重试播放", "重试")
    from fastapi import HTTPException
    if isinstance(error, HTTPException):
        return str(error.detail)
    text = str(error).strip()
    text = re.sub(r"\x1b\[[0-9;]*m", "", text)
    text = text.replace("ERROR:", "").strip()
    if url and _is_bilibili_url(url) and "B站公开 API 解析失败" in text:
        return text
    if url and _is_bilibili_url(url) and "412" in text:
        return (
            "B站请求返回 HTTP 412，被平台访问校验拒绝。"
            "请稍后重试；该状态不能单独证明视频需要登录。"
        )
    if url and _is_pornhub_url(url) and "410" in text:
        return (
            "目标站点返回 410 Gone，表示该视频页面已删除、下架、地区不可访问，"
            "或需要站点侧登录/年龄校验后才可访问。当前版本仅支持无需 Cookie 的公开资源。"
        )
    if "No Token" in text or ("470" in text and "phncdn" in text):
        return (
            "视频 CDN 返回 470 No Token，说明当前拿到的是缺少签名 token 的临时直链。"
            "请粘贴原始视频页面链接重新解析，不要直接使用 pix/phncdn 的 mp4 地址。"
        )
    if url and _is_youtube_url(url) and (
        "not a bot" in text.lower()
        or "sign in" in text.lower()
        or "cookies" in text.lower()
    ):
        return (
            "YouTube 要求确认不是机器人或需要登录态。当前版本仅支持无需 Cookie 的公开资源。"
        )
    if url and _is_douyin_url(url) and "cookies" in text.lower():
        return (
            "抖音公开视频解析失败。系统已先尝试专用公开解析模块，再回退 yt-dlp；"
            "如果仍提示 fresh cookies，通常是平台签名参数、风控或访问权限变化导致。"
            "当前版本不再依赖 Cookie，请稍后重试或更换公开链接。"
        )
    if url and _is_douyin_url(url) and ("encrypt_data" in text.lower() or "11110" in text):
        return "抖音公开接口要求加密参数，当前链接无法通过旧公开 API 直接解析，系统会继续尝试 yt-dlp 兜底。"
    return text or "处理失败，请确认链接是否公开可访问。"
