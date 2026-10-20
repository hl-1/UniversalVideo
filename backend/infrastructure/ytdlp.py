"""Shared yt-dlp integration, including webpage-independent Bilibili extraction."""
from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse, urljoin
import ssl
import time
import requests
from yt_dlp.networking import Request, Response
from yt_dlp.networking.exceptions import HTTPError, TransportError
from urllib3.exceptions import HTTPError as UrllibHTTPError, ProtocolError, ReadTimeoutError

from yt_dlp import YoutubeDL as BaseYoutubeDL
from yt_dlp.extractor.bilibili import BiliBiliIE
from yt_dlp.utils import ExtractorError, float_or_none, int_or_none, str_or_none
from backend.infrastructure import video_network, network, platform_access, video_proxy


_PUBLIC_VIDEO_URL = re.compile(
    r"https?://(?:www\.)?bilibili\.com/video/(?P<prefix>BV|av)(?P<id>[0-9A-Za-z]+)(?:[/?#]|$)",
    re.IGNORECASE,
)


def is_bilibili_video_url(url: str) -> bool:
    return _PUBLIC_VIDEO_URL.match(url) is not None


class BilibiliVideoError(ExtractorError):
    """A definitive video/part/permission error that must not select other content."""

    def __init__(self, message: str):
        super().__init__(message, expected=True)


class BilibiliPublicApiIE(BiliBiliIE):
    @classmethod
    def ie_key(cls):
        # Replace the normal extractor in place, including b23/AV redirects.
        return BiliBiliIE.ie_key()

    def _real_extract(self, url):
        if not is_bilibili_video_url(url):
            return super()._real_extract(url)
        try:
            return self._extract_api_info(url)
        except BilibiliVideoError:
            raise
        except (ExtractorError, AttributeError, IndexError, KeyError, TypeError, ValueError) as api_error:
            api_message = (
                api_error.orig_msg if isinstance(api_error, ExtractorError)
                else "B站公开 API 响应结构不兼容"
            )
            self.report_warning("B站公开 API 暂不可用，正在尝试网页备用解析")
            try:
                return super()._real_extract(url)
            except ExtractorError as page_error:
                raise ExtractorError(
                    f"B站公开 API 解析失败：{api_message}；网页备用解析失败：{page_error.orig_msg}",
                    expected=True,
                ) from api_error

    def _extract_api_info(self, url):
        match = _PUBLIC_VIDEO_URL.match(url)
        prefix, identifier = match.group("prefix", "id")
        query = parse_qs(urlparse(url).query, keep_blank_values=True)
        raw_part = query.get("p", ["1"])[-1]
        if not re.fullmatch(r"[0-9]+", raw_part) or int(raw_part) < 1:
            raise BilibiliVideoError("B站分 P 参数无效，请使用从 1 开始的正整数。")
        part_number = int(raw_part)
        is_bv = prefix.upper() == "BV"
        identity = "BV" + identifier if is_bv else identifier
        headers = {"Referer": "https://www.bilibili.com/", "Origin": "https://www.bilibili.com"}
        payload = self._download_json(
            "https://api.bilibili.com/x/web-interface/view", identity,
            query={"bvid" if is_bv else "aid": identity}, headers=headers,
            note="Downloading public video metadata",
        )
        if not isinstance(payload, dict):
            raise ExtractorError("B站元数据接口响应格式异常", expected=True)
        if payload.get("code") != 0:
            message = f"B站元数据接口返回 {payload.get('code')}：{payload.get('message') or '无法访问视频'}"
            if payload.get("code") in (-404, -403, -101, -10403):
                raise BilibiliVideoError(message)
            raise ExtractorError(message, expected=True)
        data = payload.get("data")
        if not isinstance(data, dict) or not data.get("bvid"):
            raise ExtractorError("B站元数据接口缺少视频信息", expected=True)
        bvid = data["bvid"]
        if (is_bv and bvid != identity) or (not is_bv and str(data.get("aid")) != identifier):
            raise BilibiliVideoError("B站元数据与请求的视频不一致，已停止解析。")
        pages = data.get("pages")
        if not isinstance(pages, list) or not pages:
            raise ExtractorError("B站元数据接口缺少分 P 信息", expected=True)
        part = next((p for p in pages if isinstance(p, dict) and p.get("page") == part_number), None)
        if part is None:
            raise BilibiliVideoError(f"B站视频不存在第 {part_number} P，请检查链接中的 p 参数。")
        cid = int_or_none(part.get("cid"))
        if not cid or cid < 1:
            raise ExtractorError("B站分 P 信息缺少有效 cid", expected=True)
        if data.get("is_upower_exclusive"):
            raise BilibiliVideoError("该 B站视频需要充电专属权限，当前仅支持公开完整视频。")
        rights = data.get("rights") or {}
        if data.get("redirect_url") or (isinstance(rights, dict) and rights.get("is_stein_gate")):
            raise ExtractorError("此类 B站视频需使用原有网页解析", expected=True)

        play = self._download_playinfo(bvid, cid, headers=headers, query={"try_look": 1})
        if not isinstance(play, dict):
            raise ExtractorError("B站播放接口未返回有效播放信息", expected=True)
        formats = self.extract_formats(play)
        # Prefer the platform's conventional CDN backups over PCDN when supplied
        # for the exact same DASH stream. Never manufacture or rewrite a URL.
        dash = play.get("dash") or {}
        sources = (dash.get("video") or []) + (dash.get("audio") or [])
        for source in sources:
            primary = source.get("baseUrl") or source.get("base_url")
            backups = source.get("backupUrl") or source.get("backup_url") or []
            candidates = [value for value in [primary, *backups] if isinstance(value, str)]
            conventional = [value for value in candidates
                            if video_network.matches(value, ("bilivideo.com", "bilivideo.cn"))
                            and urlparse(value).scheme == "https" and urlparse(value).port in {None, 443}
                            and not urlparse(value).username and not urlparse(value).password]
            conventional.sort(key=lambda value: not (urlparse(value).hostname or "").startswith("upos-"))
            if conventional:
                for item in formats:
                    if item.get("url") == primary:
                        item["url"] = conventional[0]
        if not formats:
            raise ExtractorError("B站播放接口未返回可下载格式", expected=True)
        if any(item.get("fragments") for item in formats):
            raise ExtractorError("多段 B站 FLV 继续使用原有网页解析", expected=True)

        has_part = len(pages) > 1 or "p" in query
        webpage_url = f"https://www.bilibili.com/video/{bvid}/" + (f"?p={part_number}" if has_part else "")
        title = data.get("title") or "B站视频"
        if len(pages) > 1:
            title += f" p{part_number:02d} {part.get('part') or ''}"
        owner = data.get("owner") if isinstance(data.get("owner"), dict) else {}
        return {
            "id": bvid + (f"_p{part_number}" if has_part else ""),
            "title": title,
            "webpage_url": webpage_url,
            "description": data.get("desc"),
            "thumbnail": data.get("pic"),
            "uploader": owner.get("name"),
            "uploader_id": str_or_none(owner.get("mid")),
            "timestamp": int_or_none(data.get("pubdate")),
            "duration": float_or_none(play.get("timelength"), scale=1000) or int_or_none(part.get("duration")),
            "formats": formats,
            "http_headers": {**headers, "Referer": webpage_url},
            "subtitles": self.extract_subtitles(bvid, cid, data.get("aid")),
        }


