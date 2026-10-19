from __future__ import annotations

import re
import html
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from backend.infrastructure import runtime
from backend.modules.video import constants as video_constants


def _browser_executable() -> Path | None:
    return runtime.browser_executable()


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
    return bool(video_constants.VIDEO_CDN_PATTERN.search(lower))


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
