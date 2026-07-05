from __future__ import annotations

import re
import json
import hashlib
import html
import subprocess
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Literal
from urllib.parse import parse_qs, quote, unquote, urljoin, urlparse
from urllib.request import Request, urlopen

import requests
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from playwright.sync_api import sync_playwright
from pydantic import BaseModel, Field, HttpUrl
from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError


ROOT_DIR = Path(__file__).resolve().parent.parent
DOWNLOAD_DIR = ROOT_DIR / "downloads"
THUMBNAIL_DIR = DOWNLOAD_DIR / "_thumbnails"
CONFIG_DIR = ROOT_DIR / "config"
FFMPEG_DIR = Path(r"C:\softWare\environment\ffmpeg")
NODE_EXE = Path(r"C:\softWare\environment\nodejs\node.exe")
CHROME_EXE = Path(r"C:\Users\11871\AppData\Local\Google\Chrome\Application\chrome.exe")
EDGE_EXE = Path(r"C:\Program Files (x86)\Microsoft\EdgeCore\126.0.2592.113\msedge.exe")
BILIBILI_COOKIES_FILE = CONFIG_DIR / "bilibili-cookies.txt"
PORNHUB_COOKIES_FILE = CONFIG_DIR / "pornhub-cookies.txt"
YOUTUBE_COOKIES_FILE = CONFIG_DIR / "youtube-cookies.txt"
DOUYIN_COOKIES_FILE = CONFIG_DIR / "douyin-cookies.txt"
DOWNLOAD_DIR.mkdir(exist_ok=True)
THUMBNAIL_DIR.mkdir(exist_ok=True)
CONFIG_DIR.mkdir(exist_ok=True)

TaskStatus = Literal["pending", "running", "finished", "failed"]
BrowserCookieSource = Literal["auto", "chrome", "edge", "firefox", "brave", "vivaldi"]
BrowserCookieProvider = Literal["chrome", "edge", "firefox", "brave", "vivaldi"]


class ParseRequest(BaseModel):
    url: HttpUrl
    use_browser_cookies: bool = False
    browser: BrowserCookieSource = "auto"


class DownloadRequest(BaseModel):
    url: HttpUrl
    format_id: str | None = Field(default=None, max_length=4096)
    use_browser_cookies: bool = False
    browser: BrowserCookieSource = "auto"


class CookiesRequest(BaseModel):
    platform: Literal["bilibili", "youtube", "pornhub", "douyin"]
    content: str = Field(min_length=20)


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
        use_browser_cookies: bool = False,
        browser: BrowserCookieSource = "auto",
    ) -> None:
        self.task_id = task_id
        self.url = normalize_url(url)
        self.format_id = format_id
        self.use_browser_cookies = use_browser_cookies
        self.browser = browser
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


app = FastAPI(title="VideoDream API", version="0.1.0")
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
executor = ThreadPoolExecutor(max_workers=2)
sniff_cache: dict[str, tuple[float, ParseResponse]] = {}
sniff_cache_lock = threading.Lock()
SNIFF_CACHE_TTL_SECONDS = 600

COOKIE_FILES = {
    "bilibili": BILIBILI_COOKIES_FILE,
    "youtube": YOUTUBE_COOKIES_FILE,
    "pornhub": PORNHUB_COOKIES_FILE,
    "douyin": DOUYIN_COOKIES_FILE,
}

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
            "可以开启前端“本机授权模式”，让系统在你的电脑上读取已登录浏览器状态后重试。"
        )
    if url and _is_pornhub_url(url) and "410" in text:
        return (
            "目标站点返回 410 Gone，表示该视频页面已删除、下架、地区不可访问，"
            "或需要站点侧登录/年龄校验后才可访问。系统已补充常规浏览器请求头；"
            "如果你确认浏览器中能正常打开，请导出你本人账号的 cookies 到 "
            "config/pornhub-cookies.txt 后重试。"
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
            "YouTube 要求确认不是机器人或需要登录态。请导出你本人浏览器中的 YouTube cookies "
            "到 config/youtube-cookies.txt，或开启前端“本机授权模式”后重试。"
        )
    if url and _is_douyin_url(url) and "cookies" in text.lower():
        return (
            "抖音公开视频解析失败。系统已先尝试专用公开解析模块，再回退 yt-dlp；"
            "如果仍提示 fresh cookies，通常是平台签名参数、风控或访问权限变化导致。"
            "可以开启前端“本机授权模式”后重试。"
        )
    if url and _is_douyin_url(url) and ("encrypt_data" in text.lower() or "11110" in text):
        return "抖音公开接口要求加密参数，当前链接无法通过旧公开 API 直接解析，系统会继续尝试 yt-dlp 兜底。"
    return text or "处理失败，请确认链接是否公开可访问。"


