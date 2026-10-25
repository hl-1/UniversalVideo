from __future__ import annotations

import secrets
import time
from typing import Literal
from fastapi import HTTPException
from pydantic import Field
from backend.modules.social import store as s, chat, shares
from backend.modules.social.models import Input
from backend.modules.music.library import TrackInput


class RoomInput(Input):
    title: str = Field(min_length=1, max_length=80)
    kind: Literal["listen", "karaoke", "duet"] = "listen"
    visibility: Literal["private", "friends", "invite"] = "private"
    capacity: int = Field(default=10, ge=2, le=20)


class JoinInput(Input):
    code: str = Field(default="", max_length=100)


class ControlInput(Input):
    revision: int = Field(ge=0)
    action: Literal["play", "pause", "seek", "next", "enqueue", "cancel_queue", "transfer", "admin", "controller", "remove", "mute", "leave", "close", "ready", "finish", "react"]
    track: TrackInput | None = None
    karaoke_song_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$")
    queue_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$")
    position_ms: int = Field(default=0, ge=0, le=86400000)
    target_user_id: int | None = Field(default=None, gt=0)
    allow: bool = True
    reaction: Literal["👏", "❤️", "🎵", "🎉"] = "👏"
    microphone_ready: bool = False


def require(cursor, key, *, lock=False):
    cursor.execute("SELECT * FROM social_rooms WHERE id=%s" + (" FOR UPDATE" if lock else ""), (key,))
    room = s.decode(cursor.fetchone(), "config", "playback")
    cursor.execute("SELECT * FROM social_room_members WHERE room_id=%s AND user_id=%s AND status='active'", (key, s.uid()))
    membership = s.decode(cursor.fetchone(), "permissions")
    if room["status"] != "active":
        raise HTTPException(410, "房间已结束。")
    s.active_user(cursor, room["owner_id"])
    if room["owner_id"] is not None and room["owner_id"] != s.uid() and s.blocked(cursor, s.uid(), room["owner_id"]):
        raise HTTPException(403, "无法访问该房间。")
    return room, membership


def public(cursor, room):
    room = s.decode(room, "config", "playback")
    cursor.execute("""SELECT m.user_id,m.role,m.permissions,m.joined_at,u.username FROM social_room_members m
        JOIN users u ON u.id=m.user_id WHERE m.room_id=%s AND m.status='active' ORDER BY m.joined_at""", (room["id"],))
    room["members"] = [s.decode(x, "permissions") for x in cursor.fetchall()]
    cursor.execute("SELECT * FROM social_room_queue WHERE room_id=%s AND status IN ('queued','performing') ORDER BY order_no LIMIT 100", (room["id"],))
    room["queue"] = [s.decode(x, "track_snapshot") for x in cursor.fetchall()]
    room["server_time"] = time.time()
    return room


def listing():
    with s.database() as cursor:
        cursor.execute("""SELECT DISTINCT r.* FROM social_rooms r LEFT JOIN social_room_members m ON m.room_id=r.id AND m.user_id=%s
            WHERE r.status='active' AND (m.status='active' OR (r.visibility='friends' AND EXISTS
                (SELECT 1 FROM social_relations f WHERE f.user_id=%s AND f.target_user_id=r.owner_id AND f.kind='friend' AND f.status='accepted')))
            AND NOT EXISTS (SELECT 1 FROM social_relations b WHERE b.kind='block' AND b.status='active' AND
                ((b.user_id=%s AND b.target_user_id=r.owner_id) OR (b.user_id=r.owner_id AND b.target_user_id=%s)))
            ORDER BY r.updated_at DESC LIMIT 100""", (s.uid(), s.uid(), s.uid(), s.uid()))
        return {"items": [s.decode(x, "config", "playback") for x in cursor.fetchall()]}


def create(payload):
    config = s.enabled("social_rooms_enabled")
    if payload.kind != "listen":
        s.enabled("social_karaoke_enabled")
    if payload.kind == "duet":
        s.enabled("social_realtime_duet_enabled")
    s.rate("create-room", 10)
    key, now = s.identifier(), time.time()
    cap = min(payload.capacity, config.get("social_listen_capacity", 10) if payload.kind == "listen" else config.get("social_karaoke_capacity", 20))
    with s.database() as cursor:
        conversation = chat.create_conversation(cursor, "room", payload.title, s.uid(), [(s.uid(), "owner", "active")])
        cursor.execute("""INSERT INTO social_rooms
            (id,conversation_id,owner_id,kind,title,visibility,config,playback,last_active_at,created_at,updated_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""", (key, conversation, s.uid(), payload.kind, payload.title, payload.visibility,
            s.encode({"capacity": cap, "recording": False}), s.encode({"state": "paused", "track": None, "position_ms": 0, "started_at": None}), now, now, now))
        cursor.execute("INSERT INTO social_room_members (room_id,user_id,role,permissions,joined_at,updated_at) VALUES (%s,%s,'owner',%s,%s,%s)", (key, s.uid(), s.encode({"control": True, "can_publish": False}), now, now))
    return get(key)


