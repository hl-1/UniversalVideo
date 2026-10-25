"""Bounded public playlist readers shared by imports and the scene agent."""
from __future__ import annotations

import html
import json
import re
import threading
from urllib.parse import parse_qs, urlparse, urljoin, urlencode

import requests

from backend.infrastructure.network import public_get, public_url
from backend.modules.music import sources as music

MAX_TRACKS = 500
PAGE_SIZE = 100
MAX_BYTES = 8 * 1024 * 1024
HOSTS = ("music.163.com", "163cn.tv", "y.qq.com", "c.y.qq.com", "u.y.qq.com", "kugou.com")


class PlaylistError(ValueError):
    pass


class RangeRequired(PlaylistError):
    def __init__(self, metadata):
        super().__init__("歌单超过 500 首，请选择起始位置和数量后重新导入。")
        self.metadata = metadata


def validate_url(url: str) -> str:
    p = urlparse(url)
    host = (p.hostname or "").lower()
    if not any(host == root or host.endswith("." + root) for root in HOSTS):
        raise PlaylistError("仅支持 QQ 音乐、网易云音乐、酷狗音乐的歌单链接。")
    return public_url(url)


def request(url: str, stop: threading.Event, *, params=None, as_json=True):
    if params:
        url += ("&" if "?" in url else "?") + urlencode(params)
    # Use a session to keep redirect handling in public_get; every hop is checked.
    for attempt in range(3):
        if stop.is_set():
            raise PlaylistError("歌单读取已取消。")
        try:
            with requests.Session() as session:
                with public_get(url, session=session, validate=validate_url, stream=True,
                                headers={"User-Agent": "Mozilla/5.0", "Referer": "https://y.qq.com/"}, timeout=(5, 15)) as response:
                    response.raise_for_status()
                    chunks, size = [], 0
                    for chunk in response.iter_content(65536):
                        if stop.is_set():
                            raise PlaylistError("歌单读取已取消。")
                        size += len(chunk)
                        if size > MAX_BYTES:
                            raise PlaylistError("平台响应过大，请缩小导入范围。")
                        chunks.append(chunk)
                    body = b"".join(chunks).decode("utf-8", errors="replace")
                    return json.loads(body) if as_json else (response.url, body)
        except requests.RequestException as exc:
            status = exc.response.status_code if exc.response is not None else None
            if attempt == 2 or status is not None and status != 429 and status < 500:
                raise PlaylistError("平台暂不可访问或请求受限，请稍后重试，也可使用截图或文本导入。") from exc
            if stop.wait(0.5 * (2 ** attempt)):
                raise PlaylistError("歌单读取已取消。") from exc
        except (json.JSONDecodeError, UnicodeError) as exc:
            raise PlaylistError("平台未返回有效歌单，可能需要登录或接口已变化。") from exc


def identify(url: str):
    p = urlparse(url)
    host = (p.hostname or "").lower()
    path = p.path + (p.fragment.split("?", 1)[0] if p.fragment else "")
    query = parse_qs(p.query)
    if "?" in p.fragment:
        query.update(parse_qs(p.fragment.split("?", 1)[1]))
    def value(*keys):
        return next((query[k][0] for k in keys if query.get(k)), "")
    provider, identifier = None, ""
    if host == "music.163.com" or host.endswith(".music.163.com"):
        if "playlist" in path:
            provider, identifier = "netease", value("id")
    elif host == "y.qq.com" or host.endswith(".y.qq.com"):
        match = re.search(r"/(?:playlist|taoge)/(\d+)", path)
        if match or any(word in path.lower() for word in ("playlist", "taoge", "diss")):
            provider, identifier = "qq", match[1] if match else value("disstid", "id", "dissid")
    elif host == "kugou.com" or host.endswith(".kugou.com"):
        match = re.search(r"/(?:special/single|plist/list)/(\d+)", path)
        if match or any(word in path.lower() for word in ("special", "songlist", "playlist", "sharelist")):
            provider, identifier = "kugou", match[1] if match else value("specialid", "special_id")
    return (provider, identifier) if provider and re.fullmatch(r"\d{1,24}", identifier) else None


