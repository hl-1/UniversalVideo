from __future__ import annotations

from typing import Any

from backend.modules.video import schemas as video_schemas
from backend.modules.video import thumbnails as video_thumbnails
from backend.infrastructure import platform_access


def pornhub_selector(height: int, extension: str = "mp4", hls: bool = True) -> str:
    # HLS IDs contain measured bandwidth and can change on the next extraction.
    # Keep the requested quality, while allowing the same quality's HTTP backup.
    base = f"best[height={height}][ext={extension}]"
    return f"{base}[protocol^=m3u8]/{base}" if hls else f"{base}[protocol^=http]/{base}"


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


def _extract_formats(info: dict[str, Any]) -> list[video_schemas.FormatInfo]:
    candidates: list[video_schemas.FormatInfo] = []
    seen: set[str] = set()
    items = info.get("formats") or ([info] if info.get("url") else [])
    for item in items:
        raw_format_id = str(item.get("format_id") or ("best" if item is info else ""))
        if not raw_format_id or raw_format_id in seen:
            continue
        if item.get("vcodec") == "none" and item.get("acodec") == "none":
            continue
        seen.add(raw_format_id)

        has_video = item.get("vcodec") not in (None, "none")
        has_audio = item.get("acodec") not in (None, "none")
        format_id = raw_format_id
        label = _format_label(item)
        if (platform_access.platform(info.get("webpage_url")) == "pornhub" or str(info.get("extractor_key", "")).lower() == "pornhub") and item.get("height") and item.get("ext") in {"mp4", "webm"}:
            format_id = pornhub_selector(int(item["height"]), item["ext"], str(item.get("protocol", "")).startswith("m3u8"))
            label += " · HLS" if str(item.get("protocol", "")).startswith("m3u8") else " · HTTP"
        elif has_video and not has_audio:
            format_id = f"{raw_format_id}+bestaudio/best"
            label = f"{label} · 含最佳音频"

        candidates.append(
            video_schemas.FormatInfo(
                format_id=format_id,
                label=label,
                ext=item.get("ext"),
                resolution=item.get("resolution")
                or (f"{item.get('height')}p" if item.get("height") else None),
                filesize=video_thumbnails._safe_int(item.get("filesize") or item.get("filesize_approx")),
                has_video=has_video if item.get("vcodec") is not None else None,
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
