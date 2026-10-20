from __future__ import annotations

import json
import math
import mimetypes
import subprocess
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import Literal
from urllib.parse import quote

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field

from backend.core.paths import ROOT_DIR as PROJECT_ROOT
from backend.modules.accounts import service as accounts
from backend.modules.watermark import inpainting
from backend.infrastructure.runtime import executable_name, ffmpeg_directory


DOWNLOAD_DIR = PROJECT_ROOT / "downloads"
FFMPEG_DIR = ffmpeg_directory()
VIDEO_EXTENSIONS = {".mp4", ".webm", ".mkv", ".mov", ".avi", ".m4v"}
router = APIRouter(prefix="/api/watermarks", tags=["watermarks"])
executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="watermark")
jobs_lock = threading.Lock()


class WatermarkRegion(BaseModel):
    x: int = Field(ge=1)
    y: int = Field(ge=1)
    width: int = Field(ge=4)
    height: int = Field(ge=4)


class WatermarkRequest(BaseModel):
    filename: str = Field(min_length=1, max_length=255)
    regions: list[WatermarkRegion] = Field(min_length=1, max_length=8)
    method: Literal["lama", "delogo"] = "delogo"


class WatermarkPreviewRequest(WatermarkRequest):
    time: float = Field(default=0, ge=0, allow_inf_nan=False)


class VideoInfo(BaseModel):
    filename: str
    width: int
    height: int
    duration: float
    preview_url: str
    fps: float = 30


class WatermarkResponse(BaseModel):
    task_id: str
    status: Literal["pending", "running", "finished", "failed", "cancelled"]
    progress: float
    message: str
    error: str | None = None
    filename: str | None = None
    download_url: str | None = None
    frames_processed: int = 0
    total_frames: int = 0
    elapsed_seconds: float = 0
    remaining_seconds: float | None = None
    processing_device: str | None = None


@dataclass
class WatermarkJob:
    task_id: str
    source: Path
    info: VideoInfo
    regions: list[WatermarkRegion]
    status: str = "pending"
    progress: float = 0
    message: str = "等待处理"
    error: str | None = None
    filename: str | None = None
    cancel: threading.Event = field(default_factory=threading.Event)
    process: subprocess.Popen | None = None
    decoder: subprocess.Popen | None = None
    method: Literal["lama", "delogo"] = "delogo"
    frames_processed: int = 0
    started_at: float | None = None
    stopped_at: float | None = None
    processing_started_at: float | None = None
    processing_device: str | None = None
    user_id: int = 0

    def response(self) -> WatermarkResponse:
        now = self.stopped_at or time.perf_counter()
        total_frames = max(self.frames_processed, round(self.info.duration * self.info.fps)) if self.method == "lama" else 0
        remaining = None
        if self.status == "running" and self.frames_processed and self.processing_started_at is not None:
            seconds_per_frame = (now - self.processing_started_at) / self.frames_processed
            remaining = max(0, total_frames - self.frames_processed) * seconds_per_frame
        return WatermarkResponse(
            task_id=self.task_id,
            status=self.status,
            progress=round(self.progress, 2),
            message=self.message,
            error=self.error,
            filename=self.filename,
            download_url=f"/api/watermarks/videos/{quote(self.filename, safe='')}" if self.filename else None,
            frames_processed=self.frames_processed,
            total_frames=total_frames,
            elapsed_seconds=round(max(0, now - self.started_at), 1) if self.started_at is not None else 0,
            remaining_seconds=round(remaining) if remaining is not None else None,
            processing_device=self.processing_device,
        )
jobs: dict[str, WatermarkJob] = {}


def _video_path(filename: str) -> Path:
    if accounts.principal.get():
        accounts.resource("file", filename)
    if any(char in filename for char in ("/", "\\", ":", "\x00")) or Path(filename).name != filename:
        raise HTTPException(status_code=422, detail="视频文件名不合法。")
    candidate = DOWNLOAD_DIR / filename
    if candidate.suffix.lower() not in VIDEO_EXTENSIONS:
        raise HTTPException(status_code=422, detail="请选择视频文件，音频暂不支持去水印。")
    if not candidate.is_file() or candidate.resolve().parent != DOWNLOAD_DIR.resolve():
        raise HTTPException(status_code=404, detail="视频不存在，请刷新文件列表。")
    return candidate.resolve()


