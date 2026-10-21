from __future__ import annotations

import ipaddress
import socket
from contextvars import ContextVar
from urllib.parse import urlparse, urljoin

import requests
from fastapi import HTTPException


FAKE_IP_NETWORK = ipaddress.ip_network("198.18.0.0/15")
request_guard: ContextVar = ContextVar("videodream_network_request_guard",default=None)


def charge_request():
    guard = request_guard.get()
    if guard is not None:
        guard()


def raise_if_guarded(exc):
    if request_guard.get() is not None and getattr(exc,"agent_policy_error",False):
        raise exc
VIDEO_DOMAINS = ("bilibili.com", "b23.tv", "hdslb.com", "bilivideo.com", "youtube.com", "youtu.be",
                 "googlevideo.com", "ytimg.com", "douyin.com", "douyinvod.com", "byteimg.com",
                 "xiaohongshu.com", "xhslink.com", "weibo.com", "weibo.cn", "pornhub.com")


def _public_address(host: str, address: str) -> bool:
    ip = ipaddress.ip_address(address)
    if ip.is_global:
        return True
    # TUN proxies use the benchmark range for public platform DNS responses.
    from backend.infrastructure.platform_access import DOMAINS
    domains = (*VIDEO_DOMAINS, *(domain for group in DOMAINS.values() for domain in group))
    return ip in FAKE_IP_NETWORK and any(host == domain or host.endswith("." + domain) for domain in domains)


def public_url(value: str, *, allowed_ports=(80, 443)) -> str:
    from backend.infrastructure import platform_access
    platform_access.require(value)
    parsed = urlparse(value)
    try:
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.port not in {None, *allowed_ports}:
            raise ValueError("Invalid URL")
        addresses = socket.getaddrinfo(parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM)
        if not addresses or any(not _public_address(parsed.hostname.lower(), address[4][0]) for address in addresses):
            raise ValueError("Private address")
    except (ValueError, OSError) as exc:
        raise HTTPException(422, "仅支持可访问的公网 HTTP/HTTPS 地址。") from exc
    return value


def public_get(url: str, *, session=None, validate=None, **kwargs):
    headers = dict(kwargs.pop("headers", {}) or {})
    for _ in range(6):
        charge_request()
        from backend.infrastructure import platform_access
        platform_access.require(url)
        (validate or public_url)(url)
        if session is None:
            from backend.infrastructure import video_network, video_proxy
            if video_network.domestic(url) or video_proxy.selected(url):
                return video_network.get(url, headers=headers, **kwargs)
        response = (session.get if session is not None else requests.get)(url, headers=headers, allow_redirects=False, **kwargs)
        if response.status_code not in {301, 302, 303, 307, 308}:
            return response
        location = response.headers.get("Location", "")
        response.close()
        destination = urljoin(url, location)
        if urlparse(url).netloc != urlparse(destination).netloc:
            headers = {key: value for key, value in headers.items() if key.lower() not in {"cookie", "authorization"}}
        url = destination
    raise HTTPException(422, "远程地址重定向过多。")
