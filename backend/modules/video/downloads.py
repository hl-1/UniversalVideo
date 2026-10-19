from __future__ import annotations

import logging


logger = logging.getLogger("videodream.summary")

import re
import hashlib
import subprocess
import time
from pathlib import Path
from typing import Any
from urllib.parse import unquote
import requests
from yt_dlp import YoutubeDL
from backend.modules.music import sources as music
from backend.modules.music.media import start_cover_backfill
from backend.modules.music import library
from backend.core import paths
from backend.infrastructure import audio
from backend.infrastructure import runtime
from backend.modules.video import constants as video_constants
from backend.modules.video import douyin as video_douyin
from backend.modules.video import parse_cache as video_parse_cache
from backend.modules.video import parsing as video_parsing
from backend.modules.video import resources as video_resources
from backend.modules.video import sniffing as video_sniffing
from backend.modules.video import tasks as video_tasks
from backend.modules.video import urls as video_urls


def _progress_hook(task: video_tasks.DownloadTask):
    def hook(payload: dict[str, Any]) -> None:
        status = payload.get("status")
        if status == "downloading":
            total = payload.get("total_bytes") or payload.get("total_bytes_estimate") or 0
            downloaded = payload.get("downloaded_bytes") or 0
            progress = min(99.0, downloaded / total * 100) if total else task.progress
            speed = payload.get("speed")
            speed_label = f" · {speed / 1024 / 1024:.1f} MB/s" if speed else ""
            video_tasks._set_task(task, status="running", progress=progress, message=f"正在下载{speed_label}")
        elif status == "finished":
            video_tasks._set_task(task, progress=99.0, message="下载完成，正在整理文件")

    return hook


