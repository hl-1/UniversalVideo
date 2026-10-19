from __future__ import annotations

import re
import json
import html
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse
import logging

import requests
from yt_dlp import YoutubeDL
from yt_dlp.cookies import extract_cookies_from_browser
from yt_dlp.utils import DownloadError


logger = logging.getLogger("videodream.summary")
from backend.core import paths
from backend.modules.video import constants as video_constants
from backend.modules.video import parsing as video_parsing
from backend.modules.video import schemas as video_schemas
from backend.modules.video import thumbnails as video_thumbnails
from backend.modules.video import urls as video_urls


def _format_timestamp(seconds: float) -> str:
    total = max(0, int(seconds))
    hours = total // 3600
    minutes = (total % 3600) // 60
    secs = total % 60
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def _parse_timestamp(value: str) -> float:
    text = value.strip().replace(",", ".")
    parts = text.split(":")
    try:
        if len(parts) == 3:
            hours, minutes, seconds = parts
            return int(hours) * 3600 + int(minutes) * 60 + float(seconds)
        if len(parts) == 2:
            minutes, seconds = parts
            return int(minutes) * 60 + float(seconds)
        return float(text)
    except ValueError:
        return 0.0


def _clean_subtitle_text(value: str) -> str:
    text = html.unescape(value)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\{\\.*?\}", " ", text)
    text = re.sub(r"\\[Nn]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _dedupe_segments(segments: list[video_schemas.SubtitleSegment]) -> list[video_schemas.SubtitleSegment]:
    deduped: list[video_schemas.SubtitleSegment] = []
    previous_key = ""
    for segment in sorted(segments, key=lambda item: item.start):
        clean_text = _clean_subtitle_text(segment.text)
        if not clean_text:
            continue
        key = f"{round(segment.start, 1)}:{clean_text}"
        repeated_text = deduped and deduped[-1].text == clean_text
        if key == previous_key or repeated_text:
            continue
        previous_key = key
        deduped.append(
            video_schemas.SubtitleSegment(
                start=segment.start,
                end=segment.end,
                timestamp=_format_timestamp(segment.start),
                text=clean_text,
            )
        )
    return deduped


def _parse_vtt_or_srt(content: str) -> list[video_schemas.SubtitleSegment]:
    normalized = content.replace("\r\n", "\n").replace("\r", "\n")
    blocks = re.split(r"\n{2,}", normalized)
    segments: list[video_schemas.SubtitleSegment] = []
    time_pattern = re.compile(
        r"(?P<start>\d{1,2}:\d{2}(?::\d{2})?[\.,]\d{1,3}|\d{1,2}:\d{2}:\d{2})\s*-->\s*"
        r"(?P<end>\d{1,2}:\d{2}(?::\d{2})?[\.,]\d{1,3}|\d{1,2}:\d{2}:\d{2})"
    )
    for block in blocks:
        lines = [line.strip() for line in block.split("\n") if line.strip()]
        if not lines:
            continue
        timing_index = next((index for index, line in enumerate(lines) if "-->" in line), -1)
        if timing_index < 0:
            continue
        match = time_pattern.search(lines[timing_index])
        if not match:
            continue
        text = " ".join(lines[timing_index + 1 :])
        start = _parse_timestamp(match.group("start"))
        end = _parse_timestamp(match.group("end"))
        segments.append(
            video_schemas.SubtitleSegment(
                start=start,
                end=end,
                timestamp=_format_timestamp(start),
                text=text,
            )
        )
    return _dedupe_segments(segments)


def _json3_events_to_segments(events: list[dict[str, Any]]) -> list[video_schemas.SubtitleSegment]:
    segments: list[video_schemas.SubtitleSegment] = []
    for event in events:
        start_ms = event.get("tStartMs")
        if start_ms is None:
            continue
        duration_ms = event.get("dDurationMs")
        pieces = event.get("segs") or []
        text = "".join(str(piece.get("utf8") or "") for piece in pieces if isinstance(piece, dict))
        start = float(start_ms) / 1000
        end = start + float(duration_ms or 0) / 1000 if duration_ms else None
        segments.append(
            video_schemas.SubtitleSegment(
                start=start,
                end=end,
                timestamp=_format_timestamp(start),
                text=text,
            )
        )
    return _dedupe_segments(segments)


def _bilibili_json_body_to_segments(body: list[dict[str, Any]]) -> list[video_schemas.SubtitleSegment]:
    segments: list[video_schemas.SubtitleSegment] = []
    for item in body:
        start_value = item.get("from")
        if start_value is None:
            continue
        try:
            start = float(start_value)
            end_value = item.get("to")
            end = float(end_value) if end_value is not None else None
        except (TypeError, ValueError):
            continue
        segments.append(
            video_schemas.SubtitleSegment(
                start=start,
                end=end,
                timestamp=_format_timestamp(start),
                text=str(item.get("content") or ""),
            )
        )
    return _dedupe_segments(segments)


def _parse_subtitle_content(content: str, ext: str) -> list[video_schemas.SubtitleSegment]:
    normalized_ext = ext.lower()
    if normalized_ext in {"json", "json3"}:
        try:
            payload = json.loads(content)
            if isinstance(payload, dict):
                events = payload.get("events")
                if isinstance(events, list):
                    return _json3_events_to_segments(events)
                body = payload.get("body")
                if isinstance(body, list):
                    return _bilibili_json_body_to_segments(body)
        except json.JSONDecodeError:
            return []
        return []
    return _parse_vtt_or_srt(content)


def _language_rank(language: str) -> tuple[int, str]:
    lowered = language.lower()
    if lowered.startswith("ai-"):
        lowered = lowered.removeprefix("ai-")
    for index, preferred in enumerate(video_constants.SUBTITLE_LANGUAGE_PRIORITY):
        preferred_lower = preferred.lower()
        if lowered == preferred_lower or lowered.startswith(f"{preferred_lower}-"):
            return index, language
    return len(video_constants.SUBTITLE_LANGUAGE_PRIORITY), language


def _format_rank(ext: str) -> int:
    normalized = ext.lower()
    try:
        return video_constants.SUBTITLE_FORMAT_PRIORITY.index(normalized)
    except ValueError:
        return len(video_constants.SUBTITLE_FORMAT_PRIORITY)


def _select_subtitle(info: dict[str, Any]) -> tuple[str, str, dict[str, Any]] | None:
    candidates: list[tuple[tuple[int, str, int], str, str, dict[str, Any]]] = []
    for source_key, source_label in (("subtitles", "manual"), ("automatic_captions", "automatic")):
        subtitle_map = info.get(source_key)
        if not isinstance(subtitle_map, dict):
            continue
        for language, formats in subtitle_map.items():
            if str(language).lower() == "danmaku":
                continue
            if not isinstance(formats, list):
                continue
            language_rank = _language_rank(str(language))
            for item in formats:
                if not isinstance(item, dict) or not item.get("url"):
                    continue
                ext = str(item.get("ext") or "").lower()
                candidates.append(((*language_rank, _format_rank(ext)), str(language), source_label, item))
    if not candidates:
        return None
    _, language, source, subtitle = sorted(candidates, key=lambda item: item[0])[0]
    return language, source, subtitle


def _download_subtitle(subtitle: dict[str, Any], referer: str) -> tuple[str, str]:
    subtitle_url = str(subtitle.get("url") or "")
    ext = str(subtitle.get("ext") or Path(urlparse(subtitle_url).path).suffix.lstrip(".") or "vtt").lower()
    if not subtitle_url:
        raise RuntimeError("字幕地址为空。")
    response = requests.get(
        subtitle_url,
        headers={**video_constants.DEFAULT_HEADERS, "Referer": referer},
        timeout=(10, 45),
    )
    response.raise_for_status()
    response.encoding = response.encoding or "utf-8"
    return response.text, ext


def _is_cookie_error(error: Exception) -> bool:
    return "cookies" in str(error).lower()


def _extract_douyin_page_text_summary_source(url: str) -> tuple[dict[str, Any], str, str, list[video_schemas.SubtitleSegment]]:
    parsed = video_parsing.parse_video(url)
    lines = []
    title = parsed.title.strip() if parsed.title else "抖音视频"
    if title:
        lines.append(f"视频标题：{title}")
    if parsed.uploader:
        lines.append(f"作者：{parsed.uploader}")
    if parsed.duration:
        lines.append(f"时长：{_format_timestamp(parsed.duration)}")

    text = "\n".join(lines).strip()
    if not text:
        raise RuntimeError("抖音没有可用字幕或页面文案，暂时无法生成总结。")

    return (
        {
            "title": title,
            "webpage_url": parsed.webpage_url or url,
        },
        "zh-CN",
        "page_text",
        [
            video_schemas.SubtitleSegment(
                start=0.0,
                end=None,
                timestamp="0:00",
                text=text,
            )
        ],
    )


def _bilibili_cookie_header(cookie_jar: Any) -> str:
    cookies = {
        cookie.name: cookie.value
        for cookie in cookie_jar
        if str(cookie.domain).lower().lstrip(".").endswith("bilibili.com")
    }
    if not cookies.get("SESSDATA"):
        return ""
    return "; ".join(f"{name}={value}" for name, value in sorted(cookies.items()))


def _bilibili_subtitle_source(subtitle: dict[str, Any]) -> str:
    language = str(subtitle.get("lan") or "").lower()
    return "automatic" if language.startswith("ai-") or subtitle.get("type") == 1 or subtitle.get("ai_type") else "manual"


def _extract_bilibili_browser_cookie_header() -> str:
    for browser_name in ("firefox", "chrome", "edge"):
        try:
            header = _bilibili_cookie_header(extract_cookies_from_browser(browser_name))
            if header:
                return header
        except Exception as exc:
            logger.warning("Bilibili cookie extraction failed for %s: %s", browser_name, type(exc).__name__)
            continue
    return ""


def _extract_bilibili_api_summary_source(
    url: str,
) -> tuple[dict[str, Any], str, str, list[video_schemas.SubtitleSegment]] | None:
    match = re.search(r"/video/(BV[0-9A-Za-z]+)", url, flags=re.IGNORECASE)
    if not match:
        return None
    bvid = match.group(1)
    headers = {**video_constants.DEFAULT_HEADERS, "Referer": url}
    try:
        view_response = requests.get(
            "https://api.bilibili.com/x/web-interface/view",
            params={"bvid": bvid},
            headers=headers,
            timeout=(10, 30),
        )
        view_response.raise_for_status()
        view_payload = view_response.json()
        view_data = view_payload.get("data") if isinstance(view_payload, dict) else None
        if view_payload.get("code") != 0 or not isinstance(view_data, dict) or view_data.get("bvid", bvid) != bvid:
            return None
        pages = view_data.get("pages")
        if not isinstance(pages, list) or not pages:
            return None
        page_number = max(1, video_thumbnails._safe_int(parse_qs(urlparse(url).query).get("p", [1])[0]) or 1)
        page = next((item for item in pages if isinstance(item, dict) and item.get("page") == page_number), pages[0])
        cid = page.get("cid") if isinstance(page, dict) else None
        if not cid:
            return None

        def load_player_payload(request_headers: dict[str, str]) -> dict[str, Any]:
            response = requests.get(
                "https://api.bilibili.com/x/player/wbi/v2",
                params={"bvid": bvid, "cid": cid},
                headers=request_headers,
                timeout=(10, 30),
            )
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict) or payload.get("code") != 0:
                return {}
            data = payload.get("data")
            if not isinstance(data, dict) or data.get("bvid", bvid) != bvid or str(data.get("cid", cid)) != str(cid):
                return {}
            return payload

        player_payload = load_player_payload(headers)
        player_data = player_payload.get("data") if isinstance(player_payload.get("data"), dict) else {}
        subtitle_data = player_data.get("subtitle") if isinstance(player_data.get("subtitle"), dict) else {}
        subtitles = subtitle_data.get("subtitles")
        if (not isinstance(subtitles, list) or not subtitles) and player_data.get("need_login_subtitle"):
            cookie_header = _extract_bilibili_browser_cookie_header()
            if cookie_header:
                headers = {**headers, "Cookie": cookie_header}
                player_payload = load_player_payload(headers)
                player_data = player_payload.get("data") if isinstance(player_payload.get("data"), dict) else {}
                subtitle_data = player_data.get("subtitle") if isinstance(player_data.get("subtitle"), dict) else {}
                subtitles = subtitle_data.get("subtitles")
        if not isinstance(subtitles, list) or not subtitles:
            return None
        candidates = [item for item in subtitles if isinstance(item, dict) and item.get("subtitle_url")]
        if not candidates:
            return None
        subtitle = sorted(candidates, key=lambda item: _language_rank(str(item.get("lan") or "")))[0]
        subtitle_url = str(subtitle["subtitle_url"])
        if subtitle_url.startswith("//"):
            subtitle_url = "https:" + subtitle_url
        subtitle_response = requests.get(subtitle_url, headers=headers, timeout=(10, 45))
        subtitle_response.raise_for_status()
        segments = _parse_subtitle_content(subtitle_response.text, "json")
        if not segments:
            return None
        owner = view_data.get("owner") if isinstance(view_data.get("owner"), dict) else {}
        info = {
            "title": str(view_data.get("title") or "B站视频"),
            "uploader": str(owner.get("name") or ""),
            "webpage_url": url,
        }
        source = _bilibili_subtitle_source(subtitle)
        return info, str(subtitle.get("lan") or "zh"), source, segments
    except (requests.RequestException, ValueError, TypeError, json.JSONDecodeError) as exc:
        logger.warning("Bilibili subtitle API failed: %s", type(exc).__name__)
        return None


