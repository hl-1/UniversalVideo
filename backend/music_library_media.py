from __future__ import annotations

import io
import json
import re
import secrets
import subprocess
import threading
import time
from pathlib import Path
from typing import Literal
from urllib.parse import parse_qs, urlparse

import requests
from fastapi import APIRouter, HTTPException, UploadFile
from fastapi.responses import FileResponse
from PIL import Image, ImageOps, UnidentifiedImageError
from pydantic import BaseModel, Field, model_validator

from backend import accounts, library, music


router = APIRouter(prefix="/api/library", tags=["music-library-media"])
MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_BACKGROUND_IMAGE_BYTES = 30 * 1024 * 1024
IMAGE_NAME = re.compile(r"[a-f0-9]{32}\.webp")
cover_lock = threading.Lock()
cover_status = {"running": False, "completed": 0, "updated": 0, "total": 0, "failed": 0}
cover_states: dict[int, dict] = {}


def status_for_owner():
    return cover_states.setdefault(accounts.owner_id(), dict(cover_status))


def image_directory() -> Path:
    return library.DOWNLOAD_DIR / "_images"


def image_path(url: str) -> Path:
    name = url.rsplit("/", 1)[-1]
    if not IMAGE_NAME.fullmatch(name):
        raise HTTPException(422, "图片地址无效。")
    return image_directory() / name


def save_image(data: bytes, *, background: bool = False, uid: int | None = None) -> str:
    limit = MAX_BACKGROUND_IMAGE_BYTES if background else MAX_IMAGE_BYTES
    if not data or len(data) > limit:
        raise HTTPException(422, f"请选择 {limit // (1024 * 1024)} MB 以内的 JPG、PNG 或 WebP 图片。")
    try:
        with Image.open(io.BytesIO(data)) as original:
            if original.format not in {"JPEG", "PNG", "WEBP"} or original.width * original.height > 40000000:
                raise HTTPException(422, "图片格式或尺寸不支持。")
            picture = ImageOps.exif_transpose(original).convert("RGB")
            if background:
                picture.thumbnail((2560, 1600), Image.Resampling.LANCZOS)
            else:
                picture = ImageOps.fit(picture, (512, 512), method=Image.Resampling.LANCZOS)
            folder = image_directory()
            folder.mkdir(parents=True, exist_ok=True)
            name = secrets.token_hex(16) + ".webp"
            picture.save(folder / name, "WEBP", quality=90)
            accounts.register_resource("image", name, {"filename": name, "size": (folder / name).stat().st_size}, uid=uid)
            return f"/api/library/images/{name}"
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError) as exc:
        raise HTTPException(422, "图片无法读取，请选择有效的 JPG、PNG 或 WebP 图片。") from exc


def upload_image(file: UploadFile, *, background: bool = False, uid: int | None = None) -> str:
    limit = MAX_BACKGROUND_IMAGE_BYTES if background else MAX_IMAGE_BYTES
    try:
        return save_image(file.file.read(limit + 1), background=background, uid=uid)
    finally:
        file.file.close()


@router.get("/images/{name}")
def serve_image(name: str):
    path = image_path(name)
    if not path.is_file():
        raise HTTPException(404, "图片不存在。")
    accounts.resource("image", name)
    return FileResponse(path, media_type="image/webp", headers={"Cache-Control": "private, no-store"})


def require_track(cursor, key: str) -> str:
    key = library.resolve_track_id(cursor, key)
    cursor.execute("SELECT id,user_id FROM music_tracks WHERE id=%s", (key,))
    row = cursor.fetchone()
    if not row or (row["user_id"] != accounts.owner_id() and accounts.current().role != "super_admin"):
        raise HTTPException(404, "歌曲不存在。")
    return key


@router.post("/tracks/{key}/cover")
def upload_track_cover(key: str, file: UploadFile):
    with library.database() as cursor:
        key = require_track(cursor, key)
        cursor.execute("SELECT user_id FROM music_tracks WHERE id=%s", (key,))
        url = upload_image(file, uid=cursor.fetchone()["user_id"])
        cursor.execute("UPDATE music_tracks SET custom_thumbnail=%s,cover_updated_at=%s WHERE id=%s", (url, time.time(), key))
    return {"id": key, "thumbnail": url, "custom_thumbnail": url}


