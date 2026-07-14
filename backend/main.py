from __future__ import annotations

import re
import json
import hashlib
import html
import logging
import os
import secrets
import subprocess
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import parse_qs, quote, unquote, urljoin, urlparse
from urllib.request import Request, urlopen

import requests
from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response, StreamingResponse
from openai import OpenAI
from playwright.sync_api import sync_playwright
from pydantic import BaseModel, Field, HttpUrl
from yt_dlp import YoutubeDL
from yt_dlp.cookies import extract_cookies_from_browser
from yt_dlp.utils import DownloadError


logger = logging.getLogger("videodream.summary")

ROOT_DIR = Path(__file__).resolve().parent.parent
DOWNLOAD_DIR = ROOT_DIR / "downloads"
THUMBNAIL_DIR = DOWNLOAD_DIR / "_thumbnails"
SUMMARY_DIR = DOWNLOAD_DIR / "_summaries"
CONFIG_DIR = ROOT_DIR / "config"
AI_CONFIG_PATH = CONFIG_DIR / "ai.json"
FFMPEG_DIR = Path(r"C:\softWare\environment\ffmpeg")
NODE_EXE = Path(r"C:\softWare\environment\nodejs\node.exe")
CHROME_EXE = Path(r"C:\Users\11871\AppData\Local\Google\Chrome\Application\chrome.exe")
EDGE_EXE = Path(r"C:\Program Files (x86)\Microsoft\EdgeCore\126.0.2592.113\msedge.exe")
DOWNLOAD_DIR.mkdir(exist_ok=True)
THUMBNAIL_DIR.mkdir(exist_ok=True)
SUMMARY_DIR.mkdir(exist_ok=True)

TaskStatus = Literal["pending", "running", "finished", "failed"]


class ParseRequest(BaseModel):
    url: HttpUrl


class DownloadRequest(BaseModel):
    url: HttpUrl
    format_id: str | None = Field(default=None, max_length=4096)


class SummaryRequest(BaseModel):
    url: HttpUrl


class PreviewRequest(BaseModel):
    url: HttpUrl
    format_id: str = Field(min_length=1, max_length=4096)


class PreviewResponse(BaseModel):
    preview_url: str
    expires_in: int


class FormatInfo(BaseModel):
    format_id: str
    label: str
    ext: str | None = None
    resolution: str | None = None
    filesize: int | None = None


class ParseResponse(BaseModel):
    title: str
    thumbnail: str | None = None
    duration: int | None = None
    uploader: str | None = None
    webpage_url: str | None = None
    formats: list[FormatInfo]


class DownloadResponse(BaseModel):
    task_id: str


class SubtitleSegment(BaseModel):
    start: float
    end: float | None = None
    timestamp: str
    text: str


class SummaryResult(BaseModel):
    title: str
    webpage_url: str
    language: str
    source: str
    summary_markdown: str
    transcript: list[SubtitleSegment]
    markdown_url: str | None = None
    json_url: str | None = None


class SummaryResponse(BaseModel):
    task_id: str


class SummaryTaskResponse(BaseModel):
    task_id: str
    status: TaskStatus
    progress: float = 0
    message: str = ""
    error: str | None = None
    result: SummaryResult | None = None


class MindMapNode(BaseModel):
    id: str
    title: str
    summary: str = ""
    timestamp: float
    segment_ids: list[int]
    children: list[MindMapNode] = Field(default_factory=list)


class MindMapResult(BaseModel):
    title: str
    nodes: list[MindMapNode]
    generated_at: str


class MindMapResponse(BaseModel):
    task_id: str


class MindMapTaskResponse(BaseModel):
    task_id: str
    summary_task_id: str
    status: TaskStatus
    progress: float = 0
    message: str = ""
    error: str | None = None
    result: MindMapResult | None = None


class TaskResponse(BaseModel):
    task_id: str
    status: TaskStatus
    progress: float = 0
    message: str = ""
    filename: str | None = None
    download_url: str | None = None
    error: str | None = None


class DownloadTask:
    def __init__(
        self,
        task_id: str,
        url: str,
        format_id: str | None,
    ) -> None:
        self.task_id = task_id
        self.url = normalize_url(url)
        self.format_id = format_id
        self.status: TaskStatus = "pending"
        self.progress = 0.0
        self.message = "等待下载任务开始"
        self.filename: str | None = None
        self.error: str | None = None
        self.created_at = time.time()
        self.updated_at = time.time()

    def to_response(self) -> TaskResponse:
        download_url = f"/api/files/{quote(self.filename, safe='')}" if self.filename else None
        return TaskResponse(
            task_id=self.task_id,
            status=self.status,
            progress=round(self.progress, 2),
            message=self.message,
            filename=self.filename,
            download_url=download_url,
            error=self.error,
        )


class SummaryTask:
    def __init__(self, task_id: str, url: str) -> None:
        self.task_id = task_id
        self.url = normalize_url(url)
        self.status: TaskStatus = "pending"
        self.progress = 0.0
        self.message = "等待总结任务开始"
        self.error: str | None = None
        self.result: SummaryResult | None = None
        self.transcript: list[SubtitleSegment] = []
        self.created_at = time.time()
        self.updated_at = time.time()

    def to_response(self) -> SummaryTaskResponse:
        return SummaryTaskResponse(
            task_id=self.task_id,
            status=self.status,
            progress=round(self.progress, 2),
            message=self.message,
            error=self.error,
            result=self.result,
        )


class MindMapTask:
    def __init__(self, task_id: str, summary_task_id: str) -> None:
        self.task_id = task_id
        self.summary_task_id = summary_task_id
        self.status: TaskStatus = "pending"
        self.progress = 0.0
        self.message = "等待思维导图任务开始"
        self.error: str | None = None
        self.result: MindMapResult | None = None
        self.created_at = time.time()
        self.updated_at = time.time()

    def to_response(self) -> MindMapTaskResponse:
        return MindMapTaskResponse(
            task_id=self.task_id,
            summary_task_id=self.summary_task_id,
            status=self.status,
            progress=round(self.progress, 2),
            message=self.message,
            error=self.error,
            result=self.result,
        )


