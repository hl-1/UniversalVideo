from __future__ import annotations

from typing import Any
from urllib.parse import quote, urlparse

from backend.modules.video import douyin as video_douyin
from backend.modules.video import metadata as video_metadata
from backend.modules.video import resources as video_resources
from backend.modules.video import schemas as video_schemas
from backend.modules.video import sniffing as video_sniffing
from backend.modules.video import thumbnails as video_thumbnails
from backend.modules.video import urls as video_urls


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


def _pornhub_player_response(url: str, page_meta: dict[str, Any], player_data: dict[str, Any]) -> video_schemas.ParseResponse | None:
    definitions = player_data.get("mediaDefinitions")
    if not isinstance(definitions, list):
        return None

    formats: list[video_schemas.FormatInfo] = []
    seen: set[str] = set()
    def sort_key(item: dict[str, Any]) -> tuple[int, int]:
        height = video_thumbnails._safe_int(item.get("height")) or video_thumbnails._safe_int(item.get("quality")) or 0
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
        if not video_resources._is_signed_pornhub_media_url(media_url):
            continue
        seen.add(media_url)
        ext = "mp4" if item.get("format") == "mp4" else video_sniffing._media_ext(media_url)
        quality = item.get("quality") or item.get("height") or "自动"
        label = f"{quality}P · HLS" if ext == "m3u8" else f"{quality}P · MP4"
        formats.append(
            video_schemas.FormatInfo(
                format_id=f"sniff:{quote(media_url, safe='')}",
                label=label,
                ext="mp4" if ext == "m3u8" else ext,
                resolution=f"{quality}p" if str(quality).isdigit() else "自动",
            )
        )

    if not formats:
        return None

    return video_schemas.ParseResponse(
        title=video_metadata._clean_meta_text(player_data.get("title")) or page_meta.get("title") or "PornHub 视频",
        thumbnail=video_thumbnails._thumbnail_url(player_data.get("thumbnail") or page_meta.get("thumbnail")),
        duration=video_thumbnails._safe_int(player_data.get("duration")) or page_meta.get("duration"),
        uploader=page_meta.get("author") or "PornHub",
        webpage_url=url,
        formats=formats[:8],
    )


def _extract_douyin_response_meta(response: Any, video_id: str | None) -> dict[str, Any]:
    if not video_id or not video_urls._is_douyin_url(response.url) or "aweme/detail" not in urlparse(response.url).path or response.status != 200:
        return {}
    try:
        item = response.json().get("aweme_detail")
        if not isinstance(item, dict) or str(item.get("aweme_id")) != video_id:
            return {}
        video = item.get("video") or {}
        milliseconds = video_thumbnails._safe_int(video.get("duration"))
        metadata = {
            "title": video_metadata._clean_meta_text(item.get("desc")),
            "author": video_metadata._clean_author_text((item.get("author") or {}).get("nickname")),
            "duration": int(milliseconds / 1000) if milliseconds and milliseconds > 0 else None,
            "thumbnail": video_douyin._pick_url_from_nested(video, "cover") or video_douyin._pick_url_from_nested(video, "origin_cover"),
        }
        return {key: value for key, value in metadata.items() if value}
    except Exception:
        return {}