def _extract_summary_source(url: str, *, speech_fallback: Callable | None = None) -> tuple[dict[str, Any], str, str, list[video_schemas.SubtitleSegment]]:
    if video_urls._is_bilibili_url(url):
        platform_source = _extract_bilibili_api_summary_source(url)
        if platform_source:
            return platform_source
    options = {
        **video_parsing._ydl_base_options(url),
        "skip_download": True,
        "writesubtitles": True,
        "writeautomaticsub": True,
        "subtitlesformat": "vtt/srt/json3/best",
    }
    try:
        with YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=False)
    except DownloadError as exc:
        if video_urls._is_douyin_url(url) and _is_cookie_error(exc):
            if speech_fallback:
                return speech_fallback(None)
            return _extract_douyin_page_text_summary_source(url)
        raise
    if not isinstance(info, dict):
        raise RuntimeError("无法解析视频信息。")

    selected = _select_subtitle(info)
    if not selected:
        if speech_fallback:
            return speech_fallback(info)
        if video_urls._is_bilibili_url(url):
            raise RuntimeError("平台字幕 API 和 yt-dlp 均未找到可用字幕。")
        raise RuntimeError("没有找到可用字幕。")
    language, source, subtitle = selected
    content, ext = _download_subtitle(subtitle, info.get("webpage_url") or url)
    segments = _parse_subtitle_content(content, ext)
    if not segments:
        if speech_fallback:
            return speech_fallback(info)
        raise RuntimeError("字幕下载成功，但无法解析为带时间戳的文本。")
    return info, language, source, segments
