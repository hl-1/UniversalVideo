from __future__ import annotations

import time
from typing import Literal
from fastapi import APIRouter, HTTPException, Query, Request, UploadFile, WebSocket
from pydantic import Field
from backend.modules.social import store as s, chat, shares, rooms, playlists, rules, karaoke, media, realtime, rtc
from backend.modules.social.models import Input, RelationInput, ConversationInput, ConversationUpdate, MemberInput, MessageInput, CursorInput, UserSettings
from backend.modules.accounts import service as accounts


router = APIRouter(prefix="/api/social", tags=["social"])


@router.post("/rtc-webhook")
async def rtc_webhook(request: Request):
    import asyncio
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > 1024 * 1024:
            raise HTTPException(413, "回调数据过大。")
    return await asyncio.to_thread(rtc.webhook, bytes(body), request.headers.get("authorization", ""))


@router.get("/capabilities")
def capabilities():
    config = accounts.settings()
    return {"enabled": config.get("social_enabled", True), "rooms": config.get("social_rooms_enabled", True),
        "rules": config.get("social_rules_enabled", True), "karaoke": config.get("social_karaoke_enabled", True),
        "rtc": rtc.available(), "realtime_duet": config.get("social_realtime_duet_enabled", False) and rtc.available(),
        "server_time": time.time(), "recall_seconds": config.get("social_recall_seconds", 120)}


@router.post("/ws-ticket")
def ws_ticket(request: Request):
    return realtime.ticket(request)


@router.websocket("/ws")
async def websocket(ws: WebSocket):
    await realtime.socket(ws)


@router.get("/settings")
def get_settings():
    s.enabled()
    return chat.settings()


@router.put("/settings")
def update_settings(payload: UserSettings):
    s.enabled()
    return chat.settings(payload)


@router.get("/users")
def search_users(q: str = Query(min_length=2, max_length=40)):
    s.enabled()
    s.rate("search-users", 60)
    return chat.users(q)


@router.get("/relations")
def relations():
    s.enabled()
    return chat.relations()


@router.post("/relations/{target}")
def relation(target: int, payload: RelationInput):
    s.enabled()
    return chat.relation(target, payload)


@router.get("/conversations")
def conversations():
    s.enabled()
    return chat.conversations()


@router.post("/conversations", status_code=201)
def create_conversation(payload: ConversationInput):
    s.enabled()
    return chat.create(payload)


@router.get("/conversations/{key}")
def conversation(key: str):
    s.enabled()
    return chat.detail(key)


@router.patch("/conversations/{key}")
def update_conversation(key: str, payload: ConversationUpdate):
    s.enabled()
    return chat.update(key, payload)


@router.post("/conversations/{key}/members")
def conversation_member(key: str, payload: MemberInput):
    s.enabled()
    return chat.change_member(key, payload)


@router.get("/conversations/{key}/messages")
def messages(key: str, after: int = Query(default=0, ge=0), before: int = Query(default=0, ge=0), q: str = Query(default="", max_length=100), limit: int = Query(default=50, ge=1, le=100)):
    s.enabled()
    return chat.messages(key, after, before, q, limit)


@router.post("/conversations/{key}/messages", status_code=201)
def send_message(key: str, payload: MessageInput):
    s.enabled()
    return chat.send(key, payload)


@router.post("/conversations/{key}/messages/{seq}/recall")
def recall_message(key: str, seq: int):
    return chat.recall(key, seq)


@router.put("/conversations/{key}/read")
def read_messages(key: str, payload: CursorInput):
    s.enabled()
    return chat.cursor_update(key, payload.seq)


@router.post("/conversations/{key}/typing")
def typing(key: str):
    s.enabled()
    s.rate("typing", 30)
    with s.database() as cursor:
        s.member(cursor, key)
        accounts.redis_client().setex(s.PREFIX + f"typing:{key}:{s.uid()}", 5, "1")
        s.event(cursor, "typing", key, 0, s.recipients(cursor, key), {"user_id": s.uid()})
    return {"ok": True}


