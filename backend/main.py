from __future__ import annotations

import re
import json
import hashlib
import html
import logging
import math
import os
import secrets
import subprocess
import threading
import time
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Callable, Literal
from urllib.parse import parse_qs, quote, unquote, urljoin, urlparse
from urllib.request import Request, urlopen

import requests
from fastapi import FastAPI, Header, HTTPException
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from openai import OpenAI
from playwright.sync_api import sync_playwright
from pydantic import BaseModel, Field, HttpUrl
from starlette.background import BackgroundTask
from yt_dlp import YoutubeDL
from yt_dlp.cookies import extract_cookies_from_browser
from yt_dlp.utils import DownloadError

from backend.watermark import router as watermark_router
from backend.audio import IncompleteAudioError, download_audio
from backend import music
from backend.music_lyrics import router as music_lyrics_router
from backend.music_library_media import router as music_library_media_router, start_cover_backfill
from backend import accounts, ai, library, music_imports, speech, video_cache, video_history, video_store
from backend.administration import router as administration_router
from backend.workspace import router as workspace_router
from backend.network import public_url, public_get
from backend.runtime import browser_executable, executable_name, ffmpeg_directory, node_executable
from backend.browser_sessions import sessions as browser_sessions, needs_verification, verification_required


logger = logging.getLogger("videodream.summary")

ROOT_DIR = Path(__file__).resolve().parent.parent
DOWNLOAD_DIR = ROOT_DIR / "downloads"
THUMBNAIL_DIR = DOWNLOAD_DIR / "_thumbnails"
SUMMARY_DIR = DOWNLOAD_DIR / "_summaries"
CONFIG_DIR = ROOT_DIR / "config"
AI_CONFIG_PATH = CONFIG_DIR / "ai.json"
FFMPEG_DIR = ffmpeg_directory()
NODE_EXE = node_executable()
DOWNLOAD_DIR.mkdir(exist_ok=True)
THUMBNAIL_DIR.mkdir(exist_ok=True)
SUMMARY_DIR.mkdir(exist_ok=True)

TaskStatus = Literal["pending", "running", "finished", "failed", "cancelled"]


class ParseRequest(BaseModel):
    url: HttpUrl
    force: bool = False


class DownloadRequest(BaseModel):
    url: HttpUrl
    format_id: str | None = Field(default=None, max_length=4096)
    media_type: Literal["video", "audio"] = "video"
    audio_format: Literal["m4a", "mp3"] = "m4a"
    parse_record_id: str | None = Field(default=None, max_length=64)


class SummaryRequest(BaseModel):
    url: HttpUrl
    parse_record_id: str | None = Field(default=None, max_length=64)


class PreviewRequest(BaseModel):
    url: HttpUrl
    format_id: str = Field(min_length=1, max_length=4096)
    parse_record_id: str | None = Field(default=None, max_length=64)


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
    record_id: str | None = None


class DownloadResponse(BaseModel):
    task_id: str


class BatchTrackRequest(BaseModel):
    token: str = Field(min_length=1, max_length=128)
    webpage_url: HttpUrl


class MusicBatchRequest(BaseModel):
    tracks: list[BatchTrackRequest] = Field(min_length=1, max_length=100)
    audio_format: Literal["m4a", "mp3"] = "m4a"


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
    version: int = 1


class TaskResponse(BaseModel):
    task_id: str
    status: TaskStatus
    progress: float = 0
    message: str = ""
    filename: str | None = None
    download_url: str | None = None
    error: str | None = None
    media_type: Literal["video", "audio"] = "video"


class DownloadTask:
    def __init__(
        self,
        task_id: str,
        url: str,
        format_id: str | None,
        media_type: Literal["video", "audio"] = "video",
        audio_format: Literal["m4a", "mp3"] = "m4a",
    ) -> None:
        self.task_id = task_id
        self.url = normalize_url(url)
        self.format_id = format_id
        self.media_type = media_type
        self.audio_format = audio_format
        self.status: TaskStatus = "pending"
        self.progress = 0.0
        self.message = "等待下载任务开始"
        self.filename: str | None = None
        self.error: str | None = None
        self.created_at = time.time()
        self.updated_at = time.time()
        self.music_track: music.Track | None = None
        self.user_id = accounts.owner_id() if accounts.principal.get() else 0
        self.cancel = threading.Event()
        self.process: subprocess.Popen | None = None
        self.parse_record_id: str | None = None

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
            media_type=self.media_type,
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
        self.parse_record_id: str | None = None
        self.created_at = time.time()
        self.updated_at = time.time()
        self.user_id = accounts.owner_id() if accounts.principal.get() else 0
        self.cancel = threading.Event()

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
        self.user_id = accounts.owner_id() if accounts.principal.get() else 0
        self.cancel = threading.Event()
        self.task_id = task_id
        self.summary_task_id = summary_task_id
        self.status: TaskStatus = "pending"
        self.progress = 0.0
        self.message = "等待思维导图任务开始"
        self.error: str | None = None
        self.result: MindMapResult | None = None
        self.created_at = time.time()
        self.updated_at = time.time()
        self.version = 1
        self.parse_record_id: str | None = None

    def to_response(self) -> MindMapTaskResponse:
        return MindMapTaskResponse(
            task_id=self.task_id,
            summary_task_id=self.summary_task_id,
            status=self.status,
            progress=round(self.progress, 2),
            message=self.message,
            error=self.error,
            result=self.result,
            version=self.version,
        )


from backend.music_agent import api as music_agent_api, mcp as music_agent_mcp, runtime as music_agent_runtime

app = FastAPI(title="VideoDream API", version="1.0.0", lifespan=music_agent_mcp.lifespan)


@app.exception_handler(RequestValidationError)
async def validation_error_response(request, exc):
    def finite(value):
        if isinstance(value, float) and not math.isfinite(value):
            return str(value)
        if isinstance(value, dict):
            return {key: finite(item) for key, item in value.items()}
        if isinstance(value, list):
            return [finite(item) for item in value]
        return value
    return JSONResponse({"detail": finite(jsonable_encoder(exc.errors()))}, status_code=422)