class PreparationError(Exception):
    def __init__(self, code: str, message: str):
        self.code, self.message = code, message
        super().__init__(message)


def network_failure(exc: Exception) -> PreparationError | None:
    """Keep URLs and proxy credentials out of user-facing network errors."""
    pending, visited = [exc], set()
    while pending:
        current = pending.pop(0)
        if id(current) in visited:
            continue
        visited.add(id(current))
        if isinstance(current, PreparationError) and current.code.startswith("network_"):
            return current
        if isinstance(current, requests.exceptions.ProxyError):
            return PreparationError("network_proxy", "无法连接网络代理，请检查代理服务后重试播放。")
        if isinstance(current, (requests.exceptions.SSLError, ssl.SSLError)):
            return PreparationError("network_tls", "视频源的安全连接中断，请检查网络或代理后重试播放。")
        if isinstance(current, (requests.exceptions.Timeout, ReadTimeoutError, TimeoutError)):
            return PreparationError("network_timeout", "连接视频源超时，请检查网络或切换线路后重试播放。")
        if isinstance(current, (requests.exceptions.ConnectionError, requests.exceptions.ChunkedEncodingError, ProtocolError, ConnectionError)):
            return PreparationError("network_connection", "视频源连接中断，请检查网络后重试播放。")
        for linked in (getattr(current, "cause", None), current.__cause__, current.__context__):
            if isinstance(linked, Exception):
                pending.append(linked)
        details = getattr(current, "exc_info", None)
        if isinstance(details, tuple) and len(details) > 1 and isinstance(details[1], Exception):
            pending.append(details[1])
        pending.extend(value for value in current.args if isinstance(value, Exception))
    return None


def retry_network(operation, check, *, wait=time.sleep, attempts=3):
    for attempt in range(attempts):
        check()
        try:
            return operation()
        except (requests.RequestException, UrllibHTTPError) as exc:
            failure = network_failure(exc)
            if failure is None:
                raise
            # A certificate validation failure will not be fixed by retrying.
            if attempt == attempts - 1 or "CERTIFICATE_VERIFY_FAILED" in str(exc):
                raise failure from exc
            wait(0.4 * (attempt + 1))
    raise AssertionError("No network attempt was made")