def resolve(share_text: str, stop: threading.Event):
    urls = re.findall(r'https?://[^\s<>"\u201c\u201d]+', html.unescape(share_text))
    urls = list(dict.fromkeys(url.rstrip("。，；！、）)]}'") for url in urls))
    if len(urls) != 1:
        raise PlaylistError("请粘贴一份包含单个歌单链接的分享内容。")
    url = urls[0]
    validate_url(url)
    found = identify(url)
    if not found:
        final_url, body = request(url, stop, as_json=False)
        found = identify(final_url)
        if not found:
            # Only follow explicit canonical/open-graph URLs, never execute page JS.
            for candidate in re.findall(r'<(?:meta|link)\b[^>]*(?:content|href)=["\']([^"\']+)["\']', body, re.I):
                candidate = urljoin(final_url, html.unescape(candidate))
                if identify(candidate):
                    validate_url(candidate)
                    found = identify(candidate)
                    break
    if not found:
        raise PlaylistError("未识别到公开歌单 ID；请复制歌单完整链接，或使用截图、文本导入。暂不支持仅含酷狗口令或 gcid 的链接。")
    return found


def canonical(provider, identifier):
    return {"netease": f"https://music.163.com/playlist?id={identifier}",
            "qq": f"https://y.qq.com/n/ryqq/playlist/{identifier}",
            "kugou": f"https://www.kugou.com/yy/special/single/{identifier}.html"}[provider]


def count(value):
    try:
        number = int(value)
        return number if number >= 0 else None
    except (ValueError, TypeError):
        return None


