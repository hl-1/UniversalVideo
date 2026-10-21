"""User-owned storage. Resource identifiers never contain filesystem paths."""
from __future__ import annotations

import hashlib
import json
import mimetypes
from pathlib import Path

from fastapi import HTTPException
from fastapi.responses import FileResponse

from backend.core import paths


FOLDERS = {"file": "files", "thumbnail": "thumbnails", "image": "images",
           "lyrics": "lyrics", "summary_file": "summaries", "batch": "batches", "temp": "tmp"}
LEGACY_FOLDERS = {"file": "", "thumbnail": "_thumbnails", "image": "_images",
                  "lyrics": "_lyrics", "summary_file": "_summaries", "batch": "_batches", "temp": "_tmp"}


def owner(uid: int | None = None) -> int:
    if uid is None:
        from backend.modules.accounts import service as accounts
        uid = accounts.owner_id()
    if isinstance(uid, bool) or not isinstance(uid, int) or uid <= 0:
        raise ValueError("A positive storage owner is required")
    return uid


def filename(name: str) -> str:
    if not name or name in {".", ".."} or Path(name).name != name or any(c in name for c in ("/", "\\", ":", "\x00")):
        raise HTTPException(422, "文件名无效。")
    return name


def contained(path: Path) -> Path:
    root = paths.DOWNLOAD_DIR.resolve()
    if not path.resolve().is_relative_to(root):
        raise HTTPException(422, "文件路径无效。")
    relative = path.relative_to(paths.DOWNLOAD_DIR)
    current = paths.DOWNLOAD_DIR
    if current.is_symlink():
        raise HTTPException(422, "不允许使用符号链接。")
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise HTTPException(422, "不允许使用符号链接。")
    return path


def directory(kind: str = "file", *, uid: int | None = None, create: bool = True) -> Path:
    folder = contained(paths.DOWNLOAD_DIR / "users" / str(owner(uid)) / FOLDERS[kind])
    if create:
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    return folder


def path(kind: str, name: str, *, uid: int | None = None, create: bool = True) -> Path:
    return contained(directory(kind, uid=uid, create=create) / filename(name))


def path_owner(source: Path) -> int:
    contained(source)
    parts = source.relative_to(paths.DOWNLOAD_DIR).parts
    if len(parts) >= 4 and parts[0] == "users" and parts[1].isdigit():
        return owner(int(parts[1]))
    return owner()


def legacy_path(kind: str, name: str, uid: int) -> Path | None:
    """Only an interrupted, explicitly started migration can authorize legacy reads."""
    marker = paths.RUN_DIR / "storage-migrations" / "active.json"
    try:
        data = json.loads(marker.read_text(encoding="utf-8"))
        if data.get("root") != str(paths.DOWNLOAD_DIR.resolve()) or data.get("state") != "applying":
            return None
        expected = str(Path(LEGACY_FOLDERS[kind]) / filename(name))
        for entry in data["entries"]:
            if (entry["uid"], entry["kind"], entry["name"], entry["source"]) != (uid, kind, name, expected):
                continue
            candidate = contained(paths.DOWNLOAD_DIR / expected)
            if candidate.is_file() and candidate.stat().st_size == entry["size"]:
                with candidate.open("rb") as stream:
                    if hashlib.file_digest(stream, "sha256").hexdigest() == entry["sha256"]:
                        return candidate
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return None


def owned_path(kind: str, name: str, *, uid: int | None = None) -> Path:
    uid = owner(uid)
    target = path(kind, name, uid=uid, create=False)
    if target.is_file():
        return target
    return legacy_path(kind, name, uid) or target


def resource_path(kind: str, name: str) -> Path:
    from backend.modules.accounts import service as accounts
    filename(name)
    row = accounts.resource(kind, name)
    return owned_path(kind, name, uid=row["user_id"])


def media_response(source: Path, *, download: bool = False) -> FileResponse:
    if not source.is_file():
        raise HTTPException(404, "文件不存在。")
    media_type = {".m4a": "audio/mp4", ".mp3": "audio/mpeg", ".mp4": "video/mp4"}.get(source.suffix.lower())
    media_type = media_type or mimetypes.guess_type(source.name)[0] or "application/octet-stream"
    return FileResponse(source, filename=source.name, media_type=media_type,
                        content_disposition_type="attachment" if download else "inline",
                        headers={"Cache-Control": "private, no-store"})
