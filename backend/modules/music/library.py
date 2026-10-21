from __future__ import annotations

from backend.core import storage

import hashlib
import json
import logging
import os
import re
import secrets
import subprocess
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Literal
from urllib.parse import quote, urlparse, unquote

import pymysql
from fastapi import APIRouter, HTTPException, UploadFile
from pydantic import BaseModel, Field, field_validator

from backend.core.paths import ROOT_DIR as PROJECT_ROOT
from backend.modules.accounts import service as accounts
from backend.infrastructure.database import database
from backend.infrastructure.runtime import executable_name, ffmpeg_directory


ROOT_DIR = PROJECT_ROOT
DOWNLOAD_DIR = ROOT_DIR / "downloads"
FFMPEG_DIR = ffmpeg_directory()
router = APIRouter(prefix="/api/library", tags=["music-library"])
logger = logging.getLogger("videodream.library")
scan_lock = threading.RLock()
AUDIO_EXTENSIONS = {".mp3", ".m4a", ".flac", ".wav", ".ogg", ".aac", ".opus"}


class TrackInput(BaseModel):
    title: str = Field(min_length=1, max_length=255)
    artist: str = Field(default="", max_length=255)
    provider: Literal["qq", "netease", "kugou", "local", "video"]
    identifier: str = Field(default="", max_length=255)
    webpage_url: str = Field(min_length=1, max_length=2048)
    thumbnail: str | None = Field(default=None, max_length=4096)
    duration: float | None = Field(default=None, ge=0, le=86400)
    access: str = Field(default="", max_length=100)

    @field_validator("webpage_url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        if value.startswith("/api/files/"):
            return value
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("歌曲链接无效")
        return value

    @field_validator("thumbnail")
    @classmethod
    def validate_thumbnail(cls, value: str | None) -> str | None:
        if value and not value.startswith("/api/") and urlparse(value).scheme not in {"http", "https"}:
            raise ValueError("封面地址无效")
        return value


class FavoriteRequest(BaseModel):
    track: TrackInput
    liked: bool


class HistoryRequest(BaseModel):
    track: TrackInput
    preview: bool = False


class PlaylistRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    description: str = Field(default="", max_length=300)

    @field_validator("name")
    @classmethod
    def non_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("歌单名称不能为空")
        return value.strip()


class ProfileRequest(BaseModel):
    nickname: str = Field(min_length=1, max_length=40)
    bio: str = Field(default="", max_length=200)
    avatar: str | None = Field(default=None, max_length=700000)

    @field_validator("nickname")
    @classmethod
    def non_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("昵称不能为空")
        return value.strip()

    @field_validator("avatar")
    @classmethod
    def image_only(cls, value: str | None) -> str | None:
        if value and not (re.fullmatch(r"data:image/(?:png|jpeg|webp);base64,[A-Za-z0-9+/=]+", value)
                          or re.fullmatch(r"/api/library/images/[a-f0-9]{32}\.webp", value)):
            raise ValueError("头像需为 PNG、JPEG 或 WebP 图片")
        return value
SCHEMA = [
    """CREATE TABLE IF NOT EXISTS music_profile (
        id INT PRIMARY KEY, nickname VARCHAR(40) NOT NULL, bio VARCHAR(200) NOT NULL DEFAULT '', avatar MEDIUMTEXT
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS music_tracks (
        id CHAR(64) PRIMARY KEY, provider VARCHAR(12) NOT NULL, identifier VARCHAR(255) NOT NULL,
        title VARCHAR(255) NOT NULL, artist VARCHAR(255) NOT NULL, webpage_url TEXT NOT NULL,
        thumbnail TEXT, duration DOUBLE, access VARCHAR(100) NOT NULL
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS music_favorites (
        track_id CHAR(64) PRIMARY KEY, created_at DOUBLE NOT NULL,
        FOREIGN KEY (track_id) REFERENCES music_tracks(id) ON DELETE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS music_history (
        track_id CHAR(64) PRIMARY KEY, played_at DOUBLE NOT NULL, play_count INT NOT NULL DEFAULT 1,
        preview BOOLEAN NOT NULL DEFAULT FALSE,
        FOREIGN KEY (track_id) REFERENCES music_tracks(id) ON DELETE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS music_playlists (
        id CHAR(24) PRIMARY KEY, name VARCHAR(80) NOT NULL, description VARCHAR(300) NOT NULL,
        created_at DOUBLE NOT NULL
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS music_playlist_tracks (
        playlist_id CHAR(24) NOT NULL, track_id CHAR(64) NOT NULL, added_at DOUBLE NOT NULL,
        PRIMARY KEY (playlist_id, track_id),
        FOREIGN KEY (playlist_id) REFERENCES music_playlists(id) ON DELETE CASCADE,
        FOREIGN KEY (track_id) REFERENCES music_tracks(id) ON DELETE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS music_downloads (
        id CHAR(24) PRIMARY KEY, track_id CHAR(64) NOT NULL, filename VARCHAR(255) NOT NULL UNIQUE,
        created_at DOUBLE NOT NULL, imported BOOLEAN NOT NULL DEFAULT FALSE,
        FOREIGN KEY (track_id) REFERENCES music_tracks(id) ON DELETE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS music_track_aliases (
        alias_id CHAR(64) PRIMARY KEY, track_id CHAR(64) NOT NULL,
        FOREIGN KEY (track_id) REFERENCES music_tracks(id) ON DELETE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS music_lyrics (
        track_id CHAR(64) PRIMARY KEY, text MEDIUMTEXT NOT NULL, source VARCHAR(80) NOT NULL,
        format VARCHAR(8) NOT NULL, signature TEXT NOT NULL, checked_at DOUBLE NOT NULL,
        manual BOOLEAN NOT NULL DEFAULT FALSE, updated_at DOUBLE NOT NULL,
        FOREIGN KEY (track_id) REFERENCES music_tracks(id) ON DELETE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS music_theme (
        id INT PRIMARY KEY, config TEXT NOT NULL
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
]


def backup_library(cursor) -> Path:
    snapshot = {}
    cursor.execute("SHOW TABLES LIKE 'music_%'")
    for row in cursor.fetchall():
        table = next(iter(row.values()))
        cursor.execute(f"SELECT * FROM `{table}`")
        snapshot[table] = cursor.fetchall()
    folder = ROOT_DIR / ".run" / "library-backups"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{time.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(4)}.json"
    path.write_text(json.dumps(snapshot, ensure_ascii=False, default=str), encoding="utf-8")
    return path


def migrate_schema(cursor) -> None:
    additions = {
        "music_tracks": {"custom_thumbnail": "TEXT", "cover_updated_at": "DOUBLE NOT NULL DEFAULT 0",
                         "cover_checked_at": "DOUBLE NOT NULL DEFAULT 0"},
        "music_playlists": {"thumbnail": "TEXT"},
        "music_downloads": {"file_hash": "CHAR(64)", "file_size": "BIGINT", "file_mtime": "BIGINT",
                            "file_available": "BOOLEAN NOT NULL DEFAULT TRUE"},
    }
    missing = []
    for table, columns in additions.items():
        cursor.execute(f"SHOW COLUMNS FROM `{table}`")
        existing = {row["Field"] for row in cursor.fetchall()}
        missing.extend((table, name, definition) for name, definition in columns.items() if name not in existing)
    if missing:
        backup_library(cursor)
        for table, name, definition in missing:
            cursor.execute(f"ALTER TABLE `{table}` ADD COLUMN `{name}` {definition}")
        cursor.execute("SHOW INDEX FROM music_downloads WHERE Key_name='file_hash_index'")
        if not cursor.fetchone():
            cursor.execute("CREATE INDEX file_hash_index ON music_downloads(file_hash)")


def track_id(track: TrackInput) -> str:
    identity = track.identifier or track.webpage_url
    prefix = "" if accounts.owner_id() == 1 else f"{accounts.owner_id()}:"
    return hashlib.sha256(f"{prefix}{track.provider}:{identity}".encode()).hexdigest()


def save_track(cursor, track: TrackInput) -> str:
    if track.provider == "local" or track.webpage_url.startswith("/api/files/"):
        accounts.resource("file", unquote(Path(track.webpage_url).name))
    if track.thumbnail and track.thumbnail.startswith("/api/") and not track.thumbnail.startswith("/api/image-proxy"):
        kind = "image" if track.thumbnail.startswith("/api/library/images/") else "thumbnail"
        accounts.resource(kind, Path(track.thumbnail).name)
    key = track_id(track)
    canonical = resolve_track_id(cursor, key)
    if canonical != key:
        return canonical
    cursor.execute("""INSERT INTO music_tracks
        (id, provider, identifier, title, artist, webpage_url, thumbnail, duration, access, user_id)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON DUPLICATE KEY UPDATE
        title=VALUES(title), artist=VALUES(artist),
        thumbnail=IF(thumbnail LIKE '/api/library/images/%%',thumbnail,COALESCE(VALUES(thumbnail),thumbnail)),
        duration=COALESCE(VALUES(duration),duration), webpage_url=VALUES(webpage_url), access=VALUES(access)""",
        (key, track.provider, track.identifier, track.title, track.artist, track.webpage_url,
         track.thumbnail, track.duration, track.access, accounts.owner_id()))
    return key


def resolve_track_id(cursor, key: str) -> str:
    cursor.execute("SELECT track_id FROM music_track_aliases WHERE alias_id=%s", (key,))
    alias = cursor.fetchone()
    canonical = alias["track_id"] if alias else key
    cursor.execute("SELECT user_id FROM music_tracks WHERE id=%s", (canonical,))
    existing = cursor.fetchone()
    if existing and existing["user_id"] != accounts.owner_id() and accounts.current().role != "super_admin":
        raise HTTPException(404, "歌曲不存在。")
    return canonical


def write_lyrics(cursor, key: str, data: dict) -> None:
    manual = data.get("source") in {"本地 LRC", "手动导入"} or bool(data.get("manual"))
    cursor.execute("SELECT * FROM music_lyrics WHERE track_id=%s FOR UPDATE", (key,))
    existing = cursor.fetchone()
    if existing and existing["manual"] and not manual:
        return
    text = data.get("text", "")
    cursor.execute("""INSERT INTO music_lyrics
        (track_id,text,source,format,signature,checked_at,manual,updated_at,user_id) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON DUPLICATE KEY UPDATE text=VALUES(text),source=VALUES(source),format=VALUES(format),
        signature=VALUES(signature),checked_at=VALUES(checked_at),manual=VALUES(manual),updated_at=VALUES(updated_at)""",
        (key, text, data.get("source", ""), "lrc" if re.search(r"\[\d+:\d+", text) else "plain",
         data.get("signature", ""), data.get("checked_at", time.time()), manual, data.get("updated_at", time.time()), accounts.owner_id()))

    cursor.execute("SHOW TABLES LIKE 'music_track_features'")
    if cursor.fetchone():
        cursor.execute("UPDATE music_track_features SET expires_at=0 WHERE user_id=%s AND track_key=%s", (accounts.owner_id(), key))


def read_lyrics(filename: str) -> dict | None:
    accounts.resource("file", filename)
    with database() as cursor:
        cursor.execute("""SELECT l.* FROM music_lyrics l JOIN music_downloads d ON d.track_id=l.track_id
            WHERE d.filename=%s""", (filename,))
        return cursor.fetchone()


def store_lyrics(filename: str, data: dict) -> dict | None:
    accounts.resource("file", filename)
    with database() as cursor:
        cursor.execute("SELECT track_id FROM music_downloads WHERE filename=%s FOR UPDATE", (filename,))
        download = cursor.fetchone()
        if not download:
            raise HTTPException(409, "音频尚未加入音乐库，请先导入音乐。")
        write_lyrics(cursor, download["track_id"], data)
        cursor.execute("SELECT * FROM music_lyrics WHERE track_id=%s", (download["track_id"],))
        return cursor.fetchone()


def merge_tracks(cursor, first: str, second: str) -> str:
    first, second = resolve_track_id(cursor, first), resolve_track_id(cursor, second)
    if first == second:
        return first
    cursor.execute("SELECT * FROM music_tracks WHERE id IN (%s,%s) FOR UPDATE", (first, second))
    rows = cursor.fetchall()
    rows.sort(key=lambda row: (row["provider"] != "local", bool(row["thumbnail"]), bool(row["artist"]), row["id"]), reverse=True)
    keep, old = rows
    key, previous = keep["id"], old["id"]
    custom = max(rows, key=lambda row: (bool(row["custom_thumbnail"]), row["cover_updated_at"]))
    cursor.execute("""UPDATE music_tracks SET thumbnail=COALESCE(thumbnail,%s),
        artist=IF(artist='',%s,artist),custom_thumbnail=%s,cover_updated_at=%s WHERE id=%s""",
        (old["thumbnail"], old["artist"], custom["custom_thumbnail"], custom["cover_updated_at"], key))
    cursor.execute("""INSERT INTO music_favorites (track_id,created_at,user_id)
        SELECT %s,src.created_at,src.user_id FROM music_favorites AS src WHERE src.track_id=%s
        ON DUPLICATE KEY UPDATE created_at=LEAST(music_favorites.created_at,VALUES(created_at))""", (key, previous))
    cursor.execute("""INSERT INTO music_history (track_id,played_at,play_count,preview,user_id)
        SELECT %s,src.played_at,src.play_count,src.preview,src.user_id FROM music_history AS src WHERE src.track_id=%s
        ON DUPLICATE KEY UPDATE preview=IF(VALUES(played_at)>music_history.played_at,VALUES(preview),music_history.preview),
        played_at=GREATEST(music_history.played_at,VALUES(played_at)),play_count=music_history.play_count+VALUES(play_count)""", (key, previous))
    cursor.execute("""INSERT INTO music_playlist_tracks (playlist_id,track_id,added_at,position)
        SELECT src.playlist_id,%s,src.added_at,src.position FROM music_playlist_tracks AS src WHERE src.track_id=%s
        ON DUPLICATE KEY UPDATE added_at=LEAST(music_playlist_tracks.added_at,VALUES(added_at)),
        position=COALESCE(LEAST(music_playlist_tracks.position,VALUES(position)),music_playlist_tracks.position,VALUES(position))""", (key, previous))
    cursor.execute("SELECT * FROM music_lyrics WHERE track_id IN (%s,%s)", (key, previous))
    lyrics = cursor.fetchall()
    if lyrics:
        selected = max(lyrics, key=lambda row: (bool(row["manual"]), bool(row["text"]), row["updated_at"]))
        write_lyrics(cursor, key, selected)
    cursor.execute("UPDATE music_downloads SET track_id=%s WHERE track_id=%s", (key, previous))
    cursor.execute("UPDATE music_track_aliases SET track_id=%s WHERE track_id=%s", (key, previous))
    cursor.execute("DELETE FROM music_track_features WHERE track_key IN (%s,%s)", (key, previous))
    cursor.execute("INSERT INTO music_track_aliases VALUES (%s,%s) ON DUPLICATE KEY UPDATE track_id=VALUES(track_id)", (previous, key))
    cursor.execute("DELETE FROM music_tracks WHERE id=%s", (previous,))
    return key
TRACK_SELECT = """SELECT t.*, (f.track_id IS NOT NULL) AS liked, h.played_at, h.play_count, h.preview,
    d.filename, d.imported FROM music_tracks t
    LEFT JOIN music_favorites f ON f.track_id=t.id
    LEFT JOIN music_history h ON h.track_id=t.id
    LEFT JOIN music_downloads d ON d.id=(SELECT x.id FROM music_downloads x
        WHERE x.track_id=t.id ORDER BY x.file_available DESC,x.created_at DESC,x.id DESC LIMIT 1) """


def serialize_track(row: dict) -> dict:
    result = dict(row)
    result["liked"] = bool(result["liked"])
    result["automatic_thumbnail"] = result.get("thumbnail")
    result["thumbnail"] = result.get("custom_thumbnail") or result.get("thumbnail")
    if result.get("filename"):
        exists = (storage.owned_path("file", result["filename"], uid=result.get("user_id"))).is_file()
        result["local_url"] = f"/api/files/{quote(result['filename'], safe='')}" if exists else None
        result["file_available"] = exists
    return result


def record_download(filename: str, metadata: dict, *, imported: bool = False) -> None:
    safe_name = Path(filename).name
    if safe_name != filename or not (storage.owned_path("file", safe_name)).is_file():
        raise ValueError("Download file is outside the library")
    track = TrackInput.model_validate(metadata)
    path = storage.owned_path("file", safe_name)
    fingerprint = hash_file(path)
    stat = path.stat()
    accounts.register_resource("file", safe_name, {"filename": safe_name, "size": stat.st_size})
    with scan_lock, database() as cursor:
        key = save_track(cursor, track)
        cursor.execute("SELECT DISTINCT track_id FROM music_downloads WHERE file_hash=%s AND user_id=%s", (fingerprint, accounts.owner_id()))
        matches = cursor.fetchall()
        if any(row["track_id"] != key for row in matches):
            backup_library(cursor)
        for row in matches:
            key = merge_tracks(cursor, key, row["track_id"])
        cursor.execute("""INSERT INTO music_downloads
            (id,track_id,filename,created_at,imported,file_hash,file_size,file_mtime,file_available,user_id)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,TRUE,%s) ON DUPLICATE KEY UPDATE track_id=VALUES(track_id),
            file_hash=VALUES(file_hash),file_size=VALUES(file_size),file_mtime=VALUES(file_mtime),file_available=TRUE""",
            (secrets.token_hex(12), key, safe_name, time.time(), imported, fingerprint, stat.st_size, stat.st_mtime_ns, accounts.owner_id()))
        migrate_file_lyrics(cursor, path, key)


def hash_file(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def migrate_file_lyrics(cursor, path: Path, key: str) -> None:
    cursor.execute("SELECT * FROM music_lyrics WHERE track_id=%s", (key,))
    existing = cursor.fetchone()
    cached = storage.owned_path("lyrics", hashlib.sha256(path.name.encode()).hexdigest() + ".json", uid=storage.path_owner(path))
    try:
        if cached.is_file() and cached.stat().st_size < 600000:
            data = json.loads(cached.read_text(encoding="utf-8"))
            if isinstance(data.get("text"), str) and len(data["text"].encode()) <= 512000:
                if not existing or (not existing["text"] and data["text"]) or (not existing["manual"] and data.get("source") == "本地 LRC"):
                    write_lyrics(cursor, key, data)
        sidecar = path.with_suffix(".lrc")
        if sidecar.is_file() and sidecar.stat().st_size <= 512000 and not (existing and existing["manual"]):
            from backend.modules.music.lyrics import decode_lrc, parse_lrc
            text = decode_lrc(sidecar.read_bytes())
            if parse_lrc(text):
                write_lyrics(cursor, key, {"text": text, "source": "本地 LRC"})
    except (ValueError, OSError, HTTPException):
        logger.warning("Skipped unreadable legacy lyrics")


def inspect_audio(path: Path) -> dict:
    result = subprocess.run(
        [str(FFMPEG_DIR / executable_name("ffprobe")), "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
    )
    if result.returncode:
        raise ValueError("文件不是有效音频。")
    data = json.loads(result.stdout)
    streams = data.get("streams") or []
    if not any(stream.get("codec_type") == "audio" for stream in streams):
        raise ValueError("文件没有音频轨。")
    tags = data.get("format", {}).get("tags", {})
    tags = {key.lower(): value for key, value in tags.items()}
    stem = re.sub(r"^(?:[a-f0-9]{12}|import-[a-f0-9]{12}|musicverify[a-z]+)\.", "", path.stem)
    thumbnail = None
    if any(stream.get("disposition", {}).get("attached_pic") for stream in streams):
        cover = storage.path("thumbnail", f"audio-{hashlib.sha256(path.name.encode()).hexdigest()[:24]}.jpg", uid=storage.path_owner(path))
        cover.parent.mkdir(exist_ok=True)
        artwork = subprocess.run([str(FFMPEG_DIR / executable_name("ffmpeg")), "-loglevel", "error", "-nostdin", "-y",
                                  "-i", str(path), "-map", "0:v:0", "-frames:v", "1", "-vf", "scale=300:-1", str(cover)],
                                 capture_output=True, timeout=30)
        if artwork.returncode == 0:
            thumbnail = f"/api/thumbnails/{cover.name}"
            accounts.register_resource("thumbnail", cover.name, {"filename": cover.name, "size": cover.stat().st_size}, uid=storage.path_owner(path))
    tagged_title = str(tags.get("title") or "")
    title = stem if re.fullmatch(r"Track\s*\d+", tagged_title, re.I) else tagged_title or stem
    return {"title": title[:255], "artist": str(tags.get("artist") or "")[:255],
            "provider": "local", "identifier": path.name, "webpage_url": f"/api/files/{quote(path.name, safe='')}",
            "duration": float(data.get("format", {}).get("duration") or 0), "thumbnail": thumbnail}


def sync_downloads() -> None:
    # Serialize scans so simultaneous page requests cannot probe the same files repeatedly.
    with scan_lock:
        with database() as cursor:
            cursor.execute("SELECT * FROM music_downloads WHERE user_id=%s", (accounts.owner_id(),))
            known = {row["filename"]: row for row in cursor.fetchall()}
            for name, row in known.items():
                path = storage.owned_path("file", name)
                exists = path.is_file()
                cursor.execute("UPDATE music_downloads SET file_available=%s WHERE id=%s", (exists, row["id"]))
                if exists:
                    stat = path.stat()
                    if not row["file_hash"] or row["file_size"] != stat.st_size or row["file_mtime"] != stat.st_mtime_ns:
                        cursor.execute("UPDATE music_downloads SET file_hash=%s,file_size=%s,file_mtime=%s WHERE id=%s",
                                       (hash_file(path), stat.st_size, stat.st_mtime_ns, row["id"]))
                    migrate_file_lyrics(cursor, path, row["track_id"])
            cursor.execute("SELECT file_hash FROM music_downloads WHERE file_hash IS NOT NULL AND user_id=%s GROUP BY file_hash HAVING COUNT(DISTINCT track_id)>1", (accounts.owner_id(),))
            duplicates = cursor.fetchall()
            if duplicates:
                backup_library(cursor)
            for group in duplicates:
                cursor.execute("SELECT DISTINCT track_id FROM music_downloads WHERE file_hash=%s AND user_id=%s", (group["file_hash"], accounts.owner_id()))
                keys = [resolve_track_id(cursor, row["track_id"]) for row in cursor.fetchall()]
                key = keys[0]
                for other in keys[1:]:
                    key = merge_tracks(cursor, key, resolve_track_id(cursor, other))
            cursor.execute("""SELECT t.id,t.title,d.filename FROM music_tracks t JOIN music_downloads d ON d.track_id=t.id
                WHERE t.provider='local' AND d.file_available=TRUE AND t.user_id=%s""", (accounts.owner_id(),))
            for row in cursor.fetchall():
                if re.fullmatch(r"Track\s*\d+", row["title"], re.I) or row["title"].startswith("musicverify"):
                    try:
                        metadata = inspect_audio(storage.owned_path("file", row["filename"]))
                        cursor.execute("UPDATE music_tracks SET title=%s,artist=IF(artist='',%s,artist) WHERE id=%s",
                                       (metadata["title"], metadata["artist"], row["id"]))
                    except (ValueError, OSError, subprocess.SubprocessError):
                        logger.warning("Skipped unreadable local metadata")
        with database() as cursor:
            cursor.execute("SELECT resource_key FROM resources WHERE kind='file' AND deleted=FALSE AND user_id=%s", (accounts.owner_id(),))
            owned = {row["resource_key"] for row in cursor.fetchall()}
        for path in storage.directory().iterdir():
            if path.is_file() and path.name in owned and path.suffix.lower() in AUDIO_EXTENSIONS and path.name not in known and ".partial" not in path.name:
                try:
                    record_download(path.name, inspect_audio(path), imported=path.name.startswith("import-"))
                except (ValueError, OSError, subprocess.SubprocessError):
                    logger.warning("Skipped unreadable local audio")


@router.get("")
def overview() -> dict:
    sync_downloads()
    with database() as cursor:
        cursor.execute("SELECT nickname, bio, avatar FROM music_profile WHERE id=%s", (accounts.owner_id(),))
        profile = cursor.fetchone()
        counts = {}
        for key, table in (("favorites", "music_favorites"), ("history", "music_history"), ("downloads", "music_downloads")):
            expression = "DISTINCT track_id" if key == "downloads" else "*"
            cursor.execute(f"SELECT COUNT({expression}) AS n FROM {table} WHERE user_id=%s", (accounts.owner_id(),))
            counts[key] = cursor.fetchone()["n"]
        cursor.execute("SELECT COUNT(*) AS n FROM music_history WHERE preview=TRUE AND user_id=%s", (accounts.owner_id(),))
        counts["trials"] = cursor.fetchone()["n"]
        cursor.execute("""SELECT p.*, COUNT(pt.track_id) AS count FROM music_playlists p
            LEFT JOIN music_playlist_tracks pt ON pt.playlist_id=p.id WHERE p.user_id=%s GROUP BY p.id ORDER BY p.created_at DESC""", (accounts.owner_id(),))
        playlists = cursor.fetchall()
        cursor.execute("SELECT provider,identifier,webpage_url,COALESCE(custom_thumbnail,thumbnail) AS thumbnail FROM music_tracks WHERE user_id=%s LIMIT 1000", (accounts.owner_id(),))
        artwork = cursor.fetchall()
    return {"profile": profile, "counts": counts, "playlists": playlists, "artwork": artwork}


@router.post("/tracks")
def index_track(payload: TrackInput) -> dict:
    with database() as cursor:
        key = save_track(cursor, payload)
    return {"id": key}


@router.get("/tracks")
def tracks(view: Literal["all", "favorites", "history", "downloads", "trials", "playlist"] = "history", playlist_id: str = "") -> dict:
    sync_downloads()
    with database() as cursor:
        parameters = ()
        if view == "all":
            query = TRACK_SELECT + "WHERE t.user_id=%s ORDER BY t.title,t.id"
        elif view == "favorites":
            query = TRACK_SELECT + "WHERE t.user_id=%s AND f.track_id IS NOT NULL ORDER BY f.created_at DESC"
        elif view in {"history", "trials"}:
            query = TRACK_SELECT + "WHERE t.user_id=%s AND h.track_id IS NOT NULL "
            if view == "trials":
                query += "AND h.preview=TRUE "
            query += "ORDER BY h.played_at DESC"
        elif view == "downloads":
            query = TRACK_SELECT + "WHERE t.user_id=%s AND d.id IS NOT NULL ORDER BY d.created_at DESC"
        else:
            require_playlist(cursor, playlist_id)
            query = TRACK_SELECT + "JOIN music_playlist_tracks pt ON pt.track_id=t.id WHERE t.user_id=%s AND pt.playlist_id=%s ORDER BY pt.position IS NULL,pt.position,pt.added_at,pt.track_id"
            parameters = (playlist_id,)
        cursor.execute(query + " LIMIT 1000", (accounts.owner_id(),) + parameters)
        result = [serialize_track(row) for row in cursor.fetchall()]
    return {"tracks": result}


@router.put("/favorites")
def favorite(payload: FavoriteRequest) -> dict:
    with database() as cursor:
        key = save_track(cursor, payload.track)
        if payload.liked:
            cursor.execute("INSERT IGNORE INTO music_favorites (track_id,created_at,user_id) VALUES (%s,%s,%s)", (key, time.time(), accounts.owner_id()))
        else:
            cursor.execute("DELETE FROM music_favorites WHERE track_id=%s", (key,))
    return {"id": key, "liked": payload.liked}


@router.post("/history")
def played(payload: HistoryRequest) -> dict:
    with database() as cursor:
        key = save_track(cursor, payload.track)
        cursor.execute("""INSERT INTO music_history (track_id, played_at, preview,user_id) VALUES (%s,%s,%s,%s)
            ON DUPLICATE KEY UPDATE played_at=VALUES(played_at), play_count=play_count+1, preview=VALUES(preview)""",
            (key, time.time(), payload.preview, accounts.owner_id()))
    return {"id": key}


@router.delete("/history")
def clear_history() -> dict:
    with database() as cursor:
        cursor.execute("DELETE FROM music_history WHERE user_id=%s", (accounts.owner_id(),))
    return {"ok": True}


@router.delete("/history/{key}")
def remove_history(key: str) -> dict:
    with database() as cursor:
        key = resolve_track_id(cursor, key)
        cursor.execute("DELETE FROM music_history WHERE track_id=%s AND user_id=%s", (key, accounts.owner_id()))
    return {"ok": True}


def require_playlist(cursor, key: str) -> None:
    cursor.execute("SELECT id,user_id FROM music_playlists WHERE id=%s", (key,))
    playlist = cursor.fetchone()
    if not playlist or (playlist["user_id"] != accounts.owner_id() and accounts.current().role != "super_admin"):
        raise HTTPException(404, "歌单不存在。")


@router.post("/playlists", status_code=201)
def create_playlist(payload: PlaylistRequest) -> dict:
    key = secrets.token_hex(12)
    with database() as cursor:
        cursor.execute("INSERT INTO music_playlists (id,name,description,created_at,user_id) VALUES (%s,%s,%s,%s,%s)", (key, payload.name, payload.description, time.time(), accounts.owner_id()))
    return {"id": key}


@router.patch("/playlists/{key}")
def update_playlist(key: str, payload: PlaylistRequest) -> dict:
    with database() as cursor:
        require_playlist(cursor, key)
        cursor.execute("UPDATE music_playlists SET name=%s, description=%s WHERE id=%s", (payload.name, payload.description, key))
    return {"ok": True}


@router.delete("/playlists/{key}")
def delete_playlist(key: str) -> dict:
    with database() as cursor:
        require_playlist(cursor, key)
        cursor.execute("DELETE FROM music_playlists WHERE id=%s", (key,))
    return {"ok": True}


@router.post("/playlists/{key}/tracks")
def add_to_playlist(key: str, payload: TrackInput) -> dict:
    with database() as cursor:
        require_playlist(cursor, key)
        track_key = save_track(cursor, payload)
        cursor.execute("SELECT MAX(position) AS last_position FROM music_playlist_tracks WHERE playlist_id=%s", (key,))
        last = cursor.fetchone()["last_position"]
        cursor.execute("INSERT IGNORE INTO music_playlist_tracks (playlist_id,track_id,added_at,position) VALUES (%s,%s,%s,%s)",
                       (key, track_key, time.time(), last + 1 if last is not None else None))
    return {"id": track_key}


@router.delete("/playlists/{key}/tracks/{track_key}")
def remove_from_playlist(key: str, track_key: str) -> dict:
    with database() as cursor:
        require_playlist(cursor, key)
        track_key = resolve_track_id(cursor, track_key)
        cursor.execute("DELETE FROM music_playlist_tracks WHERE playlist_id=%s AND track_id=%s", (key, track_key))
    return {"ok": True}


@router.patch("/profile")
def update_profile(payload: ProfileRequest) -> dict:
    if payload.avatar and payload.avatar.startswith("/api/"):
        accounts.resource("image", Path(payload.avatar).name)
    with database() as cursor:
        cursor.execute("UPDATE music_profile SET nickname=%s, bio=%s, avatar=%s WHERE id=%s",
                       (payload.nickname, payload.bio, payload.avatar, accounts.owner_id()))
    return payload.model_dump()


@router.post("/import", status_code=201)
def import_audio(file: UploadFile) -> dict:
    original = Path(file.filename or "audio").name
    extension = Path(original).suffix.lower()
    if extension not in AUDIO_EXTENSIONS:
        raise HTTPException(422, "请选择 MP3、M4A、FLAC、WAV、OGG、AAC 或 OPUS 音频。")
    with database():
        pass
    safe_name = re.sub(r'[\\/:*?"<>|]', "_", original)[:160]
    destination = storage.path("file", f"import-{secrets.token_hex(6)}.{safe_name}")
    total = 0
    try:
        with destination.open("xb") as output:
            while chunk := file.file.read(256 * 1024):
                total += len(chunk)
                if total > 512 * 1024 * 1024:
                    raise HTTPException(413, "单个音频文件不能超过 512 MB。")
                output.write(chunk)
        metadata = inspect_audio(destination)
        record_download(destination.name, metadata, imported=True)
    except Exception as exc:
        destination.unlink(missing_ok=True)
        if isinstance(exc, HTTPException):
            raise
        raise HTTPException(422, "音频文件无法读取，请检查文件是否完整。") from exc
    finally:
        file.file.close()
    return {"filename": destination.name, "track": metadata}


def download_playlist(key: str | None, name: str | None) -> str | None:
    """Resolve a download destination only when a batch is submitted."""
    if key and name is not None:
        raise HTTPException(422, "已有歌单和新歌单只能选择一个。")
    if name is not None:
        if not name.strip():
            raise HTTPException(422, "请输入歌单名称。")
        return create_playlist(PlaylistRequest(name=name.strip()))["id"]
    if key:
        with database() as cursor:
            require_playlist(cursor, key)
    return key
