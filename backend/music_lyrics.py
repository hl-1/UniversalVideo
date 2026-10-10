from __future__ import annotations

import hashlib
import json
import threading
import time
from pathlib import Path

import pylrc
import requests
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from backend.audio import probe_audio
from backend import accounts, library


DOWNLOAD_DIR = Path(__file__).resolve().parent.parent / "downloads"
FFMPEG_DIR = Path(r"C:\softWare\environment\ffmpeg")
router = APIRouter(prefix="/api/music/lyrics", tags=["music-lyrics"])
MAX_LYRICS_BYTES = 512000
fetch_lock = threading.Lock()
cache_lock = threading.Lock()
next_request_at = 0.0


def audio_path(filename: str) -> Path:
    if accounts.principal.get():
        accounts.resource("file", filename)
    if Path(filename).name != filename or "/" in filename or "\\" in filename:
        raise HTTPException(422, "音频文件名无效。")
    path = (DOWNLOAD_DIR / filename).resolve()
    if not path.is_relative_to(DOWNLOAD_DIR.resolve()) or not path.is_file():
        raise HTTPException(404, "音频文件不存在。")
    if path.suffix.lower() not in {".mp3", ".m4a", ".flac", ".wav", ".ogg", ".opus", ".aac"}:
        raise HTTPException(422, "请选择音频文件。")
    return path


def cache_path(filename: str) -> Path:
    return DOWNLOAD_DIR / "_lyrics" / (hashlib.sha256(filename.encode()).hexdigest() + ".json")


def decode_lrc(data: bytes) -> str:
    if not data or len(data) > MAX_LYRICS_BYTES:
        raise HTTPException(422, "歌词文件为空或超过 500 KB。")
    encodings = ("utf-16", "utf-8-sig", "gb18030") if data.startswith((b"\xff\xfe", b"\xfe\xff")) else ("utf-8-sig", "gb18030")
    for encoding in encodings:
        try:
            return data.decode(encoding)
        except UnicodeError:
            pass
    raise HTTPException(422, "无法识别歌词文件编码。")


def parse_lrc(text: str) -> list[dict]:
    grouped: dict[float, list[str]] = {}
    # The parser handles multiple time tags, millisecond precision, and LRC offsets.
    for line in pylrc.parse(text.strip("\ufeff")):
        timestamp = max(0.0, round(line.time, 3))
        texts = grouped.setdefault(timestamp, [])
        content = line.text.strip()
        if content not in texts:
            texts.append(content)
    return [{"time": timestamp, "text": "\n".join(texts)} for timestamp, texts in sorted(grouped.items())]


def response_data(text: str = "", source: str = "", *, message: str = "") -> dict:
    lines = parse_lrc(text)
    return {"lines": lines, "plain": "" if lines else text, "source": source, "message": message}


def read_cached(filename: str) -> dict | None:
    with cache_lock:
        return read_cached_unlocked(filename)


def save_cached(filename: str, text: str, source: str, signature: str = "") -> dict:
    with cache_lock:
        return save_cached_unlocked(filename, text, source, signature)


def local_lyrics(path: Path) -> dict | None:
    cached = read_cached(path.name)
    if cached and cached["text"]:
        return response_data(cached["text"], cached["source"])
    sidecar = path.with_suffix(".lrc")
    if sidecar.is_file() and sidecar.stat().st_size <= MAX_LYRICS_BYTES:
        text = decode_lrc(sidecar.read_bytes())
        if parse_lrc(text):
            return save_cached(path.name, text, "本地 LRC")
    return None


@router.get("/{filename}")
def get_lyrics(filename: str):
    return local_lyrics(audio_path(filename)) or response_data()