app = FastAPI(title="VideoDream API", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:5174",
        "http://127.0.0.1:5174",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

tasks: dict[str, DownloadTask] = {}
tasks_lock = threading.Lock()
summary_tasks: dict[str, SummaryTask] = {}
summary_tasks_lock = threading.Lock()
mind_map_tasks: dict[str, MindMapTask] = {}
mind_map_tasks_lock = threading.Lock()
executor = ThreadPoolExecutor(max_workers=2)
sniff_cache: dict[str, tuple[float, ParseResponse]] = {}
sniff_cache_lock = threading.Lock()
SNIFF_CACHE_TTL_SECONDS = 600
PREVIEW_TTL_SECONDS = 600
PREVIEW_RANGE_PATTERN = re.compile(r"^bytes=(?:\d+-\d*|-\d+)$")
SUBTITLE_LANGUAGE_PRIORITY = ("zh-CN", "zh-Hans", "zh", "zh-TW", "zh-Hant", "en")
SUBTITLE_FORMAT_PRIORITY = ("vtt", "srt", "json3", "json", "srv3", "ttml", "xml")
MAX_SUMMARY_TRANSCRIPT_CHARS = 28000
MAX_TRANSCRIPT_SEGMENTS_IN_RESPONSE = 500
MAX_MIND_MAP_DEPTH = 4
MAX_MIND_MAP_CHILDREN = 10

DOUYIN_API_URL = "https://www.iesdouyin.com/web/api/v2/aweme/iteminfo/"
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/json,*/*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Connection": "keep-alive",
}
DOUYIN_HEADERS = {
    **DEFAULT_HEADERS,
    "Referer": "https://www.douyin.com/",
    "Origin": "https://www.douyin.com",
}
MEDIA_URL_PATTERN = re.compile(
    r"https?://[^'\"<>\s\\]+?\.(?:m3u8|mp4|webm|mov)(?:\?[^'\"<>\s\\]*)?",
    re.IGNORECASE,
)
MEDIA_SRC_PATTERN = re.compile(
    r"""<(?:video|source)\b[^>]+?\bsrc=["']([^"']+)["']""",
    re.IGNORECASE,
)
MEDIA_EXTENSIONS = (".m3u8", ".mp4", ".webm", ".mov")
VIDEO_CDN_PATTERN = re.compile(
    r"(douyinvod\.com|googlevideo\.com/videoplayback|phncdn\.com|pornhub.*(?:mp4|m3u8)|mime_type=video_|/aweme/v1/play/|\.mp4(?:\?|$)|\.m3u8(?:\?|$)|\.webm(?:\?|$)|\.mov(?:\?|$))",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class PreviewSession:
    media_url: str
    referer: str
    expires_at: float


preview_sessions: dict[str, PreviewSession] = {}
preview_sessions_lock = threading.Lock()


def _host(url: str) -> str:
    return urlparse(url).netloc.lower()


def _is_bilibili_url(url: str) -> bool:
    host = _host(url)
    return "bilibili.com" in host or "b23.tv" in host


def _is_pornhub_url(url: str) -> bool:
    return "pornhub.com" in _host(url)


def _is_youtube_url(url: str) -> bool:
    host = _host(url)
    return "youtube.com" in host or "youtu.be" in host


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
    host = _host(url)
    return "douyin.com" in host or "iesdouyin.com" in host or "amemv.com" in host


def _extract_douyin_video_id(url: str) -> str | None:
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    for key in ("modal_id", "item_ids", "group_id", "aweme_id"):
        values = query.get(key)
        if values:
            match = re.search(r"(\d{8,24})", values[0])
            if match:
                return match.group(1)

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
        response = requests.get(
            url,
            headers=DOUYIN_HEADERS,
            allow_redirects=True,
            timeout=(10, 20),
        )
        response.raise_for_status()
        return response.url or url
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
    text = str(error).strip()
    text = re.sub(r"\x1b\[[0-9;]*m", "", text)
    text = text.replace("ERROR:", "").strip()
    if url and _is_bilibili_url(url) and "412" in text:
        return (
            "B站接口返回 412，通常是请求头或登录态校验导致。"
            "当前版本仅支持无需 Cookie 的公开资源，请确认链接可公开访问后重试。"
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


def _set_summary_task(task: SummaryTask, **changes: Any) -> None:
    with summary_tasks_lock:
        for key, value in changes.items():
            setattr(task, key, value)
        task.updated_at = time.time()


def _safe_filename_stem(value: str) -> str:
    clean = re.sub(r'[\\/:*?"<>|\s]+', " ", value).strip()[:80]
    return clean or "VideoDream"


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


def _dedupe_segments(segments: list[SubtitleSegment]) -> list[SubtitleSegment]:
    deduped: list[SubtitleSegment] = []
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
            SubtitleSegment(
                start=segment.start,
                end=segment.end,
                timestamp=_format_timestamp(segment.start),
                text=clean_text,
            )
        )
    return deduped


def _parse_vtt_or_srt(content: str) -> list[SubtitleSegment]:
    normalized = content.replace("\r\n", "\n").replace("\r", "\n")
    blocks = re.split(r"\n{2,}", normalized)
    segments: list[SubtitleSegment] = []
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
            SubtitleSegment(
                start=start,
                end=end,
                timestamp=_format_timestamp(start),
                text=text,
            )
        )
    return _dedupe_segments(segments)


def _json3_events_to_segments(events: list[dict[str, Any]]) -> list[SubtitleSegment]:
    segments: list[SubtitleSegment] = []
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
            SubtitleSegment(
                start=start,
                end=end,
                timestamp=_format_timestamp(start),
                text=text,
            )
        )
    return _dedupe_segments(segments)


def _bilibili_json_body_to_segments(body: list[dict[str, Any]]) -> list[SubtitleSegment]:
    segments: list[SubtitleSegment] = []
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
            SubtitleSegment(
                start=start,
                end=end,
                timestamp=_format_timestamp(start),
                text=str(item.get("content") or ""),
            )
        )
    return _dedupe_segments(segments)


def _parse_subtitle_content(content: str, ext: str) -> list[SubtitleSegment]:
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
    for index, preferred in enumerate(SUBTITLE_LANGUAGE_PRIORITY):
        preferred_lower = preferred.lower()
        if lowered == preferred_lower or lowered.startswith(f"{preferred_lower}-"):
            return index, language
    return len(SUBTITLE_LANGUAGE_PRIORITY), language


def _format_rank(ext: str) -> int:
    normalized = ext.lower()
    try:
        return SUBTITLE_FORMAT_PRIORITY.index(normalized)
    except ValueError:
        return len(SUBTITLE_FORMAT_PRIORITY)


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
        headers={**DEFAULT_HEADERS, "Referer": referer},
        timeout=(10, 45),
    )
    response.raise_for_status()
    response.encoding = response.encoding or "utf-8"
    return response.text, ext


def _is_cookie_error(error: Exception) -> bool:
    return "cookies" in str(error).lower()


def _extract_douyin_page_text_summary_source(url: str) -> tuple[dict[str, Any], str, str, list[SubtitleSegment]]:
    parsed = parse_video(url)
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
            SubtitleSegment(
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
) -> tuple[dict[str, Any], str, str, list[SubtitleSegment]] | None:
    match = re.search(r"/video/(BV[0-9A-Za-z]+)", url, flags=re.IGNORECASE)
    if not match:
        return None
    bvid = match.group(1)
    headers = {**DEFAULT_HEADERS, "Referer": url}
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
        if view_payload.get("code") != 0 or not isinstance(view_data, dict):
            return None
        pages = view_data.get("pages")
        if not isinstance(pages, list) or not pages:
            return None
        page_number = max(1, _safe_int(parse_qs(urlparse(url).query).get("p", [1])[0]) or 1)
        page = next((item for item in pages if isinstance(item, dict) and item.get("page") == page_number), pages[0])
        cid = page.get("cid") if isinstance(page, dict) else None
        if not cid:
            return None

        def load_player_payload(request_headers: dict[str, str]) -> dict[str, Any]:
            response = requests.get(
                "https://api.bilibili.com/x/player/v2",
                params={"bvid": bvid, "cid": cid},
                headers=request_headers,
                timeout=(10, 30),
            )
            response.raise_for_status()
            payload = response.json()
            return payload if isinstance(payload, dict) else {}

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


def _extract_summary_source(url: str) -> tuple[dict[str, Any], str, str, list[SubtitleSegment]]:
    if _is_bilibili_url(url):
        platform_source = _extract_bilibili_api_summary_source(url)
        if platform_source:
            return platform_source
    options = {
        **_ydl_base_options(url),
        "skip_download": True,
        "writesubtitles": True,
        "writeautomaticsub": True,
        "subtitlesformat": "vtt/srt/json3/best",
    }
    try:
        with YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=False)
    except DownloadError as exc:
        if _is_douyin_url(url) and _is_cookie_error(exc):
            return _extract_douyin_page_text_summary_source(url)
        raise
    if not isinstance(info, dict):
        raise RuntimeError("无法解析视频信息。")

    selected = _select_subtitle(info)
    if not selected:
        if _is_bilibili_url(url):
            raise RuntimeError("平台字幕 API 和 yt-dlp 均未找到可用字幕。")
        raise RuntimeError("没有找到可用字幕。")
    language, source, subtitle = selected
    content, ext = _download_subtitle(subtitle, info.get("webpage_url") or url)
    segments = _parse_subtitle_content(content, ext)
    if not segments:
        raise RuntimeError("字幕下载成功，但无法解析为带时间戳的文本。")
    return info, language, source, segments


def _summary_source_label(source: str) -> str:
    if source == "manual":
        return "人工字幕"
    if source == "page_text":
        return "页面文案"
    return "自动字幕"


def _load_ai_config() -> dict[str, str]:
    if not AI_CONFIG_PATH.exists():
        raise RuntimeError("缺少 AI 配置文件：请根据 config/ai.example.json 创建 config/ai.json。")
    try:
        payload = json.loads(AI_CONFIG_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError("AI 配置文件不是合法 JSON。") from exc
    api_key = str(payload.get("api_key") or os.environ.get("OPENAI_API_KEY") or "").strip()
    model = str(payload.get("model") or "").strip()
    base_url = str(payload.get("base_url") or "").strip()
    if not api_key:
        raise RuntimeError("AI 配置缺少 api_key。")
    if not model:
        raise RuntimeError("AI 配置缺少 model。")
    config = {"api_key": api_key, "model": model}
    if base_url:
        config["base_url"] = base_url
    return config


def _transcript_for_prompt(segments: list[SubtitleSegment]) -> str:
    lines: list[str] = []
    current_length = 0
    for segment in segments:
        line = f"[{segment.timestamp}] {segment.text}"
        next_length = current_length + len(line) + 1
        if next_length > MAX_SUMMARY_TRANSCRIPT_CHARS:
            lines.append("[内容过长，后续字幕已截断用于首版摘要。]")
            break
        lines.append(line)
        current_length = next_length
    return "\n".join(lines)


def _generate_ai_summary(title: str, webpage_url: str, segments: list[SubtitleSegment]) -> str:
    config = _load_ai_config()
    client_kwargs: dict[str, str] = {"api_key": config["api_key"]}
    if config.get("base_url"):
        client_kwargs["base_url"] = config["base_url"]
    client = OpenAI(**client_kwargs)
    transcript = _transcript_for_prompt(segments)
    completion = client.chat.completions.create(
        model=config["model"],
        messages=[
            {
                "role": "system",
                "content": (
                    "你是专业的视频内容整理助手。请基于带时间戳的字幕生成中文 Markdown 摘要。"
                    "必须包含四个二级标题：概览、关键要点、时间线章节、适合保存的笔记。"
                    "时间线章节中的每条要尽量保留字幕时间戳。不要编造字幕中没有的信息。"
                ),
            },
            {
                "role": "user",
                "content": (
                    f"视频标题：{title}\n"
                    f"视频链接：{webpage_url}\n\n"
                    f"字幕：\n{transcript}"
                ),
            },
        ],
        temperature=0.2,
    )
    content = completion.choices[0].message.content if completion.choices else None
    summary = str(content or "").strip()
    if not summary:
        raise RuntimeError("AI 接口返回了空摘要。")
    return summary


def _sanitize_mind_map_payload(
    payload: Any,
    segments: list[SubtitleSegment],
    default_title: str,
    allowed_segment_ids: set[int] | None = None,
) -> MindMapResult:
    if not isinstance(payload, dict):
        raise RuntimeError("AI 思维导图不是合法 JSON 对象。")
    counter = 0

    def clean_text(value: Any, maximum: int) -> str:
        return re.sub(r"\s+", " ", str(value or "")).strip()[:maximum]

    def clean_nodes(value: Any, depth: int) -> list[MindMapNode]:
        nonlocal counter
        if not isinstance(value, list) or depth > MAX_MIND_MAP_DEPTH:
            return []
        cleaned: list[MindMapNode] = []
        for candidate in value:
            if len(cleaned) >= MAX_MIND_MAP_CHILDREN:
                break
            if not isinstance(candidate, dict):
                continue
            title = clean_text(candidate.get("title"), 80)
            raw_ids = candidate.get("segment_ids")
            if not title or not isinstance(raw_ids, list):
                continue
            segment_ids = sorted(
                {
                    item
                    for item in raw_ids
                    if isinstance(item, int)
                    and not isinstance(item, bool)
                    and 0 <= item < len(segments)
                    and (allowed_segment_ids is None or item in allowed_segment_ids)
                }
            )
            if not segment_ids:
                continue
            counter += 1
            cleaned.append(
                MindMapNode(
                    id=f"node-{counter}",
                    title=title,
                    summary=clean_text(candidate.get("summary"), 300),
                    timestamp=min(segments[index].start for index in segment_ids),
                    segment_ids=segment_ids,
                    children=clean_nodes(candidate.get("children"), depth + 1),
                )
            )
        return cleaned

    nodes = clean_nodes(payload.get("nodes"), 1)
    if not nodes:
        raise RuntimeError("AI 思维导图没有有效节点。")
    title = clean_text(payload.get("title"), 80) or clean_text(default_title, 80) or "视频思维导图"
    generated_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    return MindMapResult(title=title, nodes=nodes, generated_at=generated_at)


def _mind_map_prompt_chunks(segments: list[SubtitleSegment]) -> list[tuple[str, set[int]]]:
    chunks: list[tuple[str, set[int]]] = []
    lines: list[str] = []
    segment_ids: set[int] = set()
    current_length = 0
    for index, segment in enumerate(segments):
        prefix = f"[{index}] [{segment.timestamp}] "
        text_limit = max(1, MAX_SUMMARY_TRANSCRIPT_CHARS - len(prefix) - 1)
        line = prefix + segment.text[:text_limit]
        if current_length + len(line) + 1 > MAX_SUMMARY_TRANSCRIPT_CHARS:
            if lines:
                chunks.append(("\n".join(lines), segment_ids))
            lines = []
            segment_ids = set()
            current_length = 0
        lines.append(line)
        segment_ids.add(index)
        current_length += len(line) + 1
    if lines:
        chunks.append(("\n".join(lines), segment_ids))
    return chunks


def _generate_ai_mind_map(title: str, segments: list[SubtitleSegment]) -> MindMapResult:
    config = _load_ai_config()
    client_kwargs: dict[str, str] = {"api_key": config["api_key"]}
    if config.get("base_url"):
        client_kwargs["base_url"] = config["base_url"]
    client = OpenAI(**client_kwargs)
    chunk_results: list[MindMapResult] = []
    for indexed_transcript, allowed_ids in _mind_map_prompt_chunks(segments):
        completion = client.chat.completions.create(
            model=config["model"],
            messages=[
                {
                    "role": "system",
                    "content": (
                        "你是视频学习内容整理助手。只依据提供的字幕生成思维导图 JSON，禁止补充字幕外事实。"
                        "顶层格式为 {title, nodes}。每个节点只包含 title、summary、segment_ids、children。"
                        "segment_ids 必须使用字幕行开头的整数索引。最多四层，每个节点最多十个子节点。"
                    ),
                },
                {"role": "user", "content": f"视频标题：{title}\n\n字幕：\n{indexed_transcript}"},
            ],
            temperature=0.2,
            response_format={"type": "json_object"},
        )
        content = completion.choices[0].message.content if completion.choices else None
        if not str(content or "").strip():
            raise RuntimeError("AI 接口返回了空思维导图。")
        try:
            payload = json.loads(str(content))
        except json.JSONDecodeError as exc:
            raise RuntimeError("AI 思维导图不是合法 JSON。") from exc
        chunk_results.append(_sanitize_mind_map_payload(payload, segments, title, allowed_ids))
    if not chunk_results:
        raise RuntimeError("没有可用于思维导图的字幕。")

    queues = [list(result.nodes) for result in chunk_results]
    selected: list[MindMapNode] = []
    while len(selected) < MAX_MIND_MAP_CHILDREN and any(queues):
        for queue in queues:
            if queue and len(selected) < MAX_MIND_MAP_CHILDREN:
                selected.append(queue.pop(0))

    counter = 0

    def reindex(node: MindMapNode) -> MindMapNode:
        nonlocal counter
        counter += 1
        node_id = f"node-{counter}"
        children = [reindex(child) for child in node.children]
        return node.model_copy(update={"id": node_id, "children": children})

    return MindMapResult(
        title=chunk_results[0].title,
        nodes=[reindex(node) for node in selected],
        generated_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    )


def _set_mind_map_task(task: MindMapTask, **changes: Any) -> None:
    with mind_map_tasks_lock:
        for key, value in changes.items():
            setattr(task, key, value)
        task.updated_at = time.time()


def _run_mind_map(task: MindMapTask, summary: SummaryTask) -> None:
    try:
        if not summary.result:
            raise RuntimeError("总结结果不可用。")
        _set_mind_map_task(task, status="running", progress=20.0, message="正在整理字幕结构")
        result = _generate_ai_mind_map(summary.result.title, summary.transcript or summary.result.transcript)
        _set_mind_map_task(
            task,
            status="finished",
            progress=100.0,
            message="思维导图已生成",
            result=result,
        )
    except Exception as exc:
        _set_mind_map_task(
            task,
            status="failed",
            progress=max(task.progress, 1.0),
            message="思维导图生成失败",
            error=_clean_error(exc, summary.url),
        )


def _save_summary_files(task: SummaryTask, result: SummaryResult) -> SummaryResult:
    stem = f"{task.task_id}.{_safe_filename_stem(result.title)}"
    markdown_name = f"{stem}.summary.md"
    json_name = f"{stem}.summary.json"
    markdown_path = SUMMARY_DIR / markdown_name
    json_path = SUMMARY_DIR / json_name

    transcript_markdown = "\n".join(f"- [{item.timestamp}] {item.text}" for item in result.transcript)
    markdown_content = (
        f"{result.summary_markdown.strip()}\n\n"
        "---\n\n"
        "## 字幕/转写文本\n\n"
        f"{transcript_markdown}\n"
    )
    markdown_path.write_text(markdown_content, encoding="utf-8")
    json_path.write_text(
        json.dumps(result.model_dump(exclude={"markdown_url", "json_url"}), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    result.markdown_url = f"/api/summary-files/{quote(markdown_name, safe='')}"
    result.json_url = f"/api/summary-files/{quote(json_name, safe='')}"
    return result


def _run_summary(task: SummaryTask) -> None:
    try:
        _set_summary_task(task, status="running", progress=8.0, message="正在解析视频字幕")
        info, language, source, segments = _extract_summary_source(task.url)
        title = str(info.get("title") or "未命名视频")
        webpage_url = str(info.get("webpage_url") or task.url)
        _set_summary_task(
            task,
            progress=42.0,
            message=f"已提取 {language} 字幕，正在生成 AI 摘要",
            transcript=segments,
        )
        summary_markdown = _generate_ai_summary(title, webpage_url, segments)
        _set_summary_task(task, progress=82.0, message="正在保存总结文件")
        result = SummaryResult(
            title=title,
            webpage_url=webpage_url,
            language=language,
            source=_summary_source_label(source),
            summary_markdown=summary_markdown,
            transcript=segments[:MAX_TRANSCRIPT_SEGMENTS_IN_RESPONSE],
        )
        result = _save_summary_files(task, result)
        _set_summary_task(
            task,
            status="finished",
            progress=100.0,
            message="AI 总结已生成",
            result=result,
        )
    except Exception as exc:
        _set_summary_task(
            task,
            status="failed",
            progress=max(task.progress, 1.0),
            message="总结失败",
            error=_clean_error(exc, task.url),
        )


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


def _thumbnail_url(url: str | None) -> str | None:
    if not isinstance(url, str) or not url:
        return None
    if url.startswith("/api/"):
        return url
    return _proxy_thumbnail(url)


def _thumbnail_referer(url: str) -> str:
    host = _host(url)
    if _is_pornhub_url(url):
        return "https://www.pornhub.com/"
    if _is_youtube_url(url) or "ytimg.com" in host or "googleusercontent.com" in host:
        return "https://www.youtube.com/"
    if _is_douyin_url(url) or any(token in host for token in ("byteimg.com", "douyinpic.com", "snssdk.com", "aweme")):
        return "https://www.douyin.com/"
    if _is_bilibili_url(url) or "hdslb.com" in host:
        return "https://www.bilibili.com/"
    return f"{urlparse(url).scheme}://{host}/"


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
    resolved_url = _resolve_douyin_url(url)
    video_id = _extract_douyin_video_id(resolved_url)
    if not video_id:
        raise HTTPException(status_code=422, detail="无法从抖音链接中提取视频 ID，请粘贴单个公开视频链接。")

    session = requests.Session()
    session.headers.update(DOUYIN_HEADERS)
    try:
        response = session.get(DOUYIN_API_URL, params={"item_ids": video_id}, timeout=(3, 5))
        if response.ok and response.content:
            data = response.json()
            item_list = data.get("item_list") or []
            if item_list and isinstance(item_list[0], dict):
                return video_id, item_list[0]
    except (requests.RequestException, ValueError):
        pass

    share_url = f"https://www.iesdouyin.com/share/video/{video_id}/"
    try:
        response = session.get(share_url, timeout=(3, 5))
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


def parse_douyin_public(url: str) -> ParseResponse:
    video_id, item = _fetch_douyin_item(url)
    video = item.get("video") if isinstance(item.get("video"), dict) else {}
    cover = (
        _pick_url_from_nested(video, "cover")
        or _pick_url_from_nested(video, "origin_cover")
        or _pick_url_from_nested(video, "dynamic_cover")
    )
    title = str(item.get("desc") or f"抖音视频 {video_id}").strip()
    duration = _safe_int(video.get("duration"))
    if duration and duration > 1000:
        duration = duration // 1000

    return ParseResponse(
        title=title,
        thumbnail=_proxy_thumbnail(cover),
        duration=duration,
        uploader=(item.get("author") or {}).get("nickname") if isinstance(item.get("author"), dict) else None,
        webpage_url=f"https://www.douyin.com/video/{video_id}",
        formats=[
            FormatInfo(
                format_id="douyin-public",
                label="公开视频 MP4",
                ext="mp4",
                resolution="自动",
            )
        ],
    )


def _looks_like_media_url(url: str) -> bool:
    path = urlparse(url).path.lower()
    return path.endswith(MEDIA_EXTENSIONS)


def _media_ext(url: str) -> str:
    path = urlparse(url).path.lower()
    for ext in MEDIA_EXTENSIONS:
        if path.endswith(ext):
            return ext.removeprefix(".")
    return "mp4"


def _fetch_html(url: str) -> str:
    try:
        response = requests.get(url, headers=DEFAULT_HEADERS, timeout=(10, 25))
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

    candidates = [match.group(1) for match in MEDIA_SRC_PATTERN.finditer(html_text)]
    candidates.extend(match.group(0) for match in MEDIA_URL_PATTERN.finditer(html_text))

    for raw in candidates:
        cleaned = html.unescape(raw).replace("\\/", "/").strip()
        absolute = urljoin(page_url, cleaned)
        if not _looks_like_media_url(absolute) or absolute in seen:
            continue
        seen.add(absolute)
        found.append(absolute)
    return found[:12]


def parse_sniffed_page(url: str) -> ParseResponse:
    if _looks_like_media_url(url):
        ext = _media_ext(url)
        return ParseResponse(
            title=Path(urlparse(url).path).name or "嗅探视频",
            thumbnail=None,
            duration=None,
            uploader="网页直链",
            webpage_url=url,
            formats=[
                FormatInfo(
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
            FormatInfo(
                format_id=f"sniff:{quote(media_url, safe='')}",
                label=f"{label} · 资源 {index}",
                ext="mp4" if ext == "m3u8" else ext,
                resolution="自动",
            )
        )

    return ParseResponse(
        title=title,
        thumbnail=None,
        duration=None,
        uploader=urlparse(url).netloc,
        webpage_url=url,
        formats=formats,
    )


def _browser_executable() -> Path | None:
    playwright_root = Path.home() / "AppData" / "Local" / "ms-playwright"
    playwright_candidates = []
    if playwright_root.exists():
        playwright_candidates = sorted(
            playwright_root.glob("chromium-*/chrome-win*/chrome.exe"),
            reverse=True,
        )

    for candidate in (CHROME_EXE, EDGE_EXE, *playwright_candidates):
        if candidate.exists():
            return candidate
    return None


