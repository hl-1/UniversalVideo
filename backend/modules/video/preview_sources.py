"""Resolve cached video selections and download media using native, guarded readers."""
from __future__ import annotations

from dataclasses import dataclass
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from contextvars import copy_context
import threading
from pathlib import Path
import time
import ssl
from urllib.parse import unquote, urljoin, urlparse

import requests
from fastapi import HTTPException
from yt_dlp.downloader import get_suitable_downloader
from yt_dlp.downloader.dash import DashSegmentsFD
from yt_dlp.downloader.hls import HlsFD
from yt_dlp.downloader.http import HttpFD
from yt_dlp.dependencies import Cryptodome
from yt_dlp.networking import Request, Response
from yt_dlp.networking.exceptions import HTTPError, TransportError
from urllib3.exceptions import HTTPError as UrllibHTTPError, ProtocolError, ReadTimeoutError

from backend.infrastructure import network
from backend.infrastructure import video_network
from backend.infrastructure.ytdlp import YoutubeDL, PreparationError, network_failure, retry_network, GuardedResponse
from backend.modules.video import douyin, parse_cache, parsing, urls


@dataclass(frozen=True)
class PreviewSource:
    media_url: str
    audio_url: str | None
    referer: str
    alternate: bool = False
    format_id: str | None = None


def resolve(url: str, format_id: str) -> PreviewSource:
    page = urls.normalize_url(url)
    if format_id == "douyin-public" and urls._is_douyin_url(page):
        _, item = douyin._fetch_douyin_item(page)
        return PreviewSource(douyin._douyin_media_url(item), None, page)
    cached = parse_cache._get_sniff_cache(page)
    if not cached:
        raise HTTPException(404, "解析结果已过期，请重新解析后播放。")
    selected = next((item for item in cached.formats if item.format_id == format_id), None)
    if not selected:
        raise HTTPException(404, "播放格式与最近解析结果不匹配。")
    if selected.has_video is False or (selected.has_video is not True and (selected.ext or "").lower() in {"mp3", "m4a", "aac", "wav", "flac", "opus", "ogg"}):
        raise HTTPException(422, "当前格式仅含音频，请选择视频格式预览。")
    if not format_id.startswith(("sniff:", "sniffpair:")):
        if format_id.startswith("music:") or format_id == "douyin-public":
            raise HTTPException(422, "该格式不是有效的视频播放源。")
        return PreviewSource(page, None, page, format_id=format_id)
    original = format_id
    if format_id.startswith("sniff:"):
        pair = next((item for item in cached.formats if item.format_id.startswith("sniffpair:") and item.has_video is not False), None)
        if pair:
            format_id = pair.format_id
    audio = None
    if format_id.startswith("sniffpair:"):
        video, separator, audio = format_id.removeprefix("sniffpair:").partition("|")
        if not separator or not video or not audio:
            raise HTTPException(422, "完整音视频格式无效，请重新解析。")
        video, audio = unquote(video), unquote(audio)
    else:
        video = unquote(format_id.removeprefix("sniff:"))
    for source in (video, audio):
        if source is not None:
            parsed = urlparse(source)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
                raise HTTPException(422, "解析到的媒体地址不合法。")
    return PreviewSource(video, audio, page, format_id != original)


def headers(referer: str) -> dict:
    return {**parsing._ydl_base_options(referer)["http_headers"], "Referer": referer}


def is_manifest(url: str) -> bool:
    return urlparse(url).path.lower().endswith((".m3u8", ".mpd"))


class PreviewYoutubeDL(YoutubeDL):
    pass


class ClosingFragments:
    def _prepare_frag_download(self, ctx):
        super()._prepare_frag_download(ctx)
        self.preview_streams.append(ctx["dest_stream"])


class NativeDashFD(ClosingFragments, DashSegmentsFD):
    pass