def _probe_video(source: Path) -> VideoInfo:
    probe = FFMPEG_DIR / executable_name("ffprobe")
    if not probe.is_file():
        raise HTTPException(status_code=503, detail="未找到 ffprobe，请检查 ffmpeg 安装目录。")
    try:
        result = subprocess.run(
            [str(probe), "-v", "error", "-select_streams", "v:0", "-show_streams", "-show_format", "-of", "json", str(source)],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20,
        )
        if result.returncode:
            raise ValueError("ffprobe failed")
        data = json.loads(result.stdout)
        stream = data["streams"][0]
        width, height = int(stream["width"]), int(stream["height"])
        sar = stream.get("sample_aspect_ratio", "1:1").split(":")
        if len(sar) == 2 and int(sar[0]) > 0 and int(sar[1]) > 0:
            width = round(width * int(sar[0]) / int(sar[1]))
        rotation = float(stream.get("tags", {}).get("rotate", 0))
        for side_data in stream.get("side_data_list", []):
            if "rotation" in side_data:
                rotation = float(side_data["rotation"])
        if round(rotation / 90) % 2:
            width, height = height, width
        # Match the displayed orientation and square pixels before filtering.
        width, height = max(2, round(width / 2) * 2), max(2, round(height / 2) * 2)
        duration = float(data.get("format", {}).get("duration") or stream.get("duration") or 0)
        fps = 30
        for rate in (stream.get("avg_frame_rate"), stream.get("r_frame_rate")):
            try:
                candidate = float(Fraction(rate or "0"))
            except (ValueError, ZeroDivisionError):
                continue
            if math.isfinite(candidate) and 0 < candidate <= 240:
                fps = candidate
                break
        if not math.isfinite(duration) or duration <= 0 or not (8 <= width <= 8192 and 8 <= height <= 8192):
            raise ValueError("unsupported dimensions or duration")
    except (OSError, subprocess.TimeoutExpired, ValueError, KeyError, IndexError, TypeError, ZeroDivisionError) as exc:
        raise HTTPException(status_code=422, detail="无法读取视频信息，文件可能不完整或格式不受支持。") from exc
    return VideoInfo(
        filename=source.name, width=width, height=height, duration=duration,
        preview_url=f"/api/watermarks/videos/{quote(source.name, safe='')}", fps=fps,
    )


def _validate_regions(regions: list[WatermarkRegion], info: VideoInfo) -> None:
    for region in regions:
        if region.x + region.width >= info.width or region.y + region.height >= info.height:
            raise HTTPException(status_code=422, detail="水印区域超出视频画面，四周至少保留 1 像素。")


def _filter_chain(info: VideoInfo, regions: list[WatermarkRegion]) -> str:
    filters = [f"scale={info.width}:{info.height}", "setsar=1"]
    filters.extend(f"delogo=x={r.x}:y={r.y}:w={r.width}:h={r.height}" for r in regions)
    return ",".join(filters)


def _update(job: WatermarkJob, **changes) -> None:
    if job.user_id and not job.cancel.is_set() and time.monotonic() - getattr(job, "_checked_at", 0) >= 1:
        try:
            accounts.active_worker(job.user_id, source={"source_filename": job.source.name})
            job._checked_at = time.monotonic()
        except HTTPException:
            job.cancel.set()
            _stop_processes(job)
    with jobs_lock:
        for key, value in changes.items():
            setattr(job, key, value)
    if job.user_id and (time.monotonic() - getattr(job, "_saved_at", 0) >= 1 or job.status in {"finished", "failed", "cancelled"}):
        accounts.register_resource("watermark", job.task_id, dict(job.response().model_dump(), source_filename=job.source.name), uid=job.user_id)
        job._saved_at = time.monotonic()
        if job.status == "finished" and job.filename:
            path = DOWNLOAD_DIR / job.filename
            accounts.register_resource("file", job.filename, {"filename": job.filename, "size": path.stat().st_size, "source_filename": job.source.name}, uid=job.user_id)