app.include_router(watermark_router)
app.include_router(music.router)
app.include_router(music_agent_api.router)
app.router.routes.extend(music_agent_mcp.http_app.routes)
app.include_router(music_imports.router)
app.include_router(music_lyrics_router)
app.include_router(music_library_media_router)
app.include_router(library.router)
app.include_router(accounts.router)
app.include_router(administration_router)
app.include_router(workspace_router)
app.include_router(video_history.router)
app.add_middleware(accounts.AuthenticationMiddleware)
FRONTEND_DIST = ROOT_DIR / "frontend" / "dist"
if (FRONTEND_DIST / "assets").is_dir():
    app.mount("/assets", StaticFiles(directory=FRONTEND_DIST / "assets"), name="frontend-assets")
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
music_batches: dict[str, dict[str, Any]] = {}
music_batches_lock = threading.RLock()
music_batch_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="music-batch")
MUSIC_BATCH_INTERVAL = 2.0
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
    _checkpoint(task, changes)
    with summary_tasks_lock:
        for key, value in changes.items():
            setattr(task, key, value)
        task.updated_at = time.time()
    _persist_task(task, "summary")


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


def _extract_summary_source(url: str, *, speech_fallback: Callable | None = None) -> tuple[dict[str, Any], str, str, list[SubtitleSegment]]:
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
        if _is_bilibili_url(url):
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


def _summary_source_label(source: str) -> str:
    if source == "manual":
        return "人工字幕"
    if source == "page_text":
        return "页面文案"
    if source == "speech":
        return "语音转写（分段时间戳）"
    return "自动字幕"


def _load_ai_config() -> dict[str, str]:
    return ai.load_config("text", legacy_path=AI_CONFIG_PATH)


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


def _generate_ai_summary(title: str, webpage_url: str, segments: list[SubtitleSegment], *, source: str = "manual") -> str:
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
                    f"视频链接：{webpage_url}\n\n" +
                    ("语音转写的时间戳代表音频分段起点，章节定位仅为近似值；转写可能包含识别错误。\n" if source == "speech" else "") +
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
    _checkpoint(task, changes)
    with mind_map_tasks_lock:
        for key, value in changes.items():
            setattr(task, key, value)
        task.updated_at = time.time()
    _persist_task(task, "mind_map")


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
    accounts.register_resource("summary_file", markdown_name, {"filename": markdown_name, "size": markdown_path.stat().st_size}, uid=task.user_id)
    accounts.register_resource("summary_file", json_name, {"filename": json_name, "size": json_path.stat().st_size}, uid=task.user_id)
    return result


def _extract_speech_summary_source(task: SummaryTask, info: dict | None):
    _set_summary_task(task, progress=10, message="未找到可用字幕，正在准备音轨转写")
    cached = _get_sniff_cache(task.url)
    if info is None:
        cached = cached or parse_video(task.url)
        info = {"title": cached.title, "duration": cached.duration, "webpage_url": cached.webpage_url or task.url}
    source_url, direct = task.url, False
    if cached:
        selected = next((item for item in cached.formats if item.format_id.startswith("sniffpair:")), None)
        selected = selected or next((item for item in cached.formats if item.format_id.startswith("sniff:")), None)
        if selected:
            direct = True
            if selected.format_id.startswith("sniffpair:"):
                source_url = unquote(selected.format_id.removeprefix("sniffpair:").partition("|")[2])
            else:
                source_url = unquote(selected.format_id.removeprefix("sniff:"))
    options = _ydl_base_options(task.url)
    options["http_headers"]["Referer"] = task.url
    rows = speech.transcribe_video(
        url=source_url, title=str(info.get("title") or "视频音轨"), task_id=task.task_id,
        work_root=ROOT_DIR / ".run" / "speech", ffmpeg_dir=FFMPEG_DIR, options=options, direct=direct,
        expected_duration=info.get("duration"),
        checkpoint=lambda: _checkpoint(task, {}),
        progress=lambda value, message: _set_summary_task(task, progress=value, message=message),
    )
    segments = [SubtitleSegment(start=item.start, end=item.end, timestamp=_format_timestamp(item.start), text=item.text) for item in rows]
    return info, "自动识别", "speech", segments


def _run_summary(task: SummaryTask) -> None:
    try:
        _set_summary_task(task, status="running", progress=8.0, message="正在解析视频字幕")
        info, language, source, segments = _extract_summary_source(
            task.url, speech_fallback=lambda info: _extract_speech_summary_source(task, info),
        )
        title = str(info.get("title") or "未命名视频")
        webpage_url = str(info.get("webpage_url") or task.url)
        _set_summary_task(
            task,
            progress=64.0 if source == "speech" else 42.0,
            message="语音转写完成，正在生成 AI 摘要" if source == "speech" else f"已提取 {language} 字幕，正在生成 AI 摘要",
            transcript=segments,
        )
        summary_markdown = _generate_ai_summary(title, webpage_url, segments, source=source)
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


def _cache_thumbnail(url: str) -> str:
    response = image_proxy(url)
    ext = {"image/png": "png", "image/webp": "webp", "image/gif": "gif"}.get(response.media_type, "jpg")
    filename = f"video-{accounts.owner_id()}-{hashlib.sha256(url.encode()).hexdigest()[:24]}.{ext}"
    path = THUMBNAIL_DIR / filename
    path.write_bytes(response.body)
    accounts.register_resource("thumbnail", filename, {"filename": filename, "size": path.stat().st_size})
    return _local_thumbnail(filename)


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
    return browser_executable()


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


def _is_douyin_audio_stream(url: str) -> bool:
    parsed = urlparse(html.unescape(url))
    host = (parsed.hostname or "").lower()
    mime = parse_qs(parsed.query).get("mime_type", [""])[0].lower()
    return (host == "douyinvod.com" or host.endswith(".douyinvod.com")) and (
        "/media-audio-" in parsed.path.lower() or mime.startswith("audio")
    )


def _is_youtube_progressive_stream(url: str) -> bool:
    parsed = urlparse(html.unescape(url))
    query = parse_qs(parsed.query)
    itag = query.get("itag", [""])[0]
    return itag in {"18", "22"}