class NativeHlsFD(ClosingFragments, HlsFD):
    def real_download(self, filename, info_dict):
        # Do not allow yt-dlp to silently delegate remote URLs to FFmpeg.
        with self.ydl.urlopen(Request(info_dict["url"], headers=info_dict.get("http_headers"))) as response:
            manifest, final_url = response.read().decode("utf-8", "replace"), response.url
        if "#EXT-X-ENDLIST" not in manifest:
            raise PreparationError("live", "当前链接为直播或未结束的视频流，请结束后重新解析。")
        if self._has_drm(manifest):
            raise PreparationError("drm", "该视频受 DRM 保护，无法在线预览。")
        if not self.can_download(manifest, info_dict, False) or ("METHOD=AES-128" in manifest and not Cryptodome.AES):
            raise PreparationError("manifest", "当前播放清单无法准备，请选择其他线路或下载后播放。")
        return super().real_download(filename, {**info_dict, "url": final_url, "hls_media_playlist_data": manifest})


class QuietLogger:
    def debug(self, message):
        pass

    info = warning = error = debug


def download(job, url: str, target: Path, check, receive, *, format_id=None, on_selected=None) -> tuple[Path, Path | None]:
    options = {**parsing._ydl_base_options(job.value["referer"]), "http_headers": headers(job.value["referer"]),
               "format": format_id or "bestvideo+bestaudio/best", "cachedir": False,
               "noprogress": True, "logger": QuietLogger(), "retries": 0, "fragment_retries": 0,
               "extractor_retries": 0, "socket_timeout": 10, "concurrent_fragment_downloads": 1,
               "hls_prefer_native": True, "hls_use_mpegts": True, "fixup": "never",
               "skip_unavailable_fragments": False, "continuedl": False, "overwrites": True}
    with PreviewYoutubeDL(options, check, receive) as ydl:
        info = ydl.extract_info(url, download=False)
        check()
        if not info or info.get("_type", "video") != "video":
            raise PreparationError("media_invalid", "请使用单个视频链接预览。")
        if info.get("is_live") or info.get("live_status") in {"is_live", "is_upcoming"}:
            raise PreparationError("live", "当前链接为直播或未开始的视频，请使用已发布的视频链接。")
        if info.get("has_drm"):
            raise PreparationError("drm", "该视频受 DRM 保护，无法在线预览。")
        selected = info.get("requested_formats") or [info]
        if len(selected) > 2 or not selected:
            raise PreparationError("media_invalid", "当前音视频组合无法预览，请选择其他格式。")
        members = []
        for index, item in enumerate(selected):
            check()
            media = {**info, **item}
            media.pop("requested_formats", None)
            media.pop("requested_downloads", None)
            if media.get("has_drm"):
                raise PreparationError("drm", "该视频受 DRM 保护，无法在线预览。")
            downloader = get_suitable_downloader(media, options)
            if downloader not in {HttpFD, HlsFD, DashSegmentsFD}:
                raise PreparationError("protocol", "当前媒体协议无法在线预览，请选择其他线路。")
            path = target.with_name(f"{target.name}.{index}")
            members.append(dict(path=path, media=media, downloader=downloader, done=threading.Event(), error=None,
                                video=item.get("vcodec") != "none", audio=item.get("acodec") != "none", duration=info.get("duration")))
        if on_selected:
            on_selected(members)

        def transfer(member, parallel=False):
            try:
                context = PreviewYoutubeDL(options, check, receive) if parallel else nullcontext(ydl)
                with context as reader:
                    if parallel:
                        for cookie in ydl.cookiejar:
                            reader.cookiejar.set_cookie(cookie)
                    downloader = member["downloader"]
                    fd = {HlsFD: NativeHlsFD, DashSegmentsFD: NativeDashFD}.get(downloader, downloader)(reader, options)
                    fd.preview_streams = []
                    try:
                        success, _ = fd.download(str(member["path"]), member["media"])
                    finally:
                        for stream in fd.preview_streams:
                            stream.close()
                if not success or not member["path"].is_file() or not member["path"].stat().st_size:
                    raise PreparationError("upstream", "视频源下载失败或已过期，请重新解析。")
            except BaseException as exc:
                member["error"] = exc
                raise
            finally:
                member["done"].set()

        if on_selected and len(members) == 2:
            with ThreadPoolExecutor(max_workers=2, thread_name_prefix="preview-track") as executor:
                futures = [executor.submit(copy_context().run, transfer, member, True) for member in members]
                for future in futures:
                    future.result()
        else:
            for member in members:
                transfer(member)
        video = next((member["path"] for member in members if member["video"]), None)
        audio = next((member["path"] for member in members if not member["video"]), None)
        # An audio manifest is valid as the audio member of an explicit pair.
        return video or audio, audio if video else None