def _is_probable_video_resource(url: str) -> bool:
    lower = html.unescape(url).lower()
    if any(token in lower for token in (".css", ".scss", ".less")):
        return False
    if "pornhub" in lower or "phncdn.com" in lower:
        return _is_signed_pornhub_media_url(lower)
    if "phncdn.com" in lower and ".mp4" in lower and not _is_signed_pornhub_media_url(lower):
        return False
    blocked_tokens = (
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".webp",
        ".apk",
        ".json",
        ".js",
        "douyin_pc_client",
        "download/douyin",
        "client.mp4",
        "appdownload",
        "install",
        "douyinstatic.com",
        "douyin-pc-web",
        "lf-douyin-pc-web",
    )
    if any(token in lower for token in blocked_tokens):
        return False
    return bool(VIDEO_CDN_PATTERN.search(lower))


def _is_signed_pornhub_media_url(url: str) -> bool:
    lower = html.unescape(url).lower()
    if "phncdn.com" not in lower:
        return False
    parsed = urlparse(lower)
    host = parsed.netloc
    path = parsed.path
    if host.startswith("pix-") or host.startswith("pix."):
        return False
    if any(ext in path for ext in (".jpg", ".jpeg", ".png", ".webp", ".gif")):
        return False
    if ".m3u8" in lower:
        return True
    if not path.endswith(".mp4"):
        return False
    filename = Path(path).name
    if not re.search(r"\d+p[_-]\d+k", filename):
        return False
    return any(token in lower for token in ("token=", "ttl=", "validfrom=", "validto=", "hash=", "ipa=", "burst="))


