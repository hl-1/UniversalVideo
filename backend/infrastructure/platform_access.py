"""Source-based authorization shared by video, music, previews and derived files."""
from __future__ import annotations

import json
import re
from contextvars import ContextVar
from urllib.parse import unquote, urlparse

from fastapi import HTTPException

DOMAINS = {
    "youtube": ("youtube.com", "youtu.be", "youtube-nocookie.com", "googlevideo.com", "ytimg.com",
                "youtubei.googleapis.com", "youtube.googleapis.com"),
    "pornhub": ("pornhub.com", "pornhub.org", "pornhubpremium.com", "phncdn.com", "phprcdn.com"),
}
_cache: ContextVar[dict | None] = ContextVar("platform_access_cache", default=None)
_URL = re.compile(r"https?://[^\s\"'<>]+", re.I)
_LOCAL = re.compile(r"/api/(files|summary-files|thumbnails|watermarks/videos|library/images)/([^/?#]+)")


def platform(url: str | None) -> str | None:
    try:
        host = (urlparse(url or "").hostname or "").lower().rstrip(".")
    except ValueError:
        return None
    return next((name for name, domains in DOMAINS.items()
                 if any(host == domain or host.endswith("." + domain) for domain in domains)), None)


def source(value, depth=0) -> str | None:
    if depth > 10:
        return None
    if isinstance(value, str):
        decoded = unquote(value)
        for url in _URL.findall(decoded):
            found = platform(url)
            if found:
                return found
    elif isinstance(value, dict):
        explicit = str(value.get("source_platform") or value.get("platform") or "").lower()
        if explicit in {"youtube", "pornhub"}:
            return explicit
        for key, item in value.items():
            # Collections are authorized one row at a time, never by their siblings.
            if key not in {"items", "tracks", "playlists", "artwork", "versions", "related_tasks"}:
                found = source(item, depth + 1)
                if found:
                    return found
    elif isinstance(value, (list, tuple)):
        return next((found for item in value if (found := source(item, depth + 1))), None)
    return None


def _payload(row):
    value = row.get("payload", row) if isinstance(row, dict) else {}
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (ValueError, TypeError):
            return {}
    return value if isinstance(value, dict) else {}


def resource_metadata(kind: str, key: str, seen=None) -> dict:
    """Follow durable lineage, including legacy task-prefixed output filenames."""
    from backend.modules.accounts import service as accounts
    from backend.modules.video import store
    seen = set() if seen is None else seen
    identity = (kind, key)
    if identity in seen or len(seen) >= 16:
        return {}
    seen.add(identity)
    cache = _cache.get()
    if cache is not None and identity in cache:
        return cache[identity]
    with accounts.database() as cursor:
        if kind == "track":
            cursor.execute("SELECT * FROM music_tracks WHERE id=%s", (key,))
            row = cursor.fetchone()
        elif kind in store.TABLES:
            row = store.get(cursor, kind, key, include_deleted=True)
        else:
            cursor.execute("SELECT * FROM resources WHERE kind=%s AND resource_key=%s", (kind, key))
            row = cursor.fetchone()
        data = _payload(row)
        metadata = metadata_for(data, seen)
        if not metadata and kind in {"file", "summary_file"}:
            for identifier in re.findall(r"(?:^|\.)([a-f0-9]{12})(?=\.)", key):
                for parent in ("download", "watermark", "summary"):
                    metadata = resource_metadata(parent, identifier, seen)
                    if metadata:
                        break
                if metadata:
                    break
        if not metadata and kind in {"thumbnail", "image"}:
            # Older thumbnail indexes only stored filename/size. Recover their origin.
            cursor.execute("SELECT snapshot FROM video_parse_records WHERE snapshot LIKE %s", ("%" + key + "%",))
            for record in cursor.fetchall():
                if (metadata := metadata_for(_payload({"payload": record["snapshot"]}), seen)):
                    break
            if not metadata:
                cursor.execute("SELECT webpage_url FROM music_tracks WHERE thumbnail LIKE %s OR custom_thumbnail LIKE %s", ("%" + key + "%", "%" + key + "%"))
                for track in cursor.fetchall():
                    if (metadata := metadata_for(track, seen)):
                        break
    if cache is not None:
        cache[identity] = metadata
    return metadata


def metadata_for(value, seen=None) -> dict:
    value = _payload(value)
    found = source(value)
    if found or value.get("access_scope") == "super_admin":
        return {"access_scope": "super_admin", "source_platform": found or "restricted"}
    links = (("parse_record_id", "video_parse"), ("record_id", "video_parse"), ("summary_task_id", "summary"), ("mind_map_id", "mind_map"), ("track_id", "track"),
             ("source_task_id", "download"), ("source_filename", "file"), ("filename", "file"))
    for field, kind in links:
        key = value.get(field)
        if isinstance(key, str) and key:
            metadata = resource_metadata(kind, key, seen)
            if metadata:
                return metadata
    for field in ("webpage_url", "local_url", "download_url", "preview_url", "thumbnail"):
        url = value.get(field)
        if isinstance(url, str) and (match := _LOCAL.search(unquote(url))):
            kind = {"files": "file", "watermarks/videos": "file", "summary-files": "summary_file", "thumbnails": "thumbnail", "library/images": "image"}[match[1]]
            metadata = resource_metadata(kind, match[2], seen)
            if metadata:
                return metadata
    return {}


def require(value=None, *, metadata=None):
    from backend.modules.accounts import service as accounts
    user = accounts.principal.get()
    if user is None or user.role == "super_admin":
        return
    if source(value) or (metadata or {}).get("access_scope") == "super_admin":
        raise HTTPException(403, "YouTube、Pornhub 及其关联资源仅限超级管理员使用。")


def require_payload(value):
    from backend.modules.accounts import service as accounts
    if not accounts.principal.get() or accounts.current().role == "super_admin":
        return
    if isinstance(value, dict):
        require(value, metadata=metadata_for(value))
        for item in value.values():
            require_payload(item)
    elif isinstance(value, list):
        for item in value:
            require_payload(item)
    else:
        require(value)


def visible(value) -> bool:
    from backend.modules.accounts import service as accounts
    if not accounts.principal.get() or accounts.current().role == "super_admin":
        return True
    if isinstance(value, dict):
        if value.get("kind") and value.get("resource_key"):
            return not resource_metadata(value["kind"], value["resource_key"])
        return not metadata_for(value)
    return not source(value)


def filter_response(value):
    """Remove restricted entries from aggregate music/workspace/history responses."""
    if isinstance(value, list):
        return [filter_response(item) for item in value if visible(item)]
    if isinstance(value, dict):
        if not visible(value):
            raise HTTPException(403, "该平台的关联资源仅限超级管理员访问。")
        return {key: filter_response(item) for key, item in value.items()}
    return value