class GuardedResponse(Response):
    def __init__(self, upstream, owner):
        super().__init__(upstream.raw, upstream.url, upstream.headers, upstream.status_code, upstream.reason)
        self.upstream, self.owner = upstream, owner

    def read(self, amt=None):
        # Even an extractor's unbounded read checks cancellation and the byte budget.
        pieces, remaining, total = [], amt, 0
        try:
            while remaining is None or remaining > 0:
                self.owner.check()
                chunk = self.fp.read(min(256 * 1024, remaining) if remaining is not None else 256 * 1024, decode_content=True)
                if not chunk:
                    break
                self.owner.receive(len(chunk))
                pieces.append(chunk)
                total += len(chunk)
                if remaining is not None:
                    remaining -= len(chunk)
                elif total > 16 * 1024 * 1024:
                    raise PreparationError("size", "播放清单或页面过大，请下载后播放。")
            return b"".join(pieces)
        except (requests.RequestException, UrllibHTTPError) as exc:
            raise TransportError(cause=exc) from exc

    def close(self):
        self.upstream.close()
        super().close()


class GuardedYoutubeDL(BaseYoutubeDL):
    """Route extractor, manifest, key and segment requests through public URL checks."""
    def __init__(self, options=None, check=None, receive=None, **kwargs):
        options = options or {}
        check = check or (lambda: None)
        receive = receive or (lambda size: None)
        self.check, self.receive = check, receive
        active_route = video_proxy._active.get()
        self.proxy_profile = dict(active_route) if active_route else None
        self.session = requests.Session()
        self.session.trust_env = options.get("proxy") != ""
        if options.get("proxy"):
            self.session.trust_env = False
            self.session.proxies.update(http=options["proxy"], https=options["proxy"])
        self.responses = []
        super().__init__(options, **kwargs)

    def extract_info(self, url, *args, **kwargs):
        with video_proxy.scope(url) as route:
            if route and self.proxy_profile is None:
                self.proxy_profile = dict(route)
            return super().extract_info(url, *args, **kwargs)

    def urlopen(self, req):
        if isinstance(req, str):
            req = Request(req)
        elif not isinstance(req, Request):
            req = Request(req.full_url, data=req.data, headers=dict(req.header_items()), method=req.get_method())
        target, method, data = req.url, req.method, req.data
        request_headers = {**self.params.get("http_headers", {}), **req.headers}
        self.session.cookies.update(self.cookiejar)
        for _ in range(6):
            def send():
                network.charge_request()
                platform_access.require(target)
                video_network.validate(target)
                # Native fragment threads do not inherit ContextVars. Retain the
                # extractor/task's route for every CDN, key and redirect request.
                chosen = self.proxy_profile or video_proxy.selected(target, request_headers.get("Referer"))
                if chosen and self.proxy_profile is None:
                    self.proxy_profile = dict(chosen)
                if chosen:
                    self.session.trust_env = False
                    self.session.proxies.update(http=chosen["url"], https=chosen["url"])
                return self.session.request(method, target, data=data, headers=request_headers,
                                            stream=True, allow_redirects=False,
                                            timeout=min(10, max(0.05, self.params.get("socket_timeout") or 10)))
            try:
                upstream = retry_network(send, self.check, attempts=3 if method in {"GET", "HEAD"} else 1)
            except requests.RequestException as exc:
                raise TransportError(cause=exc) from exc
            for cookie in self.session.cookies:
                self.cookiejar.set_cookie(cookie)
            if upstream.status_code not in {301, 302, 303, 307, 308}:
                result = GuardedResponse(upstream, self)
                self.responses.append(result)
                if upstream.status_code >= 400:
                    raise HTTPError(result)
                return result
            location = upstream.headers.get("Location", "")
            status = upstream.status_code
            upstream.close()
            if not location:
                raise PreparationError("upstream", "视频源返回了无效重定向。")
            destination = urljoin(target, location)
            if urlparse(target).netloc != urlparse(destination).netloc:
                request_headers = {k: v for k, v in request_headers.items() if k.lower() not in {"cookie", "authorization"}}
            if status == 303 or (status in {301, 302} and method == "POST"):
                method, data = "GET", None
                request_headers = {k: v for k, v in request_headers.items() if k.lower() not in {"content-type", "content-length"}}
            target = destination
        raise PreparationError("upstream", "视频源重定向过多，请重新解析。")

    def close(self):
        try:
            super().close()
        finally:
            for response in self.responses:
                response.close()
            self.responses.clear()
            self.session.close()


class YoutubeDL(GuardedYoutubeDL):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.add_info_extractor(BilibiliPublicApiIE())