def _score_video_resource(url: str) -> int:
    lower = html.unescape(url).lower()
    score = 0
    if _is_signed_pornhub_media_url(lower):
        score += 130
    if ".m3u8" in lower and ("phncdn.com" in lower or "pornhub" in lower):
        score += 125
    if "googlevideo.com/videoplayback" in lower:
        score += 120
    if "douyinvod.com" in lower:
        score += 100
    if "mime_type=video" in lower:
        score += 80
    if "/aweme/v1/play/" in lower:
        score += 60
    if ".mp4" in lower:
        score += 40
    if ".m3u8" in lower:
        score += 30
    return score


def _is_high_confidence_video(url: str) -> bool:
    lower = html.unescape(url).lower()
    return (
        _is_signed_pornhub_media_url(lower)
        or "douyinvod.com" in lower
        or "googlevideo.com/videoplayback" in lower
        or ("mime_type=video" in lower and ("video_mp4" in lower or "video/" in lower))
        or "/aweme/v1/play/" in lower
    )


def _youtube_mime(url: str) -> str:
    query = parse_qs(urlparse(html.unescape(url)).query)
    mime = query.get("mime", [""])[0]
    return unquote(mime).lower()


def _is_youtube_video_stream(url: str) -> bool:
    mime = _youtube_mime(url)
    return "video/" in mime


def _is_youtube_audio_stream(url: str) -> bool:
    mime = _youtube_mime(url)
    return "audio/" in mime


def _is_youtube_progressive_stream(url: str) -> bool:
    parsed = urlparse(html.unescape(url))
    query = parse_qs(parsed.query)
    itag = query.get("itag", [""])[0]
    return itag in {"18", "22"}


def _get_sniff_cache(url: str) -> ParseResponse | None:
    with sniff_cache_lock:
        cached = sniff_cache.get(url)
        if not cached:
            return None
        created_at, response = cached
        if time.time() - created_at > SNIFF_CACHE_TTL_SECONDS:
            sniff_cache.pop(url, None)
            return None
        return response


def _set_sniff_cache(url: str, response: ParseResponse) -> None:
    with sniff_cache_lock:
        sniff_cache[url] = (time.time(), response)


def _resolve_douyin_preview_source(url: str, format_id: str) -> tuple[str, str]:
    normalized_url = normalize_url(url)
    if not _is_douyin_url(normalized_url):
        raise HTTPException(status_code=422, detail="首版仅支持抖音视频在线播放。")

    if format_id == "douyin-public":
        _, item = _fetch_douyin_item(normalized_url)
        return _douyin_media_url(item), normalized_url

    if not format_id.startswith("sniff:"):
        raise HTTPException(status_code=422, detail="该格式暂不支持在线播放。")

    cached = _get_sniff_cache(normalized_url)
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


@app.post("/api/previews", response_model=PreviewResponse)
def create_preview(payload: PreviewRequest) -> PreviewResponse:
    media_url, referer = _resolve_douyin_preview_source(str(payload.url), payload.format_id)
    now = time.time()
    token = secrets.token_urlsafe(24)
    with preview_sessions_lock:
        _cleanup_preview_sessions(now)
        preview_sessions[token] = PreviewSession(
            media_url=media_url,
            referer=referer,
            expires_at=now + PREVIEW_TTL_SECONDS,
        )
    return PreviewResponse(
        preview_url=f"/api/previews/{token}/content",
        expires_in=PREVIEW_TTL_SECONDS,
    )


def _get_preview_session(token: str) -> PreviewSession:
    now = time.time()
    with preview_sessions_lock:
        session = preview_sessions.get(token)
        if not session:
            raise HTTPException(status_code=404, detail="预览会话不存在，请重新解析后播放。")
        if session.expires_at <= now:
            preview_sessions.pop(token, None)
            raise HTTPException(status_code=410, detail="预览地址已过期，请重新解析后播放。")
        return session


