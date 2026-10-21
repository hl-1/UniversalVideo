from __future__ import annotations

from backend.core import storage

import logging


logger = logging.getLogger("videodream.summary")

import re
import shutil
import hashlib
import subprocess
import time
import tempfile
import threading
from pathlib import Path
from typing import Any
from urllib.parse import unquote
import requests
from backend.modules.music import sources as music
from backend.modules.music.media import start_cover_backfill
from backend.modules.music import library
from backend.core import paths
from backend.infrastructure import audio
from backend.infrastructure import runtime
from backend.infrastructure import video_network, video_transfer, video_proxy, platform_access
from backend.infrastructure.ytdlp import YoutubeDL
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
            progress = payload.get("overall_progress", min(99.0, downloaded / total * 100) if total else task.progress)
            speed = payload.get("speed")
            speed_label = f" · {speed / 1024 / 1024:.1f} MiB/s" if speed else ""
            size_label = f" · {downloaded / 1024 / 1024:.1f}" + (f"/{total / 1024 / 1024:.1f} MiB" if total else " MiB")
            eta = payload.get("eta")
            eta_label = f" · 剩余 {int(max(0, eta))} 秒" if eta is not None else ""
            stage = payload.get("stage", "正在下载")
            video_tasks._set_task(task, status="running", progress=progress, message=f"{stage}{speed_label}{size_label}{eta_label}")
        elif status == "finished":
            video_tasks._set_task(task, progress=99.0, message="下载完成，正在整理文件")

    return hook


