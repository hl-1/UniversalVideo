"""Account/record scoped reuse of immutable, completed local preview files."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
from pathlib import Path

TTL = 1800
MAX_BYTES = 512 * 1024 * 1024
MAX_ENTRIES = 128


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _entries(root: Path):
    for manifest in (root / "_cache").glob("*/manifest.json"):
        try:
            value = json.loads(manifest.read_text(encoding="utf-8"))
            path = manifest.parent / "preview.mp4"
            if (value["version"] != 1 or value["expires_at"] <= time.time()
                    or path.is_symlink() or path.stat().st_size != value["size"]):
                raise ValueError("Expired or incomplete cache")
            yield manifest.parent, value
        except (OSError, ValueError, KeyError, TypeError):
            shutil.rmtree(manifest.parent, ignore_errors=True)


def cleanup(root: Path):
    entries = sorted(_entries(root), key=lambda item: item[1]["created_at"], reverse=True)
    total = 0
    for index, (directory, value) in enumerate(entries):
        total += value["size"]
        if index >= MAX_ENTRIES or total > MAX_BYTES:
            shutil.rmtree(directory, ignore_errors=True)
    # A crash between the hard link and manifest must not retain orphan files.
    for directory in (root / "_cache").glob("*"):
        if directory.is_dir() and not (directory / "manifest.json").is_file():
            shutil.rmtree(directory, ignore_errors=True)


def store(root: Path, source: Path, value: dict, selection: str):
    if not value.get("parse_record_id"):
        return
    cleanup(root)
    size = source.stat().st_size
    if not 0 < size <= MAX_BYTES:
        return
    now = time.time()
    metadata = dict(version=1, user_id=value["user_id"], parse_record_id=value["parse_record_id"],
                    page_hash=digest(value["referer"]), format_hash=digest(selection), size=size,
                    has_audio=value["has_audio"], duration=value["duration"], alternate=value["alternate"],
                    created_at=now, expires_at=min(value["expires_at"], now + TTL))
    for name in ("access_scope", "source_platform", "route_revision"):
        if name in value:
            metadata[name] = value[name]
    identity = digest(json.dumps([metadata[key] for key in ("user_id", "parse_record_id", "page_hash", "format_hash")]))
    directory = root / "_cache" / identity
    shutil.rmtree(directory, ignore_errors=True)
    try:
        directory.mkdir(parents=True)
        # Keep caching off the critical path even for large files. Unsupported
        # filesystems simply skip reuse instead of copying the entire media.
        os.link(source, directory / "preview.mp4")
        (directory / "manifest.json").write_text(json.dumps(metadata), encoding="utf-8")
        cleanup(root)
    except OSError:
        shutil.rmtree(directory, ignore_errors=True)


def find(root: Path, uid: int, record_id: str, page: str, selection: str | None):
    from backend.infrastructure import platform_access, video_proxy
    platform_access.require(page)
    revision = video_proxy.profile()["revision"] if platform_access.platform(page) else None
    candidates = [(directory, value) for directory, value in _entries(root)
                  if value["user_id"] == uid and value["parse_record_id"] == record_id
                  and value["page_hash"] == digest(page)
                  and (revision is None or value.get("route_revision") == revision)
                  and (not selection or value["format_hash"] == digest(selection))]
    return max(candidates, key=lambda item: item[1]["created_at"], default=None)


def purge(root: Path, uid: int, record_id: str | None = None):
    for directory, value in list(_entries(root)):
        if value["user_id"] == uid and (record_id is None or value["parse_record_id"] == record_id):
            shutil.rmtree(directory, ignore_errors=True)