@app.get("/api/previews/{token}/content")
def preview_content(
    token: str,
    range_header: Annotated[str | None, Header(alias="Range")] = None,
) -> StreamingResponse:
    session = _get_preview_session(token)
    if range_header and not PREVIEW_RANGE_PATTERN.fullmatch(range_header.strip()):
        raise HTTPException(status_code=416, detail="仅支持单段 bytes Range 请求。")

    headers = {
        **DOUYIN_HEADERS,
        "Referer": session.referer,
    }
    if range_header:
        headers["Range"] = range_header.strip()

    try:
        upstream = requests.get(
            session.media_url,
            headers=headers,
            stream=True,
            allow_redirects=True,
            timeout=(10, 60),
        )
    except requests.RequestException as exc:
        raise HTTPException(status_code=502, detail="连接抖音视频源失败，请重新解析后重试。") from exc

    content_type = upstream.headers.get("Content-Type", "").split(";", 1)[0].lower()
    if upstream.status_code not in {200, 206} or not (
        content_type.startswith("video/") or content_type == "application/octet-stream"
    ):
        upstream.close()
        raise HTTPException(status_code=502, detail="抖音视频源返回了无效内容，请重新解析后重试。")

    allowed_headers = {"content-type", "content-length", "content-range", "accept-ranges"}
    response_headers = {
        name: value for name, value in upstream.headers.items() if name.lower() in allowed_headers
    }

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


def _clean_meta_text(value: str | None) -> str | None:
    if not value:
        return None
    cleaned = html.unescape(value)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    cleaned = re.sub(r"\s*[-|]\s*YouTube\s*$", "", cleaned, flags=re.IGNORECASE).strip()
    cleaned = re.sub(r"[-_ ]*抖音(?:网页版)?[-_ ]*.*$", "", cleaned).strip()
    cleaned = re.sub(r"\s*-\s*抖音\s*$", "", cleaned).strip()
    cleaned = re.sub(r"开启读屏标签.*$", "", cleaned).strip()
    return cleaned or None


def _clean_author_text(value: str | None) -> str | None:
    cleaned = _clean_meta_text(value)
    if not cleaned:
        return None
    cleaned = re.split(r"(?:粉丝|获赞|关注|作品|喜欢|认证|私信)", cleaned, maxsplit=1)[0].strip()
    cleaned = re.sub(r"^@+", "", cleaned).strip()
    return cleaned[:48].strip() or None


def _parse_iso_duration(value: Any) -> int | None:
    if isinstance(value, (int, float)):
        return int(value) if value > 0 else None
    if not isinstance(value, str) or not value.strip():
        return None

    text = value.strip()
    if text.isdigit():
        return int(text)

    match = re.fullmatch(
        r"P(?:(?P<days>\d+)D)?(?:T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+)S)?)?",
        text,
        re.IGNORECASE,
    )
    if not match:
        return None
    days = int(match.group("days") or 0)
    hours = int(match.group("hours") or 0)
    minutes = int(match.group("minutes") or 0)
    seconds = int(match.group("seconds") or 0)
    total = days * 86400 + hours * 3600 + minutes * 60 + seconds
    return total or None


def _extract_douyin_author_from_text(text: str | None) -> str | None:
    if not text:
        return None
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    for index, line in enumerate(lines):
        if line.startswith("发布时间") and index + 1 < len(lines):
            candidate = lines[index + 1]
            if candidate not in {"全部评论", "请先登录后发表评论", "大家都在搜："}:
                return _clean_author_text(candidate)
    for index, line in enumerate(lines):
        if "粉丝" in line and index > 0:
            return _clean_author_text(lines[index - 1])
    return None


def _capture_page_thumbnail(page: Any, normalized_url: str) -> str | None:
    try:
        filename = f"{hashlib.sha1(normalized_url.encode('utf-8')).hexdigest()[:16]}.png"
        target_path = THUMBNAIL_DIR / filename
        locator = page.locator("video").first if page.locator("video").count() else page.locator("body").first
        locator.screenshot(path=str(target_path), timeout=2000)
        if target_path.exists() and target_path.stat().st_size > 0:
            return _local_thumbnail(filename)
    except Exception:
        try:
            filename = f"{hashlib.sha1(normalized_url.encode('utf-8')).hexdigest()[:16]}.png"
            target_path = THUMBNAIL_DIR / filename
            page.screenshot(path=str(target_path), timeout=2000, full_page=False)
            if target_path.exists() and target_path.stat().st_size > 0:
                return _local_thumbnail(filename)
        except Exception:
            return None
    return None


def _extract_browser_page_meta(page: Any, normalized_url: str) -> dict[str, Any]:
    try:
        data = page.evaluate(
            """() => {
                const meta = (name) => {
                    const selectors = [
                        `meta[property="${name}"]`,
                        `meta[name="${name}"]`
                    ];
                    for (const selector of selectors) {
                        const node = document.querySelector(selector);
                        const content = node && node.getAttribute('content');
                        if (content) return content;
                    }
                    return '';
                };
                const firstValue = (value) => {
                    if (!value) return '';
                    if (Array.isArray(value)) return firstValue(value[0]);
                    if (typeof value === 'object') {
                        return value.url || value.contentUrl || value['@id'] || '';
                    }
                    return String(value);
                };
                const jsonLdItems = [];
                for (const node of document.querySelectorAll('script[type="application/ld+json"]')) {
                    try {
                        const parsed = JSON.parse(node.textContent || '');
                        const items = Array.isArray(parsed) ? parsed : [parsed];
                        for (const item of items) {
                            if (item && Array.isArray(item['@graph'])) jsonLdItems.push(...item['@graph']);
                            else if (item) jsonLdItems.push(item);
                        }
                    } catch {}
                }
                const ld =
                    jsonLdItems.find((item) => /VideoObject|Clip|Movie/.test(String(item?.['@type'] || ''))) ||
                    jsonLdItems.find((item) => item?.name || item?.thumbnailUrl || item?.author) ||
                    {};
                const poster = document.querySelector('video[poster]')?.getAttribute('poster') || '';
                const images = Array.from(document.images)
                    .map((img) => ({
                        src: img.currentSrc || img.src || img.getAttribute('data-src') || '',
                        alt: img.alt || '',
                        className: img.className || '',
                    }))
                    .filter((item) => item.src && !/avatar|emoji|qrcode|qr|logo|icon|sprite|loading/i.test(`${item.src} ${item.alt} ${item.className}`))
                    .filter((item) => /douyin|aweme|byteimg|ytimg|googleusercontent|douyinpic|snssdk/i.test(item.src));
                const ldAuthor = firstValue(ld.author?.name || ld.author);
                const author =
                    ldAuthor ||
                    meta('author') ||
                    document.querySelector('[data-e2e="user-name"]')?.textContent ||
                    document.querySelector('[data-e2e="video-author"]')?.textContent ||
                    document.querySelector('#owner-name a')?.textContent ||
                    document.querySelector('ytd-channel-name a')?.textContent ||
                    document.querySelector('[data-e2e="user-info"]')?.textContent ||
                    document.querySelector('[class*="author"]')?.textContent ||
                    '';
                return {
                    title: firstValue(ld.name) || meta('og:title') || meta('twitter:title') || document.querySelector('h1')?.textContent || document.title || '',
                    thumbnail: firstValue(ld.thumbnailUrl) || firstValue(ld.image) || meta('og:image') || meta('twitter:image') || poster || images[0]?.src || '',
                    author,
                    duration: firstValue(ld.duration) || meta('video:duration') || meta('duration') || '',
                    bodyText: document.body?.innerText?.slice(0, 3000) || ''
                };
            }"""
        )
    except Exception:
        data = {}

    title = _clean_meta_text(data.get("title") if isinstance(data, dict) else None)
    thumbnail = data.get("thumbnail") if isinstance(data, dict) else None
    author = _clean_author_text(data.get("author") if isinstance(data, dict) else None)
    if not author and _is_douyin_url(normalized_url):
        author = _extract_douyin_author_from_text(data.get("bodyText") if isinstance(data, dict) else None)
    duration = _parse_iso_duration(data.get("duration") if isinstance(data, dict) else None)
    if isinstance(thumbnail, str) and thumbnail:
        thumbnail = urljoin(normalized_url, html.unescape(thumbnail))
    else:
        thumbnail = None

    if not thumbnail and _is_youtube_url(normalized_url):
        video_id = _extract_youtube_video_id(normalized_url)
        if video_id:
            thumbnail = f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"

    return {"title": title, "thumbnail": thumbnail, "author": author, "duration": duration}


