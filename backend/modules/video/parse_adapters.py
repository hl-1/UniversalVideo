"""Platform capabilities; scheduling and limits remain in the common parser."""
from dataclasses import dataclass

from backend.infrastructure.ytdlp import is_bilibili_video_url
from backend.modules.video import browser, douyin, sniffing, urls


@dataclass(frozen=True)
class Adapter:
    routes: tuple
    definitive_extractor_error: bool = False


def select(url, extractor):
    if sniffing._looks_like_media_url(url):
        return Adapter((("direct", sniffing.parse_sniffed_page),))
    if urls._is_douyin_url(url):
        # This adapter owns verification and the account's existing browser page.
        return Adapter((("browser", browser.parse_browser_sniffed_page),
                        ("public_api", douyin.parse_douyin_public),
                        ("extractor", extractor), ("html", sniffing.parse_sniffed_page)))
    if is_bilibili_video_url(url):
        # Its extractor already owns API/webpage fallback and exact part selection.
        return Adapter((("extractor", extractor),), definitive_extractor_error=True)
    return Adapter((("extractor", extractor), ("html", sniffing.parse_sniffed_page),
                    ("browser", browser.parse_browser_sniffed_page)))