def _get_sniff_cache(url: str) -> ParseResponse | None:
    if accounts.principal.get():
        value = video_cache.get(video_cache.parse_key(url))
        if not value:
            return None
        try:
            return ParseResponse.model_validate(value)
        except ValueError:
            video_cache.remove(video_cache.parse_key(url))
            return None
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
    if accounts.principal.get():
        value = response.model_dump(exclude={"record_id"})
        ttl = video_cache.parse_ttl(value)
        if ttl > 0:
            video_cache.put(video_cache.parse_key(url), value, ttl)
        return
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
    video_history.validate_relation(payload.parse_record_id, normalize_url(str(payload.url)))
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
        return PreviewResponse(preview_url=f"/api/previews/{token}/content", expires_in=ttl)
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
        if accounts.principal.get():
            video_cache.remove(video_cache.key("preview", token))
            video_cache.remove(video_cache.parse_key(session.referer))
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
    identity = f"{accounts.owner_id()}:{normalized_url}" if accounts.principal.get() else normalized_url
    try:
        filename = f"{hashlib.sha1(identity.encode('utf-8')).hexdigest()[:16]}.png"
        target_path = THUMBNAIL_DIR / filename
        videos = page.locator("video")
        index = videos.evaluate_all("""(videos) => videos.findIndex(video => video.currentSrc.startsWith('blob:') || video.currentSrc.includes('.douyinvod.com/'))""")
        locator = videos.nth(index) if index >= 0 else page.locator("body").first
        locator.screenshot(path=str(target_path), timeout=2000)
        if target_path.exists() and target_path.stat().st_size > 0:
            accounts.register_resource("thumbnail", filename, {"filename": filename, "size": target_path.stat().st_size})
            return _local_thumbnail(filename)
    except Exception:
        try:
            filename = f"{hashlib.sha1(identity.encode('utf-8')).hexdigest()[:16]}.png"
            target_path = THUMBNAIL_DIR / filename
            page.screenshot(path=str(target_path), timeout=2000, full_page=False)
            if target_path.exists() and target_path.stat().st_size > 0:
                accounts.register_resource("thumbnail", filename, {"filename": filename, "size": target_path.stat().st_size})
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
                const videos = Array.from(document.querySelectorAll('video'));
                const video = location.hostname.endsWith('douyin.com')
                    ? videos.find(item => item.currentSrc.startsWith('blob:') || item.currentSrc.includes('.douyinvod.com/'))
                    : videos[0];
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
                    duration: firstValue(ld.duration) || meta('video:duration') || meta('duration') ||
                        (Number.isFinite(video?.duration) ? video.duration : ''),
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


def _extract_douyin_response_meta(response: Any, video_id: str | None) -> dict[str, Any]:
    if not video_id or not _is_douyin_url(response.url) or "aweme/detail" not in urlparse(response.url).path or response.status != 200:
        return {}
    try:
        item = response.json().get("aweme_detail")
        if not isinstance(item, dict) or str(item.get("aweme_id")) != video_id:
            return {}
        video = item.get("video") or {}
        milliseconds = _safe_int(video.get("duration"))
        metadata = {
            "title": _clean_meta_text(item.get("desc")),
            "author": _clean_author_text((item.get("author") or {}).get("nickname")),
            "duration": int(milliseconds / 1000) if milliseconds and milliseconds > 0 else None,
            "thumbnail": _pick_url_from_nested(video, "cover") or _pick_url_from_nested(video, "origin_cover"),
        }
        return {key: value for key, value in metadata.items() if value}
    except Exception:
        return {}


@contextmanager
def _temporary_browser_page():
    with sync_playwright() as playwright:
        options = {
            "headless": True,
            "args": ["--disable-blink-features=AutomationControlled", "--autoplay-policy=no-user-gesture-required",
                     "--disable-extensions", "--disable-notifications", "--mute-audio"],
        }
        executable = _browser_executable()
        if executable:
            options["executable_path"] = str(executable)
        browser = playwright.chromium.launch(**options)
        try:
            yield browser.new_page(user_agent=DEFAULT_HEADERS["User-Agent"], locale="zh-CN",
                                   viewport={"width": 1365, "height": 768})
        finally:
            browser.close()


def parse_browser_sniffed_page(url: str) -> ParseResponse:
    normalized = normalize_url(url)
    cached = _get_sniff_cache(normalized)
    if cached:
        return cached
    if _is_douyin_url(normalized) and accounts.principal.get():
        return browser_sessions.run(accounts.owner_id(), lambda page: _parse_browser_sniffed_page(normalized, page), url=normalized)
    return _parse_browser_sniffed_page(normalized)


def _parse_browser_sniffed_page(url: str, persistent_page=None) -> ParseResponse:

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
    douyin_meta: dict[str, Any] = {}

    verification_detected = False
    with nullcontext(persistent_page) if persistent_page is not None else _temporary_browser_page() as page:

        def remember(candidate: str) -> None:
            cleaned = html.unescape(candidate).replace("&amp;", "&")
            if _is_probable_video_resource(cleaned) and cleaned not in captured:
                captured.append(cleaned)

        def route_request(route: Any) -> None:
            request = route.request
            if accounts.principal.get() and request.url.startswith(("http://", "https://")):
                try:
                    public_url(request.url)
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
                douyin_meta.update(_extract_douyin_response_meta(response, _extract_douyin_video_id(normalized_url)))
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
                    if any(_is_youtube_progressive_stream(item) for item in captured) or (
                        any(_is_youtube_video_stream(item) for item in captured)
                        and any(_is_youtube_audio_stream(item) for item in captured)
                    ):
                        break
                elif is_douyin_page:
                    has_video = any(("douyinvod.com" in item.lower() or "mime_type=video" in item.lower()) and not _is_douyin_audio_stream(item) for item in captured)
                    has_audio = any(_is_douyin_audio_stream(item) for item in captured)
                    if has_video and has_audio:
                        break
                    if has_video and time.time() > deadline - 3:
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
            if is_douyin_page:
                verification_detected = needs_verification(page)
                if verification_detected:
                    raise verification_required(normalized_url)
                try:
                    page.evaluate("""() => { const video = Array.from(document.querySelectorAll('video')).find(item => item.currentSrc.startsWith('blob:') || item.currentSrc.includes('.douyinvod.com/')); if (video) { video.muted = true; video.play().catch(() => {}); } }""")
                    page.wait_for_function("""() => { const video = Array.from(document.querySelectorAll('video')).find(item => item.currentSrc.startsWith('blob:') || item.currentSrc.includes('.douyinvod.com/')); return video?.readyState >= 2 && video.videoWidth > 0 && video.currentTime > 0.05; }""", timeout=3000)
                except Exception:
                    pass
            page_meta = _extract_browser_page_meta(page, normalized_url)
            if _is_douyin_url(normalized_url):
                page_meta.update(douyin_meta)
                if not douyin_meta.get("thumbnail"):
                    local_thumbnail = _capture_page_thumbnail(page, normalized_url)
                    if local_thumbnail:
                        page_meta["thumbnail"] = local_thumbnail
            if is_pornhub_page:
                pornhub_player_data = _extract_pornhub_player_data(page)
            page_html = page.content()
        finally:
            page.remove_listener("request", record_request)
            page.remove_listener("response", record_response)
            page.unroute("**/*", route_request)

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
        if is_douyin_page:
            raise verification_required(normalized_url, detected=verification_detected)
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
        if is_douyin_page and _is_douyin_audio_stream(media_url):
            continue
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

    if is_douyin_page:
        audio_stream = next((item for item in ranked if _is_douyin_audio_stream(item)), None)
        video_stream = next((item for item in ranked if not _is_douyin_audio_stream(item)), None)
        if audio_stream and video_stream:
            formats.append(FormatInfo(
                format_id=f"sniffpair:{quote(video_stream, safe='')}|{quote(audio_stream, safe='')}",
                label="MP4 · 完整音视频", ext="mp4", resolution="自动",
            ))

    if _is_youtube_url(normalized_url) and not formats:
        raise HTTPException(status_code=422, detail="浏览器嗅探没有捕获可直接下载的 YouTube 音视频组合。")
    if is_douyin_page and not formats:
        raise verification_required(normalized_url, detected=False)

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
    if (FFMPEG_DIR / executable_name("ffmpeg")).is_file():
        options["ffmpeg_location"] = str(FFMPEG_DIR)
    if NODE_EXE and NODE_EXE.is_file():
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
    if _is_douyin_search_page(url):
        raise HTTPException(status_code=422, detail="这是抖音搜索结果页，请打开具体视频后复制视频地址，或使用该视频的分享链接。")
    if _is_douyin_url(url):
        try:
            return parse_browser_sniffed_page(url)
        except HTTPException as exc:
            if isinstance(exc.detail, dict) and exc.detail.get("code") == "browser_verification_required":
                raise
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


