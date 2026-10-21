from __future__ import annotations

import html
import re
import secrets
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Annotated, Callable, Literal
from urllib.parse import parse_qs, urlparse

import requests
from fastapi import APIRouter, Header, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, HttpUrl
from yt_dlp import YoutubeDL

from backend.modules.accounts import service as accounts
from backend.modules.music import full_playback


Provider = Literal["qq", "netease", "kugou"]
router = APIRouter(prefix="/api/music", tags=["music"])
HEADERS = {"User-Agent": "Mozilla/5.0", "Referer": "https://y.qq.com/"}
GD_API = "https://music-api.gdstudio.xyz/api.php"
AT38_URL = "https://www.at38.cn/"
TOKEN_TTL = 1800
SEARCH_PAGE_SIZE = 20
RANGE_PATTERN = re.compile(r"^bytes=(?:\d+-\d*|-\d+)$")
lock = threading.Lock()
search_pool = ThreadPoolExecutor(max_workers=3, thread_name_prefix="music-search")


class Track(BaseModel):
    title: str
    artist: str = ""
    duration: float | None = None
    thumbnail: str | None = None
    webpage_url: str
    provider: Provider
    identifier: str
    token: str = ""
    access: str = "权限未核实"
    filename: str | None = None
    local_url: str | None = None
    file_available: bool = False


@dataclass
class TrackSession:
    track: Track
    expires_at: float


class PlaybackRequest(BaseModel):
    token: str = Field(min_length=1, max_length=128)
    webpage_url: HttpUrl


@dataclass
class PlaybackSession:
    audio: full_playback.PreparedAudio
    expires_at: float
sessions: dict[str, TrackSession] = {}
playback_sessions: dict[str, PlaybackSession] = {}
search_cache: dict[tuple[str, str, int], tuple[float, list[Track]]] = {}


def _json(url: str, *, params: dict | None = None, data: dict | None = None, payload: dict | None = None, referer: str | None = None):
    from backend.infrastructure.network import charge_request
    charge_request()
    headers = {**HEADERS, "Referer": referer or HEADERS["Referer"]}
    response = requests.post(url, data=data, json=payload, headers=headers, timeout=(5, 15)) if data is not None or payload is not None else requests.get(url, params=params, headers=headers, timeout=(5, 15))
    response.raise_for_status()
    return response.json()


def _clean(value) -> str:
    return html.unescape(re.sub(r"<[^>]*>", "", str(value or ""))).strip()


def _number(value, divisor: float = 1) -> float | None:
    try:
        number = float(value) / divisor
        return number if 0 < number < 86400 else None
    except (ValueError, TypeError):
        return None


def _register(track: Track) -> Track:
    now = time.time()
    with lock:
        for token in [token for token, session in sessions.items() if session.expires_at <= now]:
            del sessions[token]
        if len(sessions) >= 2000:
            del sessions[next(iter(sessions))]
        track = track.model_copy(update={"token": secrets.token_urlsafe(24)})
        sessions[track.token] = TrackSession(track, now + TOKEN_TTL)
    return track


def get_track(token: str, webpage_url: str) -> Track:
    with lock:
        session = sessions.get(token)
    if not session or session.expires_at <= time.time():
        raise HTTPException(410, "歌曲信息已过期，请重新搜索或解析。")
    if session.track.webpage_url != webpage_url:
        raise HTTPException(422, "歌曲信息与下载链接不一致，请重新解析。")
    return session.track


def identify(url: str) -> tuple[Provider, str] | None:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    query = parse_qs(parsed.query)
    if host == "music.163.com" or host.endswith(".music.163.com"):
        query.update(parse_qs(urlparse(parsed.fragment).query))
        identifier = (query.get("id") or [""])[0]
        if identifier.isdigit() and "/song" in parsed.path + parsed.fragment:
            return "netease", identifier
    if host == "y.qq.com" or host.endswith(".y.qq.com"):
        match = re.search(r"/songDetail/([A-Za-z0-9]+)", parsed.path)
        identifier = match[1] if match else (query.get("songmid") or [""])[0]
        if re.fullmatch(r"[A-Za-z0-9]{10,20}", identifier):
            return "qq", identifier
    if host == "kugou.com" or host.endswith(".kugou.com"):
        query.update(parse_qs(parsed.fragment))
        identifier = (query.get("hash") or [""])[0]
        if re.fullmatch(r"[A-Fa-f0-9]{32}", identifier):
            return "kugou", identifier.upper()
    return None


def _netease_track(item: dict) -> Track:
    identifier = str(item["id"])
    album = item.get("al") or item.get("album") or {}
    artists = item.get("ar") or item.get("artists") or []
    fee = item.get("fee")
    access = "会员专享" if fee == 1 else "单曲或专辑付费" if fee == 4 else "权限未核实"
    return Track(title=_clean(item.get("name")), artist=" / ".join(_clean(a.get("name")) for a in artists),
                 duration=_number(item.get("dt") or item.get("duration"), 1000), thumbnail=album.get("picUrl"),
                 webpage_url=f"https://music.163.com/song?id={identifier}", provider="netease", identifier=identifier, access=access)


