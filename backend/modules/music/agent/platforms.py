from __future__ import annotations

import re

from backend.modules.music import sources as music
from backend.modules.music.agent import skill


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


def track_lyrics(track, charge, check=None):
    from backend.modules.music.lyric_lookup import lookup
    return lookup(track, check=check)