def _checkpoint(task, changes):
    if not task.user_id:
        return
    if changes.get("status") in {"failed", "cancelled"}:
        if task.cancel.is_set():
            changes.update(status="cancelled", message="账号已停用或任务已取消", error=None)
        return
    try:
        if task.cancel.is_set():
            raise HTTPException(401, "任务已取消。")
        if time.monotonic() - getattr(task, "_checked_at", 0) >= 1:
            accounts.active_worker(task.user_id)
            kind = "download" if isinstance(task, DownloadTask) else "summary" if isinstance(task, SummaryTask) else "mind_map"
            with accounts.database() as cursor:
                if kind in video_store.TABLES:
                    stored = video_store.get(cursor, kind, task.task_id, include_deleted=True)
                else:
                    cursor.execute("SELECT deleted,payload FROM resources WHERE kind=%s AND resource_key=%s", (kind, task.task_id))
                    stored = cursor.fetchone()
                if stored and (stored["deleted"] or json.loads(stored["payload"]).get("status") == "cancelled"):
                    raise HTTPException(409, "任务已取消或删除。")
            task._checked_at = time.monotonic()
    except HTTPException:
        task.cancel.set()
        if isinstance(task, DownloadTask) and task.process and task.process.poll() is None:
            task.process.kill()
        task.status, task.message = "cancelled", "账号已停用或任务已取消"
        _persist_task(task, "download" if isinstance(task, DownloadTask) else "summary" if isinstance(task, SummaryTask) else "mind_map", force=True)
        raise RuntimeError("任务已停止。")


def _persist_task(task, kind, force=False):
    if task.user_id and (force or time.monotonic() - getattr(task, "_saved_at", 0) >= 1 or task.status in {"finished", "failed", "cancelled"}):
        snapshot = task.to_response().model_dump()
        if isinstance(task, SummaryTask):
            snapshot["transcript"] = [segment.model_dump() for segment in task.transcript]
        snapshot.update(parse_record_id=getattr(task, "parse_record_id", None), url=getattr(task, "url", None), worker_id=video_history.WORKER_ID)
        with accounts.database() as cursor:
            record_id = getattr(task, "parse_record_id", None)
            if record_id:
                cursor.execute("SELECT deleted FROM video_parse_records WHERE record_id=%s FOR UPDATE", (record_id,))
                parent = cursor.fetchone()
            else:
                parent = None
            if kind in video_store.TABLES:
                stored = video_store.get(cursor, kind, task.task_id, lock=True, include_deleted=True)
            else:
                cursor.execute("SELECT deleted,payload FROM resources WHERE kind=%s AND resource_key=%s FOR UPDATE", (kind, task.task_id))
                stored = cursor.fetchone()
            if stored and stored["deleted"]:
                task.cancel.set()
                if isinstance(task, SummaryTask):
                    paths = list(SUMMARY_DIR.glob(f"{task.task_id}.*.summary.*"))
                    file_kind = "summary_file"
                else:
                    paths = [DOWNLOAD_DIR / task.filename] if getattr(task, "filename", None) else []
                    file_kind = "file"
                for path in paths:
                    path.unlink(missing_ok=True)
                    cursor.execute("UPDATE resources SET deleted=TRUE WHERE kind=%s AND resource_key=%s AND user_id=%s", (file_kind, path.name, task.user_id))
                return
            if stored and json.loads(stored["payload"]).get("status") == "cancelled":
                task.cancel.set()
                task.status = "cancelled"
                snapshot.update(status="cancelled", message="任务已取消")
            if record_id and not stored and (not parent or parent["deleted"]):
                raise HTTPException(409, "解析记录已删除，无法创建任务。")
            now = time.time()
            if kind in video_store.TABLES:
                video_store.save(cursor, kind, task.task_id, task.user_id, snapshot, updated=now)
            else:
                cursor.execute("""INSERT INTO resources (kind,resource_key,user_id,payload,created_at,updated_at) VALUES (%s,%s,%s,%s,%s,%s)
                    ON DUPLICATE KEY UPDATE payload=VALUES(payload),updated_at=VALUES(updated_at)""",
                               (kind, task.task_id, task.user_id, json.dumps(snapshot, ensure_ascii=False), now, now))
        if task.status == "finished" and getattr(task, "filename", None):
            path = DOWNLOAD_DIR / task.filename
            accounts.register_resource("file", task.filename, {"filename": task.filename, "size": path.stat().st_size if path.is_file() else 0}, uid=task.user_id)
        task._saved_at = time.monotonic()


def _set_task(task: DownloadTask, **changes: Any) -> None:
    _checkpoint(task, changes)
    with tasks_lock:
        for key, value in changes.items():
            setattr(task, key, value)
        task.updated_at = time.time()
    _persist_task(task, "download")


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
            ffmpeg = FFMPEG_DIR / executable_name("ffmpeg")
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
            task.process = process
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
        ffmpeg = FFMPEG_DIR / executable_name("ffmpeg")
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
            task.process = process
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