def _stop_processes(job: WatermarkJob) -> None:
    for process in (job.process, job.decoder):
        if process and process.poll() is None:
            process.kill()


def _run_lama(job: WatermarkJob, partial: Path, errors, timed_out: threading.Event) -> None:
    import numpy as np

    inpainting._get_session()
    if job.cancel.is_set() or timed_out.is_set():
        return
    _update(job, message="正在 AI 修复画面", processing_device=inpainting.device_label(), processing_started_at=time.perf_counter())
    ffmpeg = str(FFMPEG_DIR / executable_name("ffmpeg"))
    dimensions = f"{job.info.width}x{job.info.height}"
    fps = str(job.info.fps)
    with jobs_lock:
        if job.cancel.is_set() or timed_out.is_set():
            return
        job.decoder = subprocess.Popen([
            ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-i", str(job.source),
            "-map", "0:v:0", "-vf", f"scale={job.info.width}:{job.info.height},setsar=1,fps={fps}",
            "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1",
        ], stdout=subprocess.PIPE, stderr=errors)
        job.process = subprocess.Popen([
            ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-f", "rawvideo",
            "-pix_fmt", "rgb24", "-s", dimensions, "-r", fps, "-i", "pipe:0", "-i", str(job.source),
            "-map", "0:v:0", "-map", "1:a?", "-c:v", "libx264", "-preset", "fast", "-crf", "18",
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", "-shortest", str(partial),
        ], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=errors)
    decoder, encoder = job.decoder, job.process
    frame_size = job.info.width * job.info.height * 3
    count = 0
    while not job.cancel.is_set() and not timed_out.is_set():
        buffer = bytearray()
        while len(buffer) < frame_size:
            chunk = decoder.stdout.read(frame_size - len(buffer))
            if not chunk:
                break
            buffer.extend(chunk)
        if not buffer:
            break
        if len(buffer) != frame_size:
            raise RuntimeError("视频解码得到不完整画面。")
        frame = np.frombuffer(buffer, dtype=np.uint8).reshape(job.info.height, job.info.width, 3)
        repaired = inpainting.repair_frame(frame, job.regions)
        if job.cancel.is_set() or timed_out.is_set():
            return
        encoder.stdin.write(repaired.tobytes())
        count += 1
        _update(job, frames_processed=count, progress=min(99, count / job.info.fps / job.info.duration * 100), processing_device=inpainting.device_label())
    encoder.stdin.close()
    encoder.stdin = None
    if not job.cancel.is_set() and not timed_out.is_set():
        _update(job, message="正在保存修复视频")
    decoder.wait()
    encoder.wait()
    if not job.cancel.is_set() and (count == 0 or decoder.returncode or encoder.returncode):
        raise RuntimeError("AI 修复的视频解码或编码失败，请检查原视频。")


