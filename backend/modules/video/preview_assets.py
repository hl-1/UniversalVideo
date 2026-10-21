"""Short lived, account scoped MP4 preparation for the native preview player."""
from __future__ import annotations

from backend.infrastructure import video_network, platform_access, video_proxy

import json
import logging
import math
import os
import re
import secrets
import shutil
import stat
import subprocess
import threading
import time
import traceback
from contextvars import copy_context
from dataclasses import dataclass, field
from pathlib import Path

from fastapi import HTTPException

from backend.core import paths
from backend.infrastructure import network, runtime
from backend.modules.accounts import service as accounts
from backend.modules.video import cache, history, preview_cache
from backend.modules.video import preview_sources as sources
from backend.modules.video import preview_streams as streams

PreparationError = sources.PreparationError

ROOT = paths.RUN_DIR / "video-previews"
WORKER_ID = f"{os.getpid()}:{secrets.token_hex(8)}"
TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
MAX_INPUT = 1024 ** 3
MAX_DISK = 4 * 1024 ** 3
MAX_SECONDS = 300
INPUT_OPTIONS = ["-protocol_whitelist", "file,pipe", "-format_whitelist", "mov,mp4,m4a,3gp,3g2,mj2,matroska,webm,mp3,aac,mpegts,mpeg,flv,avi,asf,ogg"]
lock = threading.RLock()
jobs: dict[str, Job] = {}
stop_event = threading.Event()
cleaner: threading.Thread | None = None


@dataclass
class Job:
    token: str
    value: dict
    authenticated: bool
    format_id: str | None = None
    fast: bool = True
    cancelled: threading.Event = field(default_factory=threading.Event)
    process: subprocess.Popen | None = None
    readers: int = 0
    received: int = 0
    deadline: float = field(default_factory=lambda: time.monotonic() + MAX_SECONDS)
    started: float = field(default_factory=time.monotonic)
    prepared_inputs: tuple | None = None
    producers: int = 0
    selection: str | None = None

    @property
    def directory(self) -> Path:
        return ROOT / self.token


def _key(job: Job) -> str:
    return cache.key("preview", job.token, job.value["user_id"])


def _write(job: Job, *, creating=False) -> bool:
    if job.cancelled.is_set():
        return False
    if not job.authenticated:
        return True
    # A removed key (record removal, account reset or cancellation) must not reappear.
    if not creating and cache.get(_key(job)) is None:
        job.cancelled.set()
        return False
    return cache.put(_key(job), job.value, max(1, math.ceil(job.value["expires_at"] - time.time())), only_existing=not creating)


def create(media_url: str, audio_url: str | None, referer: str, record_id: str | None, alternate=False, *, format_id=None, fast=True, selection=None) -> dict:
    platform_access.require_payload({"media_url": media_url, "audio_url": audio_url, "referer": referer, "parse_record_id": record_id})
    ttl = cache.media_ttl([url for url in (media_url, audio_url) if url])
    if ttl <= 0:
        raise HTTPException(410, "播放源已过期，请重新解析。")
    authenticated = bool(accounts.principal.get())
    uid = accounts.owner_id() if authenticated else 0
    with lock:
        cleanup()
        # A cancelled download still occupies its slot until its blocking read exits.
        pending = [job for job in jobs.values() if job.value["status"] == "preparing" or job.process is not None or job.producers]
        if len(pending) >= 2 or any(job.value["user_id"] == uid for job in pending):
            raise HTTPException(429, "已有视频正在准备，请稍后重试或取消当前预览。")
        token = secrets.token_urlsafe(24)
        now = time.time()
        value = dict(version=2, user_id=uid, parse_record_id=record_id, status="preparing",
                     source_kind="extractor" if format_id else "pair" if audio_url else "single", referer=referer,
                     media_url=media_url, audio_url=audio_url, file_id=None, has_audio=None,
                     duration=None, error_code=None, message="正在准备音视频", worker_id=WORKER_ID,
                     created_at=now, expires_at=now + ttl, alternate=alternate)
        value.update(delivery="file", complete=False, timings_ms={})
        value.update(platform_access.metadata_for(value))
        route = video_proxy.selected(referer)
        if route:
            value["route_revision"] = route["revision"]
        job = Job(token, value, authenticated, format_id=format_id, fast=fast, selection=selection)
        if not _write(job, creating=True):
            raise HTTPException(503, "预览缓存暂不可用，请稍后重试。")
        jobs[token] = job
        result = response(token, value)
        _start(job)
        return result