def _run_audio_download(task: DownloadTask) -> None:
    _set_task(task, status="running", progress=1, message="正在解析音轨")
    try:
        if task.format_id and task.format_id.startswith("music:"):
            track = task.music_track or music.get_track(task.format_id.removeprefix("music:"), task.url)
            source_url, options, source_name = music.resolve(track, _ydl_base_options(task.url))
            def download_track():
                return download_audio(
                    url=source_url, title=track.title, task_id=task.task_id, output_format=task.audio_format,
                    download_dir=DOWNLOAD_DIR, ffmpeg_dir=FFMPEG_DIR, options=options, direct=True,
                    expected_duration=track.duration, progress=lambda value, message: _set_task(task, progress=value, message=message),
                )
            try:
                target = download_track()
            except IncompleteAudioError:
                if source_name not in ("原平台", "酷狗音乐"):
                    raise
                _set_task(task, message="原平台仅返回试听，正在尝试解析服务")
                source_url, options, source_name = music.resolve(track, _ydl_base_options(task.url), public_only=True)
                target = download_track()
            _set_task(task, progress=98, message="正在保存音频索引")
            _remember_audio_download(target, track.model_dump())
            _set_task(task, status="finished", progress=100, message=f"音频已准备好 · {source_name}", filename=target.name)
            return
        result = _get_sniff_cache(task.url) or parse_video(task.url)
        if not result.formats:
            raise RuntimeError("没有可用音轨，请使用单个视频或歌曲链接。")
        selected = next((item for item in result.formats if item.format_id.startswith("sniffpair:")), None)
        if not selected:
            selected = next((item for item in result.formats if item.format_id == task.format_id), None)
        if not selected:
            selected = next((item for item in result.formats if item.format_id.startswith("sniff:")), None)
        source_url, direct = task.url, False
        if selected and selected.format_id.startswith("sniffpair:"):
            source_url = unquote(selected.format_id.removeprefix("sniffpair:").partition("|")[2])
            direct = True
        elif selected and selected.format_id.startswith("sniff:"):
            source_url = unquote(selected.format_id.removeprefix("sniff:"))
            direct = True
        options = _ydl_base_options(task.url)
        options["http_headers"]["Referer"] = task.url
        target = download_audio(
            url=source_url, title=result.title, task_id=task.task_id, output_format=task.audio_format,
            download_dir=DOWNLOAD_DIR, ffmpeg_dir=FFMPEG_DIR, options=options, direct=direct,
            expected_duration=result.duration, progress=lambda value, message: _set_task(task, progress=value, message=message),
        )
        _set_task(task, progress=98, message="正在保存音频索引")
        _remember_audio_download(target, {"title": result.title, "artist": result.uploader or "", "provider": "video",
                                         "webpage_url": task.url, "thumbnail": result.thumbnail, "duration": result.duration})
        _set_task(task, status="finished", progress=100, message="音轨已准备好", filename=target.name)
    except Exception as exc:
        _set_task(task, status="failed", message="音轨下载失败", error=_clean_error(exc, task.url))


def _remember_audio_download(target: Path, metadata: dict) -> None:
    try:
        library.record_download(target.name, metadata)
        start_cover_backfill()
    except Exception:
        logger.warning("Audio file saved; library indexing will retry on the next library scan.")


def _run_download(task: DownloadTask) -> None:
    if task.media_type == "audio":
        _run_audio_download(task)
        return
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


@app.get("/", include_in_schema=False)
def frontend_endpoint() -> FileResponse:
    index = FRONTEND_DIST / "index.html"
    if not index.is_file():
        raise HTTPException(404, "前端尚未构建，请在 frontend 目录执行 npm run build。")
    return FileResponse(index)


@app.post("/api/parse", response_model=ParseResponse)
def parse_endpoint(payload: ParseRequest) -> ParseResponse:
    public_url(str(payload.url))
    url = normalize_url(str(payload.url))
    if not accounts.principal.get():
        return parse_video(url)
    record_id = video_history.create_record(url)
    try:
        if payload.force:
            video_cache.remove(video_cache.parse_key(url))
        result = _get_sniff_cache(url) or parse_video(url)
        if result.thumbnail and not result.thumbnail.startswith("/api/thumbnails/"):
            image_url = parse_qs(urlparse(result.thumbnail).query).get("url", [result.thumbnail])[0]
            try:
                result.thumbnail = _cache_thumbnail(image_url)
            except Exception:
                pass
        video_history.finish_record(record_id, result.model_dump())
        video_history.link_legacy_results(record_id)
        _set_sniff_cache(url, result)
        if result.webpage_url and result.webpage_url != url:
            _set_sniff_cache(result.webpage_url, result)
        return result.model_copy(update={"record_id": record_id})
    except Exception as exc:
        message = exc.detail if isinstance(exc, HTTPException) else _clean_error(exc, url)
        if isinstance(message, dict):
            message = message.get("message", str(message))
        video_history.finish_record(record_id, error=str(message))
        if isinstance(exc, HTTPException):
            detail = exc.detail if isinstance(exc.detail, dict) else {"message": str(exc.detail)}
            raise HTTPException(exc.status_code, detail | {"record_id": record_id}) from exc
        raise HTTPException(422, {"message": str(message), "record_id": record_id}) from exc


@app.post("/api/videos/records/{record_id}/formats", response_model=ParseResponse)
def refresh_record_formats(record_id: str, force: bool = False):
    row = video_history.owned_record(record_id)
    stored = video_history.decode(row)
    url = normalize_url((stored.get("result") or {}).get("webpage_url") or stored["url"])
    public_url(url)
    if force:
        video_cache.remove(video_cache.parse_key(url))
    result = _get_sniff_cache(url) or parse_video(url)
    _set_sniff_cache(url, result)
    return result.model_copy(update={"record_id": record_id})


@app.post("/api/browser-session/open")
def open_browser_session(payload: ParseRequest):
    url = normalize_url(str(payload.url))
    public_url(url)
    if not _is_douyin_url(url) or _is_douyin_search_page(url):
        raise HTTPException(422, "验证窗口仅支持具体的抖音视频或分享链接。")
    return browser_sessions.open(accounts.current().id, url)


@app.get("/api/browser-session")
def browser_session_status():
    return browser_sessions.status(accounts.current().id)


@app.delete("/api/browser-session")
def close_browser_session(reset: bool = False):
    result = browser_sessions.close(accounts.current().id, reset=reset)
    if reset:
        video_cache.clear_user(accounts.current().id)
    return result


@app.post("/api/music/parse")
def parse_music_endpoint(payload: ParseRequest):
    url = str(payload.url)
    public_url(url)
    host = _host(url)
    if music.identify(url) or any(host == domain or host.endswith(f".{domain}") for domain in ("music.163.com", "y.qq.com", "kugou.com")):
        return music.parse_url(url)
    result = parse_video(url)
    if not result.formats:
        raise HTTPException(422, "请使用单个视频或歌曲链接，当前链接没有可用音轨。")
    return result.model_dump()