def _extract_pornhub_player_data(page: Any) -> dict[str, Any]:
    try:
        data = page.evaluate(
            """async () => {
                const flashKey = Object.keys(window).find((key) => /^flashvars_/.test(key));
                const flashvars = flashKey ? window[flashKey] : null;
                if (!flashvars) return {};
                let mp4Definitions = [];
                const remote = (flashvars.mediaDefinitions || []).find((item) =>
                    item && item.format === 'mp4' && String(item.videoUrl || '').includes('/video/get_media')
                );
                if (remote?.videoUrl) {
                    try {
                        const response = await fetch(remote.videoUrl, {
                            credentials: 'include',
                            headers: {
                                Accept: 'application/json,text/plain,*/*',
                                'X-Requested-With': 'XMLHttpRequest'
                            }
                        });
                        const text = await response.text();
                        const parsed = JSON.parse(text);
                        if (Array.isArray(parsed)) mp4Definitions = parsed;
                    } catch {}
                }
                return {
                    title: flashvars.video_title || document.querySelector('h1')?.textContent || document.title || '',
                    thumbnail: flashvars.image_url || '',
                    duration: flashvars.video_duration || '',
                    mediaDefinitions: [...mp4Definitions, ...(flashvars.mediaDefinitions || [])]
                };
            }"""
        )
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _pornhub_player_response(url: str, page_meta: dict[str, Any], player_data: dict[str, Any]) -> ParseResponse | None:
    definitions = player_data.get("mediaDefinitions")
    if not isinstance(definitions, list):
        return None

    formats: list[FormatInfo] = []
    seen: set[str] = set()
    def sort_key(item: dict[str, Any]) -> tuple[int, int]:
        height = _safe_int(item.get("height")) or _safe_int(item.get("quality")) or 0
        media_url = str(item.get("videoUrl") or "").lower()
        is_direct_mp4 = item.get("format") == "mp4" or "/video/get_media" in media_url
        preferred = {720: 0, 480: 1, 1080: 2, 240: 3}.get(height, 4)
        if is_direct_mp4:
            preferred -= 10
        return (preferred, -height)

    sorted_defs = sorted((item for item in definitions if isinstance(item, dict)), key=sort_key)
    for item in sorted_defs:
        media_url = item.get("videoUrl")
        if not isinstance(media_url, str) or not media_url or media_url in seen:
            continue
        if "/video/get_media" in media_url:
            continue
        if not _is_signed_pornhub_media_url(media_url):
            continue
        seen.add(media_url)
        ext = "mp4" if item.get("format") == "mp4" else _media_ext(media_url)
        quality = item.get("quality") or item.get("height") or "自动"
        label = f"{quality}P · HLS" if ext == "m3u8" else f"{quality}P · MP4"
        formats.append(
            FormatInfo(
                format_id=f"sniff:{quote(media_url, safe='')}",
                label=label,
                ext="mp4" if ext == "m3u8" else ext,
                resolution=f"{quality}p" if str(quality).isdigit() else "自动",
            )
        )

    if not formats:
        return None

    return ParseResponse(
        title=_clean_meta_text(player_data.get("title")) or page_meta.get("title") or "PornHub 视频",
        thumbnail=_thumbnail_url(player_data.get("thumbnail") or page_meta.get("thumbnail")),
        duration=_safe_int(player_data.get("duration")) or page_meta.get("duration"),
        uploader=page_meta.get("author") or "PornHub",
        webpage_url=url,
        formats=formats[:8],
    )


def parse_browser_sniffed_page(url: str) -> ParseResponse:
    executable = _browser_executable()

    captured: list[str] = []
    page_meta: dict[str, Any] = {}
    pornhub_player_data: dict[str, Any] = {}
    page_html = ""
    normalized_url = normalize_url(url)
    cached = _get_sniff_cache(normalized_url)
    if cached:
        return cached
    is_youtube_page = _is_youtube_url(normalized_url)
    is_douyin_page = _is_douyin_url(normalized_url)
    is_pornhub_page = _is_pornhub_url(normalized_url)

    with sync_playwright() as playwright:
        launch_options = {
            "headless": True,
            "args": [
                "--disable-blink-features=AutomationControlled",
                "--autoplay-policy=no-user-gesture-required",
                "--disable-extensions",
                "--disable-notifications",
                "--mute-audio",
            ],
        }
        if executable:
            launch_options["executable_path"] = str(executable)
        browser = playwright.chromium.launch(**launch_options)
        page = browser.new_page(
            user_agent=DEFAULT_HEADERS["User-Agent"],
            locale="zh-CN",
            viewport={"width": 1365, "height": 768},
        )

        def remember(candidate: str) -> None:
            cleaned = html.unescape(candidate).replace("&amp;", "&")
            if _is_probable_video_resource(cleaned) and cleaned not in captured:
                captured.append(cleaned)

        def route_request(route: Any) -> None:
            request = route.request
            remember(request.url)
            if request.resource_type in {"image", "font", "stylesheet"} or (
                request.resource_type == "media" and not (is_youtube_page or is_douyin_page or is_pornhub_page)
            ):
                route.abort()
                return
            route.continue_()

        page.route("**/*", route_request)
        page.on("request", lambda request: remember(request.url))
        page.on("response", lambda response: remember(response.url))
        try:
            page.goto(normalized_url, wait_until="commit", timeout=8000 if is_douyin_page else 12000)
            deadline = time.time() + (8 if is_douyin_page else 12 if (is_youtube_page or is_pornhub_page) else 4)
            while time.time() < deadline:
                if is_youtube_page:
                    if any(_is_youtube_progressive_stream(item) for item in captured) or (
                        any(_is_youtube_video_stream(item) for item in captured)
                        and any(_is_youtube_audio_stream(item) for item in captured)
                    ):
                        break
                elif is_douyin_page:
                    if any("douyinvod.com" in item.lower() or "mime_type=video" in item.lower() for item in captured):
                        break
                elif is_pornhub_page:
                    if any(_is_signed_pornhub_media_url(item) for item in captured):
                        break
                elif captured and (
                    any(_is_high_confidence_video(item) for item in captured)
                    or time.time() > deadline - 2
                ):
                    break
                page.wait_for_timeout(500)
            page_meta = _extract_browser_page_meta(page, normalized_url)
            if _is_douyin_url(normalized_url):
                local_thumbnail = _capture_page_thumbnail(page, normalized_url)
                if local_thumbnail:
                    page_meta["thumbnail"] = local_thumbnail
            if is_pornhub_page:
                pornhub_player_data = _extract_pornhub_player_data(page)
            page_html = page.content()
        finally:
            browser.close()

    if is_pornhub_page:
        player_response = _pornhub_player_response(normalized_url, page_meta, pornhub_player_data)
        if player_response:
            _set_sniff_cache(normalized_url, player_response)
            return player_response

    for match in re.finditer(r"https?://[^\"'<>\\\s]+", page_html):
        remember(match.group(0))
    for match in re.finditer(r'(?:videoUrl|qualityUrl|defaultQuality)[^"\']*["\'](https?://[^"\']+)["\']', page_html):
        remember(match.group(1).encode("utf-8").decode("unicode_escape"))

    ranked = sorted(captured, key=_score_video_resource, reverse=True)
    if not ranked:
        raise HTTPException(status_code=422, detail="浏览器嗅探没有发现可下载的视频资源。")

    formats = []
    if _is_youtube_url(normalized_url):
        video_stream = next((item for item in ranked if _is_youtube_video_stream(item)), None)
        audio_stream = next((item for item in ranked if _is_youtube_audio_stream(item)), None)
        if video_stream and audio_stream:
            formats.append(
                FormatInfo(
                    format_id=f"sniffpair:{quote(video_stream, safe='')}|{quote(audio_stream, safe='')}",
                    label="MP4 · 自动合并音频",
                    ext="mp4",
                    resolution="自动",
                )
            )

    for index, media_url in enumerate(ranked[:8], start=1):
        ext = _media_ext(media_url)
        if _is_youtube_url(normalized_url):
            if _is_youtube_progressive_stream(media_url):
                label = "MP4 · 含音频"
            else:
                continue
        elif _is_douyin_url(normalized_url):
            label = "HLS · 高清线路" if ext == "m3u8" else "MP4 · 高清线路"
        elif _is_pornhub_url(normalized_url):
            label = "HLS · 播放器线路" if ext == "m3u8" else "MP4 · 播放器线路"
        elif "mime_type=video" in media_url.lower() and ext == "mp4":
            label = "MP4 · 视频"
        elif ext == "m3u8":
            label = "HLS · 视频"
        else:
            label = f"{ext.upper()} · 视频"
        formats.append(
            FormatInfo(
                format_id=f"sniff:{quote(media_url, safe='')}",
                label=f"{label} · 线路 {index}",
                ext="mp4" if ext == "m3u8" else ext,
                resolution="自动",
            )
        )

    if _is_youtube_url(normalized_url) and not formats:
        raise HTTPException(status_code=422, detail="浏览器嗅探没有捕获可直接下载的 YouTube 音视频组合。")

    default_title = "YouTube 视频" if _is_youtube_url(normalized_url) else "抖音视频" if _is_douyin_url(normalized_url) else "网页视频"
    default_uploader = "YouTube" if _is_youtube_url(normalized_url) else "抖音" if _is_douyin_url(normalized_url) else urlparse(url).netloc
    response = ParseResponse(
        title=page_meta.get("title") or default_title,
        thumbnail=_thumbnail_url(page_meta.get("thumbnail")),
        duration=page_meta.get("duration"),
        uploader=page_meta.get("author") or default_uploader,
        webpage_url=normalized_url,
        formats=formats,
    )
    _set_sniff_cache(normalized_url, response)
    return response


def _format_label(item: dict[str, Any]) -> str:
    resolution = item.get("resolution")
    height = item.get("height")
    ext = item.get("ext") or "media"
    note = item.get("format_note")
    filesize = item.get("filesize") or item.get("filesize_approx")
    parts = []
    if resolution and resolution != "audio only":
        parts.append(str(resolution))
    elif height:
        parts.append(f"{height}p")
    elif item.get("vcodec") == "none":
        parts.append("音频")
    if note:
        parts.append(str(note))
    parts.append(str(ext).upper())
    if filesize:
        parts.append(f"{filesize / 1024 / 1024:.1f} MB")
    return " · ".join(parts)