def reuse(page: str, selection: str | None, record_id: str) -> dict | None:
    if not accounts.principal.get():
        return None
    uid = accounts.owner_id()
    platform_access.require_payload({"referer": page, "parse_record_id": record_id})
    with lock:
        hit = preview_cache.find(ROOT, uid, record_id, page, selection)
        if hit is None:
            return None
        directory, metadata = hit
        platform_access.require(metadata, metadata=metadata)
        token = secrets.token_urlsafe(24)
        value = dict(version=2, user_id=uid, parse_record_id=record_id, status="ready", source_kind="cache",
                     referer=page, media_url=page, audio_url=None, file_id=token,
                     has_audio=metadata["has_audio"], duration=metadata["duration"], error_code=None,
                     message="正在播放已缓存的视频" if metadata["has_audio"] else "当前源仅含画面，没有可用音频。",
                     worker_id=WORKER_ID, created_at=time.time(), expires_at=metadata["expires_at"],
                     alternate=metadata["alternate"], delivery="file", complete=True, reused=True, timings_ms={})
        for name in ("access_scope", "source_platform", "route_revision"):
            if name in metadata:
                value[name] = metadata[name]
        job = Job(token, value, True)
        try:
            job.directory.mkdir(parents=True)
            os.link(directory / "preview.mp4", job.directory / "preview.mp4")
            (job.directory / "lease.json").write_text(json.dumps(dict(user_id=uid, authenticated=True,
                expires_at=value["expires_at"])), encoding="utf-8")
            if not _write(job, creating=True):
                raise HTTPException(503, "预览缓存暂不可用，请稍后重试。")
        except OSError:
            shutil.rmtree(job.directory, ignore_errors=True)
            return None
        except Exception:
            shutil.rmtree(job.directory, ignore_errors=True)
            raise
        jobs[token] = job
        return response(token, value)


def _start(job: Job) -> None:
    threading.Thread(target=copy_context().run, args=(_prepare, job), daemon=True, name="video-preview").start()


def response(token: str, value: dict) -> dict:
    ready = value.get("status", "ready") == "ready"
    return dict(preview_id=token, status=value.get("status", "ready"),
                status_url=f"/api/previews/{token}",
                preview_url=f"/api/previews/{token}/content" if ready else None,
                expires_in=max(0, math.ceil(value["expires_at"] - time.time())),
                has_audio=value.get("has_audio"), message=value.get("message", ""),
                error_code=value.get("error_code"), alternate=value.get("alternate", False),
                delivery=value.get("delivery", "file"), complete=value.get("complete", True), reused=value.get("reused", False), timings_ms=value.get("timings_ms", {}))


def get(token: str) -> dict | None:
    """Return v2 metadata; None lets the router handle a legacy memory session."""
    if not TOKEN_PATTERN.fullmatch(token):
        return None
    with lock:
        job = jobs.get(token)
        if accounts.principal.get():
            value = cache.get(cache.key("preview", token))
            if not value or value.get("user_id") != accounts.current().id:
                raise HTTPException(404, "预览会话不存在，请重新获取播放源。")
        else:
            value = job.value.copy() if job and not job.authenticated else None
        if value is None:
            return None
        platform_access.require(value, metadata=platform_access.metadata_for(value))
        if platform_access.source(value) and value.get("route_revision") != video_proxy.profile()["revision"]:
            raise HTTPException(410, "代理线路已变化，请重新解析后播放。")
        if value.get("parse_record_id") and accounts.principal.get():
            history.owned_record(value["parse_record_id"])
        if value["expires_at"] <= time.time() or (job and job.cancelled.is_set()):
            raise HTTPException(410, "预览地址已过期，请重新获取播放源。")
        if value.get("version") != 2:
            return value
        if (value["status"] == "preparing" or (value.get("delivery") == "fragmented" and not value.get("complete"))) and (not job or value["worker_id"] != WORKER_ID):
            value.update(status="failed", error_code="interrupted", message="预览准备已中断，请重新播放。")
            if accounts.principal.get():
                cache.put(cache.key("preview", token), value, response(token, value)["expires_in"], only_existing=True)
        if value["status"] == "ready" and value.get("delivery") != "proxy" and not file_path(token, value).is_file():
            raise HTTPException(410, "预览文件已清理，请重新播放。")
        return value.copy()