def _run_domestic_download(task: video_tasks.DownloadTask) -> None:
    # Native manifest readers share the same guarded transport as online preview;
    # FFmpeg receives local files only. Selected DASH members run sequentially,
    # keeping the whole task below four simultaneous media connections.
    from yt_dlp.downloader import get_suitable_downloader
    from yt_dlp.downloader.http import HttpFD
    from yt_dlp.downloader.hls import HlsFD
    from yt_dlp.downloader.dash import DashSegmentsFD
    from backend.modules.video import preview_sources, cache

    checkpoint_lock = threading.RLock()

    def check():
        with checkpoint_lock:
            video_tasks._checkpoint(task, {})

    native_format = task.format_id if task.format_id and not task.format_id.startswith(("sniff:", "sniffpair:", "douyin-public")) else "bestvideo+bestaudio/best"
    if platform_access.platform(task.url) == "pornhub" and re.fullmatch(r"hls-\d+(?:-\d+)?", native_format or ""):
        # Older history stored bandwidth-based IDs. Recover the exact quality.
        from backend.modules.video.formats import pornhub_selector
        cached = video_parse_cache._get_sniff_cache(task.url)
        formats = [item.model_dump() for item in cached.formats] if cached else []
        if not formats and task.parse_record_id:
            from backend.modules.accounts import service as accounts
            import json
            stored = json.loads(accounts.resource("video_parse", task.parse_record_id)["payload"])
            formats = (stored.get("result") or {}).get("formats", [])
        chosen = next((item for item in formats if item.get("format_id") == native_format), None)
        resolution = (chosen or {}).get("resolution") or ""
        height = re.search(r"(?:x|^)(\d+)(?:p)?$", resolution)
        if not height:
            raise RuntimeError("旧视频格式已失效，请重新解析并选择清晰度。")
        native_format = pornhub_selector(int(height[1]), (chosen or {}).get("ext") or "mp4")
    options = {**video_parsing._ydl_base_options(task.url), "format": native_format,
               "cachedir": False, "noprogress": True, "hls_prefer_native": True,
               "fixup": "never", "skip_unavailable_fragments": False}
    if task.media_type == "audio":
        options["format"] = "bestaudio/best"
    selection = task.format_id or "best"

    def resolve(ydl):
        duration = None
        if selection.startswith(("sniff:", "sniffpair:")):
            encoded = selection.partition(":")[2].split("|")
            if len(encoded) > 2 or any(not value for value in encoded):
                raise RuntimeError("视频源组合无效，请重新解析。")
            members = [{"url": unquote(value), "http_headers": {**options["http_headers"], "Referer": task.url},
                        "ext": video_sniffing._media_ext(unquote(value)), "vcodec": "none" if i else "unknown"}
                       for i, value in enumerate(encoded)]
            if task.media_type == "audio" and len(members) == 2:
                members = members[1:]
            cached = video_parse_cache._get_sniff_cache(task.url)
            title, duration = (cached.title, cached.duration) if cached else ("video", None)
        elif selection == "douyin-public":
            _, item = video_douyin._fetch_douyin_item(task.url)
            members = [{"url": video_douyin._douyin_media_url(item), "ext": "mp4", "http_headers": video_constants.DOUYIN_HEADERS}]
            title = str(item.get("desc") or "douyin")
            duration_ms = (item.get("video") or {}).get("duration")
            duration = float(duration_ms) / 1000 if duration_ms else None
        else:
            info = ydl.extract_info(task.url, download=False)
            if not info or info.get("_type", "video") != "video" or info.get("is_live") or info.get("has_drm"):
                raise RuntimeError("仅支持单个已发布且未受 DRM 保护的视频。")
            members, title = info.get("requested_formats") or [info], info.get("title") or "video"
            duration = info.get("duration")
        # Resolve raw HLS/DASH URLs into native fragment metadata before choosing a downloader.
        expanded = []
        for member in members:
            if preview_sources.is_manifest(member["url"]) and not member.get("protocol"):
                info = ydl.extract_info(member["url"], download=False)
                expanded.extend(info.get("requested_formats") or [info])
            else:
                expanded.append(member)
        if not expanded or len(expanded) > 2:
            raise RuntimeError("视频源音视频组合不受支持，请重新选择格式。")
        return title, expanded, duration

    try:
        video_tasks._set_task(task, status="running", progress=1, message="正在通过海外代理连接视频源" if platform_access.platform(task.url) else "正在直连国内视频源")
        for attempt in range(2):
            try:
                with tempfile.TemporaryDirectory(prefix=f"video-{task.task_id}-", dir=storage.directory("temp", uid=task.user_id or None)) as temporary, \
                     preview_sources.PreviewYoutubeDL(options, check, lambda size: None) as ydl:
                    work = Path(temporary)
                    title, members, duration = resolve(ydl)
                    check()
                    files = []
                    native_media = False
                    for index, member in enumerate(members):
                        check()
                        if member.get("has_drm") or member.get("is_live"):
                            raise RuntimeError("不支持直播或 DRM 视频。")
                        extension = re.sub(r"[^a-zA-Z0-9]", "", member.get("ext") or "mp4")
                        source = work / f"source-{index}.{extension}"
                        last_report = [0.0]

                        def hook(payload):
                            # yt-dlp's native fragment workers can invoke hooks concurrently.
                            with checkpoint_lock:
                                if task.cancel.is_set():
                                    raise RuntimeError("任务已停止。")
                                if payload.get("status") != "downloading" or time.monotonic() - last_report[0] < 0.3:
                                    return
                                last_report[0] = time.monotonic()
                                ratio = payload.get("downloaded_bytes", 0) / (payload.get("total_bytes") or payload.get("total_bytes_estimate") or float("inf"))
                                connections = payload.get("connections", options.get("concurrent_fragment_downloads", 1))
                                transport = "HLS/DASH" if member.get("protocol") in {"m3u8_native", "http_dash_segments"} else "HTTP"
                                route = "海外代理" if platform_access.platform(task.url) else "国内直连"
                                stage = f"{route} · {transport} 最多 {connections} 路 · " + ("正在下载音频" if member.get("vcodec") == "none" else "正在下载视频")
                                if payload.get("fallback_reason"):
                                    stage += "（" + payload["fallback_reason"] + "）"
                                _progress_hook(task)({**payload, "overall_progress": min(97, (index + min(1, ratio)) / len(members) * 97), "stage": stage})

                        media_headers = {**options["http_headers"], **member.get("http_headers", {})}
                        cookie = ydl.cookiejar.get_cookie_header(member["url"])
                        if cookie:
                            media_headers["Cookie"] = cookie
                        media = {**member, "http_headers": media_headers}
                        downloader = get_suitable_downloader(media, options)
                        if downloader is HttpFD:
                            video_transfer.download(media["url"], source, referer=task.url, headers=media_headers,
                                root=storage.directory("temp", uid=task.user_id or None) / "transfers", owner=task.user_id,
                                key=f"{task.url}:{task.media_type}:{task.audio_format}:{task.format_id or 'best'}:{getattr(task, 'route_revision', '')}:{index}",
                                task_id=task.task_id, progress=hook, check=check, cancel=task.cancel)
                        elif downloader in {HlsFD, DashSegmentsFD}:
                            native_media = True
                            native_options = {**options, "progress_hooks": [hook], "continuedl": False}
                            fd = {HlsFD: preview_sources.NativeHlsFD, DashSegmentsFD: preview_sources.NativeDashFD}[downloader](ydl, native_options)
                            fd.preview_streams = []
                            try:
                                success, _ = fd.download(str(source), media)
                            finally:
                                for stream in fd.preview_streams:
                                    stream.close()
                            if not success:
                                raise RuntimeError("视频分片下载失败，请重新解析。")
                        else:
                            raise RuntimeError("当前源暂不支持该媒体协议，请选择 HTTP、HLS 或 DASH 格式。")
                        if not source.is_file() or not source.stat().st_size:
                            raise RuntimeError("视频源没有生成完整文件。")
                        with source.open("rb") as stream:
                            beginning = stream.read(64).lstrip()
                        if beginning.startswith((b"\xff\xd8\xff", b"\x89PNG", b"{", b"[", b"<!DOCTYPE", b"<html")):
                            raise RuntimeError("下载结果不是有效媒体文件，请重新解析。")
                        files.append(source)
                    stem = re.sub(r'[\\/:*?"<>|\s]+', " ", title).strip()[:80] or "video"
                    if task.media_type == "audio":
                        target = storage.directory(uid=task.user_id or None) / f"{task.task_id}.{stem}.{task.audio_format}"
                        video_tasks._set_task(task, progress=98, message="下载完成，正在提取并校验音轨")
                        audio.export_audio(files[-1], target, paths.FFMPEG_DIR, duration)
                        _remember_audio_download(target, {"title": title, "provider": "video", "webpage_url": task.url})
                    elif len(files) == 1 and not native_media and files[0].suffix.lower() in {".mp4", ".webm", ".mkv", ".flv"}:
                        target = storage.directory(uid=task.user_id or None) / f"{task.task_id}.{stem}{files[0].suffix}"
                        check()
                        files[0].replace(target)
                    else:
                        target = storage.directory(uid=task.user_id or None) / f"{task.task_id}.{stem}.mp4"
                        video_tasks._set_task(task, progress=98, message="下载完成，正在本地合并音视频")
                        command = [str(paths.FFMPEG_DIR / runtime.executable_name("ffmpeg")), "-y", "-nostdin", "-loglevel", "error"]
                        for source in files:
                            command += ["-i", str(source)]
                        if len(files) == 2:
                            command += ["-map", "0:v:0", "-map", "1:a:0"]
                        command += ["-c", "copy", "-movflags", "+faststart", str(target)]
                        with (work / "merge.log").open("w+") as log:
                            task.process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=log)
                            try:
                                while task.process.poll() is None:
                                    check()
                                    time.sleep(0.2)
                                if task.process.returncode:
                                    raise RuntimeError("本地音视频合并失败，请重新选择格式。")
                            finally:
                                if task.process.poll() is None:
                                    task.process.kill()
                                task.process.wait()
                                task.process = None
                    check()
                    video_tasks._set_task(task, status="finished", progress=100, message="文件已准备好", filename=target.name)
                return
            except Exception as exc:
                if not isinstance(exc, (video_transfer.SourceExpired, video_transfer.SourceChanged)) and not re.search(r"HTTP Error (?:401|403|410)\b", str(exc)):
                    raise
                check()
                if attempt:
                    raise
                video_tasks._set_task(task, message="视频源已过期，正在重新解析一次")
                if selection.startswith(("sniff:", "sniffpair:")):
                    previous = video_parse_cache._get_sniff_cache(task.url)
                    chosen = next((f for f in previous.formats if f.format_id == selection), None) if previous else None
                    if task.user_id:
                        cache.remove(cache.parse_key(task.url, task.user_id))
                    with video_parse_cache.sniff_cache_lock:
                        video_parse_cache.sniff_cache.pop(task.url, None)
                    refreshed = video_parsing.parse_video(task.url)
                    matched = next((f for f in refreshed.formats if chosen and (f.label, f.ext, f.resolution) == (chosen.label, chosen.ext, chosen.resolution)), None)
                    if not matched:
                        raise RuntimeError("原格式已失效，请重新解析后选择清晰度。")
                    selection = matched.format_id
    except Exception as exc:
        for partial in storage.directory(uid=task.user_id or None).glob(f"{task.task_id}.*"):
            if partial.is_file():
                partial.unlink(missing_ok=True)
        error = re.sub(r'https?://[^\s\x27"<>]+', '[视频源地址]', video_urls._clean_error(exc, task.url))
        video_tasks._set_task(task, status="cancelled" if task.cancel.is_set() else "failed",
                              message="任务已取消" if task.cancel.is_set() else "下载失败", error=None if task.cancel.is_set() else error)


