from __future__ import annotations

import base64
import html
import re

from backend import music
from backend.music_agent import skill


def categories():
    return {"providers": [
        {"provider": "netease", "playlist_search": True, "category_directory": False,
         "method": "keyword", "categories": skill.load()["categories"]["scenes"]},
        {"provider": "qq", "playlist_search": True, "category_directory": False, "method": "keyword"},
        {"provider": "kugou", "playlist_search": False, "category_directory": False, "method": "track_keyword"}],
        "version": skill.load()["categories"]["version"]}


def search_playlists(provider, query, page, charge):
    charge(search_rounds=1)
    if provider == "netease":
        data = music._json("https://music.163.com/api/search/get/web", data={"s": query, "type": 1000, "limit": 10, "offset": (page-1)*10}, referer="https://music.163.com/")
        result = data.get("result") or {}
        rows = [{"provider": provider, "playlist_id": str(p["id"]), "name": music._clean(p["name"]),
                 "description": music._clean(p.get("description", ""))[:500], "count": p.get("trackCount", 0),
                 "thumbnail": p.get("coverImgUrl"), "method": "keyword", "query": query} for p in result.get("playlists", [])]
        return {"playlists": rows, "page": page, "has_more": page*10 < result.get("playlistCount", 0)}
    if provider == "qq":
        data = music._qq_call("search", "music.search.SearchCgiService", "DoSearchForQQMusicDesktop",
                              {"query": query, "num_per_page": 10, "page_num": page, "search_type": 3})
        result = (data.get("body") or {}).get("songlist") or {}
        rows = [{"provider": provider, "playlist_id": str(p.get("dissid") or p.get("diss_id")),
                 "name": music._clean(p.get("dissname") or p.get("diss_name")), "description": "",
                 "count": p.get("song_count", 0), "thumbnail": p.get("imgurl"), "method": "keyword", "query": query}
                for p in result.get("list", []) if p.get("dissid") or p.get("diss_id")]
        return {"playlists": rows, "page": page, "has_more": len(rows) == 10}
    raise ValueError("此平台不支持歌单检索")


def playlist_tracks(provider, playlist_id, offset, limit, charge):
    if not re.fullmatch(r"\d{1,24}", playlist_id):
        raise ValueError("歌单 ID 无效")
    if provider == "netease":
        data = music._json("https://music.163.com/api/playlist/detail", params={"id": playlist_id}, referer="https://music.163.com/")
        playlist = data.get("result") or data.get("playlist") or {}
        if not playlist:
            raise RuntimeError("平台未返回歌单，可能需要登录")
        source = playlist.get("tracks") or []
        rows = [music._netease_track(t).model_dump(exclude={"token"}) for t in source[offset:offset+limit]]
        return {"tracks": rows, "offset": offset, "next_offset": offset+len(rows),
                "has_more": offset+len(rows) < len(source), "total": playlist.get("trackCount", len(source)),
                "coverage": {"returned_by_provider": len(source), "playlist_total": playlist.get("trackCount", len(source)),
                             "truncated_by_provider": len(source) < playlist.get("trackCount", len(source))},
                "playlist": {"name": music._clean(playlist.get("name")), "tags": playlist.get("tags") or []}}
    if provider == "qq":
        data = music._json("https://c.y.qq.com/qzone/fcg-bin/fcg_ucc_getcdinfo_byids_cp.fcg",
                           params={"type": 1, "json": 1, "utf8": 1, "onlysong": 0, "disstid": playlist_id, "format": "json"}, referer="https://y.qq.com/")
        lists = data.get("cdlist") or []
        if not lists:
            raise RuntimeError("QQ 音乐歌单当前不可访问")
        source = lists[0].get("songlist") or []
        return {"tracks": [music._qq_track(t).model_dump(exclude={"token"}) for t in source[offset:offset+limit]],
                "offset": offset, "next_offset": offset+limit, "has_more": offset+limit < len(source),
                "total": len(source), "coverage": {"returned_by_provider": len(source)},
                "playlist": {"name": music._clean(lists[0].get("dissname")), "tags": []}}
    raise ValueError("此平台不支持歌单曲目")


def track_lyrics(track, charge):
    provider, identifier = track["provider"], track["identifier"]
    if provider == "netease":
        data = music._json("https://music.163.com/api/song/lyric", params={"id": identifier, "lv": -1, "kv": -1, "tv": -1}, referer="https://music.163.com/")
        text = (data.get("lrc") or {}).get("lyric") or ""
        if text or data.get("nolyric") is True:
            return {"text": text[:24000], "source": "网易云歌曲身份歌词", "instrumental": data.get("nolyric") is True,
                    "document_complete": len(text) <= 24000, "identity": f"netease:{identifier}"}
    elif provider == "qq":
        data = music._json("https://c.y.qq.com/lyric/fcgi-bin/fcg_query_lyric_new.fcg",
                           params={"songmid": identifier, "format": "json", "nobase64": 1}, referer="https://y.qq.com/")
        text = data.get("lyric") or ""
        if data.get("code") == 0 and text:
            if not text.startswith("[") and "&#" not in text:
                try:
                    text = base64.b64decode(text, validate=True).decode("utf-8")
                except (ValueError, UnicodeError):
                    pass
            text = html.unescape(text)
            return {"text": text[:24000], "source": "QQ 音乐歌曲身份歌词", "instrumental": None,
                    "document_complete": len(text) <= 24000, "identity": f"qq:{identifier}"}
    if not track.get("artist") or not track.get("duration"):
        return {"text": "", "instrumental": None, "source": "", "document_complete": False}
    data = music._json("https://lrclib.net/api/get", params={"track_name": track["title"], "artist_name": track["artist"], "duration": round(track["duration"])})
    def normalized(value):
        return re.sub(r"[\W_]+", "", str(value).casefold())
    if (normalized(data.get("trackName")) != normalized(track["title"])
            or normalized(data.get("artistName")) != normalized(track["artist"])
            or abs(float(data.get("duration") or 0)-track["duration"]) > max(3, track["duration"]*.03)):
        raise ValueError("歌词歌曲身份不匹配")
    text = data.get("syncedLyrics") or data.get("plainLyrics") or ""
    return {"text": text[:24000], "source": "LRCLIB 身份匹配", "instrumental": data.get("instrumental"),
            "document_complete": len(text) <= 24000, "identity": f"{provider}:{identifier}"}