def file_path(token: str, value: dict) -> Path:
    if not TOKEN_PATTERN.fullmatch(token) or value.get("file_id") != token:
        raise HTTPException(410, "预览文件无效，请重新播放。")
    result = ROOT / token / "preview.mp4"
    if result.resolve().parent.parent != ROOT.resolve():
        raise HTTPException(410, "预览文件无效，请重新播放。")
    return result


def acquire(token: str, value: dict) -> Path:
    with lock:
        path = file_path(token, value)
        if not path.is_file():
            raise HTTPException(410, "预览文件已清理，请重新播放。")
        job = jobs.get(token)
        if job is None:
            job = Job(token, value, bool(accounts.principal.get()))
            jobs[token] = job
        if job.cancelled.is_set():
            raise HTTPException(410, "预览已取消。")
        job.readers += 1
        return path


def release(token: str) -> None:
    with lock:
        if job := jobs.get(token):
            job.readers = max(0, job.readers - 1)


def hold(token: str, value: dict) -> Job:
    with lock:
        job = jobs.get(token)
        if job is None:
            job = Job(token, value, bool(accounts.principal.get()))
            jobs[token] = job
        if job.cancelled.is_set():
            raise HTTPException(410, "预览已取消。")
        if job.readers >= 4:
            raise HTTPException(429, "播放请求过多，请稍后重试。")
        job.readers += 1
        return job


def cancel(token: str) -> None:
    with lock:
        if job := jobs.get(token):
            job.cancelled.set()
            if job.process and job.process.poll() is None:
                job.process.kill()
        if accounts.principal.get():
            cache.remove(cache.key("preview", token))
        cleanup()


def cancel_user(uid: int, resource_key: str | None = None) -> None:
    if resource_key is not None:
        return
    with lock:
        preview_cache.purge(ROOT, uid)
        for job in jobs.values():
            if job.value["user_id"] == uid:
                job.cancelled.set()
                if job.authenticated:
                    cache.remove(_key(job))
                if job.process and job.process.poll() is None:
                    job.process.kill()
        cleanup()


def cancel_record(uid: int, record_id: str) -> None:
    with lock:
        preview_cache.purge(ROOT, uid, record_id)
        for job in jobs.values():
            if job.value["user_id"] == uid and job.value["parse_record_id"] == record_id:
                job.cancelled.set()
                if job.authenticated:
                    cache.remove(_key(job))
                if job.process and job.process.poll() is None:
                    job.process.kill()
        cleanup()


def _disk_usage(directory: Path) -> int:
    total = 0
    for item in directory.rglob("*"):
        try:
            info = item.stat()
        except FileNotFoundError:
            # Native downloaders rename fragments and .part files during a scan.
            continue
        if stat.S_ISREG(info.st_mode):
            total += info.st_size
    return total


def _check(job: Job) -> None:
    if job.authenticated and time.monotonic() - getattr(job, "_role_checked", 0) >= 1:
        try:
            accounts.active_worker(job.value["user_id"], source=job.value)
        except HTTPException as exc:
            job.cancelled.set()
            if job.process and job.process.poll() is None:
                job.process.kill()
            raise PreparationError("cancelled", "账号权限已变化，预览已停止。") from exc
        job._role_checked = time.monotonic()
    if job.cancelled.is_set() or job.value["status"] == "failed":
        raise PreparationError("cancelled", "预览已取消。")
    if time.monotonic() > job.deadline or time.time() >= job.value["expires_at"]:
        raise PreparationError("timeout", "准备超时或播放源已过期，请重新解析后重试。")
    with lock:
        if _disk_usage(ROOT) > MAX_DISK:
            raise PreparationError("quota", "预览临时空间已满，请稍后重试。")


