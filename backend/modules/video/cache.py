from __future__ import annotations

import hashlib
import json
import logging
import math
import time
from urllib.parse import parse_qs, unquote, urlparse

import redis

from backend.modules.accounts import service as accounts


logger = logging.getLogger("videodream.video")
TTL = 600


def key(kind: str, identity: str, uid: int | None = None) -> str:
    owner = accounts.owner_id() if uid is None else uid
    return accounts.redis_prefix() + f"video:{kind}:{owner}:{identity}"


def parse_key(url: str, uid: int | None = None) -> str:
    return key("parse", hashlib.sha256(url.encode()).hexdigest(), uid)


def get(cache_key: str) -> dict | None:
    try:
        raw = accounts.redis_client().get(cache_key)
        value = json.loads(raw) if raw else None
        return value if isinstance(value, dict) else None
    except (redis.RedisError, ValueError, TypeError):
        return None


def put(cache_key: str, value: dict, ttl: int = TTL) -> bool:
    try:
        accounts.redis_client().set(cache_key, json.dumps(value, ensure_ascii=False), ex=max(1, ttl))
        return True
    except redis.RedisError:
        logger.warning("Video cache unavailable; durable data remains in MySQL")
        return False


def remove(cache_key: str) -> None:
    try:
        accounts.redis_client().delete(cache_key)
    except redis.RedisError:
        logger.warning("Could not invalidate video cache")


def clear_user(uid: int) -> None:
    client = accounts.redis_client()
    try:
        for kind in ("parse", "preview"):
            keys = list(client.scan_iter(match=key(kind, "*", uid)))
            if keys:
                client.delete(*keys)
    except redis.RedisError:
        logger.warning("Could not clear video cache")


def media_ttl(urls: list[str]) -> int:
    remaining = TTL
    now = time.time()
    for url in urls:
        query = parse_qs(urlparse(url).query)
        for name in ("expire", "expires", "expiration", "deadline", "x-expires"):
            for raw in query.get(name, []):
                try:
                    expiry = float(raw)
                    if expiry > 1e12:
                        expiry /= 1000
                    if math.isfinite(expiry) and expiry > 1e9:
                        remaining = min(remaining, max(0, int(expiry - now - 15)))
                except (ValueError, OverflowError):
                    pass
    return remaining


def parse_ttl(payload: dict) -> int:
    urls = []
    for item in payload.get("formats", []):
        identifier = item.get("format_id", "")
        if identifier.startswith("sniff:"):
            urls.append(unquote(identifier[6:]))
        elif identifier.startswith("sniffpair:"):
            video, _, audio = identifier[10:].partition("|")
            urls.extend(unquote(value) for value in (video, audio) if value)
    return media_ttl(urls)