def _extract_formats(info: dict[str, Any]) -> list[FormatInfo]:
    candidates: list[FormatInfo] = []
    seen: set[str] = set()
    for item in info.get("formats") or []:
        raw_format_id = str(item.get("format_id") or "")
        if not raw_format_id or raw_format_id in seen:
            continue
        if item.get("vcodec") == "none" and item.get("acodec") == "none":
            continue
        seen.add(raw_format_id)

        has_video = item.get("vcodec") not in (None, "none")
        has_audio = item.get("acodec") not in (None, "none")
        format_id = raw_format_id
        label = _format_label(item)
        if has_video and not has_audio:
            format_id = f"{raw_format_id}+bestaudio/best"
            label = f"{label} · 含最佳音频"

        candidates.append(
            FormatInfo(
                format_id=format_id,
                label=label,
                ext=item.get("ext"),
                resolution=item.get("resolution")
                or (f"{item.get('height')}p" if item.get("height") else None),
                filesize=_safe_int(item.get("filesize") or item.get("filesize_approx")),
            )
        )

    preferred = sorted(
        candidates,
        key=lambda fmt: (
            0 if fmt.resolution and "audio" not in fmt.label.lower() else 1,
            -(fmt.filesize or 0),
        ),
    )
    return preferred[:12]


def _ydl_base_options(url: str | None = None) -> dict[str, Any]:
    url = normalize_url(url) if url else url
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
    if (FFMPEG_DIR / "ffmpeg.exe").exists():
        options["ffmpeg_location"] = str(FFMPEG_DIR)
    if NODE_EXE.exists():
        options["js_runtimes"] = {"node": {"path": str(NODE_EXE)}}
    if url and _is_bilibili_url(url):
        options["http_headers"].update(
            {
                "Origin": "https://www.bilibili.com",
                "Referer": "https://www.bilibili.com/",
            }
        )
    if url and _is_pornhub_url(url):
        options["http_headers"].update(
            {
                "Origin": "https://www.pornhub.com",
                "Referer": "https://www.pornhub.com/",
                "Accept-Language": "en-US,en;q=0.9",
            }
        )
    if url and _is_youtube_url(url):
        options["http_headers"].update(
            {
                "Origin": "https://www.youtube.com",
                "Referer": "https://www.youtube.com/",
                "Accept-Language": "en-US,en;q=0.9",
            }
        )
        options["extractor_args"] = {"youtube": {"player_client": ["web", "android"]}}
    if url and _is_douyin_url(url):
        options["http_headers"].update(
            {
                "Origin": "https://www.douyin.com",
                "Referer": "https://www.douyin.com/",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            }
        )
    return options


def parse_video(url: str) -> ParseResponse:
    url = normalize_url(url)
    if _is_douyin_url(url):
        try:
            return parse_browser_sniffed_page(url)
        except HTTPException:
            try:
                return parse_douyin_public(url)
            except HTTPException:
                pass

    options = {
        **_ydl_base_options(url),
        "skip_download": True,
    }
    try:
        if _is_pornhub_url(url):
            try:
                return parse_browser_sniffed_page(url)
            except HTTPException:
                pass
        if _is_youtube_url(url):
            try:
                return parse_browser_sniffed_page(url)
            except HTTPException:
                pass
        with YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=False)
    except DownloadError as exc:
        try:
            return parse_sniffed_page(url)
        except HTTPException:
            try:
                return parse_browser_sniffed_page(url)
            except HTTPException:
                raise HTTPException(status_code=422, detail=_clean_error(exc, url)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=_clean_error(exc, url)) from exc

    if not isinstance(info, dict):
        raise HTTPException(status_code=422, detail="无法解析该链接，请换一个公开视频链接。")

    return ParseResponse(
        title=info.get("title") or "未命名视频",
        thumbnail=_proxy_thumbnail(info.get("thumbnail")),
        duration=_safe_int(info.get("duration")),
        uploader=info.get("uploader") or info.get("channel"),
        webpage_url=info.get("webpage_url") or url,
        formats=_extract_formats(info),
    )


def _set_task(task: DownloadTask, **changes: Any) -> None:
    with tasks_lock:
        for key, value in changes.items():
            setattr(task, key, value)
        task.updated_at = time.time()


def _progress_hook(task: DownloadTask):
    def hook(payload: dict[str, Any]) -> None:
        status = payload.get("status")
        if status == "downloading":
            total = payload.get("total_bytes") or payload.get("total_bytes_estimate") or 0
            downloaded = payload.get("downloaded_bytes") or 0
            progress = min(99.0, downloaded / total * 100) if total else task.progress
            speed = payload.get("speed")
            speed_label = f" · {speed / 1024 / 1024:.1f} MB/s" if speed else ""
            _set_task(task, status="running", progress=progress, message=f"正在下载{speed_label}")
        elif status == "finished":
            _set_task(task, progress=99.0, message="下载完成，正在整理文件")

    return hook


def _run_douyin_public_download(task: DownloadTask) -> bool:
    _set_task(task, status="running", progress=2.0, message="正在解析抖音公开视频")
    try:
        video_id, item = _fetch_douyin_item(task.url)
        media_url = _douyin_media_url(item)
        title = str(item.get("desc") or f"douyin_{video_id}")
        safe_title = re.sub(r'[\\/:*?"<>|\s]+', " ", title).strip()[:80] or f"douyin_{video_id}"
        target_path = DOWNLOAD_DIR / f"{task.task_id}.{safe_title}.mp4"
        temp_path = target_path.with_suffix(".mp4.part")

        headers = {
            **DOUYIN_HEADERS,
            "Referer": f"https://www.douyin.com/video/{video_id}",
        }
        with requests.get(
            media_url,
            headers=headers,
            stream=True,
            allow_redirects=True,
            timeout=(10, 60),
        ) as response:
            response.raise_for_status()
            content_type = response.headers.get("Content-Type", "").split(";", 1)[0].lower()
            if content_type and not (
                content_type.startswith("video/")
                or content_type == "application/octet-stream"
            ):
                raise RuntimeError(f"抖音视频地址返回了非视频内容：{content_type}")

            total = int(response.headers.get("Content-Length") or 0)
            downloaded = 0
            with temp_path.open("wb") as file_obj:
                for chunk in response.iter_content(chunk_size=1024 * 256):
                    if not chunk:
                        continue
                    file_obj.write(chunk)
                    downloaded += len(chunk)
                    if total:
                        progress = min(99.0, downloaded / total * 100)
                        _set_task(task, progress=progress, message="正在下载抖音视频")
                    else:
                        _set_task(task, progress=min(95.0, task.progress + 1), message="正在下载抖音视频")

        temp_path.replace(target_path)
        _set_task(
            task,
            status="finished",
            progress=100.0,
            message="文件已准备好",
            filename=target_path.name,
        )
        return True
    except Exception as exc:
        _set_task(task, progress=max(task.progress, 3.0), message="抖音专用解析失败，正在尝试 yt-dlp")
        return False


def _run_sniffed_download(task: DownloadTask, media_url: str) -> None:
    if "phncdn.com" in media_url.lower() and not _is_signed_pornhub_media_url(media_url):
        _set_task(
            task,
            status="failed",
            progress=max(task.progress, 1.0),
            message="下载失败",
            error="视频 CDN 返回 470 No Token，当前直链缺少签名 token。请粘贴原始视频页面链接重新解析后下载。",
        )
        return

    ext = _media_ext(media_url)
    output_ext = "mp4" if ext == "m3u8" else ext
    url_hash = hashlib.sha1(media_url.encode("utf-8")).hexdigest()[:8]
    target_path = DOWNLOAD_DIR / f"{task.task_id}.sniffed-{url_hash}.{output_ext}"
    temp_path = target_path.with_suffix(f".{output_ext}.part")
    _set_task(task, status="running", progress=2.0, message="正在下载嗅探到的视频资源")

    try:
        if ext == "m3u8":
            ffmpeg = FFMPEG_DIR / "ffmpeg.exe"
            if not ffmpeg.exists():
                raise RuntimeError("当前资源是 HLS/m3u8，需要先安装 ffmpeg。")
            command = [
                str(ffmpeg),
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-nostdin",
                "-reconnect",
                "1",
                "-reconnect_streamed",
                "1",
                "-reconnect_delay_max",
                "5",
                "-headers",
                f"User-Agent: {DEFAULT_HEADERS['User-Agent']}\r\nReferer: {task.url}\r\n",
                "-i",
                media_url,
                "-c",
                "copy",
                "-bsf:a",
                "aac_adtstoasc",
                str(target_path),
            ]
            process = subprocess.Popen(
                command,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="ignore",
            )
            started_at = time.time()
            last_size = -1
            last_growth_at = started_at
            while process.poll() is None:
                if time.time() - started_at > 180:
                    process.kill()
                    raise RuntimeError("HLS 视频流下载超时，请改选 MP4 直链格式或较低清晰度后重试。")
                current_size = target_path.stat().st_size if target_path.exists() else 0
                if current_size > last_size:
                    last_size = current_size
                    last_growth_at = time.time()
                elif time.time() - last_growth_at > 30:
                    process.kill()
                    raise RuntimeError("HLS 视频流 30 秒没有写入数据，请重新解析后选择其他清晰度重试。")
                _set_task(task, progress=min(95.0, task.progress + 1.5), message="正在合并 HLS 视频流")
                time.sleep(1)
            if process.returncode != 0:
                raise RuntimeError("ffmpeg 下载 HLS 失败，请重新解析后选择其他清晰度重试。")
        else:
            media_headers = {
                **DEFAULT_HEADERS,
                "Referer": task.url,
            }
            with requests.get(media_url, headers=media_headers, stream=True, timeout=(10, 90)) as response:
                response.raise_for_status()
                content_type = response.headers.get("Content-Type", "").split(";", 1)[0].lower()
                if content_type.startswith("image/"):
                    raise RuntimeError("嗅探到的是图片资源，不是视频文件。请重新解析原视频页面后下载。")
                total = int(response.headers.get("Content-Length") or 0)
                downloaded = 0
                with temp_path.open("wb") as file_obj:
                    for chunk in response.iter_content(chunk_size=1024 * 256):
                        if not chunk:
                            continue
                        file_obj.write(chunk)
                        downloaded += len(chunk)
                        if total:
                            _set_task(task, progress=min(99.0, downloaded / total * 100), message="正在下载直链视频")
                        else:
                            _set_task(task, progress=min(95.0, task.progress + 1), message="正在下载直链视频")
            temp_path.replace(target_path)

        if not target_path.exists() or target_path.stat().st_size == 0:
            raise RuntimeError("嗅探资源下载结束，但没有生成有效文件。")
        with target_path.open("rb") as file_obj:
            header = file_obj.read(12)
        if (
            header.startswith(b"\xff\xd8\xff")
            or header.startswith(b"\x89PNG")
            or header.strip() in {b"[]", b"{}"}
            or target_path.stat().st_size < 1024
        ):
            target_path.unlink(missing_ok=True)
            raise RuntimeError("下载结果不是有效视频文件。请重新解析原视频页面，并选择 HLS 格式下载。")
        _set_task(
            task,
            status="finished",
            progress=100.0,
            message="文件已准备好",
            filename=target_path.name,
        )
    except Exception as exc:
        if temp_path.exists():
            temp_path.unlink(missing_ok=True)
        _set_task(
            task,
            status="failed",
            progress=max(task.progress, 1.0),
            message="下载失败",
            error=_clean_error(exc, task.url),
        )


