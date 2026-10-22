"""Immutable shared media bytes; ownership stays on each user's file resource."""
from __future__ import annotations

import hashlib
import os
import secrets
import time
from contextlib import contextmanager
from pathlib import Path

from backend.core import paths, storage
from backend.infrastructure.database import database


@contextmanager
def mutex(identity: str):
    # A connection-scoped lock also coordinates separate application workers.
    with database() as cursor:
        name = "blob:" + hashlib.sha256(identity.encode()).hexdigest()[:56]
        cursor.execute("SELECT GET_LOCK(%s, 15) AS acquired", (name,))
        if not cursor.fetchone()["acquired"]:
            raise TimeoutError("文件发布繁忙，请重试。")
        try:
            yield
        finally:
            cursor.execute("SELECT RELEASE_LOCK(%s)", (name,))


def publish(source: Path) -> tuple[str, int]:
    storage.contained(source)
    with source.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    size = source.stat().st_size
    pool = storage.contained(paths.DOWNLOAD_DIR / "_blobs" / "sha256" / digest[:2])
    pool.mkdir(parents=True, exist_ok=True, mode=0o700)
    blob = pool / digest
    with mutex(digest):
        if blob.exists() and blob.stat().st_size != size:
            raise ValueError("共享文件校验失败。")
        if blob.exists():
            with blob.open("rb") as stream:
                if hashlib.file_digest(stream, "sha256").hexdigest() != digest:
                    raise ValueError("共享文件校验失败。")
        if not blob.exists():
            try:
                os.link(source, blob)
            except OSError:
                # Cross-device storage retains a normal user file.
                return digest, size
        if source.stat().st_ino != blob.stat().st_ino or source.stat().st_dev != blob.stat().st_dev:
            temporary = source.with_name("." + source.name + ".link-" + secrets.token_hex(6))
            try:
                os.link(blob, temporary)
                os.replace(temporary, source)
            finally:
                temporary.unlink(missing_ok=True)
    return digest, size


def collect(*, grace_seconds: int = 86400) -> int:
    """Only pool entries with no user hard links can be collected."""
    root = paths.DOWNLOAD_DIR / "_blobs" / "sha256"
    removed = 0
    for blob in root.glob("*/*"):
        if blob.is_symlink() or not blob.is_file() or time.time() - blob.stat().st_mtime < grace_seconds:
            continue
        with mutex(blob.name):
            if blob.exists() and blob.stat().st_nlink == 1:
                with database() as cursor:
                    cursor.execute("SELECT 1 FROM music_downloads WHERE file_hash=%s AND file_available=TRUE LIMIT 1", (blob.name,))
                    if cursor.fetchone():
                        continue
                    cursor.execute("SELECT 1 FROM music_server_assets WHERE sha256=%s AND status='ready' LIMIT 1", (blob.name,))
                    if cursor.fetchone():
                        continue
                blob.unlink()
                removed += 1
    return removed