def _validate_cookie_content(content: str) -> str:
    normalized = content.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized:
        raise HTTPException(status_code=400, detail="cookies 内容不能为空。")
    if "\t" not in normalized and "# Netscape HTTP Cookie File" not in normalized:
        raise HTTPException(
            status_code=400,
            detail="请粘贴 Netscape 格式 cookies 文件内容，而不是浏览器 Cookie 请求头。",
        )
    if "# Netscape HTTP Cookie File" not in normalized:
        normalized = "# Netscape HTTP Cookie File\n" + normalized
    return normalized + "\n"


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
    for candidate in (CHROME_EXE, EDGE_EXE):
        if candidate.exists():
            return candidate
    return None


def _is_probable_video_resource(url: str) -> bool:
    lower = html.unescape(url).lower()
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
    if ".m3u8" in lower:
        return True
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
    return itag in {"18", "22"} or query.get("ratebypass", [""])[0].lower() == "yes"


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


def parse_browser_sniffed_page(url: str) -> ParseResponse:
    executable = _browser_executable()
    if not executable:
        raise HTTPException(status_code=422, detail="没有找到可用于嗅探的本机浏览器。")

    captured: list[str] = []
    page_meta: dict[str, Any] = {}
    page_html = ""
    normalized_url = normalize_url(url)
    cached = _get_sniff_cache(normalized_url)
    if cached:
        return cached
    is_youtube_page = _is_youtube_url(normalized_url)
    is_douyin_page = _is_douyin_url(normalized_url)
    is_pornhub_page = _is_pornhub_url(normalized_url)

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=True,
            executable_path=str(executable),
            args=[
                "--disable-blink-features=AutomationControlled",
                "--autoplay-policy=no-user-gesture-required",
                "--disable-extensions",
                "--disable-notifications",
                "--mute-audio",
            ],
        )
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
            page_html = page.content()
        finally:
            browser.close()

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
            elif _is_youtube_audio_stream(media_url):
                label = "M4A · 音频"
            elif _is_youtube_video_stream(media_url):
                label = "MP4 · 视频"
            else:
                label = "媒体资源"
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


def _ydl_base_options(
    url: str | None = None,
    use_browser_cookies: bool = False,
    browser: BrowserCookieSource = "auto",
) -> dict[str, Any]:
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
    if use_browser_cookies and browser != "auto":
        options["cookiesfrombrowser"] = (browser,)
    if url and _is_bilibili_url(url):
        options["http_headers"].update(
            {
                "Origin": "https://www.bilibili.com",
                "Referer": "https://www.bilibili.com/",
            }
        )
        if BILIBILI_COOKIES_FILE.exists() and not use_browser_cookies:
            options["cookiefile"] = str(BILIBILI_COOKIES_FILE)
    if url and _is_pornhub_url(url):
        options["http_headers"].update(
            {
                "Origin": "https://www.pornhub.com",
                "Referer": "https://www.pornhub.com/",
                "Accept-Language": "en-US,en;q=0.9",
            }
        )
        if PORNHUB_COOKIES_FILE.exists() and not use_browser_cookies:
            options["cookiefile"] = str(PORNHUB_COOKIES_FILE)
    if url and _is_youtube_url(url):
        options["http_headers"].update(
            {
                "Origin": "https://www.youtube.com",
                "Referer": "https://www.youtube.com/",
                "Accept-Language": "en-US,en;q=0.9",
            }
        )
        options["extractor_args"] = {"youtube": {"player_client": ["web", "android"]}}
        if YOUTUBE_COOKIES_FILE.exists() and not use_browser_cookies:
            options["cookiefile"] = str(YOUTUBE_COOKIES_FILE)
    if url and _is_douyin_url(url):
        options["http_headers"].update(
            {
                "Origin": "https://www.douyin.com",
                "Referer": "https://www.douyin.com/",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            }
        )
        if DOUYIN_COOKIES_FILE.exists() and not use_browser_cookies:
            options["cookiefile"] = str(DOUYIN_COOKIES_FILE)
    return options