def _download(job: Job, url: str, target: Path) -> tuple[Path, Path | None] | None:
    # Each attempt opens the same partial input with wb; incomplete bytes are never published.
    return sources.retry_network(lambda: _download_once(job, url, target), lambda: _check(job), wait=job.cancelled.wait)


def _download_once(job: Job, url: str, target: Path) -> tuple[Path, Path | None] | None:
    _check(job)
    # public_get validates every redirect; FFmpeg never receives remote URLs.
    upstream = video_network.get(url, referer=job.value["referer"], headers=sources.headers(job.value["referer"]),
                                  stream=True, timeout=(10, 10))
    try:
        kind = upstream.headers.get("Content-Type", "").split(";", 1)[0].lower()
        if upstream.status_code == 200 and kind in {"application/vnd.apple.mpegurl", "application/x-mpegurl", "audio/mpegurl", "audio/x-mpegurl", "application/dash+xml"}:
            upstream.close()
            return sources.download(job, url, target, lambda: _check(job), lambda size: _receive(job, size))
        if upstream.status_code != 200 or not (kind.startswith(("video/", "audio/")) or kind in {"application/octet-stream", "binary/octet-stream", "application/mp4"}):
            raise PreparationError("upstream", "视频源返回无效内容或已过期，请重新解析。")
        length = upstream.headers.get("Content-Length", "")
        if length.isdigit() and job.received + int(length) > MAX_INPUT:
            raise PreparationError("size", "视频超过预览大小限制，请下载后播放。")
        with target.open("wb") as output:
            for chunk in upstream.iter_content(256 * 1024):
                _check(job)
                if chunk:
                    job.received += len(chunk)
                    if job.received > MAX_INPUT:
                        raise PreparationError("size", "视频超过预览大小限制，请下载后播放。")
                    output.write(chunk)
        with target.open("rb") as source:
            beginning = source.read(4096).lstrip()
        if beginning.startswith(b"#EXTM3U") or b"<MPD" in beginning:
            return sources.download(job, url, target, lambda: _check(job), lambda size: _receive(job, size))
    finally:
        upstream.close()


def _receive(job: Job, size: int) -> None:
    _check(job)
    with lock:
        job.received += size
        if job.received > MAX_INPUT:
            raise PreparationError("size", "视频超过预览大小限制，请下载后播放。")


def _input(job: Job, url: str, target: Path, *, format_id=None) -> tuple[Path, Path | None]:
    if format_id or sources.is_manifest(url):
        return sources.download(job, url, target, lambda: _check(job), lambda size: _receive(job, size), format_id=format_id)
    result = _download(job, url, target)
    return result if isinstance(result, tuple) else (target, None)


def _run(job: Job, command: list[str], output: Path, on_tick=None) -> None:
    _check(job)
    with output.open("wb") as stdout, (job.directory / "tool.log").open("wb") as stderr:
        with lock:
            _check(job)
            job.process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr)
            process = job.process
        try:
            while process.poll() is None:
                _check(job)
                if on_tick:
                    on_tick()
                job.cancelled.wait(0.1)
            if process.returncode:
                raise PreparationError("media_invalid", "媒体检查或合流失败，请重新解析或下载后播放。")
        finally:
            if process.poll() is None:
                process.kill()
            process.wait()
            with lock:
                job.process = None


def _probe(job: Job, path: Path) -> dict:
    output = job.directory / "probe.json"
    _run(job, [str(paths.FFMPEG_DIR / runtime.executable_name("ffprobe")), "-v", "error", *INPUT_OPTIONS,
               "-show_streams", "-show_format", "-of", "json", str(path)], output)
    return json.loads(output.read_text(encoding="utf-8"))


