from __future__ import annotations

import json
import math
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Callable, Literal

import requests
from yt_dlp import YoutubeDL

from backend.runtime import executable_name


AudioFormat = Literal["m4a", "mp3"]
Progress = Callable[[float, str], None]


class IncompleteAudioError(RuntimeError):
    pass


def probe_audio(path: Path, ffmpeg_dir: Path) -> dict:
    result = subprocess.run(
        [str(ffmpeg_dir / executable_name("ffprobe")), "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
    )
    if result.returncode:
        raise RuntimeError("无法读取音频，音源可能返回了错误页面或不完整文件。")
    data = json.loads(result.stdout)
    streams = data.get("streams") or []
    audio = next((stream for stream in streams if stream.get("codec_type") == "audio"), None)
    duration = float(data.get("format", {}).get("duration") or (audio or {}).get("duration") or 0)
    if not audio or not math.isfinite(duration) or duration <= 0:
        raise RuntimeError("资源中没有有效音轨。")
    return {"codec": audio.get("codec_name"), "duration": duration, "streams": streams}


def export_audio(source: Path, target: Path, ffmpeg_dir: Path, expected_duration: float | None = None) -> None:
    info = probe_audio(source, ffmpeg_dir)
    if expected_duration and info["duration"] < expected_duration - max(3, expected_duration * 0.03):
        raise IncompleteAudioError("音源时长短于原内容，可能仅提供试听，未保存为完整音频。")
    if expected_duration and info["duration"] > expected_duration + max(3, expected_duration * 0.03):
        raise RuntimeError("音源时长与原内容不符，可能返回了其他版本，未保存文件。")
    codec = ["-c:a", "copy"] if target.suffix == ".m4a" and info["codec"] == "aac" else (
        ["-c:a", "aac", "-b:a", "192k"] if target.suffix == ".m4a" else ["-c:a", "libmp3lame", "-q:a", "2"]
    )
    partial = target.with_name(f"{target.stem}.partial{target.suffix}")
    try:
        result = subprocess.run(
            [str(ffmpeg_dir / executable_name("ffmpeg")), "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
             "-i", str(source), "-map", "0:a:0", "-vn", *codec, str(partial)],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300,
        )
        if result.returncode:
            raise RuntimeError("音轨导出失败，请更换音频格式或重新解析。")
        output = probe_audio(partial, ffmpeg_dir)
        if any(stream.get("codec_type") == "video" for stream in output["streams"]):
            raise RuntimeError("导出结果仍包含视频轨。")
        if abs(output["duration"] - info["duration"]) > max(2, info["duration"] * 0.01):
            raise RuntimeError("导出音轨不完整，请重新下载。")
        partial.replace(target)
    finally:
        partial.unlink(missing_ok=True)


def download_audio(
    *, url: str, title: str, task_id: str, output_format: AudioFormat, download_dir: Path,
    ffmpeg_dir: Path, options: dict, progress: Progress, direct: bool = False,
    expected_duration: float | None = None,
) -> Path:
    if not all((ffmpeg_dir / executable_name(name)).is_file() for name in ("ffmpeg", "ffprobe")):
        raise RuntimeError("音轨下载需要 ffmpeg 和 ffprobe，请检查安装目录。")
    stem = re.sub(r'[\\/:*?"<>|\s]+', " ", title).strip()[:80] or "audio"
    target = download_dir / f"{task_id}.{stem}.{output_format}"
    with tempfile.TemporaryDirectory(prefix=f"audio-{task_id}-", dir=download_dir) as working:
        work = Path(working)
        progress(3, "正在连接音源")
        if direct and ".m3u8" not in url.lower() and ".mpd" not in url.lower():
            source = work / "source.media"
            with requests.get(url, headers=options.get("http_headers"), stream=True, timeout=(10, 60)) as response:
                response.raise_for_status()
                content_type = response.headers.get("Content-Type", "").lower()
                if any(content_type.startswith(prefix) for prefix in ("text/", "image/", "application/json")):
                    raise RuntimeError("音源返回了网页、图片或错误信息，无法下载。")
                total = int(response.headers.get("Content-Length") or 0)
                downloaded = 0
                with source.open("wb") as output:
                    for chunk in response.iter_content(256 * 1024):
                        if chunk:
                            output.write(chunk)
                            downloaded += len(chunk)
                            if downloaded > 1024 * 1024 * 1024:
                                raise RuntimeError("音源超过 1 GB，请选择较小的资源。")
                            progress(min(84, 4 + downloaded / total * 80) if total else 10, "正在下载音轨源文件")
                if total and downloaded != total:
                    raise RuntimeError("音源下载不完整，请重试。")
        else:
            def require_recording(info: dict, *, incomplete: bool = False):
                if info.get("is_live"):
                    raise RuntimeError("暂不支持直播音轨，请使用已发布的视频。")
                if info.get("has_drm"):
                    raise RuntimeError("音源受加密保护，无法导出。")
                return None

            def hook(data: dict) -> None:
                if data.get("status") == "downloading":
                    total = data.get("total_bytes") or data.get("total_bytes_estimate")
                    progress(min(84, 4 + data.get("downloaded_bytes", 0) / total * 80) if total else 10, "正在下载音轨源文件")

            ydl_options = {
                **options, "format": "bestaudio/best", "noplaylist": True,
                "outtmpl": str(work / "source.%(ext)s"), "progress_hooks": [hook],
                "ffmpeg_location": str(ffmpeg_dir), "max_filesize": 1024 * 1024 * 1024,
                "match_filter": require_recording,
            }
            with YoutubeDL(ydl_options) as ydl:
                info = ydl.extract_info(url, download=True)
            if not isinstance(info, dict) or info.get("_type") in ("playlist", "multi_video"):
                raise RuntimeError("请使用单个视频或歌曲链接。")
            if info.get("is_live"):
                raise RuntimeError("暂不支持直播音轨，请使用已发布的视频。")
            candidates = [path for path in work.glob("source.*") if path.suffix not in (".part", ".ytdl", ".json")]
            if len(candidates) != 1:
                raise RuntimeError("下载结束但没有取得完整音轨文件。")
            source = candidates[0]
            expected_duration = expected_duration or info.get("duration")
        progress(88, "正在提取音轨并校验时长")
        export_audio(source, target, ffmpeg_dir, expected_duration)
    return target
