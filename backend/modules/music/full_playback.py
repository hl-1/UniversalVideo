"""Prepare a complete recording before exposing it to the native audio player."""
from __future__ import annotations

import math
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from backend.core import paths, storage
from backend.infrastructure import video_network
from backend.infrastructure.audio import probe_audio
from backend.infrastructure.runtime import executable_name


PREPARATION_SECONDS = 90
MAX_BYTES = 64 * 1024 * 1024


class PreparationExpired(RuntimeError):
    pass


@dataclass
class PreparedAudio:
    path: Path
    media_type: str
    duration: float
    source: str
    source_url: str
    media_url: str

    def close(self):
        shutil.rmtree(self.path.parent, ignore_errors=True)


def _normalize(value: str) -> str:
    return re.sub(r"[\W_]+", "", value.casefold())


def same_recording(original, candidate) -> bool:
    """Keep version words (Live, remaster, etc.) and require the same artists."""
    return bool(original.artist and original.duration and candidate.duration
                and _normalize(original.title) == _normalize(candidate.title)
                and _normalize(original.artist) == _normalize(candidate.artist)
                and abs(original.duration - candidate.duration) <= max(3, original.duration * .03))


def _cleanup_stale(root: Path, ttl: int):
    # Also reclaim files left by a process restart; never follow user symlinks.
    cutoff = time.time() - ttl - PREPARATION_SECONDS - 60
    for directory in root.glob("music-playback-*"):
        try:
            if directory.is_dir() and not directory.is_symlink() and directory.stat().st_mtime < cutoff:
                shutil.rmtree(directory, ignore_errors=True)
        except FileNotFoundError:
            pass  # Another request may have just reclaimed the same expired directory.


def _check_duration(actual: float, expected: float):
    if not math.isfinite(actual) or actual <= 0 or abs(actual - expected) > max(3, expected * .03):
        raise RuntimeError("音源不完整或歌曲版本不匹配")


def prepare(track) -> PreparedAudio:
    from backend.modules.music import sources as music

    expected = track.duration
    if not expected or not math.isfinite(expected) or expected <= 0:
        raise RuntimeError("歌曲原始时长未知，无法确认完整音源，请重新搜索后播放。")
    root = storage.directory("temp")
    _cleanup_stale(root, music.TOKEN_TTL)
    folder = Path(tempfile.mkdtemp(prefix="music-playback-", dir=root))
    path = folder / "audio.media"
    deadline = time.monotonic() + PREPARATION_SECONDS
    prepared = None
    seen_urls = set()

    def remaining():
        seconds = deadline - time.monotonic()
        if seconds <= 0:
            raise PreparationExpired("完整音源获取超时，请稍后重试。")
        return seconds

    def validate(url, options, source, candidate):
        nonlocal prepared
        remaining()
        if url in seen_urls:
            raise RuntimeError("已排除重复的不完整音源")
        seen_urls.add(url)
        path.unlink(missing_ok=True)
        headers = {**options.get("http_headers", {}), "Accept-Encoding": "identity"}
        with video_network.get(url, referer=candidate.webpage_url, headers=headers,
                               stream=True, timeout=(5, min(5, remaining()))) as response:
            content_type = response.headers.get("Content-Type", "").split(";", 1)[0].lower()
            if response.status_code != 200 or not (content_type.startswith("audio/") or content_type in {
                "video/mp4", "application/octet-stream", "application/ogg",
            }):
                raise RuntimeError("音源没有返回完整音频文件")
            length = int(response.headers.get("Content-Length") or 0)
            if length > MAX_BYTES:
                raise RuntimeError("音源超过完整播放校验的 64 MiB 上限")
            received = 0
            with path.open("wb") as output:
                for chunk in response.iter_content(256 * 1024):
                    remaining()
                    received += len(chunk)
                    if received > MAX_BYTES:
                        raise RuntimeError("音源超过完整播放校验的 64 MiB 上限")
                    output.write(chunk)
            if not received or length and received != length:
                raise RuntimeError("音源传输不完整")
        remaining()
        info = probe_audio(path, paths.FFMPEG_DIR)
        _check_duration(info["duration"], expected)
        # A container can advertise the full duration even when its payload is short.
        command = [str(paths.FFMPEG_DIR / executable_name("ffmpeg")), "-v", "error", "-xerror",
                   "-nostdin", "-i", str(path), "-map", "0:a:0", "-vn", "-progress", "pipe:1",
                   "-nostats", "-f", "null", "-"]
        decoded = subprocess.run(command, capture_output=True, text=True, timeout=min(30, remaining()))
        times = re.findall(r"^out_time_us=(\d+)$", decoded.stdout, flags=re.MULTILINE)
        if decoded.returncode or not times:
            raise RuntimeError("音源无法完整解码")
        _check_duration(max(map(int, times)) / 1_000_000, expected)
        remaining()
        if not content_type.startswith("audio/"):
            codec = info["codec"]
            content_type = {"mp3": "audio/mpeg", "flac": "audio/flac", "opus": "audio/ogg",
                            "vorbis": "audio/ogg", "aac": "audio/mp4"}.get(codec, content_type)
        prepared = PreparedAudio(path, content_type, info["duration"], source, candidate.webpage_url, url)

    def try_track(candidate):
        remaining()
        try:
            music.resolve(candidate, {"http_headers": music.HEADERS}, public_only=True,
                          validate_source=lambda url, options, source: validate(url, options, source, candidate))
        except PreparationExpired:
            raise
        except Exception as exc:
            from backend.infrastructure.network import raise_if_guarded
            raise_if_guarded(exc)
        return prepared

    try:
        if try_track(track):
            return prepared
        # Search only after exhausting the selected platform's parser and backup.
        for provider in ("qq", "netease", "kugou"):
            if provider == track.provider:
                continue
            remaining()
            try:
                candidates = music._search(provider, f"{track.title} {track.artist}")
            except Exception as exc:
                from backend.infrastructure.network import raise_if_guarded
                raise_if_guarded(exc)
                continue
            matches = [candidate for candidate in candidates if same_recording(track, candidate)]
            for candidate in matches[:2]:
                if try_track(candidate):
                    return prepared
        raise RuntimeError("未找到可验证的完整歌曲，已尝试备用音源，请稍后重试。")
    finally:
        if prepared is None:
            shutil.rmtree(folder, ignore_errors=True)
        else:
            # Session TTL begins after validation, not when its download began.
            folder.touch()