def _publish(job, has_audio, duration, delivery, *, complete=False):
    _check(job)
    if job.authenticated and job.value["parse_record_id"]:
        history.owned_record(job.value["parse_record_id"])
    with lock:
        first = job.value["status"] != "ready"
        ttl = min(86400, max(1800, math.ceil(duration) + 600))
        if delivery == "proxy":
            ttl = cache.media_ttl([job.value["media_url"]], ceiling=ttl)
        timings = job.value["timings_ms"]
        timings["ready"] = timings.get("ready", round((time.monotonic() - job.started) * 1000, 1))
        if complete:
            timings["total"] = round((time.monotonic() - job.started) * 1000, 1)
        job.value.update(status="ready", file_id=None if delivery == "proxy" else job.token,
                         delivery=delivery, complete=complete, has_audio=has_audio, duration=duration,
                         expires_at=time.time() + ttl,
                         message="当前源仅含画面，没有可用音频。" if not has_audio else "预览使用完整音视频线路" if job.value["alternate"] else "")
        if delivery != "proxy":
            (job.directory / "lease.json").write_text(json.dumps({"user_id": job.value["user_id"], "authenticated": job.authenticated,
                                                                 "expires_at": job.value["expires_at"]}), encoding="utf-8")
        if not _write(job):
            raise PreparationError("cache", "预览缓存暂不可用，请稍后重试。")
        if complete and delivery != "proxy" and job.selection and job.authenticated:
            try:
                preview_cache.store(ROOT, job.directory / "preview.mp4", job.value, job.selection)
            except OSError:
                logging.getLogger("videodream.video").info("Video preview reuse cache unavailable")
        if first:
            logging.getLogger("videodream.video").info("Video preview delivery=%s ready_ms=%.1f", delivery, timings["ready"])


def _try_stream(job):
    if not job.fast:
        return False
    if job.format_id or sources.is_manifest(job.value["media_url"]):
        return _native_stream(job)
    inputs, infos = [], []
    metadata_deadline = min(job.deadline, time.monotonic() + 8)

    def check_metadata():
        _check(job)
        if time.monotonic() >= metadata_deadline:
            raise streams.Unavailable()

    try:
        for index, url in enumerate(filter(None, [job.value["media_url"], job.value["audio_url"]])):
            remote = streams.RemoteMP4(job, url, check_metadata, lambda size: _receive(job, size))
            target = job.directory / f"metadata-{index}.mp4"
            remote.inspect(target)
            infos.append(_probe(job, target))
            inputs.append(remote)
    except streams.Unavailable:
        return False
    for remote in inputs:
        remote.check = lambda: _check(job)
    video = next((s for s in infos[0].get("streams", []) if s.get("codec_type") == "video"), None)
    audio = next((s for s in infos[-1].get("streams", []) if s.get("codec_type") == "audio"), None)
    if not video or (len(inputs) == 2 and not audio):
        return False
    duration = float(infos[0].get("format", {}).get("duration", 0))
    if not math.isfinite(duration) or duration <= 0:
        return False
    job.value["timings_ms"]["metadata"] = round((time.monotonic() - job.started) * 1000, 1)
    compatible = video.get("codec_name") == "h264" and video.get("pix_fmt") in {"yuv420p", "yuvj420p"}
    if len(inputs) == 1 and compatible and (not audio or audio.get("codec_name") == "aac"):
        _publish(job, bool(audio), duration, "proxy", complete=True)
        return True
    pipes, threads, errors = [], [], []
    try:
        command = [str(paths.FFMPEG_DIR / runtime.executable_name("ffmpeg")), "-y", "-v", "error", "-nostdin"]
        for index, remote in enumerate(inputs):
            pipe = streams.LocalPipe(job.directory, f"{job.token}-{index}", lambda: _check(job))
            pipes.append(pipe)
            threads.append(streams.start_feed(pipe, remote.feed(), errors))
            command += ["-thread_queue_size", "64", "-analyzeduration", "1000000", "-probesize", "32768", *INPUT_OPTIONS, "-i", pipe.path]
        command += ["-map", "0:v:0"]
        if audio:
            command += ["-map", "1:a:0" if len(inputs) == 2 else "0:a:0", "-c:a", "copy" if audio.get("codec_name") == "aac" else "aac"]
        command += ["-c:v", "copy"] if compatible else ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p", "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2"]
        output = job.directory / "preview.mp4"
        command += ["-sn", "-dn", "-map_metadata", "-1", "-flush_packets", "1", "-movflags", "+empty_moov+default_base_moof+frag_keyframe", "-frag_duration", "2000000", "-f", "mp4", str(output)]

        def tick():
            if errors:
                raise errors[0]
            if job.value["status"] == "preparing" and streams.first_fragment(output):
                _publish(job, bool(audio), duration, "fragmented")

        _run(job, command, job.directory / "mux.out", tick)
        if errors:
            raise errors[0]
        result = _probe(job, output)
        has_audio = any(s.get("codec_type") == "audio" for s in result.get("streams", []))
        if bool(audio) != has_audio:
            raise PreparationError("output_invalid", "合流结果缺少音轨，请重新解析。")
        _publish(job, has_audio, duration, "fragmented", complete=True)
        return True
    finally:
        for pipe in pipes:
            pipe.close()
        for thread in threads:
            thread.join(timeout=1)


