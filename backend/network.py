from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse, urljoin

import requests
from fastapi import HTTPException


def public_url(value: str) -> str:
    parsed = urlparse(value)
    try:
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.port not in {None, 80, 443}:
            raise ValueError("Invalid URL")
        addresses = socket.getaddrinfo(parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM)
        if not addresses or any(not ipaddress.ip_address(address[4][0]).is_global for address in addresses):
            raise ValueError("Private address")
    except (ValueError, OSError) as exc:
        raise HTTPException(422, "仅支持可访问的公网 HTTP/HTTPS 地址。") from exc
    return value


def public_get(url: str, **kwargs):
    for _ in range(6):
        public_url(url)
        response = requests.get(url, allow_redirects=False, **kwargs)
        if response.status_code not in {301, 302, 303, 307, 308}:
            return response
        location = response.headers.get("Location", "")
        response.close()
        url = urljoin(url, location)
    raise HTTPException(422, "远程地址重定向过多。")