@app.post("/api/download", response_model=DownloadResponse)
def download_endpoint(payload: DownloadRequest) -> DownloadResponse:
    public_url(str(payload.url))
    relation = video_history.validate_relation(payload.parse_record_id, normalize_url(str(payload.url)))
    if payload.format_id and payload.format_id.startswith(("sniff:", "sniffpair:")):
        cached = _get_sniff_cache(normalize_url(str(payload.url)))
        if not cached or not any(item.format_id == payload.format_id for item in cached.formats):
            raise HTTPException(422, "下载格式与当前账号的解析结果不匹配，请重新解析。")
    task_id = uuid.uuid4().hex[:12]
    task = DownloadTask(
        task_id=task_id,
        url=str(payload.url),
        format_id=payload.format_id,
        media_type=payload.media_type,
        audio_format=payload.audio_format,
    )
    task.parse_record_id = payload.parse_record_id
    if relation:
        task.user_id = relation["user_id"]
    with tasks_lock:
        tasks[task_id] = task
    _persist_task(task, "download", force=True)
    owner_marker = accounts.data_owner.set(task.user_id)
    try:
        accounts.submit(executor, _run_download, task)
    finally:
        accounts.data_owner.reset(owner_marker)
    return DownloadResponse(task_id=task_id)


def _music_batch_response(batch: dict[str, Any]) -> dict[str, Any]:
    items = []
    for entry in batch["items"]:
        with tasks_lock:
            result = entry["task"].to_response().model_dump()
        if entry["cancelled"]:
            result.update(status="cancelled", progress=0, message="已取消")
        track = entry["track"]
        result.update(title=track.title, artist=track.artist, provider=track.provider, identifier=track.identifier, webpage_url=track.webpage_url)
        items.append(result)
    counts = {status: sum(item["status"] == status for item in items)
              for status in ("finished", "failed", "cancelled")}
    progress = sum(100 if item["status"] in counts else item["progress"] for item in items) / len(items)
    terminal = batch["status"] in {"finished", "failed", "cancelled"}
    return {"batch_id": batch["batch_id"], "status": batch["status"], "audio_format": batch["audio_format"],
            "progress": round(progress, 2), "total": len(items), **counts, "items": items,
            "download_url": f"/api/music/batches/{batch['batch_id']}/archive" if terminal and counts["finished"] else None}


def _get_music_batch(batch_id: str) -> dict[str, Any]:
    batch = music_batches.get(batch_id)
    if not batch and accounts.principal.get():
        stored = accounts.resource("batch", batch_id)
        data = json.loads(stored["payload"])
        entries = []
        for item in data.get("items", []):
            track = music.Track(title=item["title"], artist=item.get("artist", ""), provider=item["provider"],
                                identifier=item.get("identifier", ""), webpage_url=item.get("webpage_url", "https://example.com"))
            task = DownloadTask(item["task_id"], track.webpage_url, "music:", media_type="audio", audio_format=data["audio_format"])
            task.user_id, task.music_track = stored["user_id"], track
            for field in ("status", "progress", "message", "filename", "error"):
                setattr(task, field, item.get(field))
            entries.append({"track": track, "task": task, "cancelled": item["status"] == "cancelled"})
        batch = {"batch_id": batch_id, "user_id": stored["user_id"], "audio_format": data["audio_format"],
                 "status": data["status"], "stop": threading.Event(), "items": entries}
        music_batches[batch_id] = batch
    if not batch:
        raise HTTPException(404, "批量任务不存在或服务已重启。")
    return batch


def _new_batch_entry(track: music.Track, audio_format: str) -> dict[str, Any]:
    task = DownloadTask(uuid.uuid4().hex[:12], track.webpage_url, f"music:{track.token}",
                        media_type="audio", audio_format=audio_format)
    # The validated snapshot survives token expiry while waiting in the queue.
    task.music_track = track.model_copy(deep=True)
    with tasks_lock:
        tasks[task.task_id] = task
    _persist_task(task, "download", force=True)
    return {"track": task.music_track, "task": task, "cancelled": False}


def _run_music_batch(batch: dict[str, Any]) -> None:
    with music_batches_lock:
        if not batch["stop"].is_set():
            batch["status"] = "running"
    attempted = False
    for entry in batch["items"]:
        if entry["task"].status != "pending" or entry["cancelled"]:
            continue
        if attempted:
            batch["stop"].wait(MUSIC_BATCH_INTERVAL)
        with music_batches_lock:
            if batch["stop"].is_set():
                entry["cancelled"] = True
                continue
            _set_task(entry["task"], status="running", message="正在解析音轨")
        attempted = True
        try:
            _run_audio_download(entry["task"])
        except Exception as exc:
            _set_task(entry["task"], status="failed", message="音轨下载失败", error=str(exc))
    with music_batches_lock:
        statuses = [entry["task"].status for entry in batch["items"]]
        batch["status"] = "cancelled" if batch["stop"].is_set() else "finished" if "finished" in statuses else "failed"
    accounts.register_resource("batch", batch["batch_id"], _music_batch_response(batch), uid=batch.get("user_id"))


@app.post("/api/music/batches")
def create_music_batch(payload: MusicBatchRequest) -> dict[str, Any]:
    tracks, identities = [], set()
    for item in payload.tracks:
        track = music.get_track(item.token, str(item.webpage_url))
        identity = (track.provider, track.identifier)
        if identity not in identities:
            identities.add(identity)
            tracks.append(track)
    with music_batches_lock:
        if any(batch["status"] in {"pending", "running", "cancelling"} and batch.get("user_id") == (accounts.owner_id() if accounts.principal.get() else 0) for batch in music_batches.values()):
            raise HTTPException(409, "已有批量任务正在处理，请等待完成或停止后再添加。")
        batch_id = uuid.uuid4().hex[:12]
        batch = {"batch_id": batch_id, "status": "pending", "audio_format": payload.audio_format,
                 "user_id": accounts.owner_id() if accounts.principal.get() else 0,
                 "stop": threading.Event(), "items": [_new_batch_entry(track, payload.audio_format) for track in tracks]}
        music_batches[batch_id] = batch
        response = _music_batch_response(batch)
        accounts.register_resource("batch", batch_id, response, uid=batch["user_id"])
        accounts.submit(music_batch_executor, _run_music_batch, batch)
    return response