def _native_stream(job):
    selected, errors, feed_errors = [], [], []
    selected_event, done, stopped = threading.Event(), threading.Event(), threading.Event()

    def check_transfer():
        if stopped.is_set():
            raise PreparationError("cancelled", "预览输入已停止。")
        _check(job)

    def on_selected(members):
        selected.extend(members)
        selected_event.set()

    def transfer():
        try:
            job.prepared_inputs = sources.download(job, job.value["media_url"], job.directory / "native.input",
                check_transfer, lambda size: _receive(job, size), format_id=job.format_id, on_selected=on_selected)
        except BaseException as exc:
            errors.append(exc)
        finally:
            with lock:
                job.producers -= 1
            done.set()
            selected_event.set()
            if job.value["status"] == "failed":
                _clean_files(job)

    job.producers += 1
    producer = threading.Thread(target=copy_context().run, args=(transfer,), daemon=True, name="preview-native")
    producer.start()
    pipes, feeders = [], []
    try:
        while not selected_event.wait(.05):
            _check(job)
        if errors:
            raise errors[0]
        if not selected or not any(item["video"] for item in selected):
            raise PreparationError("video_codec", "当前源缺少有效视频轨。")
        video_index = next(i for i, item in enumerate(selected) if item["video"])
        audio_index = next((i for i, item in enumerate(selected) if item["audio"] and not item["video"]), video_index)
        expected_audio = len(selected) == 2
        duration = float(selected[video_index].get("duration") or 0)
        if not math.isfinite(duration) or duration < 0:
            duration = 0
        command = [str(paths.FFMPEG_DIR / runtime.executable_name("ffmpeg")), "-y", "-v", "error", "-nostdin"]
        for index, member in enumerate(selected):
            pipe = streams.LocalPipe(job.directory, f"{job.token}-native-{index}", lambda: _check(job))
            pipes.append(pipe)
            feeders.append(streams.start_feed(pipe, streams.follow(member, lambda: _check(job)), feed_errors))
            command += ["-thread_queue_size", "64", "-analyzeduration", "1000000", "-probesize", "32768", *INPUT_OPTIONS, "-i", pipe.path]
        command += ["-map", f"{video_index}:v:0", "-map", f"{audio_index}:a:0" + ("" if expected_audio else "?"),
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p",
                    "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2", "-c:a", "aac", "-sn", "-dn", "-map_metadata", "-1",
                    "-flush_packets", "1", "-movflags", "+empty_moov+default_base_moof+frag_keyframe", "-frag_duration", "2000000", "-f", "mp4", str(job.directory / "preview.mp4")]
        output = job.directory / "preview.mp4"

        def tick():
            if errors:
                raise errors[0]
            if feed_errors:
                frames = [f"{Path(frame.filename).name}:{frame.lineno}" for frame in traceback.extract_tb(feed_errors[0].__traceback__)]
                logging.getLogger("videodream.video").warning("Preview pipe failed type=%s errno=%s frames=%s", type(feed_errors[0]).__name__, getattr(feed_errors[0], "errno", None), frames)
                raise PreparationError("media_invalid", "媒体流暂不能提前播放。") from feed_errors[0]
            if job.value["status"] == "preparing" and streams.first_fragment(output):
                # Inspect a complete local fragment without replacing the running encoder handle.
                probe = subprocess.run([str(paths.FFMPEG_DIR / runtime.executable_name("ffprobe")), "-v", "error", *INPUT_OPTIONS,
                    "-show_streams", "-show_format", "-of", "json", str(output)], capture_output=True, timeout=3, check=True)
                info = json.loads(probe.stdout)
                has_audio = any(item.get("codec_type") == "audio" for item in info.get("streams", []))
                if expected_audio and not has_audio:
                    raise PreparationError("audio_missing", "配套音频缺少有效音轨，请重新解析。")
                _publish(job, has_audio, duration or float(info.get("format", {}).get("duration") or 0), "fragmented")

        try:
            _run(job, command, job.directory / "mux.out", tick)
        except PreparationError as exc:
            if exc.code != "media_invalid" or job.value["status"] != "preparing":
                raise
            # Tail-indexed/non-seekable inputs can use the completed local files.
            for pipe in pipes:
                pipe.close()
            while not done.wait(.05):
                _check(job)
            if errors:
                raise errors[0]
            return False
        while not done.wait(.05):
            _check(job)
        if errors:
            raise errors[0]
        result = _probe(job, output)
        has_audio = any(item.get("codec_type") == "audio" for item in result.get("streams", []))
        final_duration = float(result.get("format", {}).get("duration") or 0)
        if not any(item.get("codec_type") == "video" for item in result.get("streams", [])) or final_duration <= 0 or (expected_audio and not has_audio):
            raise PreparationError("output_invalid", "合流结果缺少有效音视频。")
        _publish(job, has_audio, final_duration, "fragmented", complete=True)
        return True
    finally:
        stopped.set()
        for pipe in pipes:
            pipe.close()
        for feeder in feeders:
            feeder.join(timeout=1)
        # IO readers retain their slot until bounded network reads have stopped.
        if not done.is_set():
            producer.join(timeout=1)


