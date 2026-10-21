from __future__ import annotations

from backend.core import storage

import hashlib
import re
import subprocess
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

from backend.modules.accounts import service as accounts
from backend.modules.music import library
from backend.modules.music import sources as music
from backend.infrastructure import network
from backend.infrastructure.audio import probe_audio
from backend.infrastructure.runtime import executable_name


def normalize(value):
    return re.sub(r"[\W_]+", "", str(value or "").casefold())


def same_identity(first, second):
    return (first["provider"] == second["provider"] and first["identifier"] == second["identifier"]
            and normalize(first["title"]) == normalize(second["title"])
            and normalize(first["artist"]) == normalize(second["artist"])
            and (not first.get("duration") or not second.get("duration")
                 or abs(first["duration"]-second["duration"]) <= max(3, first["duration"]*.03)))


def local_path(track):
    if not track.get("filename"):
        return None
    name = track["filename"]
    if Path(name).name != name or any(c in name for c in "\\/:"):
        raise ValueError("本地歌曲路径无效")
    accounts.resource("file", name)
    path = storage.resource_path("file", name)
    if not path.is_file():
        return None
    return path


@contextmanager
def audio_file(track, charge, allow_network, check=None):
    local = local_path(track)
    if local:
        yield local, "本地完整文件"
        return
    if not allow_network:
        raise RuntimeError("禁止联网且没有本地完整音频")
    if track["provider"] not in {"netease", "qq", "kugou"}:
        raise RuntimeError("此来源没有可校验的完整音频")
    registered = music.Track.model_validate(track)
    url, options, source = music.resolve(registered, {"http_headers": music.HEADERS, "socket_timeout": 10}, public_only=True)
    with tempfile.TemporaryDirectory(prefix="videodream-agent-") as folder:
        path = Path(folder)/"audio.media"
        with network.public_get(url, headers=options.get("http_headers"), stream=True, timeout=(5, 15)) as response:
            response.raise_for_status()
            total = 0
            started = time.monotonic()
            digest = hashlib.sha256()
            with path.open("wb") as output:
                for chunk in response.iter_content(256*1024):
                    if check:
                        check()
                    if time.monotonic()-started > 45:
                        raise RuntimeError("音频校验下载超时")
                    total += len(chunk)
                    if total > 32*1024*1024:
                        raise RuntimeError("完整音频超过校验的 32 MiB 上限")
                    digest.update(chunk)
                    output.write(chunk)
        if response.headers.get("Content-Length") and total != int(response.headers["Content-Length"]):
            raise RuntimeError("音频传输不完整")
        yield path, source


def validate(track, charge, allow_network, check=None):
    result = {"checked_at": time.time(), "playable": False, "full": False, "identity": False, "reason": "", "duration": None}
    try:
        if track["provider"] in {"netease", "qq", "kugou"} and allow_network:
            canonical = music.parse_url(track["webpage_url"]).model_dump()
            if not same_identity(track, canonical):
                result["reason"] = "平台身份或歌曲版本不匹配"
                return result
            result["identity"] = True
        elif local_path(track):
            result["identity"] = True
        else:
            result["reason"] = "无法在当前网络设置下核对歌曲身份"
            return result
        with audio_file(track, charge, allow_network, check) as (path, source):
            info = probe_audio(path, library.FFMPEG_DIR)
            duration = info["duration"]
            result.update(playable=True, duration=round(duration, 3), source=source)
            expected = track.get("duration")
            if source == "本地完整文件" and not expected:
                expected = duration
            if not expected or expected <= 0:
                result["reason"] = "原始时长未知，无法证明完整播放"
            elif duration < expected-max(3, expected*.03):
                result["reason"] = "音源仅试听或不完整"
            elif duration > expected+max(3, expected*.03):
                result["reason"] = "音源时长与歌曲版本不匹配"
                result["identity"] = False
            else:
                # Decode to the end as well as checking the container duration.
                command = [str(library.FFMPEG_DIR/executable_name("ffmpeg")), "-v", "error", "-xerror", "-nostdin", "-i", str(path), "-map", "0:a:0", "-f", "null", "-"]
                decoded = subprocess.run(command, capture_output=True, timeout=30)
                if decoded.returncode:
                    result["reason"] = "音轨存在解码错误"
                else:
                    result.update(full=True, reason="完整音频已探测并校验时长")
    except Exception as exc:
        from backend.modules.music.agent.budget import BudgetExceeded
        from fastapi import HTTPException
        if isinstance(exc, (BudgetExceeded, HTTPException)):
            raise
        result["reason"] = str(exc)[:200]
    return result