def _qq_call(key: str, module: str, method: str, params: dict) -> dict:
    data = _json("https://u.y.qq.com/cgi-bin/musicu.fcg", payload={
        "comm": {"ct": 24, "cv": 0, "uin": "0", "format": "json"},
        key: {"module": module, "method": method, "param": params},
    })
    part = data.get(key) or {}
    if part.get("code", -1) != 0:
        raise RuntimeError("QQ 音乐接口暂不可用或要求登录。")
    return part.get("data") or {}


def _qq_track(item: dict) -> Track:
    identifier = str(item.get("mid") or item["songmid"])
    album = item.get("album") or {}
    album_mid = album.get("mid") or item.get("albummid")
    pay = item.get("pay") or {}
    return Track(title=_clean(item.get("title") or item.get("songname")), artist=" / ".join(_clean(a.get("name")) for a in item.get("singer", [])),
                 duration=_number(item.get("interval")), thumbnail=f"https://y.gtimg.cn/music/photo_new/T002R300x300M000{album_mid}.jpg" if album_mid else None,
                 webpage_url=f"https://y.qq.com/n/ryqq/songDetail/{identifier}", provider="qq", identifier=identifier,
                 access="会员或付费" if pay.get("pay_play") or pay.get("pay_down") else "权限未核实")


def _kugou_track(item: dict) -> Track:
    identifier = str(item.get("hash") or item.get("FileHash") or item.get("audio_id") or "").upper()
    if not re.fullmatch(r"[A-F0-9]{32}", identifier):
        raise ValueError("酷狗结果缺少有效歌曲标识。")
    artist = item.get("singername") or item.get("SingerName") or item.get("singerName") or item.get("author_name") or ""
    title = item.get("songname") or item.get("SongName") or item.get("songName") or item.get("audio_name") or item.get("filename") or item.get("fileName") or ""
    thumbnail = item.get("img") or item.get("Image") or item.get("album_img") or item.get("imgUrl")
    milliseconds = item.get("timelength") or (item.get("extra") or {}).get("128timelength")
    duration = _number(milliseconds, 1000) if milliseconds else _number(item.get("duration") or item.get("Duration") or item.get("timeLength"))
    return Track(title=_clean(title), artist=_clean(artist), duration=duration,
                 thumbnail=thumbnail.replace("{size}", "240") if thumbnail else None,
                 webpage_url=f"https://www.kugou.com/song/#hash={identifier}", provider="kugou", identifier=identifier,
                 access="会员或付费" if item.get("privilege") or item.get("Privilege") or item.get("128privilege") else "权限未核实")


def parse_url(url: str) -> Track:
    identity = identify(url)
    if not identity:
        raise HTTPException(422, "请使用单曲链接；歌单、专辑和该平台的短链接暂不支持。")
    provider, identifier = identity
    try:
        if provider == "netease":
            data = _json("https://music.163.com/api/song/detail/", params={"id": identifier, "ids": f"[{identifier}]"}, referer="https://music.163.com/")
            track = _netease_track(data["songs"][0])
        elif provider == "qq":
            data = _qq_call("songinfo", "music.pf_song_detail_svr", "get_song_detail_yqq", {"song_mid": identifier})
            track = _qq_track(data["track_info"])
        else:
            data = _json("https://www.kugou.com/yy/index.php", params={"r": "play/getdata", "hash": identifier}, referer="https://www.kugou.com/")
            item = data.get("data") or {}
            if not isinstance(item, dict) or not any(item.get(key) for key in ("audio_name", "songname", "filename")):
                item = _json("https://m.kugou.com/app/i/getSongInfo.php", params={"cmd": "playInfo", "hash": identifier}, referer="https://m.kugou.com/")
            item = {**item, "hash": identifier}
            track = _kugou_track(item)
            if not track.title:
                raise RuntimeError("酷狗接口未返回歌曲信息，可能需要登录。")
        return _register(track)
    except HTTPException:
        raise
    except Exception as exc:
        from backend.infrastructure.network import raise_if_guarded
        raise_if_guarded(exc)
        raise HTTPException(422, "平台未返回单曲信息，可能要求登录或当前接口不可用。") from exc


