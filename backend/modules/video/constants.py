from __future__ import annotations

import re
from typing import Literal


TaskStatus = Literal["pending", "running", "finished", "failed", "cancelled"]
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