def _prepare(job: Job) -> None:
    try:
        with video_proxy.scope(job.value["referer"]):
            _prepare_scoped(job)
    except HTTPException:
        job.value.update(status="failed", error_code="network_proxy", message="海外代理配置已变化或不可用，请重新播放。")
        _write(job)


def _prepare_scoped(job: Job) -> None:
    try:
        _check(job)
        job.directory.mkdir(parents=True, exist_ok=False)
        for name in ("ffmpeg", "ffprobe"):
            if not (paths.FFMPEG_DIR / runtime.executable_name(name)).is_file():
                raise PreparationError("tool_missing", "预览需要 FFmpeg 和 ffprobe，请检查服务配置。")
        if _try_stream(job):
            return
        video, separate_audio = job.prepared_inputs or _input(job, job.value["media_url"], job.directory / "video.input", format_id=job.format_id)
        video_info = _probe(job, video)
        video_track = next((s for s in video_info["streams"] if s.get("codec_type") == "video"), None)
        if not video_track:
            raise PreparationError("video_codec", "当前源缺少有效视频轨，请重新选择视频格式。")
        audio = video
        audio_info = video_info
        paired = bool(job.value["audio_url"] or separate_audio)
        if paired:
            if separate_audio:
                audio = separate_audio
            else:
                audio, _ = _input(job, job.value["audio_url"], job.directory / "audio.input")
            audio_info = _probe(job, audio)
        audio_track = next((s for s in audio_info["streams"] if s.get("codec_type") == "audio"), None)
        if paired and not audio_track:
            raise PreparationError("audio_missing", "配套音频缺少有效音轨，请重新解析。")
        command = [str(paths.FFMPEG_DIR / runtime.executable_name("ffmpeg")), "-y", "-v", "error", "-nostdin", *INPUT_OPTIONS, "-i", str(video)]
        if paired:
            command += [*INPUT_OPTIONS, "-i", str(audio)]
        command += ["-map", "0:v:0"]
        if audio_track:
            command += ["-map", "1:a:0" if paired else "0:a:0", "-c:a", "copy" if audio_track.get("codec_name") == "aac" else "aac"]
        if video_track.get("codec_name") == "h264" and video_track.get("pix_fmt") in {"yuv420p", "yuvj420p"}:
            command += ["-c:v", "copy"]
        else:
            command += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p",
                        "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2"]
        command += ["-sn", "-dn", "-map_metadata", "-1", "-movflags", "+faststart", "-f", "mp4", str(job.directory / "preview.part")]
        _run(job, command, job.directory / "mux.out")
        result = _probe(job, job.directory / "preview.part")
        streams = result.get("streams", [])
        has_audio = any(s.get("codec_type") == "audio" for s in streams)
        if not any(s.get("codec_type") == "video" for s in streams) or bool(audio_track) != has_audio:
            raise PreparationError("output_invalid", "合流结果缺少音视频轨，请重新解析。")
        duration = float(result.get("format", {}).get("duration", 0))
        if not math.isfinite(duration) or duration <= 0:
            raise PreparationError("output_invalid", "视频时长无效，请重新解析。")
        _check(job)
        with lock:
            _check(job)
            (job.directory / "preview.part").replace(job.directory / "preview.mp4")
        _publish(job, has_audio, duration, "file", complete=True)
    except Exception as exc:
        failure = exc if isinstance(exc, PreparationError) else sources.network_failure(exc)
        if failure is None:
            logging.getLogger("videodream.video").warning("Video preview preparation failed", exc_info=True)
            failure = PreparationError("prepare_failed", "准备音视频失败，请重新解析或稍后重试。")
        with lock:
            job.value.update(status="failed", file_id=None, error_code=failure.code, message=failure.message)
            if not _write(job):
                job.cancelled.set()
    finally:
        _clean_files(job)