def _run_douyin_public_download(task: video_tasks.DownloadTask) -> bool:
    video_tasks._set_task(task, status="running", progress=2.0, message="正在解析抖音公开视频")
    try:
        video_id, item = video_douyin._fetch_douyin_item(task.url)
        media_url = video_douyin._douyin_media_url(item)
        title = str(item.get("desc") or f"douyin_{video_id}")
        safe_title = re.sub(r'[\\/:*?"<>|\s]+', " ", title).strip()[:80] or f"douyin_{video_id}"
        target_path = paths.DOWNLOAD_DIR / f"{task.task_id}.{safe_title}.mp4"
        temp_path = target_path.with_suffix(".mp4.part")

        headers = {
            **video_constants.DOUYIN_HEADERS,
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
                        video_tasks._set_task(task, progress=progress, message="正在下载抖音视频")
                    else:
                        video_tasks._set_task(task, progress=min(95.0, task.progress + 1), message="正在下载抖音视频")

        temp_path.replace(target_path)
        video_tasks._set_task(
            task,
            status="finished",
            progress=100.0,
            message="文件已准备好",
            filename=target_path.name,
        )
        return True
    except Exception as exc:
        video_tasks._set_task(task, progress=max(task.progress, 3.0), message="抖音专用解析失败，正在尝试 yt-dlp")
        return False


def _run_sniffed_download(task: video_tasks.DownloadTask, media_url: str) -> None:
    if "phncdn.com" in media_url.lower() and not video_resources._is_signed_pornhub_media_url(media_url):
        video_tasks._set_task(
            task,
            status="failed",
            progress=max(task.progress, 1.0),
            message="下载失败",
            error="视频 CDN 返回 470 No Token，当前直链缺少签名 token。请粘贴原始视频页面链接重新解析后下载。",
        )
        return

    ext = video_sniffing._media_ext(media_url)
    output_ext = "mp4" if ext == "m3u8" else ext
    url_hash = hashlib.sha1(media_url.encode("utf-8")).hexdigest()[:8]
    target_path = paths.DOWNLOAD_DIR / f"{task.task_id}.sniffed-{url_hash}.{output_ext}"
    temp_path = target_path.with_suffix(f".{output_ext}.part")
    video_tasks._set_task(task, status="running", progress=2.0, message="正在下载嗅探到的视频资源")

    try:
        if ext == "m3u8":
            ffmpeg = paths.FFMPEG_DIR / runtime.executable_name("ffmpeg")
            if not ffmpeg.exists():
                raise RuntimeError("当前资源是 HLS/m3u8，需要先安装 ffmpeg。")
            command = [
                str(ffmpeg),
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-nostdin",
                "-reconnect",
                "1",
                "-reconnect_streamed",
                "1",
                "-reconnect_delay_max",
                "5",
                "-headers",
                f"User-Agent: {video_constants.DEFAULT_HEADERS['User-Agent']}\r\nReferer: {task.url}\r\n",
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
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="ignore",
            )
            started_at = time.time()
            task.process = process
            last_size = -1
            last_growth_at = started_at
            while process.poll() is None:
                if time.time() - started_at > 180:
                    process.kill()
                    raise RuntimeError("HLS 视频流下载超时，请改选 MP4 直链格式或较低清晰度后重试。")
                current_size = target_path.stat().st_size if target_path.exists() else 0
                if current_size > last_size:
                    last_size = current_size
                    last_growth_at = time.time()
                elif time.time() - last_growth_at > 30:
                    process.kill()
                    raise RuntimeError("HLS 视频流 30 秒没有写入数据，请重新解析后选择其他清晰度重试。")
                video_tasks._set_task(task, progress=min(95.0, task.progress + 1.5), message="正在合并 HLS 视频流")
                time.sleep(1)
            if process.returncode != 0:
                raise RuntimeError("ffmpeg 下载 HLS 失败，请重新解析后选择其他清晰度重试。")
        else:
            media_headers = {
                **video_constants.DEFAULT_HEADERS,
                "Referer": task.url,
            }
            with requests.get(media_url, headers=media_headers, stream=True, timeout=(10, 90)) as response:
                response.raise_for_status()
                content_type = response.headers.get("Content-Type", "").split(";", 1)[0].lower()
                if content_type.startswith("image/"):
                    raise RuntimeError("嗅探到的是图片资源，不是视频文件。请重新解析原视频页面后下载。")
                total = int(response.headers.get("Content-Length") or 0)
                downloaded = 0
                with temp_path.open("wb") as file_obj:
                    for chunk in response.iter_content(chunk_size=1024 * 256):
                        if not chunk:
                            continue
                        file_obj.write(chunk)
                        downloaded += len(chunk)
                        if total:
                            video_tasks._set_task(task, progress=min(99.0, downloaded / total * 100), message="正在下载直链视频")
                        else:
                            video_tasks._set_task(task, progress=min(95.0, task.progress + 1), message="正在下载直链视频")
            temp_path.replace(target_path)

        if not target_path.exists() or target_path.stat().st_size == 0:
            raise RuntimeError("嗅探资源下载结束，但没有生成有效文件。")
        with target_path.open("rb") as file_obj:
            header = file_obj.read(12)
        if (
            header.startswith(b"\xff\xd8\xff")
            or header.startswith(b"\x89PNG")
            or header.strip() in {b"[]", b"{}"}
            or target_path.stat().st_size < 1024
        ):
            target_path.unlink(missing_ok=True)
            raise RuntimeError("下载结果不是有效视频文件。请重新解析原视频页面，并选择 HLS 格式下载。")
        video_tasks._set_task(
            task,
            status="finished",
            progress=100.0,
            message="文件已准备好",
            filename=target_path.name,
        )
    except Exception as exc:
        if temp_path.exists():
            temp_path.unlink(missing_ok=True)
        video_tasks._set_task(
            task,
            status="failed",
            progress=max(task.progress, 1.0),
            message="下载失败",
            error=video_urls._clean_error(exc, task.url),
        )


def _run_sniffed_pair_download(task: video_tasks.DownloadTask, video_url: str, audio_url: str) -> None:
    target_path = paths.DOWNLOAD_DIR / f"{task.task_id}.sniffed-merged.mp4"
    video_tasks._set_task(task, status="running", progress=2.0, message="正在合并网页视频和音频")
    try:
        ffmpeg = paths.FFMPEG_DIR / runtime.executable_name("ffmpeg")
        if not ffmpeg.exists():
            raise RuntimeError("需要 ffmpeg 合并视频和音频。")
        headers = f"User-Agent: {video_constants.DEFAULT_HEADERS['User-Agent']}\r\nReferer: {task.url}\r\n"
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
            task.process = process
            video_tasks._set_task(task, progress=min(95.0, task.progress + 2), message="正在合并网页视频和音频")
            time.sleep(1)
        if process.returncode != 0:
            stderr = process.stderr.read() if process.stderr else ""
            raise RuntimeError(stderr.strip() or "ffmpeg 合并失败。")
        if not target_path.exists() or target_path.stat().st_size == 0:
            raise RuntimeError("合并结束，但没有生成有效文件。")
        video_tasks._set_task(
            task,
            status="finished",
            progress=100.0,
            message="文件已准备好",
            filename=target_path.name,
        )
    except Exception as exc:
        video_tasks._set_task(
            task,
            status="failed",
            progress=max(task.progress, 1.0),
            message="下载失败",
            error=video_urls._clean_error(exc, task.url),
        )


def _run_audio_download(task: video_tasks.DownloadTask) -> None:
    video_tasks._set_task(task, status="running", progress=1, message="正在解析音轨")
    try:
        if task.format_id and task.format_id.startswith("music:"):
            track = task.music_track or music.get_track(task.format_id.removeprefix("music:"), task.url)
            source_url, options, source_name = music.resolve(track, video_parsing._ydl_base_options(task.url))
            def download_track():
                return audio.download_audio(
                    url=source_url, title=track.title, task_id=task.task_id, output_format=task.audio_format,
                    download_dir=paths.DOWNLOAD_DIR, ffmpeg_dir=paths.FFMPEG_DIR, options=options, direct=True,
                    expected_duration=track.duration, progress=lambda value, message: video_tasks._set_task(task, progress=value, message=message),
                )
            try:
                target = download_track()
            except audio.IncompleteAudioError:
                if source_name not in ("原平台", "酷狗音乐"):
                    raise
                video_tasks._set_task(task, message="原平台仅返回试听，正在尝试解析服务")
                source_url, options, source_name = music.resolve(track, video_parsing._ydl_base_options(task.url), public_only=True)
                target = download_track()
            video_tasks._set_task(task, progress=98, message="正在保存音频索引")
            _remember_audio_download(target, track.model_dump())
            video_tasks._set_task(task, status="finished", progress=100, message=f"音频已准备好 · {source_name}", filename=target.name)
            return
        result = video_parse_cache._get_sniff_cache(task.url) or video_parsing.parse_video(task.url)
        if not result.formats:
            raise RuntimeError("没有可用音轨，请使用单个视频或歌曲链接。")
        selected = next((item for item in result.formats if item.format_id.startswith("sniffpair:")), None)
        if not selected:
            selected = next((item for item in result.formats if item.format_id == task.format_id), None)
        if not selected:
            selected = next((item for item in result.formats if item.format_id.startswith("sniff:")), None)
        source_url, direct = task.url, False
        if selected and selected.format_id.startswith("sniffpair:"):
            source_url = unquote(selected.format_id.removeprefix("sniffpair:").partition("|")[2])
            direct = True
        elif selected and selected.format_id.startswith("sniff:"):
            source_url = unquote(selected.format_id.removeprefix("sniff:"))
            direct = True
        options = video_parsing._ydl_base_options(task.url)
        options["http_headers"]["Referer"] = task.url
        target = audio.download_audio(
            url=source_url, title=result.title, task_id=task.task_id, output_format=task.audio_format,
            download_dir=paths.DOWNLOAD_DIR, ffmpeg_dir=paths.FFMPEG_DIR, options=options, direct=direct,
            expected_duration=result.duration, progress=lambda value, message: video_tasks._set_task(task, progress=value, message=message),
        )
        video_tasks._set_task(task, progress=98, message="正在保存音频索引")
        _remember_audio_download(target, {"title": result.title, "artist": result.uploader or "", "provider": "video",
                                         "webpage_url": task.url, "thumbnail": result.thumbnail, "duration": result.duration})
        video_tasks._set_task(task, status="finished", progress=100, message="音轨已准备好", filename=target.name)
    except Exception as exc:
        video_tasks._set_task(task, status="failed", message="音轨下载失败", error=video_urls._clean_error(exc, task.url))


def _remember_audio_download(target: Path, metadata: dict) -> None:
    try:
        library.record_download(target.name, metadata)
        start_cover_backfill()
    except Exception:
        logger.warning("Audio file saved; library indexing will retry on the next library scan.")


def _run_download(task: video_tasks.DownloadTask) -> None:
    if task.media_type == "audio":
        _run_audio_download(task)
        return
    if task.format_id and task.format_id.startswith("sniffpair:"):
        payload = task.format_id.removeprefix("sniffpair:")
        video_part, _, audio_part = payload.partition("|")
        _run_sniffed_pair_download(task, unquote(video_part), unquote(audio_part))
        return

    if task.format_id and task.format_id.startswith("sniff:"):
        _run_sniffed_download(task, unquote(task.format_id.removeprefix("sniff:")))
        return

    if video_urls._is_douyin_url(task.url) and _run_douyin_public_download(task):
        return

    video_tasks._set_task(task, status="running", progress=1.0, message="正在连接视频源")
    output_template = str(paths.DOWNLOAD_DIR / f"{task.task_id}.%(title).80s.%(ext)s")
    format_value = task.format_id or "bestvideo+bestaudio/best"
    options = {
        **video_parsing._ydl_base_options(task.url),
        "format": format_value,
        "outtmpl": output_template,
        "merge_output_format": "mp4",
        "progress_hooks": [_progress_hook(task)],
    }

    before = set(paths.DOWNLOAD_DIR.glob(f"{task.task_id}.*"))
    try:
        with YoutubeDL(options) as ydl:
            ydl.download([task.url])
        after = set(paths.DOWNLOAD_DIR.glob(f"{task.task_id}.*"))
        files = sorted(after - before or after, key=lambda path: path.stat().st_mtime, reverse=True)
        if not files:
            raise RuntimeError("下载已结束，但没有找到生成的文件。")
        video_tasks._set_task(
            task,
            status="finished",
            progress=100.0,
            message="文件已准备好",
            filename=files[0].name,
        )
    except Exception as exc:
        video_tasks._set_task(
            task,
            status="failed",
            progress=max(task.progress, 1.0),
            message="下载失败",
            error=video_urls._clean_error(exc, task.url),
        )