def _run_douyin_public_download(task: video_tasks.DownloadTask) -> bool:
    video_tasks._set_task(task, status="running", progress=2.0, message="正在解析抖音公开视频")
    temp_path = None
    try:
        video_id, item = video_douyin._fetch_douyin_item(task.url)
        media_url = video_douyin._douyin_media_url(item)
        title = str(item.get("desc") or f"douyin_{video_id}")
        safe_title = re.sub(r'[\\/:*?"<>|\s]+', " ", title).strip()[:80] or f"douyin_{video_id}"
        target_path = storage.directory(uid=task.user_id or None) / f"{task.task_id}.{safe_title}.mp4"
        temp_path = storage.path("temp", target_path.name + ".part", uid=task.user_id or None)

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
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


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
    target_path = storage.directory(uid=task.user_id or None) / f"{task.task_id}.sniffed-{url_hash}.{output_ext}"
    temp_path = storage.path("temp", target_path.stem + f".partial.{output_ext}", uid=task.user_id or None)
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
                str(temp_path),
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
                current_size = temp_path.stat().st_size if temp_path.exists() else 0
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
    target_path = storage.directory(uid=task.user_id or None) / f"{task.task_id}.sniffed-merged.mp4"
    temporary = storage.path("temp", target_path.stem + ".partial.mp4", uid=task.user_id or None)
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
            str(temporary),
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
        if not temporary.exists() or temporary.stat().st_size == 0:
            raise RuntimeError("合并结束，但没有生成有效文件。")
        temporary.replace(target_path)
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

    finally:
        temporary.unlink(missing_ok=True)