def _search(provider: Provider, query: str, page: int = 1) -> list[Track]:
    cache_key = (provider, query, page)
    with lock:
        cached = search_cache.get(cache_key)
    if cached and cached[0] > time.time():
        return [_register(track) for track in cached[1]]
    if provider == "netease":
        data = _json("https://music.163.com/api/search/get/web", data={"s": query, "type": 1, "limit": SEARCH_PAGE_SIZE, "offset": (page - 1) * SEARCH_PAGE_SIZE}, referer="https://music.163.com/")
        tracks = [_netease_track(item) for item in data.get("result", {}).get("songs", [])]
    elif provider == "qq":
        data = _json("https://c.y.qq.com/soso/fcgi-bin/client_search_cp", params={"w": query, "p": page, "n": SEARCH_PAGE_SIZE, "format": "json", "aggr": 1, "new_json": 1})
        tracks = [_qq_track(item) for item in data.get("data", {}).get("song", {}).get("list", [])]
    else:
        data = _json("https://songsearch.kugou.com/song_search_v2", params={"keyword": query, "page": page, "pagesize": SEARCH_PAGE_SIZE, "platform": "WebFilter", "filter": 2}, referer="https://www.kugou.com/")
        tracks = []
        for item in data.get("data", {}).get("lists", []):
            try:
                tracks.append(_kugou_track(item))
            except ValueError:
                continue
    with lock:
        for key in [key for key, entry in search_cache.items() if entry[0] <= time.time()]:
            del search_cache[key]
        if len(search_cache) >= 100:
            del search_cache[next(iter(search_cache))]
        search_cache[cache_key] = (time.time() + 120, tracks)
    return [_register(track) for track in tracks]


@router.get("/search")
def search(q: str = Query(min_length=1, max_length=100), source: Literal["all", "qq", "netease", "kugou"] = "all",
           page: Annotated[int, Query(ge=1, le=20)] = 1) -> dict:
    if not q.strip():
        raise HTTPException(422, "请输入歌曲名称。")
    providers = ["qq", "netease", "kugou"] if source == "all" else [source]
    futures = [(provider, search_pool.submit(_search, provider, q.strip(), page)) for provider in providers]
    tracks, statuses, has_more = [], [], False
    for provider, future in futures:
        try:
            found = future.result()
            tracks.extend(found)
            has_more = has_more or len(found) >= SEARCH_PAGE_SIZE
            statuses.append({"provider": provider, "available": True, "message": f"找到 {len(found)} 首歌曲" if found else "未找到歌曲或平台未返回结果。"})
        except Exception:
            statuses.append({"provider": provider, "available": False, "message": "搜索接口暂不可用，可能需要登录或触发了平台限制。"})
    if accounts.principal.get():
        from backend.modules.music import library
        personal = library.downloaded_status([library.TrackInput.model_validate(track.model_dump()) for track in tracks])
        tracks = [track.model_copy(update=status) for track, status in zip(tracks, personal)]
    return {"tracks": tracks, "sources": statuses, "page": page, "has_more": has_more and page < 20}


def _at38_url(track: Track) -> str:
    source_type = {"qq": "qq", "netease": "wy"}.get(track.provider)
    if not source_type:
        raise RuntimeError("at38 暂不支持该音乐平台。")
    params = {"action": "geturl", "id": track.identifier, "type": source_type,
              "filename": f"{track.artist} - {track.title}"}
    with requests.Session() as session:
        for attempt in range(2):
            from backend.infrastructure.network import charge_request
            charge_request()
            response = session.get(AT38_URL, params=params, headers={**HEADERS, "Referer": AT38_URL}, timeout=(5, 15))
            response.raise_for_status()
            try:
                data = response.json()
            except ValueError as exc:
                # The normal visitor page sets this cookie in JavaScript before reloading.
                cookie = re.search(r"""document\.cookie\s*=\s*["']mk_visit=([^;"'\s]+)""", response.text)
                if attempt or not cookie:
                    raise RuntimeError("at38 未返回音源，请稍后重试。") from exc
                session.cookies.set("mk_visit", cookie[1], domain="www.at38.cn", path="/")
                continue
            media_url = data.get("url") if isinstance(data, dict) and data.get("code") == 1 else None
            parsed = urlparse(media_url) if isinstance(media_url, str) else None
            if not parsed or parsed.scheme not in {"http", "https"} or not parsed.hostname:
                raise RuntimeError("at38 未返回有效音源。")
            return media_url
    raise RuntimeError("at38 未返回音源，请稍后重试。")