def parse_video(
    url: str,
    use_browser_cookies: bool = False,
    browser: BrowserCookieSource = "auto",
) -> ParseResponse:
    url = normalize_url(url)
    if _is_douyin_url(url) and not use_browser_cookies:
        try:
            return parse_browser_sniffed_page(url)
        except HTTPException:
            try:
                return parse_douyin_public(url)
            except HTTPException:
                pass

    options = {
        **_ydl_base_options(url, use_browser_cookies, browser),
        "skip_download": True,
    }
    try:
        if _is_pornhub_url(url) and not use_browser_cookies:
            try:
                return parse_browser_sniffed_page(url)
            except HTTPException:
                pass
        if _is_youtube_url(url) and not use_browser_cookies:
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
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="ignore",
            )
            while process.poll() is None:
                _set_task(task, progress=min(95.0, task.progress + 1.5), message="正在合并 HLS 视频流")
                time.sleep(1)
            if process.returncode != 0:
                stderr = process.stderr.read() if process.stderr else ""
                raise RuntimeError(stderr.strip() or "ffmpeg 下载 HLS 失败。")
        else:
            media_headers = {
                **DEFAULT_HEADERS,
                "Referer": task.url,
            }
            with requests.get(media_url, headers=media_headers, stream=True, timeout=(10, 90)) as response:
                response.raise_for_status()
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

    if _is_douyin_url(task.url) and not task.use_browser_cookies and _run_douyin_public_download(task):
        return

    _set_task(task, status="running", progress=1.0, message="正在连接视频源")
    output_template = str(DOWNLOAD_DIR / f"{task.task_id}.%(title).80s.%(ext)s")
    format_value = task.format_id or "bestvideo+bestaudio/best"
    options = {
        **_ydl_base_options(task.url, task.use_browser_cookies, task.browser),
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
    return parse_video(str(payload.url), payload.use_browser_cookies, payload.browser)


@app.post("/api/download", response_model=DownloadResponse)
def download_endpoint(payload: DownloadRequest) -> DownloadResponse:
    task_id = uuid.uuid4().hex[:12]
    task = DownloadTask(
        task_id=task_id,
        url=str(payload.url),
        format_id=payload.format_id,
        use_browser_cookies=payload.use_browser_cookies,
        browser=payload.browser,
    )
    with tasks_lock:
        tasks[task_id] = task
    executor.submit(_run_download, task)
    return DownloadResponse(task_id=task_id)


@app.post("/api/cookies")
def save_cookies_endpoint(payload: CookiesRequest) -> dict[str, str]:
    cookie_file = COOKIE_FILES[payload.platform]
    content = _validate_cookie_content(payload.content)
    cookie_file.write_text(content, encoding="utf-8")
    return {"status": "ok", "platform": payload.platform}


@app.get("/api/cookies/status")
def cookies_status_endpoint() -> dict[str, bool]:
    return {platform: path.exists() and path.stat().st_size > 0 for platform, path in COOKIE_FILES.items()}


@app.get("/api/tasks/{task_id}", response_model=TaskResponse)
def task_endpoint(task_id: str) -> TaskResponse:
    with tasks_lock:
        task = tasks.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在或服务已重启。")
    return task.to_response()


@app.get("/api/files/{filename}")
def file_endpoint(filename: str) -> FileResponse:
    safe_name = Path(unquote(filename)).name
    file_path = DOWNLOAD_DIR / safe_name
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(status_code=404, detail="文件不存在。")
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