def _clean_files(job):
    with lock:
        if job.producers:
            return
        for item in job.directory.glob("*"):
            if item.name not in {"preview.mp4", "lease.json"} or job.value["status"] != "ready" or job.cancelled.is_set():
                try:
                    item.unlink(missing_ok=True)
                except OSError:
                    pass


def cleanup() -> None:
    with lock:
        preview_cache.cleanup(ROOT)
        now = time.time()
        for token, job in list(jobs.items()):
            missing = job.authenticated and cache.get(_key(job)) is None
            if missing or job.value["expires_at"] <= now:
                job.cancelled.set()
            if job.cancelled.is_set():
                if job.process and job.process.poll() is None:
                    job.process.kill()
                if not job.readers and not job.producers and job.process is None and job.value["status"] != "preparing":
                    shutil.rmtree(job.directory, ignore_errors=True)
                    if not job.directory.exists():
                        jobs.pop(token, None)
        # A restart can leave finished files without a local Job. Redis owns their lifetime.
        if ROOT.exists():
            for folder in ROOT.iterdir():
                if folder.is_dir() and TOKEN_PATTERN.fullmatch(folder.name) and folder.name not in jobs:
                    try:
                        lease = json.loads((folder / "lease.json").read_text(encoding="utf-8"))
                        keep = lease["authenticated"] and lease["expires_at"] > now and cache.get(cache.key("preview", folder.name, lease["user_id"]))
                    except (OSError, ValueError, KeyError):
                        keep = now - folder.stat().st_mtime < MAX_SECONDS
                    if not keep:
                        shutil.rmtree(folder, ignore_errors=True)


def start() -> None:
    global cleaner
    if cleaner and cleaner.is_alive():
        return
    stop_event.clear()
    def maintain():
        while not stop_event.wait(60):
            try:
                cleanup()
            except Exception:
                # Cache outages must not terminate cleanup permanently.
                continue
    cleaner = threading.Thread(target=maintain, daemon=True, name="preview-cleanup")
    cleaner.start()


def stop() -> None:
    stop_event.set()
    with lock:
        for job in jobs.values():
            if job.value["status"] == "preparing" or job.process is not None or job.producers:
                job.cancelled.set()
                if job.process and job.process.poll() is None:
                    job.process.kill()
