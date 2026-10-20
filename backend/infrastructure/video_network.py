"""Per-request routing for domestic video platforms; never changes system proxy settings."""
from __future__ import annotations

from urllib.parse import urlparse

import requests

from backend.infrastructure import network, platform_access, video_proxy


DOMESTIC_DOMAINS = {
    "bilibili": ("bilibili.com", "b23.tv", "bilivideo.com", "bilivideo.cn", "hdslb.com"),
    "douyin": ("douyin.com", "iesdouyin.com", "amemv.com", "douyinvod.com", "byteimg.com", "snssdk.com", "ibytedtos.com", "bytecdn.cn"),
    "xiaohongshu": ("xiaohongshu.com", "xhslink.com", "xhscdn.com", "xiaohongshu.net"),
    "weibo": ("weibo.com", "weibo.cn", "weibocdn.com", "sinaimg.cn", "sina.com.cn"),
}


def matches(url: str | None, domains) -> bool:
    host = (urlparse(url or "").hostname or "").lower().rstrip(".")
    return any(host == domain or host.endswith("." + domain) for domain in domains)


def domestic(url: str | None, referer: str | None = None) -> bool:
    # The original platform controls routing when it uses a shared CDN.
    return any(matches(value, domains) for value in (referer, url) for domains in DOMESTIC_DOMAINS.values())


def validate(url: str):
    # Bilibili may return HTTPS PCDN endpoints on these two media ports.
    # Keep generic public URLs restricted to 80/443 and retain DNS/IP checks.
    parsed = urlparse(url)
    if matches(url, ("mcdn.bilivideo.cn",)) and parsed.scheme == "https":
        return network.public_url(url, allowed_ports=(80, 443, 4483, 8082))
    return network.public_url(url)


def get(url: str, *, referer: str | None = None, **kwargs):
    platform_access.require(url)
    platform_access.require(referer)
    overseas = video_proxy.selected(url, referer)
    if not domestic(url, referer) and not overseas:
        return network.public_get(url, **kwargs)
    session = video_proxy.session(url, referer, proxy=overseas["url"] if overseas else "")
    try:
        response = network.public_get(url, session=session, validate=validate, **kwargs)
    except BaseException:
        session.close()
        raise
    close = response.close

    def close_owned_session():
        try:
            close()
        finally:
            session.close()

    response.close = close_owned_session
    return response
