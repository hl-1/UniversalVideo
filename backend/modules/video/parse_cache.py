from __future__ import annotations

import time
import threading

from backend.modules.accounts import service as accounts
from backend.modules.video import cache as video_cache
from backend.modules.video import constants as video_constants
from backend.modules.video import schemas as video_schemas


sniff_cache: dict[str, tuple[float, video_schemas.ParseResponse]] = {}
sniff_cache_lock = threading.Lock()


def _get_sniff_cache(url: str) -> video_schemas.ParseResponse | None:
    if accounts.principal.get():
        value = video_cache.get(video_cache.parse_key(url))
        if not value:
            return None
        from backend.infrastructure import platform_access
        platform_access.require_payload(value)
        try:
            return video_schemas.ParseResponse.model_validate(value)
        except ValueError:
            video_cache.remove(video_cache.parse_key(url))
            return None
    with sniff_cache_lock:
        cached = sniff_cache.get(url)
        if not cached:
            return None
        created_at, response = cached
        if time.time() - created_at > video_constants.SNIFF_CACHE_TTL_SECONDS:
            sniff_cache.pop(url, None)
            return None
        return response


def _set_sniff_cache(url: str, response: video_schemas.ParseResponse) -> None:
    if accounts.principal.get():
        value = response.model_dump(exclude={"record_id"})
        ttl = video_cache.parse_ttl(value)
        if ttl > 0:
            video_cache.put(video_cache.parse_key(url), value, ttl)
        return
    with sniff_cache_lock:
        sniff_cache[url] = (time.time(), response)