@app.get("/api/music/batches/{batch_id}")
def music_batch_status(batch_id: str) -> dict[str, Any]:
    with music_batches_lock:
        return _music_batch_response(_get_music_batch(batch_id))


@app.post("/api/music/imports/{import_id}/download")
def download_music_import(import_id: str, payload: music_imports.ImportDownload):
    tracks = music_imports.selected_tracks(import_id, payload)
    return create_music_batch(MusicBatchRequest(
        tracks=[BatchTrackRequest(token=track.token, webpage_url=track.webpage_url) for track in tracks],
        audio_format=payload.audio_format,
    ))


@app.post("/api/music/batches/{batch_id}/cancel")
def cancel_music_batch(batch_id: str) -> dict[str, Any]:
    with music_batches_lock:
        batch = _get_music_batch(batch_id)
        if batch["status"] in {"pending", "running", "cancelling"}:
            batch["stop"].set()
            batch["status"] = "cancelling"
            for entry in batch["items"]:
                if entry["task"].status == "pending":
                    entry["cancelled"] = True
        return _music_batch_response(batch)


@app.post("/api/music/batches/{batch_id}/retry")
def retry_music_batch(batch_id: str) -> dict[str, Any]:
    with music_batches_lock:
        batch = _get_music_batch(batch_id)
        if any(item["status"] in {"pending", "running", "cancelling"} and item.get("user_id") == batch.get("user_id") for item in music_batches.values()):
            raise HTTPException(409, "请等待当前批量任务停止或完成。")
        failed = [entry for entry in batch["items"] if entry["task"].status == "failed"]
        if not failed:
            raise HTTPException(409, "没有需要重试的失败歌曲。")
        owner_marker = accounts.data_owner.set(batch.get("user_id") or None)
        try:
            for entry in failed:
                entry.update(_new_batch_entry(entry["track"], batch["audio_format"]))
            batch["stop"].clear()
            batch["status"] = "pending"
            response = _music_batch_response(batch)
            accounts.register_resource("batch", batch_id, response, uid=batch.get("user_id"))
            accounts.submit(music_batch_executor, _run_music_batch, batch)
        finally:
            accounts.data_owner.reset(owner_marker)
        return response


@app.get("/api/music/batches/{batch_id}/archive")
def music_batch_archive(batch_id: str) -> FileResponse:
    with music_batches_lock:
        batch = _get_music_batch(batch_id)
        if batch["status"] in {"pending", "running", "cancelling"}:
            raise HTTPException(409, "批量任务仍在处理中。")
        entries = [(index, entry["track"], entry["task"].filename) for index, entry in enumerate(batch["items"], 1)
                   if entry["task"].status == "finished" and entry["task"].filename]
    if not entries:
        raise HTTPException(409, "没有成功下载的音频可供打包。")
    archive_dir = DOWNLOAD_DIR / "_batches"
    archive_dir.mkdir(exist_ok=True)
    archive = archive_dir / f"{uuid.uuid4().hex}.zip"
    try:
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_STORED) as bundle:
            for index, track, filename in entries:
                if accounts.principal.get():
                    accounts.resource("file", filename)
                file_path = (DOWNLOAD_DIR / filename).resolve()
                if file_path.parent != DOWNLOAD_DIR.resolve() or not file_path.is_file():
                    raise HTTPException(404, f"音频文件不存在：{track.title}")
                label = re.sub(r'[\x00-\x1f\\/:*?"<>|]+', " ", f"{track.artist} - {track.title}").strip(" .")[:120] or "audio"
                bundle.write(file_path, arcname=f"{index:03d}. {label}{file_path.suffix}")
    except Exception:
        archive.unlink(missing_ok=True)
        raise
    return FileResponse(archive, media_type="application/zip", filename=f"music-{batch_id}.zip",
                        background=BackgroundTask(archive.unlink, missing_ok=True))


@app.post("/api/summaries", response_model=SummaryResponse)
def summary_endpoint(payload: SummaryRequest) -> SummaryResponse:
    public_url(str(payload.url))
    relation = video_history.validate_relation(payload.parse_record_id, normalize_url(str(payload.url)))
    task_id = uuid.uuid4().hex[:12]
    task = SummaryTask(task_id=task_id, url=str(payload.url))
    task.parse_record_id = payload.parse_record_id
    if relation:
        task.user_id = relation["user_id"]
    with summary_tasks_lock:
        summary_tasks[task_id] = task
    _persist_task(task, "summary", force=True)
    owner_marker = accounts.data_owner.set(task.user_id)
    try:
        accounts.submit(executor, _run_summary, task)
    finally:
        accounts.data_owner.reset(owner_marker)
    return SummaryResponse(task_id=task_id)


@app.get("/api/tasks/{task_id}", response_model=TaskResponse)
def task_endpoint(task_id: str) -> TaskResponse:
    with tasks_lock:
        task = tasks.get(task_id)
    if not task:
        if accounts.principal.get():
            return TaskResponse.model_validate(json.loads(accounts.resource("download", task_id)["payload"]))
        raise HTTPException(status_code=404, detail="任务不存在或服务已重启。")
    return task.to_response()


@app.get("/api/summaries/{task_id}", response_model=SummaryTaskResponse)
def summary_task_endpoint(task_id: str) -> SummaryTaskResponse:
    with summary_tasks_lock:
        task = summary_tasks.get(task_id)
    if not task:
        if accounts.principal.get():
            return SummaryTaskResponse.model_validate(json.loads(accounts.resource("summary", task_id)["payload"]))
        raise HTTPException(status_code=404, detail="总结任务不存在或服务已重启。")
    return task.to_response()