def _run_sniffed_pair_download(task: DownloadTask, video_url: str, audio_url: str) -> None:
    target_path = DOWNLOAD_DIR / f"{task.task_id}.sniffed-merged.mp4"
    _set_task(task, status="running", progress=2.0, message="正在合并网页视频和音频")
    try:
        ffmpeg = FFMPEG_DIR / "ffmpeg.exe"
        if not ffmpeg.exists():
            raise RuntimeError("需要 ffmpeg 合并视频和音频。")
        headers = f"User-Agent: {DEFAULT_HEADERS['User-Agent']}\r\nReferer: {task.url}\r\n"
        command = [
            str(ffmpeg),
            "-y",
            "-headers",
            headers,
            "-i",
            video_url,
            "-headers",
            headers,
            "-i",
            audio_url,
            "-c",
            "copy",
            "-movflags",
            "+faststart",
            str(target_path),
        ]
        process = subprocess.Popen(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="ignore",
        )
        while process.poll() is None:
            _set_task(task, progress=min(95.0, task.progress + 2), message="正在合并网页视频和音频")
            time.sleep(1)
        if process.returncode != 0:
            stderr = process.stderr.read() if process.stderr else ""
            raise RuntimeError(stderr.strip() or "ffmpeg 合并失败。")
        if not target_path.exists() or target_path.stat().st_size == 0:
            raise RuntimeError("合并结束，但没有生成有效文件。")
        _set_task(
            task,
            status="finished",
            progress=100.0,
            message="文件已准备好",
            filename=target_path.name,
        )
    except Exception as exc:
        _set_task(
            task,
            status="failed",
            progress=max(task.progress, 1.0),
            message="下载失败",
            error=_clean_error(exc, task.url),
        )


def _run_download(task: DownloadTask) -> None:
    if task.format_id and task.format_id.startswith("sniffpair:"):
        payload = task.format_id.removeprefix("sniffpair:")
        video_part, _, audio_part = payload.partition("|")
        _run_sniffed_pair_download(task, unquote(video_part), unquote(audio_part))
        return

    if task.format_id and task.format_id.startswith("sniff:"):
        _run_sniffed_download(task, unquote(task.format_id.removeprefix("sniff:")))
        return

    if _is_douyin_url(task.url) and _run_douyin_public_download(task):
        return

    _set_task(task, status="running", progress=1.0, message="正在连接视频源")
    output_template = str(DOWNLOAD_DIR / f"{task.task_id}.%(title).80s.%(ext)s")
    format_value = task.format_id or "bestvideo+bestaudio/best"
    options = {
        **_ydl_base_options(task.url),
        "format": format_value,
        "outtmpl": output_template,
        "merge_output_format": "mp4",
        "progress_hooks": [_progress_hook(task)],
    }

    before = set(DOWNLOAD_DIR.glob(f"{task.task_id}.*"))
    try:
        with YoutubeDL(options) as ydl:
            ydl.download([task.url])
        after = set(DOWNLOAD_DIR.glob(f"{task.task_id}.*"))
        files = sorted(after - before or after, key=lambda path: path.stat().st_mtime, reverse=True)
        if not files:
            raise RuntimeError("下载已结束，但没有找到生成的文件。")
        _set_task(
            task,
            status="finished",
            progress=100.0,
            message="文件已准备好",
            filename=files[0].name,
        )
    except Exception as exc:
        _set_task(
            task,
            status="failed",
            progress=max(task.progress, 1.0),
            message="下载失败",
            error=_clean_error(exc, task.url),
        )


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/parse", response_model=ParseResponse)
def parse_endpoint(payload: ParseRequest) -> ParseResponse:
    return parse_video(str(payload.url))


@app.post("/api/download", response_model=DownloadResponse)
def download_endpoint(payload: DownloadRequest) -> DownloadResponse:
    task_id = uuid.uuid4().hex[:12]
    task = DownloadTask(
        task_id=task_id,
        url=str(payload.url),
        format_id=payload.format_id,
    )
    with tasks_lock:
        tasks[task_id] = task
    executor.submit(_run_download, task)
    return DownloadResponse(task_id=task_id)


@app.post("/api/summaries", response_model=SummaryResponse)
def summary_endpoint(payload: SummaryRequest) -> SummaryResponse:
    task_id = uuid.uuid4().hex[:12]
    task = SummaryTask(task_id=task_id, url=str(payload.url))
    with summary_tasks_lock:
        summary_tasks[task_id] = task
    executor.submit(_run_summary, task)
    return SummaryResponse(task_id=task_id)


@app.get("/api/tasks/{task_id}", response_model=TaskResponse)
def task_endpoint(task_id: str) -> TaskResponse:
    with tasks_lock:
        task = tasks.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在或服务已重启。")
    return task.to_response()


@app.get("/api/summaries/{task_id}", response_model=SummaryTaskResponse)
def summary_task_endpoint(task_id: str) -> SummaryTaskResponse:
    with summary_tasks_lock:
        task = summary_tasks.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="总结任务不存在或服务已重启。")
    return task.to_response()


@app.post("/api/summaries/{task_id}/mind-map", response_model=MindMapResponse)
def create_mind_map_endpoint(task_id: str, regenerate: bool = False) -> MindMapResponse:
    with summary_tasks_lock:
        summary = summary_tasks.get(task_id)
    if not summary:
        raise HTTPException(status_code=404, detail="总结任务不存在或服务已重启。")
    if summary.status != "finished" or not summary.result:
        raise HTTPException(status_code=409, detail="总结完成后才能生成思维导图。")
    with mind_map_tasks_lock:
        existing = mind_map_tasks.get(task_id)
        if existing and not regenerate and existing.status in {"pending", "running", "finished"}:
            return MindMapResponse(task_id=existing.task_id)
        task = MindMapTask(uuid.uuid4().hex[:12], task_id)
        mind_map_tasks[task_id] = task
    executor.submit(_run_mind_map, task, summary)
    return MindMapResponse(task_id=task.task_id)


@app.get("/api/summaries/{task_id}/mind-map", response_model=MindMapTaskResponse)
def mind_map_task_endpoint(task_id: str) -> MindMapTaskResponse:
    with summary_tasks_lock:
        if task_id not in summary_tasks:
            raise HTTPException(status_code=404, detail="总结任务不存在或服务已重启。")
    with mind_map_tasks_lock:
        task = mind_map_tasks.get(task_id)
        if not task:
            raise HTTPException(status_code=404, detail="思维导图任务不存在。")
        return task.to_response()


@app.get("/api/files/{filename}")
def file_endpoint(filename: str) -> FileResponse:
    safe_name = Path(unquote(filename)).name
    file_path = DOWNLOAD_DIR / safe_name
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(status_code=404, detail="文件不存在。")
    return FileResponse(file_path, filename=safe_name)


@app.get("/api/summary-files/{filename}")
def summary_file_endpoint(filename: str) -> FileResponse:
    safe_name = Path(unquote(filename)).name
    file_path = SUMMARY_DIR / safe_name
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(status_code=404, detail="总结文件不存在。")
    return FileResponse(file_path, filename=safe_name)


@app.get("/api/thumbnails/{filename}")
def thumbnail_endpoint(filename: str) -> FileResponse:
    safe_name = Path(unquote(filename)).name
    file_path = THUMBNAIL_DIR / safe_name
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(status_code=404, detail="封面不存在。")
    return FileResponse(file_path)


@app.get("/api/image-proxy")
def image_proxy(url: str) -> Response:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise HTTPException(status_code=400, detail="图片地址不合法。")
    request = Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/126.0.0.0 Safari/537.36"
            ),
            "Referer": _thumbnail_referer(url),
        },
    )
    try:
        with urlopen(request, timeout=12) as result:
            content = result.read()
            media_type = result.headers.get_content_type() or "image/jpeg"
    except Exception as exc:
        raise HTTPException(status_code=502, detail=_clean_error(exc, url)) from exc
    return Response(content=content, media_type=media_type)