@router.get("/presence/{user_id}")
def presence(user_id: int):
    s.enabled()
    with s.database() as cursor:
        s.require_friend(cursor, s.uid(), user_id)
        preferences = chat.user_settings(cursor, user_id)
        online = False
        listening = None
        if preferences["online_visibility"] == "friends":
            online = bool(accounts.redis_client().zcount(s.PREFIX + f"user-presence:{user_id}", time.time() - 45, "+inf"))
        if preferences["listening_visibility"] == "friends":
            cursor.execute("""SELECT r.title,r.playback FROM social_rooms r JOIN social_room_members m ON m.room_id=r.id
                WHERE m.user_id=%s AND m.status='active' AND r.status='active' AND r.visibility='friends' ORDER BY r.updated_at DESC LIMIT 1""", (user_id,))
            row = cursor.fetchone()
            if row:
                listening = s.decode(row, "playback")
        return {"online": online, "listening": listening}


@router.get("/notifications")
def notifications():
    s.enabled()
    with s.database() as cursor:
        cursor.execute("SELECT * FROM social_notifications WHERE user_id=%s ORDER BY created_at DESC LIMIT 100", (s.uid(),))
        return {"items": [s.decode(x, "payload") for x in cursor.fetchall()]}


@router.put("/notifications/{key}/read")
def read_notification(key: str):
    s.enabled()
    with s.database() as cursor:
        cursor.execute("UPDATE social_notifications SET read_at=COALESCE(read_at,%s) WHERE id=%s AND user_id=%s", (time.time(), key, s.uid()))
    return {"ok": True}


@router.post("/assets", status_code=201)
def upload_asset(file: UploadFile, kind: str = Query(default="file")):
    return media.upload(file, kind)


@router.api_route("/assets/{key}/content", methods=["GET", "HEAD"])
def asset_content(key: str, share_id: str | None = None, token: str | None = None):
    s.enabled()
    return media.content(key, share_id, token)


@router.post("/shares", status_code=201)
def create_share(payload: shares.ShareInput):
    s.enabled()
    return shares.create(payload)


@router.get("/shares/{key}")
def get_share(key: str, token: str | None = None):
    s.enabled()
    return shares.get(key, token)


@router.delete("/shares/{key}")
def revoke_share(key: str):
    s.enabled()
    return shares.revoke(key)


@router.post("/shares/{key}/copy")
def copy_share(key: str, token: str | None = None):
    s.enabled()
    return shares.copy_playlist(key, token)


@router.get("/rooms")
def list_rooms():
    s.enabled("social_rooms_enabled")
    return rooms.listing()


@router.post("/rooms", status_code=201)
def create_room(payload: rooms.RoomInput):
    return rooms.create(payload)


@router.post("/rooms/join")
def join_code(payload: rooms.JoinInput):
    return rooms.join(None, payload.code)


@router.get("/rooms/{key}")
def get_room(key: str):
    s.enabled("social_rooms_enabled")
    return rooms.get(key)


@router.post("/rooms/{key}/join")
def join_room(key: str, payload: rooms.JoinInput):
    return rooms.join(key, payload.code)


@router.post("/rooms/{key}/invites")
def invite_room(key: str, target: int | None = Query(default=None, gt=0)):
    s.enabled("social_rooms_enabled")
    return rooms.invite(key, target)


@router.post("/rooms/{key}/control")
def control_room(key: str, payload: rooms.ControlInput):
    s.enabled("social_rooms_enabled")
    return rooms.control(key, payload)


@router.post("/rooms/{key}/rtc")
def rtc_credentials(key: str):
    return rtc.credentials(key)


class RoomHeartbeat(Input):
    connection_id: str = Field(pattern=r"^[a-f0-9]{32}$")


@router.post("/rooms/{key}/heartbeat")
def room_heartbeat(key: str, payload: RoomHeartbeat):
    s.enabled("social_rooms_enabled")
    return rooms.heartbeat(key, payload.connection_id)


@router.get("/rooms/{key}/song")
def room_song(key: str):
    s.enabled("social_karaoke_enabled")
    with s.database() as cursor:
        room, _ = rooms.require(cursor, key)
        cursor.execute("SELECT * FROM social_karaoke_songs WHERE id=%s AND state='ready'", (room["playback"].get("karaoke_song_id"),))
        return s.decode(cursor.fetchone(), "track_snapshot", "lyrics")


