"""Explicit overseas proxy profiles; no environment or system-proxy fallback."""
from __future__ import annotations

import hashlib
import os
from contextlib import contextmanager
from contextvars import ContextVar
from urllib.parse import unquote, urlparse

import requests
from fastapi import HTTPException

from backend.infrastructure import platform_access

_active: ContextVar[dict | None] = ContextVar("video_proxy_profile", default=None)


def profile() -> dict:
    from backend.core.configuration import local_environment
    config = local_environment()
    value = os.getenv("VIDEODREAM_OVERSEAS_PROXY_URL", config.get("VIDEODREAM_OVERSEAS_PROXY_URL", "http://127.0.0.1:7890"))
    enabled = str(os.getenv("VIDEODREAM_OVERSEAS_ENABLED", config.get("VIDEODREAM_OVERSEAS_ENABLED", "true"))).lower() == "true"
    parsed = urlparse(value or "")
    try:
        valid = parsed.scheme in {"http", "https", "socks5h"} and parsed.hostname and parsed.port and parsed.path in {"", "/"} and not parsed.query and not parsed.fragment
    except ValueError:
        valid = False
    if not enabled or not valid:
        raise HTTPException(503, "海外视频代理未启用或配置无效，请联系超级管理员。")
    revision = str(os.getenv("VIDEODREAM_OVERSEAS_ROUTE_REVISION", config.get("VIDEODREAM_OVERSEAS_ROUTE_REVISION", "1")))
    # Digest keeps routing/auth changes separate without storing the proxy address.
    identity = hashlib.sha256((value + "\0" + revision).encode()).hexdigest()[:20]
    return {"url": value, "revision": identity, "scheme": parsed.scheme}


def selected(url=None, referer=None):
    if platform_access.platform(url) or platform_access.platform(referer):
        platform_access.require(url or referer)
        platform_access.require(referer)
        return _active.get() or profile()
    return _active.get()


@contextmanager
def scope(url):
    selected_profile = (_active.get() or profile()) if platform_access.platform(url) else _active.get()
    platform_access.require(url)
    token = _active.set(selected_profile)
    try:
        yield selected_profile
    finally:
        _active.reset(token)


def session(url=None, referer=None, *, proxy=None):
    chosen = selected(url, referer)
    connection = requests.Session()
    connection.trust_env = False
    value = proxy if proxy is not None else (chosen or {}).get("url")
    if value:
        connection.proxies.update(http=value, https=value)
    return connection


def browser_options(url):
    chosen = selected(url)
    if not chosen:
        return None
    parsed = urlparse(chosen["url"])
    # Chromium cannot authenticate a SOCKS5 proxy; prefer the client's HTTP port.
    if parsed.scheme == "socks5h":
        raise HTTPException(503, "浏览器备用解析需要 HTTP/HTTPS 代理端口，请调整海外代理配置。")
    result = {"server": f"{parsed.scheme}://{parsed.hostname}:{parsed.port}"}
    if parsed.username:
        result.update(username=unquote(parsed.username), password=unquote(parsed.password or ""))
    return result