@router.delete("/tracks/{key}/cover")
def reset_track_cover(key: str):
    with library.database() as cursor:
        key = require_track(cursor, key)
        cursor.execute("UPDATE music_tracks SET custom_thumbnail=NULL,cover_updated_at=%s WHERE id=%s", (time.time(), key))
        cursor.execute("SELECT thumbnail FROM music_tracks WHERE id=%s", (key,))
        thumbnail = cursor.fetchone()["thumbnail"]
    return {"id": key, "thumbnail": thumbnail, "custom_thumbnail": None}


@router.post("/playlists/{key}/cover")
def upload_playlist_cover(key: str, file: UploadFile):
    with library.database() as cursor:
        library.require_playlist(cursor, key)
        cursor.execute("SELECT user_id FROM music_playlists WHERE id=%s", (key,))
        url = upload_image(file, uid=cursor.fetchone()["user_id"])
        cursor.execute("UPDATE music_playlists SET thumbnail=%s WHERE id=%s", (url, key))
    return {"thumbnail": url}


@router.delete("/playlists/{key}/cover")
def reset_playlist_cover(key: str):
    with library.database() as cursor:
        library.require_playlist(cursor, key)
        cursor.execute("UPDATE music_playlists SET thumbnail=NULL WHERE id=%s", (key,))
    return {"thumbnail": None}


@router.post("/profile/avatar")
def upload_avatar(file: UploadFile):
    with library.database() as cursor:
        url = upload_image(file)
        cursor.execute("UPDATE music_profile SET avatar=%s WHERE id=%s", (url, accounts.owner_id()))
    return {"avatar": url}


@router.delete("/profile/avatar")
def reset_avatar():
    with library.database() as cursor:
        cursor.execute("UPDATE music_profile SET avatar=NULL WHERE id=%s", (accounts.owner_id(),))
    return {"avatar": None}


def luminance(color: str) -> float:
    values = [int(color[index:index + 2], 16) / 255 for index in (1, 3, 5)]
    linear = [value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4 for value in values]
    return sum(value * factor for value, factor in zip(linear, (0.2126, 0.7152, 0.0722)))


class Theme(BaseModel):
    mode: Literal["light", "dark"] = "light"
    accent: str = Field(default="#109b6d", pattern=r"^#[a-fA-F0-9]{6}$")
    background: str = Field(default="#fafbf9", pattern=r"^#[a-fA-F0-9]{6}$")
    text: str = Field(default="#252c29", pattern=r"^#[a-fA-F0-9]{6}$")
    background_image: str | None = Field(default=None, pattern=r"^/api/library/images/[a-f0-9]{32}\.webp$")
    fit: Literal["cover", "contain", "tile"] = "cover"
    position: Literal["center", "top", "bottom"] = "center"
    overlay: int = Field(default=88, ge=0, le=100)
    blur: int = Field(default=0, ge=0, le=24)

    @model_validator(mode="after")
    def readable_colors(self):
        light, dark = sorted((luminance(self.background), luminance(self.text)), reverse=True)
        if (light + 0.05) / (dark + 0.05) < 4.5:
            raise ValueError("文字与背景颜色对比度不足，请调整颜色。")
        return self


@router.get("/theme")
def get_theme():
    with library.database() as cursor:
        cursor.execute("SELECT config FROM music_theme WHERE id=%s", (accounts.owner_id(),))
        saved = cursor.fetchone()
    return Theme.model_validate(json.loads(saved["config"]) if saved else {}).model_dump()


@router.put("/theme")
def save_theme(theme: Theme):
    if theme.background_image and not image_path(theme.background_image).is_file():
        raise HTTPException(422, "背景图片不存在，请重新上传。")
    data = theme.model_dump()
    if theme.background_image:
        accounts.resource("image", Path(theme.background_image).name)
    with library.database() as cursor:
        cursor.execute("INSERT INTO music_theme (id,config,user_id) VALUES (%s,%s,%s) ON DUPLICATE KEY UPDATE config=VALUES(config)", (accounts.owner_id(), json.dumps(data), accounts.owner_id()))
    return data