@router.get("/playlists")
def list_playlists():
    s.enabled()
    return playlists.listing()


@router.post("/playlists", status_code=201)
def create_playlist(payload: playlists.PlaylistInput):
    s.enabled()
    return playlists.create(payload)


@router.get("/playlists/{key}")
def get_playlist(key: str):
    s.enabled()
    return playlists.get(key)


@router.patch("/playlists/{key}")
def edit_playlist(key: str, payload: playlists.EditInput):
    s.enabled()
    return playlists.edit(key, payload)


@router.get("/rules")
def list_rules(mine: bool = False):
    s.enabled("social_rules_enabled")
    return rules.listing(mine)


@router.post("/rules/trial")
def trial_rule(payload: rules.Config):
    s.enabled("social_rules_enabled")
    return rules.trial(payload)


@router.get("/rules/{key}")
def get_rule(key: str):
    s.enabled("social_rules_enabled")
    return rules.get(key)


@router.post("/rules", status_code=201)
def create_rule(payload: rules.RuleInput):
    return rules.save(payload)


@router.post("/rules/{key}/versions", status_code=201)
def update_rule(key: str, payload: rules.RuleInput):
    return rules.save(payload, key)


@router.post("/rules/{key}/actions")
def rule_action(key: str, payload: rules.RuleAction):
    s.enabled("social_rules_enabled")
    return rules.action(key, payload)


@router.get("/karaoke/songs")
def karaoke_songs():
    s.enabled("social_karaoke_enabled")
    return karaoke.songs()


@router.post("/karaoke/songs", status_code=201)
def create_karaoke_song(payload: karaoke.SongInput):
    return karaoke.create_song(payload)


@router.get("/performances")
def performances():
    s.enabled("social_karaoke_enabled")
    return karaoke.listing()


@router.post("/performances", status_code=201)
def create_performance(payload: karaoke.PerformanceInput):
    return karaoke.create(payload)


@router.post("/performances/claim")
def claim_performance(payload: rooms.JoinInput):
    s.enabled("social_karaoke_enabled")
    return karaoke.claim(payload.code)


@router.get("/performances/{key}")
def get_performance(key: str):
    s.enabled("social_karaoke_enabled")
    return karaoke.get(key)


@router.post("/performances/{key}/actions")
def performance_action(key: str, payload: karaoke.PerformanceAction):
    s.enabled("social_karaoke_enabled")
    return karaoke.action(key, payload)


class ReportInput(Input):
    target_kind: Literal["message", "share", "rule", "performance", "room"]
    target_key: str = Field(min_length=1, max_length=96)
    reason: str = Field(min_length=1, max_length=500)


@router.post("/reports", status_code=201)
def report(payload: ReportInput):
    s.enabled()
    s.rate("report", 10)
    with s.database() as cursor:
        evidence = {}
        if payload.target_kind == "message":
            try:
                conversation, seq = payload.target_key.split(":", 1)
                seq = int(seq)
            except ValueError:
                raise HTTPException(422, "消息标识无效。")
            _, member = s.member(cursor, conversation)
            cursor.execute("SELECT * FROM social_messages WHERE conversation_id=%s AND seq=%s AND seq>%s", (conversation, seq, max(member["joined_seq"], member["hidden_before_seq"])))
            evidence = s.decode(cursor.fetchone(), "body")
        elif payload.target_kind == "share":
            evidence = shares.require(cursor, payload.target_key)["snapshot"]
        elif payload.target_kind == "rule":
            evidence = {"title": rules.get(payload.target_key)["title"]}
        elif payload.target_kind == "performance":
            row, _, _ = karaoke.require(cursor, payload.target_key)
            evidence = {"performance_id": row["id"], "revision": row["revision"]}
        else:
            row, _ = rooms.require(cursor, payload.target_key)
            evidence = {"title": row["title"]}
        key = s.identifier()
        cursor.execute("INSERT INTO social_reports (id,reporter_id,target_kind,target_key,reason,evidence,created_at) VALUES (%s,%s,%s,%s,%s,%s,%s)", (key, s.uid(), payload.target_kind, payload.target_key, payload.reason, s.encode(evidence), time.time()))
    return {"id": key}
