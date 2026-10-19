from __future__ import annotations

import re
import hashlib
import html
from typing import Any
from urllib.parse import urljoin

from backend.modules.accounts import service as accounts
from backend.core import paths
from backend.modules.video import thumbnails as video_thumbnails
from backend.modules.video import urls as video_urls


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
        target_path = paths.THUMBNAIL_DIR / filename
        videos = page.locator("video")
        index = videos.evaluate_all("""(videos) => videos.findIndex(video => video.currentSrc.startsWith('blob:') || video.currentSrc.includes('.douyinvod.com/'))""")
        locator = videos.nth(index) if index >= 0 else page.locator("body").first
        locator.screenshot(path=str(target_path), timeout=2000)
        if target_path.exists() and target_path.stat().st_size > 0:
            accounts.register_resource("thumbnail", filename, {"filename": filename, "size": target_path.stat().st_size})
            return video_thumbnails._local_thumbnail(filename)
    except Exception:
        try:
            filename = f"{hashlib.sha1(identity.encode('utf-8')).hexdigest()[:16]}.png"
            target_path = paths.THUMBNAIL_DIR / filename
            page.screenshot(path=str(target_path), timeout=2000, full_page=False)
            if target_path.exists() and target_path.stat().st_size > 0:
                accounts.register_resource("thumbnail", filename, {"filename": filename, "size": target_path.stat().st_size})
                return video_thumbnails._local_thumbnail(filename)
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
    if not author and video_urls._is_douyin_url(normalized_url):
        author = _extract_douyin_author_from_text(data.get("bodyText") if isinstance(data, dict) else None)
    duration = _parse_iso_duration(data.get("duration") if isinstance(data, dict) else None)
    if isinstance(thumbnail, str) and thumbnail:
        thumbnail = urljoin(normalized_url, html.unescape(thumbnail))
    else:
        thumbnail = None

    if not thumbnail and video_urls._is_youtube_url(normalized_url):
        video_id = video_urls._extract_youtube_video_id(normalized_url)
        if video_id:
            thumbnail = f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"

    return {"title": title, "thumbnail": thumbnail, "author": author, "duration": duration}