@router.delete("/theme")
def reset_theme():
    with library.database() as cursor:
        cursor.execute("DELETE FROM music_theme WHERE id=%s", (accounts.owner_id(),))
    return Theme().model_dump()


@router.post("/theme/background")
def upload_background(file: UploadFile):
    return {"background_image": upload_image(file, background=True)}


def normalized(value: str) -> str:
    return re.sub(r"[\W_]", "", value).casefold()


def matching_cover(track: dict) -> str | None:
    if not track["artist"] or not track.get("duration"):
        return None
    found = music.search(q=f"{track['title']} {track['artist']}"[:100], source="all", page=1)["tracks"]
    for candidate in found:
        if (normalized(candidate.title) == normalized(track["title"])
                and normalized(candidate.artist) == normalized(track["artist"])
                and candidate.duration and abs(candidate.duration - track["duration"]) <= max(3, track["duration"] * 0.01)
                and candidate.thumbnail):
            return candidate.thumbnail
    return None


def cache_cover(url: str) -> str:
    if url.startswith("/api/") and not url.startswith("/api/image-proxy"):
        return url
    if url.startswith("/api/image-proxy"):
        url = parse_qs(urlparse(url).query).get("url", [""])[0]
    if urlparse(url).scheme not in {"https", "http"}:
        raise ValueError("Invalid cover URL")
    with requests.get(url, headers=music.HEADERS, stream=True, timeout=(4, 10)) as response:
        response.raise_for_status()
        data = bytearray()
        for chunk in response.iter_content(65536):
            data.extend(chunk)
            if len(data) > MAX_IMAGE_BYTES:
                raise ValueError("Cover too large")
    return save_image(bytes(data))


def backfill_covers():
    state = status_for_owner()
    try:
        library.sync_downloads()
        with library.database() as cursor:
            cursor.execute("""SELECT t.*, (SELECT filename FROM music_downloads d WHERE d.track_id=t.id
                AND d.file_available=TRUE ORDER BY created_at DESC LIMIT 1) AS filename FROM music_tracks t
                WHERE custom_thumbnail IS NULL AND cover_checked_at<%s AND t.user_id=%s
                AND (thumbnail IS NULL OR thumbnail NOT LIKE '/api/library/images/%%')""", (time.time() - 86400, accounts.owner_id()))
            tracks = cursor.fetchall()
        with cover_lock:
            state["total"] = len(tracks)
        for track in tracks:
            accounts.active_worker(accounts.owner_id())
            updated = False
            try:
                url = track["thumbnail"]
                if not url and track["filename"]:
                    url = library.inspect_audio(library.DOWNLOAD_DIR / track["filename"])["thumbnail"]
                if not url:
                    url = matching_cover(track)
                if url:
                    thumbnail = cache_cover(url)
                    with library.database() as cursor:
                        cursor.execute("UPDATE music_tracks SET thumbnail=%s WHERE id=%s", (thumbnail, track["id"]))
                    updated = True
            except (requests.RequestException, ValueError, OSError, HTTPException, subprocess.SubprocessError) as exc:
                library.logger.warning("Cover lookup failed: %s", type(exc).__name__)
                with cover_lock:
                    state["failed"] += 1
            finally:
                with library.database() as cursor:
                    cursor.execute("UPDATE music_tracks SET cover_checked_at=%s WHERE id=%s", (time.time(), track["id"]))
                with cover_lock:
                    state["completed"] += 1
                    state["updated"] += int(updated)
    except Exception:
        library.logger.exception("Cover backfill failed")
    finally:
        with cover_lock:
            state["running"] = False


@router.get("/covers/backfill")
def cover_progress():
    with cover_lock:
        return dict(status_for_owner())


@router.post("/covers/backfill")
def start_cover_backfill():
    with cover_lock:
        state = status_for_owner()
        if not state["running"]:
            state.update(running=True, completed=0, updated=0, total=0, failed=0)
            from contextvars import copy_context
            threading.Thread(target=copy_context().run, args=(backfill_covers,), name="library-covers", daemon=True).start()
        return dict(state)