def _run_watermark(job: WatermarkJob) -> None:
    target = DOWNLOAD_DIR / f"{job.task_id}.{job.source.stem[:120]}.watermark-removed.mp4"
    partial = target.with_suffix(".part.mp4")
    timer = None
    timed_out = threading.Event()
    try:
        if job.user_id:
            accounts.active_worker(job.user_id, source={"source_filename": job.source.name})
        with jobs_lock:
            if job.cancel.is_set():
                return
            job.status = "running"
            job.started_at = time.perf_counter()
            job.message = "正在加载 AI 修复模型" if job.method == "lama" else "正在快速填补"
            job.progress = 0
        with tempfile.TemporaryFile() as errors:
            command = [
                str(FFMPEG_DIR / executable_name("ffmpeg")), "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                "-i", str(job.source), "-map", "0:v:0", "-map", "0:a?", "-vf", _filter_chain(job.info, job.regions),
                "-c:v", "libx264", "-preset", "fast", "-crf", "18", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", "-progress", "pipe:1", "-nostats", str(partial),
            ]
            def timeout() -> None:
                timed_out.set()
                _stop_processes(job)

            limit = 43200 if job.method == "lama" else 7200
            timer = threading.Timer(limit, timeout)
            timer.daemon = True
            timer.start()
            if job.method == "lama":
                _run_lama(job, partial, errors, timed_out)
            else:
                with jobs_lock:
                    if job.cancel.is_set():
                        return
                    job.process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=errors, text=True, encoding="utf-8")
                    process = job.process
                for line in process.stdout:
                    key, _, value = line.strip().partition("=")
                    if key == "out_time_us":
                        try:
                            progress = float(value) / 1_000_000 / job.info.duration * 100
                        except ValueError:
                            continue
                        if math.isfinite(progress):
                            _update(job, progress=min(99, max(job.progress, progress)))
                process.wait()
            if job.cancel.is_set():
                return
            if timed_out.is_set():
                raise RuntimeError(f"处理超过 {limit // 3600} 小时，任务已停止。")
            if job.process and job.process.returncode:
                errors.seek(0)
                detail = errors.read(4096).decode("utf-8", errors="replace")
                if "Unknown encoder" in detail or "No such filter" in detail:
                    raise RuntimeError("当前 ffmpeg 缺少 delogo 或 H.264 编码支持。")
                raise RuntimeError("视频处理失败，请检查文件完整性和水印区域。")
            if not partial.is_file() or partial.stat().st_size < 1024:
                raise RuntimeError("处理未生成有效视频。")
            _probe_video(partial)
            with jobs_lock:
                if job.cancel.is_set():
                    return
                partial.replace(target)
                job.status, job.progress, job.message = "finished", 100, "去水印完成"
                job.filename = target.name
    except Exception as exc:
        if not job.cancel.is_set():
            message = exc.detail if isinstance(exc, HTTPException) else str(exc)
            _update(job, status="failed", message="去水印失败", error=message)
    finally:
        if timer:
            timer.cancel()
        _stop_processes(job)
        for process in (job.process, job.decoder):
            if process:
                process.wait()
                if process.stdout:
                    process.stdout.close()
                if process.stdin:
                    try:
                        process.stdin.close()
                    except (OSError, BrokenPipeError):
                        pass
        partial.unlink(missing_ok=True)
        _update(job, process=None, decoder=None, stopped_at=time.perf_counter())
        if job.cancel.is_set():
            _update(job, status="cancelled", message="已取消")


@router.get("/videos")
def list_videos() -> list[dict]:
    allowed = accounts.permitted_files() if accounts.principal.get() else None
    files = [
        path for path in DOWNLOAD_DIR.iterdir()
        if path.is_file() and path.suffix.lower() in VIDEO_EXTENSIONS
        and (allowed is None or path.name in allowed)
        and not path.name.endswith(".part.mp4") and path.resolve().parent == DOWNLOAD_DIR.resolve()
    ]
    files.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    videos = []
    for path in files:
        original = None
        if path.name.endswith(".watermark-removed.mp4"):
            source_stem = path.name.removesuffix(".watermark-removed.mp4").partition(".")[2]
            original = next((candidate.name for candidate in files if candidate.stem == source_stem), None)
        videos.append({"filename": path.name, "size": path.stat().st_size, "original_filename": original})
    return videos


@router.get("/capabilities")
def capabilities() -> dict:
    return {"lama": inpainting.availability()}


@router.post("/preview")
def preview_watermark(payload: WatermarkPreviewRequest) -> Response:
    source = _video_path(payload.filename)
    info = _probe_video(source)
    _validate_regions(payload.regions, info)
    if payload.time > info.duration:
        raise HTTPException(status_code=422, detail="预览时间超出视频时长。")
    if payload.method == "lama" and not inpainting.availability()["available"]:
        raise HTTPException(status_code=503, detail="AI 修复模型未安装。")
    timestamp = min(payload.time, max(0, info.duration - 1 / info.fps))
    filters = f"scale={info.width}:{info.height},setsar=1" if payload.method == "lama" else _filter_chain(info, payload.regions)
    try:
        result = subprocess.run([
            str(FFMPEG_DIR / executable_name("ffmpeg")), "-v", "error", "-nostdin", "-ss", str(timestamp), "-i", str(source),
            "-frames:v", "1", "-vf", filters, "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1",
        ], capture_output=True, timeout=30)
        if result.returncode or len(result.stdout) != info.width * info.height * 3:
            raise RuntimeError("无法读取当前视频画面。")
        import cv2
        import numpy as np

        frame = np.frombuffer(result.stdout, dtype=np.uint8).reshape(info.height, info.width, 3)
        processing_seconds = 0
        if payload.method == "lama":
            timing = {}
            frame = inpainting.repair_frame(frame, payload.regions, timing=timing)
            processing_seconds = timing.get("seconds", 0)
        success, image = cv2.imencode(".png", cv2.cvtColor(frame, cv2.COLOR_RGB2BGR))
        if not success:
            raise RuntimeError("预览图片生成失败。")
        return Response(image.tobytes(), media_type="image/png", headers={"Cache-Control": "no-store", "X-Processing-Seconds": str(processing_seconds), "X-Processing-Device": inpainting.device_label() if payload.method == "lama" else "CPU"})
    except (OSError, subprocess.TimeoutExpired, RuntimeError, ImportError) as exc:
        raise HTTPException(status_code=503, detail="修复预览失败，请检查修复模型与运行依赖。") from exc


