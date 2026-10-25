from __future__ import annotations

import time
from fastapi import HTTPException
from pydantic import Field
from backend.modules.social import store as s, shares
from backend.modules.social.models import Input
from backend.modules.music.library import TrackInput


class PlaylistInput(Input):
    title: str = Field(min_length=1, max_length=80)
    members: list[int] = Field(default_factory=list, max_length=99)


class EditInput(Input):
    revision: int = Field(ge=0)
    track: TrackInput | None = None
    remove_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$")
    member_id: int | None = Field(default=None, gt=0)
    remove_member: bool = False
    order: list[str] | None = Field(default=None, max_length=500)


def require(cursor, key, lock=False):
    cursor.execute("SELECT * FROM social_playlists WHERE id=%s" + (" FOR UPDATE" if lock else ""), (key,))
    row = s.decode(cursor.fetchone())
    cursor.execute("SELECT * FROM social_playlist_members WHERE playlist_id=%s AND user_id=%s", (key, s.uid()))
    member = s.decode(cursor.fetchone())
    if row["owner_id"] is None or row["owner_id"] != s.uid() and s.blocked(cursor, row["owner_id"], s.uid()):
        raise HTTPException(403, "共同歌单不可访问。")
    s.active_user(cursor, row["owner_id"])
    return row, member


def listing():
    with s.database() as cursor:
        cursor.execute("""SELECT p.* FROM social_playlists p JOIN social_playlist_members m ON m.playlist_id=p.id
            WHERE m.user_id=%s ORDER BY p.updated_at DESC LIMIT 100""", (s.uid(),))
        return {"items": cursor.fetchall()}


def get(key):
    with s.database() as cursor:
        row, _ = require(cursor, key)
        cursor.execute("SELECT * FROM social_playlist_items WHERE playlist_id=%s ORDER BY order_no", (key,))
        row["items"] = [s.decode(x, "track_snapshot") for x in cursor.fetchall()]
        cursor.execute("SELECT m.*,u.username FROM social_playlist_members m JOIN users u ON u.id=m.user_id WHERE m.playlist_id=%s", (key,))
        row["members"] = cursor.fetchall()
        return row


def create(payload):
    s.rate("shared-playlist", 20)
    key, now = s.identifier(), time.time()
    with s.database() as cursor:
        for target in set(payload.members) - {s.uid()}:
            s.require_friend(cursor, s.uid(), target)
        cursor.execute("INSERT INTO social_playlists (id,owner_id,title,created_at,updated_at) VALUES (%s,%s,%s,%s,%s)", (key, s.uid(), payload.title, now, now))
        for target in set([s.uid(), *payload.members]):
            cursor.execute("INSERT INTO social_playlist_members (playlist_id,user_id,role,created_at) VALUES (%s,%s,%s,%s)", (key, target, "owner" if target == s.uid() else "editor", now))
            if target != s.uid():
                s.notify(cursor, target, "shared_playlist", key, {"playlist_id": key, "title": payload.title})
    return get(key)


def edit(key, payload):
    with s.database() as cursor:
        row, member = require(cursor, key, True)
        if payload.revision != row["revision"]:
            raise HTTPException(409, "歌单已更新，请刷新后重试。")
        if payload.member_id:
            if member["role"] != "owner" or payload.member_id == row["owner_id"]:
                raise HTTPException(403, "仅创建者可管理其他成员。")
            if payload.remove_member:
                cursor.execute("DELETE FROM social_playlist_members WHERE playlist_id=%s AND user_id=%s", (key, payload.member_id))
            else:
                s.require_friend(cursor, s.uid(), payload.member_id)
                cursor.execute("INSERT IGNORE INTO social_playlist_members (playlist_id,user_id,role,created_at) VALUES (%s,%s,'editor',%s)", (key, payload.member_id, time.time()))
        elif payload.track:
            if payload.track.provider not in {"qq", "netease", "kugou", "video"}:
                raise HTTPException(422, "共同歌单支持可独立获取音源的在线歌曲。")
            snapshot = shares.safe_track(payload.track.model_dump())
            track_key = s.digest(payload.track.provider + ":" + (payload.track.identifier or payload.track.webpage_url))
            cursor.execute("SELECT COUNT(*) AS n,COALESCE(MAX(order_no),0)+1 AS next_order FROM social_playlist_items WHERE playlist_id=%s", (key,))
            count = cursor.fetchone()
            if count["n"] >= 500:
                raise HTTPException(409, "歌单最多保存 500 首。")
            cursor.execute("INSERT IGNORE INTO social_playlist_items (id,playlist_id,contributor_id,track_key,track_snapshot,order_no,created_at) VALUES (%s,%s,%s,%s,%s,%s,%s)", (s.identifier(), key, s.uid(), track_key, s.encode(snapshot), count["next_order"], time.time()))
        elif payload.remove_id:
            cursor.execute("DELETE FROM social_playlist_items WHERE id=%s AND playlist_id=%s", (payload.remove_id, key))
        elif payload.order is not None:
            cursor.execute("SELECT id FROM social_playlist_items WHERE playlist_id=%s", (key,))
            ids = {x["id"] for x in cursor.fetchall()}
            if len(payload.order) != len(ids) or set(payload.order) != ids:
                raise HTTPException(422, "排序必须包含全部曲目且不能重复。")
            cursor.execute("UPDATE social_playlist_items SET order_no=order_no+1000000 WHERE playlist_id=%s", (key,))
            for order, item in enumerate(payload.order):
                cursor.execute("UPDATE social_playlist_items SET order_no=%s WHERE playlist_id=%s AND id=%s", (order, key, item))
        cursor.execute("UPDATE social_playlists SET revision=revision+1,updated_at=%s WHERE id=%s", (time.time(), key))
        cursor.execute("SELECT user_id FROM social_playlist_members WHERE playlist_id=%s", (key,))
        s.event(cursor, "shared_playlist", key, row["revision"] + 1, [x["user_id"] for x in cursor.fetchall()])
    return get(key)
