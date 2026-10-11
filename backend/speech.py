from __future__ import annotations

import math
import re
import subprocess
import tempfile
import time
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import requests

from backend import ai
from backend.audio import download_audio, probe_audio
from backend.runtime import executable_name


CHUNK_SECONDS = 60
MAX_AUDIO_SECONDS = 4 * 3600
RETRY_STATUSES = {429, 500, 502, 503, 504}


@dataclass(frozen=True)
class SpeechSegment:
    start: float
    end: float
    text: str


def _clean_text(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<\|[^>]+\|>", "", text)).strip()


def response_segments(payload: dict, start: float, duration: float) -> list[SpeechSegment]:
    text = payload.get("text")
    if not isinstance(text, str):
        raise RuntimeError("语音服务返回了无效转写结果。")
    pieces = payload.get("segments")
    result = []
    if isinstance(pieces, list):
        for item in pieces:
            if not isinstance(item, dict) or not isinstance(item.get("text"), str):
                continue
            try:
                left, right = float(item["start"]), float(item["end"])
            except (KeyError, ValueError, TypeError):
                continue
            cleaned = _clean_text(item["text"])
            if cleaned and math.isfinite(left) and math.isfinite(right) and 0 <= left < right <= duration + 0.5:
                result.append(SpeechSegment(start + left, start + min(right, duration), cleaned))
    if result:
        return sorted(result, key=lambda item: item.start)
    cleaned = _clean_text(text)
    # Text-only providers have chunk timestamps, not sentence-level alignment.
    return [SpeechSegment(start, start + duration, cleaned)] if cleaned else []


def _wait_retry(seconds: float, checkpoint: Callable[[], None]):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        checkpoint()
        time.sleep(min(0.2, max(0, deadline - time.monotonic())))


def transcribe_chunk(path: Path, config: dict, *, checkpoint: Callable[[], None]) -> dict:
    endpoint = config["base_url"].rstrip("/") + "/audio/transcriptions"
    for attempt in range(3):
        checkpoint()
        try:
            with path.open("rb") as audio:
                response = requests.post(
                    endpoint, headers={"Authorization": "Bearer " + config["api_key"]},
                    data={"model": config["model"]}, files={"file": (path.name, audio, "audio/wav")},
                    timeout=(10, 120), allow_redirects=False,
                )
        except requests.RequestException as exc:
            if attempt == 2:
                raise RuntimeError("语音转写服务连接失败，请稍后重试。") from exc
            _wait_retry(2 ** attempt, checkpoint)
            continue
        try:
            checkpoint()
            if response.status_code in RETRY_STATUSES and attempt < 2:
                _wait_retry(2 ** attempt, checkpoint)
                continue
            if response.status_code != 200:
                messages = {401: "语音转写服务密钥无效。", 403: "语音转写模型不可访问或服务额度不足。",
                            404: "语音转写接口或模型不存在，请检查服务地址和模型名称。",
                            429: "语音转写服务请求过多，请稍后重试。"}
                raise RuntimeError(messages.get(response.status_code, f"语音转写服务请求失败（HTTP {response.status_code}），请检查模型和服务额度。"))
            try:
                payload = response.json()
            except ValueError as exc:
                raise RuntimeError("语音服务返回了无效转写结果。") from exc
            if not isinstance(payload, dict) or not isinstance(payload.get("text"), str):
                raise RuntimeError("语音服务返回了无效转写结果。")
            return payload
        finally:
            response.close()
    raise RuntimeError("语音转写服务暂不可用。")


def split_audio(source: Path, work: Path, ffmpeg_dir: Path, *, checkpoint: Callable[[], None]) -> list[tuple[Path, float, float]]:
    command = [str(ffmpeg_dir / executable_name("ffmpeg")), "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
               "-i", str(source), "-map", "0:a:0", "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le",
               "-f", "segment", "-segment_time", str(CHUNK_SECONDS), "-reset_timestamps", "1", str(work / "chunk-%04d.wav")]
    checkpoint()
    with subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) as process:
        try:
            deadline = time.monotonic() + 300
            while process.poll() is None:
                checkpoint()
                if time.monotonic() > deadline:
                    raise RuntimeError("音轨分段超时，请使用较短的视频。")
                time.sleep(0.2)
            if process.returncode:
                raise RuntimeError("音轨分段失败，请检查 ffmpeg 和源文件。")
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
    chunks = []
    start = 0.0
    for path in sorted(work.glob("chunk-*.wav")):
        checkpoint()
        with wave.open(str(path)) as audio:
            duration = audio.getnframes() / audio.getframerate()
        if duration > 0:
            chunks.append((path, start, duration))
            start += duration
    if not chunks:
        raise RuntimeError("没有取得可用于语音转写的音轨。")
    return chunks


def transcribe_video(
    *, url: str, title: str, task_id: str, work_root: Path, ffmpeg_dir: Path, options: dict,
    progress: Callable[[float, str], None], checkpoint: Callable[[], None],
    direct: bool = False, expected_duration: float | None = None,
) -> list[SpeechSegment]:
    config = ai.load_config("speech")
    if not config.get("base_url"):
        raise RuntimeError("请配置语音转写服务地址。")
    if expected_duration and expected_duration > MAX_AUDIO_SECONDS:
        raise RuntimeError("语音转写暂支持 4 小时以内的视频。")
    checkpoint()
    work_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=f"speech-{task_id}-", dir=work_root) as working:
        work = Path(working)
        def download_progress(value, message):
            checkpoint()
            progress(12 + value * 0.14, "无可用字幕 · " + message)
        source = download_audio(
            url=url, title=title, task_id=task_id, output_format="m4a", download_dir=work,
            ffmpeg_dir=ffmpeg_dir, options=options, progress=download_progress,
            direct=direct, expected_duration=expected_duration,
        )
        checkpoint()
        duration = probe_audio(source, ffmpeg_dir)["duration"]
        if duration > MAX_AUDIO_SECONDS:
            raise RuntimeError("语音转写暂支持 4 小时以内的视频。")
        progress(27, "正在准备语音转写音频")
        chunks = split_audio(source, work, ffmpeg_dir, checkpoint=checkpoint)
        if abs(sum(item[2] for item in chunks) - duration) > max(1, duration * 0.001):
            raise RuntimeError("音轨分段不完整，请重新尝试。")
        segments = []
        for index, (path, start, length) in enumerate(chunks):
            checkpoint()
            progress(28 + index / len(chunks) * 34, f"正在语音转写 {index + 1}/{len(chunks)}")
            payload = transcribe_chunk(path, config, checkpoint=checkpoint)
            segments.extend(response_segments(payload, start, length))
            path.unlink()
            progress(28 + (index + 1) / len(chunks) * 34, f"已完成语音转写 {index + 1}/{len(chunks)}")
        if not segments:
            raise RuntimeError("语音转写未识别到有效内容，请确认视频包含清晰人声。")
        return segments