@router.get("/videos/{filename}/info", response_model=VideoInfo)
def video_info(filename: str) -> VideoInfo:
    return _probe_video(_video_path(filename))


@router.get("/videos/{filename}")
def video_content(filename: str) -> FileResponse:
    source = _video_path(filename)
    return FileResponse(source, filename=source.name, content_disposition_type="inline", media_type=mimetypes.guess_type(source.name)[0] or "video/mp4")


@router.post("/tasks", response_model=WatermarkResponse, status_code=202)
def create_watermark(payload: WatermarkRequest) -> WatermarkResponse:
    if not (FFMPEG_DIR / executable_name("ffmpeg")).is_file():
        raise HTTPException(status_code=503, detail="未找到 ffmpeg，请检查安装目录。")
    source = _video_path(payload.filename)
    info = _probe_video(source)
    _validate_regions(payload.regions, info)
    if payload.method == "lama" and not inpainting.availability()["available"]:
        raise HTTPException(status_code=503, detail="AI 修复模型未安装。")
    job = WatermarkJob(uuid.uuid4().hex[:12], source, info, payload.regions, method=payload.method)
    job.user_id = accounts.owner_id() if accounts.principal.get() else 0
    with jobs_lock:
        if sum(item.status in {"pending", "running"} for item in jobs.values()) >= 4:
            raise HTTPException(status_code=429, detail="去水印队列已满，请等待现有任务完成。")
        jobs[job.task_id] = job
        response = job.response()
    accounts.register_resource("watermark", job.task_id, dict(response.model_dump(), source_filename=job.source.name), uid=job.user_id)
    accounts.submit(executor, _run_watermark, job)
    return response


@router.get("/tasks/{task_id}", response_model=WatermarkResponse)
def get_watermark(task_id: str) -> WatermarkResponse:
    with jobs_lock:
        job = jobs.get(task_id)
        if not job:
            if accounts.principal.get():
                return WatermarkResponse.model_validate(json.loads(accounts.resource("watermark", task_id)["payload"]))
            raise HTTPException(status_code=404, detail="去水印任务不存在或服务已重启。")
        return job.response()


def cancel_user_jobs(uid: int, key: str | None = None):
    with jobs_lock:
        active = [job for job in jobs.values() if job.user_id == uid and (key is None or job.task_id == key or job.source.name == key)
                  and job.status in {"pending", "running"}]
        for job in active:
            job.cancel.set()
            _stop_processes(job)
            job.status, job.message = "cancelled", "账号已停用或任务已取消"
    for job in active:
        _update(job)
accounts.cancel_callbacks.append(cancel_user_jobs)


@router.delete("/tasks/{task_id}", response_model=WatermarkResponse)
def cancel_watermark(task_id: str) -> WatermarkResponse:
    with jobs_lock:
        job = jobs.get(task_id)
        if not job:
            raise HTTPException(status_code=404, detail="去水印任务不存在或服务已重启。")
        if job.status in {"pending", "running"}:
            job.cancel.set()
            _stop_processes(job)
            job.status, job.message, job.stopped_at = "cancelled", "已取消", time.perf_counter()
        return job.response()