def _run_audio_download(task: video_tasks.DownloadTask) -> None:
    video_tasks._set_task(task, status="running", progress=1, message="正在解析音轨")
    try:
        if task.format_id and task.format_id.startswith("music:"):
            track = task.music_track or music.get_track(task.format_id.removeprefix("music:"), task.url)
            source_url, options, source_name = music.resolve(track, video_parsing._ydl_base_options(task.url))
            def download_track():
                return audio.download_audio(
                    url=source_url, title=track.title, task_id=task.task_id, output_format=task.audio_format,
                    download_dir=storage.directory(uid=task.user_id or None), work_dir=storage.directory("temp", uid=task.user_id or None), ffmpeg_dir=paths.FFMPEG_DIR, options=options, direct=True,
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
            download_dir=storage.directory(uid=task.user_id or None), work_dir=storage.directory("temp", uid=task.user_id or None), ffmpeg_dir=paths.FFMPEG_DIR, options=options, direct=direct,
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
    try:
        with video_proxy.scope(task.url) as route:
            task.route_revision = (route or {}).get("revision")
            if (video_network.domestic(task.url) or platform_access.platform(task.url)) and not (task.format_id or "").startswith("music:"):
                _run_domestic_download(task)
            else:
                _run_legacy_download(task)
    except Exception as exc:
        video_tasks._set_task(task, status="failed", message="下载失败", error=video_urls._clean_error(exc, task.url))


def _run_legacy_download(task: video_tasks.DownloadTask) -> None:
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
    workspace = storage.directory("temp", uid=task.user_id or None) / storage.filename(task.task_id)
    workspace.mkdir(exist_ok=True)
    output_template = str(workspace / f"{task.task_id}.%(title).80s.%(ext)s")
    format_value = task.format_id or "bestvideo+bestaudio/best"
    options = {
        **video_parsing._ydl_base_options(task.url),
        "format": format_value,
        "outtmpl": output_template,
        "merge_output_format": "mp4",
        "progress_hooks": [_progress_hook(task)],
    }

    before = set(workspace.glob(f"{task.task_id}.*"))
    try:
        with YoutubeDL(options) as ydl:
            ydl.download([task.url])
        after = set(workspace.glob(f"{task.task_id}.*"))
        files = sorted((path for path in (after - before or after) if path.is_file() and path.suffix not in {".part", ".ytdl", ".json"}), key=lambda path: path.stat().st_mtime, reverse=True)
        if not files:
            raise RuntimeError("下载已结束，但没有找到生成的文件。")
        target = storage.path("file", files[0].name, uid=task.user_id or None)
        files[0].replace(target)
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

    finally:
        shutil.rmtree(workspace, ignore_errors=True)