def get(key):
    with s.database() as cursor:
        room, _ = require(cursor, key)
        return public(cursor, room)


def invite(key, target=None):
    s.rate("room-invite", 30)
    with s.database() as cursor:
        room, member = require(cursor, key, lock=True)
        if member["role"] not in {"owner", "admin"}:
            raise HTTPException(403, "需要房间管理权限。")
        if target:
            s.require_friend(cursor, s.uid(), target)
            if chat.user_settings(cursor, target)["invite_policy"] == "nobody":
                raise HTTPException(403, "好友已关闭邀请。")
        result = create_invite(cursor, room, target)
        if target:
            s.notify(cursor, target, "room_invite", result["id"], {"room_id": key, "kind": room["kind"], "title": room["title"], "code": result["code"]})
        return result


def create_invite(cursor, room, target=None):
    key_id, token, now = s.identifier(), secrets.token_urlsafe(12), time.time()
    cursor.execute("""INSERT INTO social_room_invites
            (id,room_id,inviter_id,invitee_id,token_hash,max_uses,expires_at,created_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
            (key_id, room["id"], s.uid(), target, s.digest(token), 1 if target else room["config"]["capacity"], now + 86400, now))
    return {"id": key_id, "code": token, "room_id": room["id"], "expires_at": now + 86400}


def join(key, code=""):
    s.enabled("social_rooms_enabled")
    s.rate("room-join", 30)
    now = time.time()
    with s.database() as cursor:
        if code:
            cursor.execute("SELECT room_id FROM social_room_invites WHERE token_hash=%s", (s.digest(code),))
            found = s.decode(cursor.fetchone())
            if key and key != found["room_id"]:
                raise HTTPException(404, "邀请码不属于此房间。")
            key = found["room_id"]
        cursor.execute("SELECT * FROM social_rooms WHERE id=%s FOR UPDATE", (key,))
        room = s.decode(cursor.fetchone(), "config", "playback")
        if room["status"] != "active" or room["owner_id"] is None or s.blocked(cursor, s.uid(), room["owner_id"]):
            raise HTTPException(403, "房间不可加入。")
        s.active_user(cursor, room["owner_id"])
        cursor.execute("SELECT * FROM social_room_members WHERE room_id=%s AND user_id=%s", (key, s.uid()))
        old = cursor.fetchone()
        if old and old["status"] == "active":
            return public(cursor, room)
        if old and old["ban_until"] and old["ban_until"] > now:
            raise HTTPException(403, "暂时不能重新加入此房间。")
        if code:
            cursor.execute("SELECT * FROM social_room_invites WHERE token_hash=%s AND room_id=%s FOR UPDATE", (s.digest(code), key))
            invitation = s.decode(cursor.fetchone())
            if invitation["revoked_at"] or invitation["expires_at"] <= now or invitation["used_count"] >= invitation["max_uses"] or invitation["invitee_id"] not in {None, s.uid()} or invitation["inviter_id"] is None or s.blocked(cursor, s.uid(), invitation["inviter_id"]):
                raise HTTPException(403, "邀请码已失效。")
        elif room["visibility"] != "friends" or not s.friends(cursor, s.uid(), room["owner_id"]):
            raise HTTPException(403, "请使用有效邀请码加入。")
        cursor.execute("SELECT COUNT(*) AS n FROM social_room_members WHERE room_id=%s AND status='active'", (key,))
        if cursor.fetchone()["n"] >= room["config"]["capacity"]:
            raise HTTPException(409, "房间人数已满。")
        if code:
            cursor.execute("UPDATE social_room_invites SET used_count=used_count+1 WHERE id=%s", (invitation["id"],))
        cursor.execute("""INSERT INTO social_room_members (room_id,user_id,role,permissions,joined_at,updated_at)
            VALUES (%s,%s,'audience','{}',%s,%s) ON DUPLICATE KEY UPDATE status='active',role='audience',permissions='{}',joined_at=VALUES(joined_at),left_at=NULL,updated_at=VALUES(updated_at)""", (key, s.uid(), now, now))
        cursor.execute("""INSERT INTO social_conversation_members (conversation_id,user_id,role,joined_seq,last_read_seq,created_at,updated_at)
            SELECT id,%s,'member',last_seq,last_seq,%s,%s FROM social_conversations WHERE id=%s
            ON DUPLICATE KEY UPDATE status='active',joined_seq=VALUES(joined_seq),last_read_seq=VALUES(last_read_seq),updated_at=VALUES(updated_at)""", (s.uid(), now, now, room["conversation_id"]))
        changed(cursor, room)
    return get(key)


def changed(cursor, room, playback=None):
    now = time.time()
    cursor.execute("UPDATE social_rooms SET playback=%s,revision=revision+1,last_active_at=%s,updated_at=%s WHERE id=%s", (s.encode(playback or room["playback"]), now, now, room["id"]))
    s.event(cursor, "room", room["id"], room["revision"] + 1, s.recipients(cursor, room["conversation_id"]))


def control(key, payload):
    s.rate("room-control", 120)
    with s.database() as cursor:
        room, member = require(cursor, key, lock=True)
        if room["revision"] != payload.revision:
            raise HTTPException(409, "房间状态已更新，请刷新后重试。")
        action, now = payload.action, time.time()
        manager = member["role"] in {"owner", "admin"}
        controller = manager or member["permissions"].get("control")
        playback = dict(room["playback"])
        target = payload.target_user_id
        if action in {"play", "pause", "seek", "next", "finish"} and not controller and not (action == "finish" and s.uid() in playback.get("singers", [])):
            raise HTTPException(403, "需要播放控制权限。")
        if action in {"transfer", "admin", "controller", "remove", "mute", "close"} and not manager:
            raise HTTPException(403, "需要房间管理权限。")
        if action == "enqueue":
            cursor.execute("SELECT COUNT(*) AS n FROM social_room_queue WHERE room_id=%s AND status='queued'", (key,))
            if cursor.fetchone()["n"] >= 100:
                raise HTTPException(409, "待播队列已满。")
            if room["kind"] == "listen":
                if not payload.track or payload.track.provider not in {"qq", "netease", "kugou", "video"}:
                    raise HTTPException(422, "请选择可供成员独立解析的在线歌曲。")
                track = shares.safe_track(payload.track.model_dump())
            else:
                cursor.execute("SELECT * FROM social_karaoke_songs WHERE id=%s AND state='ready' AND (creator_id=%s OR visibility='public')", (payload.karaoke_song_id, s.uid()))
                song = s.decode(cursor.fetchone(), "track_snapshot")
                if room["kind"] == "duet":
                    if not target or target == s.uid():
                        raise HTTPException(422, "请选择一位合唱伙伴。")
                    cursor.execute("SELECT 1 FROM social_room_members WHERE room_id=%s AND user_id=%s AND status='active'", (key, target))
                    if not cursor.fetchone():
                        raise HTTPException(403, "合唱伙伴须先加入房间。")
                track = song["track_snapshot"] | {"backing_asset_id": song["backing_asset_id"], "duration": song["duration_ms"] / 1000}
            cursor.execute("SELECT COALESCE(MAX(order_no),0)+1 AS n FROM social_room_queue WHERE room_id=%s", (key,))
            order = cursor.fetchone()["n"]
            cursor.execute("""INSERT INTO social_room_queue (id,room_id,requester_id,partner_id,queue_kind,track_snapshot,karaoke_song_id,order_no,created_at,updated_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""", (s.identifier(), key, s.uid(), target, "listen" if room["kind"] == "listen" else "sing", s.encode(track), payload.karaoke_song_id, order, now, now))
        elif action == "cancel_queue":
            cursor.execute("SELECT * FROM social_room_queue WHERE id=%s AND room_id=%s AND status='queued'", (payload.queue_id, key))
            item = s.decode(cursor.fetchone())
            if item["requester_id"] != s.uid() and not manager:
                raise HTTPException(403, "只能取消自己的排队。")
            cursor.execute("UPDATE social_room_queue SET status='cancelled',updated_at=%s WHERE id=%s", (now, item["id"]))
        elif action in {"next", "finish"}:
            playback = advance(cursor, room, now)
        elif action in {"play", "pause", "seek"}:
            if not playback.get("track"):
                raise HTTPException(409, "请先从队列选择歌曲。")
            if action == "pause":
                playback["position_ms"] = position(playback, now)
                playback.update(state="paused", started_at=None)
            else:
                if action == "play" and member["permissions"].get("ready_queue_id") != playback.get("queue_id"):
                    raise HTTPException(409, "请等待音源准备完成后再同步播放。")
                if action == "seek":
                    duration = (playback["track"].get("duration") or 86400) * 1000
                    if payload.position_ms > duration:
                        raise HTTPException(422, "进度超过歌曲时长。")
                    playback["position_ms"] = payload.position_ms
                if room["kind"] != "listen":
                    from backend.modules.social import rtc
                    rtc.set_speakers(cursor, room, playback.get("singers", []))
                if action == "play":
                    playback["position_ms"] = position(playback, now)
                playback.update(state="playing", started_at=now + 2)
        elif action in {"leave", "remove", "close"}:
            target = s.uid() if action == "leave" else target
            closing = action == "close"
            if closing and member["role"] != "owner":
                raise HTTPException(403, "仅房主可以结束房间。")
            if not closing:
                cursor.execute("SELECT * FROM social_room_members WHERE room_id=%s AND user_id=%s AND status='active'", (key, target))
                victim = s.decode(cursor.fetchone())
                if victim["role"] == "owner":
                    raise HTTPException(409, "房主须先移交房间，或结束房间。")
                if room["kind"] != "listen":
                    from backend.modules.social import rtc
                    rtc.remove(room["id"], target)
                cursor.execute("UPDATE social_room_members SET status='left',left_at=%s,ban_until=%s WHERE room_id=%s AND user_id=%s", (now, now + 3600 if action == "remove" else None, key, target))
                cursor.execute("UPDATE social_conversation_members SET status='left' WHERE conversation_id=%s AND user_id=%s", (room["conversation_id"], target))
                cursor.execute("UPDATE social_room_queue SET status='cancelled' WHERE room_id=%s AND requester_id=%s AND status='queued'", (key, target))
            else:
                if room["kind"] != "listen":
                    from backend.modules.social import rtc
                    rtc.delete_room(key)
                cursor.execute("UPDATE social_rooms SET status='closed',closed_at=%s WHERE id=%s", (now, key))
                cursor.execute("UPDATE social_conversations SET status='closed' WHERE id=%s", (room["conversation_id"],))
                playback.update(state="paused", started_at=None)
        elif action in {"transfer", "admin", "controller", "mute"}:
            cursor.execute("SELECT * FROM social_room_members WHERE room_id=%s AND user_id=%s AND status='active'", (key, target))
            other = s.decode(cursor.fetchone(), "permissions")
            if target == s.uid():
                raise HTTPException(422, "请选择其他成员。")
            if action == "admin":
                if member["role"] != "owner" or other["role"] == "owner":
                    raise HTTPException(403, "仅房主可以管理管理员。")
                cursor.execute("UPDATE social_room_members SET role=%s WHERE room_id=%s AND user_id=%s", ("admin" if payload.allow else "audience", key, target))
            elif action == "transfer":
                if member["role"] != "owner":
                    raise HTTPException(403, "仅房主可以移交房间。")
                cursor.execute("UPDATE social_rooms SET owner_id=%s WHERE id=%s", (target, key))
                cursor.execute("UPDATE social_room_members SET role='audience' WHERE room_id=%s AND user_id=%s", (key, s.uid()))
                cursor.execute("UPDATE social_room_members SET role='owner' WHERE room_id=%s AND user_id=%s", (key, target))
                cursor.execute("UPDATE social_conversations SET owner_id=%s WHERE id=%s", (target, room["conversation_id"]))
            else:
                permissions = other["permissions"] | {"control" if action == "controller" else "muted": payload.allow}
                if action == "mute" and payload.allow and room["kind"] != "listen":
                    from backend.modules.social import rtc
                    rtc.permission(key, target, False)
                    permissions["can_publish"] = False
                cursor.execute("UPDATE social_room_members SET permissions=%s WHERE room_id=%s AND user_id=%s", (s.encode(permissions), key, target))
        elif action == "ready":
            permissions = member["permissions"] | {"ready_queue_id": playback.get("queue_id"), "ready_at": now}
            if payload.microphone_ready:
                permissions["microphone_queue_id"] = playback.get("queue_id")
            cursor.execute("UPDATE social_room_members SET permissions=%s WHERE room_id=%s AND user_id=%s", (s.encode(permissions), key, s.uid()))
        elif action == "react":
            s.event(cursor, "reaction", key, room["revision"], s.recipients(cursor, room["conversation_id"]), {"reaction": payload.reaction, "user_id": s.uid()})
        changed(cursor, room, playback)
        if action in {"close", "leave"}:
            return {"left": True}
    return get(key)


def position(playback, now):
    elapsed = max(0, now - (playback.get("started_at") or now)) * 1000 if playback.get("state") == "playing" else 0
    return round(playback.get("position_ms", 0) + elapsed)


def heartbeat(key, connection_id):
    with s.database() as cursor:
        room, _ = require(cursor, key)
        cursor.execute("UPDATE social_rooms SET last_active_at=%s WHERE id=%s", (time.time(), key))
    client, now = s.accounts.redis_client(), time.time()
    redis_key = s.PREFIX + "room-presence:" + key
    with client.pipeline() as pipe:
        pipe.zremrangebyscore(redis_key, "-inf", now - 45)
        pipe.zadd(redis_key, {f"{s.uid()}:{connection_id}": now})
        pipe.expire(redis_key, 90)
        pipe.execute()
    return {"server_time": now, "revision": room["revision"]}


def advance(cursor, room, now):
    if room["kind"] != "listen":
        from backend.modules.social import rtc
        rtc.clear_speakers(cursor, room)
    cursor.execute("UPDATE social_room_queue SET status='finished',updated_at=%s WHERE room_id=%s AND status='performing'", (now, room["id"]))
    while True:
        cursor.execute("SELECT * FROM social_room_queue WHERE room_id=%s AND status='queued' ORDER BY order_no LIMIT 1 FOR UPDATE", (room["id"],))
        item = cursor.fetchone()
        if not item:
            return dict(track=None, state="paused", position_ms=0, started_at=None, singers=[], queue_id=None)
        item = s.decode(item, "track_snapshot")
        singers = [x for x in (item["requester_id"], item["partner_id"]) if x]
        if room["kind"] != "listen":
            cursor.execute("SELECT user_id FROM social_room_members WHERE room_id=%s AND status='active'", (room["id"],))
            active = {x["user_id"] for x in cursor.fetchall()}
            if not singers or any(x not in active for x in singers) or room["kind"] == "duet" and len(singers) != 2:
                cursor.execute("UPDATE social_room_queue SET status='skipped',updated_at=%s WHERE id=%s", (now, item["id"]))
                continue
        cursor.execute("UPDATE social_room_queue SET status='performing',updated_at=%s WHERE id=%s", (now, item["id"]))
        return dict(track=item["track_snapshot"], position_ms=0, state="paused", started_at=None, selected_at=now,
            queue_id=item["id"], singers=singers, karaoke_song_id=item["karaoke_song_id"])


def maintain():
    now = time.time()
    with s.database() as cursor:
        cursor.execute("SELECT * FROM social_rooms WHERE status='active' ORDER BY updated_at LIMIT 100 FOR UPDATE SKIP LOCKED")
        for row in cursor.fetchall():
            room = s.decode(row, "config", "playback")
            playback = room["playback"]
            cursor.execute("SELECT status FROM users WHERE id=%s", (room["owner_id"],))
            owner_active = (cursor.fetchone() or {}).get("status") == "active"
            if not owner_active or now - room["last_active_at"] > 1800:
                if room["kind"] != "listen":
                    from backend.modules.social import rtc
                    if rtc.available():
                        rtc.delete_room(room["id"])
                cursor.execute("UPDATE social_rooms SET status='closed',closed_at=%s WHERE id=%s", (now, room["id"]))
                cursor.execute("UPDATE social_conversations SET status='closed' WHERE id=%s", (room["conversation_id"],))
                changed(cursor, room, playback | {"state": "paused", "started_at": None})
                continue
            track = playback.get("track") or {}
            ended = playback.get("state") == "playing" and track.get("duration") and position(playback, now) >= track["duration"] * 1000 + 2000
            dropout = False
            if room["kind"] != "listen" and playback.get("singers") and now - (playback.get("selected_at") or now) > 90:
                members = s.accounts.redis_client().zrangebyscore(s.PREFIX + "room-presence:" + room["id"], now - 45, "+inf")
                present = {int(x.split(":", 1)[0]) for x in members}
                dropout = any(x not in present for x in playback["singers"])
            if ended or dropout:
                changed(cursor, room, advance(cursor, room, now))