@router.put("/{filename}")
async def import_lyrics(filename: str, request: Request):
    audio_path(filename)
    data = bytearray()
    async for chunk in request.stream():
        data.extend(chunk)
        if len(data) > MAX_LYRICS_BYTES:
            raise HTTPException(413, "歌词文件超过 500 KB。")
    text = decode_lrc(bytes(data))
    lines = parse_lrc(text)
    if not lines or not any(line["text"] for line in lines):
        raise HTTPException(422, "没有找到有效的 LRC 时间标签和歌词。")
    return save_cached(filename, text, "本地 LRC")


class LyricsRequest(BaseModel):
    filename: str = Field(min_length=1, max_length=300)
    title: str = Field(min_length=1, max_length=300)
    artist: str = Field(default="", max_length=300)
    album: str = Field(default="", max_length=300)


@router.post("/fetch")
def fetch_lyrics(payload: LyricsRequest):
    global next_request_at
    path = audio_path(payload.filename)
    local = local_lyrics(path)
    if local:
        return local
    if not payload.artist.strip():
        return response_data(message="缺少歌手信息，可导入本地 LRC。")
    signature = json.dumps([payload.title, payload.artist, payload.album], ensure_ascii=False)
    with fetch_lock:
        local = local_lyrics(path)
        if local:
            return local
        cached = read_cached(path.name)
        if cached and cached.get("signature") == signature and cached.get("checked_at", 0) > time.time() - 86400:
            return response_data(message="未找到匹配歌词。")
        if time.monotonic() < next_request_at:
            raise HTTPException(429, "歌词请求频繁，请稍后重试。")
        try:
            duration = probe_audio(path, FFMPEG_DIR)["duration"]
        except RuntimeError as exc:
            raise HTTPException(422, "无法读取这首音频，请重新下载。") from exc
        if not 1 <= duration <= 3600:
            return response_data(message="暂不支持匹配这段音频的歌词。")
        next_request_at = time.monotonic() + 0.5
        try:
            response = requests.get("https://lrclib.net/api/get", params={
                "track_name": payload.title, "artist_name": payload.artist,
                "album_name": payload.album, "duration": round(duration),
            }, headers={"User-Agent": "VideoDream/1.0 (https://github.com/hl-1/UniversalVideo)"}, timeout=(4, 10))
            if response.status_code in (429, 503):
                try:
                    delay = min(3600, max(1, float(response.headers.get("Retry-After", "60"))))
                except ValueError:
                    delay = 60
                next_request_at = time.monotonic() + delay
                raise HTTPException(503, "在线歌词服务暂时繁忙。")
            if response.status_code == 404:
                text = ""
            else:
                response.raise_for_status()
                data = response.json()
                if not isinstance(data, dict):
                    raise ValueError("Invalid lyrics response")
                text = data.get("syncedLyrics") or data.get("plainLyrics") or ""
                if not isinstance(text, str) or len(text.encode()) > MAX_LYRICS_BYTES:
                    raise ValueError("Invalid lyrics response")
        except (requests.RequestException, ValueError) as exc:
            raise HTTPException(502, "在线歌词暂时不可用，可导入本地 LRC。") from exc
        # Preserve an LRC imported while the remote request was in flight.
        with cache_lock:
            latest = read_cached_unlocked(path.name)
            if latest and latest.get("source") == "本地 LRC":
                return response_data(latest["text"], latest["source"])
            result = save_cached_unlocked(path.name, text, "LRCLIB" if text else "", signature)
        return result | {"message": "" if text else "未找到匹配歌词。"}


def read_cached_unlocked(filename: str) -> dict | None:
    stored = library.read_lyrics(filename)
    if stored is not None:
        return stored
    try:
        data = json.loads(cache_path(filename).read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("text"), str) and isinstance(data.get("source"), str) and len(data["text"].encode()) <= MAX_LYRICS_BYTES:
            return library.store_lyrics(filename, data)
    except (ValueError, OSError):
        pass
    return None


def save_cached_unlocked(filename: str, text: str, source: str, signature: str) -> dict:
    stored = library.store_lyrics(filename, {"text": text, "source": source, "signature": signature, "checked_at": time.time()})
    return response_data(stored["text"], stored["source"])