def resolve(track: Track, options: dict, *, public_only: bool = False,
            validate_source: Callable[[str, dict, str], None] | None = None) -> tuple[str, dict, str]:
    options = {**options, "http_headers": {**options.get("http_headers", {}), "Referer": track.webpage_url}}
    def accept(url, resolved, source):
        if validate_source:
            validate_source(url, resolved, source)
        return url, resolved, source

    try:
        if track.provider == "kugou" and not public_only:
            data = _json("https://www.kugou.com/yy/index.php", params={"r": "play/getdata", "hash": track.identifier}, referer="https://www.kugou.com/")
            media_url = (data.get("data") or {}).get("play_url")
            if not media_url:
                mobile = _json("https://m.kugou.com/app/i/getSongInfo.php", params={"cmd": "playInfo", "hash": track.identifier}, referer="https://m.kugou.com/")
                media_url = mobile.get("url")
            if media_url:
                return accept(media_url, options, "酷狗音乐")
        elif not public_only:
            with YoutubeDL({**options, "skip_download": True}) as ydl:
                info = ydl.extract_info(track.webpage_url, download=False)
            formats = [item for item in (info or {}).get("formats", []) if item.get("url") and not item.get("has_drm") and item.get("vcodec") == "none"]
            if formats:
                best = max(formats, key=lambda item: item.get("abr") or item.get("tbr") or 0)
                return accept(best["url"], {**options, "http_headers": {**options["http_headers"], **best.get("http_headers", {})}}, "原平台")
    except full_playback.PreparationExpired:
        raise
    except Exception as exc:
        from backend.infrastructure.network import raise_if_guarded
        raise_if_guarded(exc)
        pass
    # An incomplete recording is a failed candidate, so the backup is tried too.
    try:
        if track.provider == "netease":
            data = _json(GD_API, params={"types": "url", "source": "netease", "id": track.identifier, "br": 320})
            media_url, source = data.get("url"), "GD音乐台 · music.gdstudio.xyz"
        elif track.provider == "qq":
            data = _json("https://api.vkeys.cn/music/tencent/song/link", params={"mid": track.identifier, "quality": 8})
            media_url, source = (data.get("data") or {}).get("url"), "Vkeys · QQ 音乐解析"
        else:
            data = _json("https://music.lzmhhh.com/api/music/url", data={"id": track.identifier, "type": "kg"}, referer="https://music.lzmhhh.com/")
            media_url = data.get("data") if data.get("code") == 1 else None
            source = "LZM 音乐 · music.lzmhhh.com"
        if isinstance(media_url, str) and urlparse(media_url).scheme in ("http", "https"):
            return accept(media_url, options, source)
    except full_playback.PreparationExpired:
        raise
    except Exception as exc:
        from backend.infrastructure.network import raise_if_guarded
        raise_if_guarded(exc)
        pass
    if track.provider in {"qq", "netease"}:
        try:
            media_url = _at38_url(track)
            return accept(media_url, {**options, "http_headers": {**options["http_headers"], "Referer": AT38_URL}}, "51ape音乐 · at38.cn")
        except full_playback.PreparationExpired:
            raise
        except Exception as exc:
            from backend.infrastructure.network import raise_if_guarded
            raise_if_guarded(exc)
            pass
    if track.provider == "kugou":
        raise RuntimeError("酷狗音乐未返回完整音源，当前歌曲可能需要付费或平台接口要求登录。")
    raise RuntimeError("未取得完整音源：平台可能要求会员或单曲购买，或当前解析接口不可用。")


@router.post("/playback")
def create_playback(payload: PlaybackRequest) -> dict:
    track = get_track(payload.token, str(payload.webpage_url))
    try:
        prepared = full_playback.prepare(track)
    except RuntimeError as exc:
        raise HTTPException(422, str(exc)) from exc
    token = secrets.token_urlsafe(24)
    try:
        accounts.register_resource("playback", token, {"source_url": track.webpage_url,
            "resolved_source_url": prepared.source_url, "media_url": prepared.media_url})
    except BaseException:
        prepared.close()
        raise
    now = time.time()
    with lock:
        for expired in [key for key, session in playback_sessions.items() if session.expires_at <= now]:
            playback_sessions.pop(expired).audio.close()
        if len(playback_sessions) >= 200:
            playback_sessions.pop(next(iter(playback_sessions))).audio.close()
        playback_sessions[token] = PlaybackSession(prepared, now + TOKEN_TTL)
    return {"playback_url": f"/api/music/playback/{token}/content", "expires_in": TOKEN_TTL,
            "source": prepared.source, "duration": prepared.duration, "full": True}


@router.api_route("/playback/{token}/content", methods=["GET", "HEAD"])
def playback_content(token: str, range_header: Annotated[str | None, Header(alias="Range")] = None):
    if accounts.principal.get():
        accounts.resource("playback", token)
    with lock:
        session = playback_sessions.get(token)
        if session and session.expires_at <= time.time():
            del playback_sessions[token]
            session.audio.close()
            session = None
    if not session or not session.audio.path.is_file():
        raise HTTPException(410, "播放链接已过期，请重新点击播放。")
    if range_header and not RANGE_PATTERN.fullmatch(range_header.strip()):
        raise HTTPException(416, "仅支持单段音频范围请求。")
    return FileResponse(session.audio.path, media_type=session.audio.media_type,
                        content_disposition_type="inline", headers={"Cache-Control": "private, no-store"})