def read(provider, identifier, stop, *, start=1, limit=MAX_TRACKS, explicit_range=False, progress=None):
    if provider not in {"qq", "netease", "kugou"} or not re.fullmatch(r"\d{1,24}", identifier):
        raise PlaylistError("歌单 ID 无效。")
    if start < 1 or not 1 <= limit <= MAX_TRACKS:
        raise PlaylistError("请选择有效的导入范围，每次最多 500 首。")
    metadata = {"provider": provider, "playlist_id": identifier, "url": canonical(provider, identifier),
                "name": "", "total": None, "read_count": 0, "complete": False, "start": start, "limit": limit}
    warnings, tracks = [], []
    seen = set()
    def report():
        metadata["read_count"] = len(tracks)
        if progress:
            progress(dict(metadata), list(tracks), list(warnings))
    def check_range(total, name):
        metadata.update(total=total, name=music._clean(name)[:200])
        if total is not None and start > max(total, 1):
            raise PlaylistError("起始位置超过歌单曲目总数。")
        if total is not None and total > MAX_TRACKS and not explicit_range:
            raise RangeRequired(metadata)
        report()
    def append(items, convert, offset):
        for index, item in enumerate(items):
            if stop.is_set():
                raise PlaylistError("歌单读取已取消。")
            try:
                track = convert(item).model_dump(exclude={"token"})
                if not track["title"]:
                    raise ValueError("Missing title")
                album = item.get("al") or item.get("album") or {}
                track.update(album=track.get("album") or music._clean(album.get("name", "") if isinstance(album, dict) else album), position=offset + index + 1)
                key = track["identifier"]
                # Keep duplicates here: the import job counts/removes them by platform ID.
                tracks.append(track)
                seen.add(key)
            except (ValueError, TypeError, KeyError):
                warnings.append(f"第 {offset + index + 1} 首缺少有效曲目信息，未能读取。")
        report()
    try:
        if provider == "netease":
            data = request("https://music.163.com/api/v6/playlist/detail", stop, params={"id": identifier, "n": 1000})
            playlist = data.get("playlist") or data.get("result") or {}
            if not playlist:
                raise PlaylistError("网易云歌单不可访问，可能已删除、设为私密或需要登录。")
            raw = playlist.get("tracks") or []
            ids = [str(t["id"]) for t in playlist.get("trackIds", []) if isinstance(t, dict) and t.get("id")]
            total = count(playlist.get("trackCount"))
            check_range(total, playlist.get("name"))
            lookup = {str(t.get("id")): t for t in raw}
            if ids:
                wanted = ids[start - 1:start - 1 + limit]
                for offset in range(0, len(wanted), PAGE_SIZE):
                    chunk = wanted[offset:offset + PAGE_SIZE]
                    missing = [key for key in chunk if key not in lookup]
                    if missing:
                        details = request("https://music.163.com/api/song/detail", stop, params={"ids": json.dumps(missing)})
                        lookup.update({str(t.get("id")): t for t in details.get("songs", [])})
                    append([lookup.get(key, {"id": key}) for key in chunk], music._netease_track, start - 1 + offset)
            else:
                append(raw[start - 1:start - 1 + limit], music._netease_track, start - 1)
        elif provider == "qq":
            data = request("https://c.y.qq.com/qzone/fcg-bin/fcg_ucc_getcdinfo_byids_cp.fcg", stop,
                           params={"type": 1, "json": 1, "utf8": 1, "onlysong": 0, "disstid": identifier, "format": "json"})
            lists = data.get("cdlist") or []
            if not lists:
                raise PlaylistError("QQ 音乐歌单不可访问，可能已删除、设为私密或需要登录。")
            playlist = lists[0]
            check_range(count(playlist.get("songnum")), playlist.get("dissname"))
            raw = (playlist.get("songlist") or [])[start - 1:start - 1 + limit]
            for offset in range(0, len(raw), PAGE_SIZE):
                append(raw[offset:offset + PAGE_SIZE], music._qq_track, start - 1 + offset)
        else:
            # This public legacy endpoint serves HTTP; never disable TLS verification.
            info = request("http://mobilecdn.kugou.com/api/v3/special/info", stop, params={"specialid": identifier})
            detail = info.get("data") or {}
            first_page = (start - 1) // PAGE_SIZE + 1
            last_page = (start + limit - 2) // PAGE_SIZE + 1
            previous = None
            for page in range(first_page, last_page + 1):
                data = request("http://mobilecdn.kugou.com/api/v3/special/song", stop,
                               params={"specialid": identifier, "page": page, "pagesize": PAGE_SIZE})
                if data.get("status") == 0:
                    raise PlaylistError("酷狗歌单不可访问，可能已删除、设为私密或需要登录。")
                result = data.get("data") or {}
                raw = result.get("info") or []
                if page == first_page:
                    check_range(count(result.get("total")), detail.get("specialname") or f"酷狗歌单 {identifier}")
                signature = tuple(item.get("hash") for item in raw)
                if signature and signature == previous:
                    warnings.append("平台分页返回重复内容，已停止继续读取。")
                    break
                previous = signature
                low = max(0, start - 1 - (page - 1) * PAGE_SIZE)
                high = min(len(raw), start - 1 + limit - (page - 1) * PAGE_SIZE)
                def convert(item):
                    item = dict(item)
                    if not item.get("songname") and " - " in item.get("filename", ""):
                        item["singername"], item["songname"] = item["filename"].split(" - ", 1)
                    return music._kugou_track(item)
                append(raw[low:high], convert, (page - 1) * PAGE_SIZE + low)
                if len(raw) < PAGE_SIZE or metadata["total"] is not None and page * PAGE_SIZE >= metadata["total"]:
                    break
    except RangeRequired:
        raise
    except Exception:
        if not tracks or stop.is_set():
            raise
        warnings.append("后续曲目读取失败，已保留已读取部分；可重新导入剩余范围。")
    total = metadata["total"]
    expected = min(limit, max(0, total - start + 1)) if total is not None else None
    metadata.update(read_count=len(tracks), complete=total is not None and start == 1 and len(tracks) == total and not warnings,
                    range_complete=expected is not None and len(tracks) == expected and not warnings)
    if total is None:
        warnings.append(f"已读取 {len(tracks)} 首，平台未提供总数，无法确认歌单完整性。")
    elif not metadata["complete"]:
        warnings.append(f"歌单共 {total} 首，本次从第 {start} 首读取 {len(tracks)} 首，未导入完整歌单。")
    if total != 0 and not tracks:
        raise PlaylistError("未能读取有效曲目，可使用截图或文本导入。")
    return {"playlist": metadata, "tracks": tracks, "warnings": warnings}