@app.post("/api/summaries/{task_id}/mind-map", response_model=MindMapResponse)
def create_mind_map_endpoint(task_id: str, regenerate: bool = False) -> MindMapResponse:
    stored_row = accounts.resource("summary", task_id) if accounts.principal.get() else None
    with summary_tasks_lock:
        summary = summary_tasks.get(task_id)
    if not summary:
        if not accounts.principal.get():
            raise HTTPException(status_code=404, detail="总结任务不存在或服务已重启。")
        stored = json.loads(stored_row["payload"])
        summary = SummaryTask(task_id, (stored.get("result") or {}).get("webpage_url", "https://example.com"))
        summary.user_id = stored_row["user_id"]
        summary.status = stored["status"]
        summary.result = SummaryResult.model_validate(stored["result"]) if stored.get("result") else None
        summary.transcript = [SubtitleSegment.model_validate(row) for row in stored.get("transcript", [])]
        summary.parse_record_id = stored.get("parse_record_id")
        summary_tasks[task_id] = summary
    if summary.status != "finished" or not summary.result:
        raise HTTPException(status_code=409, detail="总结完成后才能生成思维导图。")
    if accounts.principal.get():
        with accounts.database() as cursor:
            record_id = json.loads(stored_row["payload"]).get("parse_record_id")
            if record_id:
                # Match task persistence and deletion: lock the parse record before its results.
                cursor.execute("SELECT deleted FROM video_parse_records WHERE record_id=%s AND user_id=%s FOR UPDATE", (record_id, stored_row["user_id"]))
                record = cursor.fetchone()
                if not record or record["deleted"]:
                    raise HTTPException(404, "解析记录已删除。")
            parent = video_store.get(cursor, "summary", task_id, lock=True)
            if not parent:
                raise HTTPException(404, "总结任务不存在。")
            if json.loads(parent["payload"])["status"] != "finished":
                raise HTTPException(409, "总结完成后才能生成思维导图。")
            cursor.execute("SELECT * FROM video_mind_maps WHERE user_id=%s AND deleted=FALSE AND summary_task_id=%s ORDER BY created_at DESC,task_id DESC", (parent["user_id"], task_id))
            versions = [json.loads(video_store.adapt("mind_map", row)["payload"]) for row in cursor.fetchall()]
            active = next((row for row in versions if row["status"] in {"pending", "running"}), None)
            finished = next((row for row in versions if row["status"] == "finished"), None)
            existing = active or (finished if not regenerate else None)
            if existing:
                return MindMapResponse(task_id=existing["task_id"])
            task = MindMapTask(uuid.uuid4().hex[:12], task_id)
            task.user_id = parent["user_id"]
            task.parse_record_id = json.loads(parent["payload"]).get("parse_record_id")
            cursor.execute("SELECT COALESCE(MAX(version),0) AS version FROM video_mind_maps WHERE summary_task_id=%s", (task_id,))
            task.version = cursor.fetchone()["version"] + 1
            value = task.to_response().model_dump() | {"parse_record_id": task.parse_record_id, "worker_id": video_history.WORKER_ID}
            now = time.time()
            video_store.save(cursor, "mind_map", task.task_id, task.user_id, value, created=now)
        with mind_map_tasks_lock:
            mind_map_tasks[task_id] = task
        owner_marker = accounts.data_owner.set(task.user_id)
        try:
            accounts.submit(executor, _run_mind_map, task, summary)
        finally:
            accounts.data_owner.reset(owner_marker)
        return MindMapResponse(task_id=task.task_id)
    with mind_map_tasks_lock:
        existing = mind_map_tasks.get(task_id)
        if existing and not regenerate and existing.status in {"pending", "running", "finished"}:
            return MindMapResponse(task_id=existing.task_id)
        task = MindMapTask(uuid.uuid4().hex[:12], task_id)
        task.user_id = summary.user_id
        mind_map_tasks[task_id] = task
    _persist_task(task, "mind_map", force=True)
    owner_marker = accounts.data_owner.set(summary.user_id)
    try:
        accounts.submit(executor, _run_mind_map, task, summary)
    finally:
        accounts.data_owner.reset(owner_marker)
    return MindMapResponse(task_id=task.task_id)


@app.get("/api/summaries/{task_id}/mind-map", response_model=MindMapTaskResponse)
def mind_map_task_endpoint(task_id: str, map_id: str | None = None) -> MindMapTaskResponse:
    if accounts.principal.get():
        parent = accounts.resource("summary", task_id)
        with library.database() as cursor:
            args: tuple = (parent["user_id"], task_id)
            query = "SELECT * FROM video_mind_maps WHERE deleted=FALSE AND user_id=%s AND summary_task_id=%s"
            if map_id:
                query += " AND task_id=%s"
                args += (map_id,)
            query += " ORDER BY (status='finished') DESC,created_at DESC,task_id DESC LIMIT 1"
            cursor.execute(query, args)
            stored = cursor.fetchone()
        if not stored:
            raise HTTPException(404, "思维导图任务不存在。")
        return MindMapTaskResponse.model_validate(json.loads(video_store.adapt("mind_map", stored)["payload"]))
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
    if Path(filename).name != filename or any(char in filename for char in ("/", "\\", ":", "\x00")):
        raise HTTPException(422, "文件名无效。")
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
    try:
        with public_get(url, headers={"User-Agent": "VideoDream/1.0", "Referer": _thumbnail_referer(url)}, stream=True, timeout=12) as result:
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
        raise HTTPException(status_code=502, detail=_clean_error(exc, url)) from exc
    return Response(content=bytes(content), media_type=media_type)


def cancel_user_tasks(uid: int, key: str | None = None):
    cancelled = []
    for collection, lock in ((tasks, tasks_lock), (summary_tasks, summary_tasks_lock), (mind_map_tasks, mind_map_tasks_lock)):
        with lock:
            for task in collection.values():
                if task.user_id == uid and (key is None or task.task_id == key or getattr(task, "summary_task_id", None) == key) and task.status in {"pending", "running"}:
                    task.cancel.set()
                    task.status, task.message = "cancelled", "账号已停用或任务已取消"
                    if isinstance(task, DownloadTask) and task.process and task.process.poll() is None:
                        task.process.kill()
                    cancelled.append(task)
    with music_batches_lock:
        for batch in music_batches.values():
            if batch.get("user_id") == uid and (key is None or batch["batch_id"] == key):
                batch["stop"].set()
                batch["status"] = "cancelled"
                for entry in batch["items"]:
                    if entry["task"].status in {"pending", "running"}:
                        entry["cancelled"] = True
                        entry["task"].cancel.set()
    for task in cancelled:
        _persist_task(task, "download" if isinstance(task, DownloadTask) else "summary" if isinstance(task, SummaryTask) else "mind_map", force=True)


accounts.cancel_callbacks.append(cancel_user_tasks)
accounts.cancel_callbacks.append(browser_sessions.cancel_user)
accounts.cancel_callbacks.append(music_agent_runtime.cancel_user)


@app.on_event("shutdown")
def close_browser_sessions():
    browser_sessions.shutdown()
    video_history.stop_worker()


@app.on_event("startup")
def restore_task_state():
    video_history.start_worker()
    try:
        video_history.restore_interrupted()
    except video_cache.redis.RedisError:
        logger.warning("Redis unavailable; interrupted task reconciliation deferred")


@app.get("/auth-worker.js", include_in_schema=False)
def authentication_worker():
    return FileResponse(ROOT_DIR / "frontend" / "public" / "auth-worker.js", media_type="text/javascript", headers={"Cache-Control": "no-cache"})
