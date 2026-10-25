"""LiveKit grants and server-side revocation. Secrets never reach the browser."""
from __future__ import annotations

import time
import base64
import hashlib
import json
import secrets
import jwt
import requests
from fastapi import HTTPException
from backend.modules.accounts import service as accounts
from backend.modules.social import store as s


def config():
    return {"url": accounts.environment("LIVEKIT_URL", ""), "key": accounts.environment("LIVEKIT_API_KEY", ""), "secret": accounts.environment("LIVEKIT_API_SECRET", "")}


def available():
    return all(config().values())


def token(room_id, identity, *, publish=False, admin=False, metadata=None):
    settings = config()
    if not all(settings.values()):
        raise HTTPException(503, "语音服务尚未配置，请联系管理员。")
    now = int(time.time())
    grant = {"room": "vd-" + room_id, "roomAdmin": True} if admin else {
        "room": "vd-" + room_id, "roomJoin": True, "canSubscribe": True, "canPublish": bool(publish), "canPublishData": False}
    claims = {"iss": settings["key"], "sub": str(identity), "nbf": now - 5, "iat": now, "exp": now + 60, "video": grant}
    if metadata:
        claims["metadata"] = s.encode(metadata)
    return jwt.encode(claims, settings["secret"], algorithm="HS256")


def call(room_id, method, body):
    settings = config()
    if not all(settings.values()):
        raise HTTPException(503, "语音服务尚未配置。")
    url = settings["url"].replace("wss://", "https://").replace("ws://", "http://").rstrip("/")
    try:
        response = requests.post(url + "/twirp/livekit.RoomService/" + method,
            headers={"Authorization": "Bearer " + token(room_id, "server", admin=True)}, json=body, timeout=(3, 5))
        if response.status_code == 404 and method in {"RemoveParticipant", "DeleteRoom", "UpdateParticipant", "ListParticipants"}:
            return
        response.raise_for_status()
        return response.json()
    except requests.RequestException as exc:
        raise HTTPException(503, "语音服务操作失败，请重试。") from exc


def permission(room_id, user_id, publish):
    call(room_id, "UpdateParticipant", {"room": "vd-" + room_id, "identity": str(user_id),
        "permission": {"canSubscribe": True, "canPublish": publish, "canPublishData": False}})


def remove(room_id, user_id):
    call(room_id, "RemoveParticipant", {"room": "vd-" + room_id, "identity": str(user_id)})


def delete_room(room_id):
    call(room_id, "DeleteRoom", {"room": "vd-" + room_id})


def clear_speakers(cursor, room):
    cursor.execute("SELECT * FROM social_room_members WHERE room_id=%s AND status='active'", (room["id"],))
    for row in cursor.fetchall():
        row = s.decode(row, "permissions")
        if row["permissions"].get("can_publish"):
            permission(room["id"], row["user_id"], False)
            cursor.execute("UPDATE social_room_members SET permissions=%s WHERE room_id=%s AND user_id=%s", (s.encode(row["permissions"] | {"can_publish": False}), room["id"], row["user_id"]))


def set_speakers(cursor, room, singers):
    if not singers:
        raise HTTPException(409, "尚无演唱者。")
    for singer in singers:
        cursor.execute("SELECT * FROM social_room_members WHERE room_id=%s AND user_id=%s AND status='active'", (room["id"], singer))
        row = s.decode(cursor.fetchone(), "permissions")
        if row["permissions"].get("muted") or row["permissions"].get("ready_queue_id") != room["playback"].get("queue_id") or row["permissions"].get("microphone_queue_id") != room["playback"].get("queue_id"):
            raise HTTPException(409, "演唱者须完成麦克风准备，且不能处于禁言状态。")
    clear_speakers(cursor, room)
    for singer in singers:
        permission(room["id"], singer, True)
        cursor.execute("SELECT permissions FROM social_room_members WHERE room_id=%s AND user_id=%s", (room["id"], singer))
        permissions = s.decode(cursor.fetchone(), "permissions")["permissions"]
        cursor.execute("UPDATE social_room_members SET permissions=%s WHERE room_id=%s AND user_id=%s", (s.encode(permissions | {"can_publish": True}), room["id"], singer))


def credentials(room_id):
    from backend.modules.social.rooms import require
    s.enabled("social_karaoke_enabled")
    with s.database() as cursor:
        room, membership = require(cursor, room_id, lock=True)
        if room["kind"] == "listen":
            raise HTTPException(422, "一起听无需开启麦克风。")
        if room["kind"] == "duet":
            s.enabled("social_realtime_duet_enabled")
        reference = secrets.token_hex(16)
        user = accounts.get_user(s.uid())
        accounts.redis_client().setex(s.PREFIX + "rtc-session:" + reference, 300, s.encode({"uid": s.uid(),
            "sid": accounts.current().session_id, "auth_version": user["auth_version"], "room_id": room_id}))
        return {"url": config()["url"], "token": token(room_id, s.uid(), publish=False, metadata={"grant": reference}), "can_publish": membership["permissions"].get("can_publish", False)}


def reconcile_participant(room_id, participant):
    """Recheck admission and publishing, including old tokens used to rejoin."""
    identity = participant.get("identity", "")
    try:
        reference = json.loads(participant.get("metadata") or "{}").get("grant", "")
        if len(reference) != 32:
            raise ValueError()
        grant_key = s.PREFIX + "rtc-session:" + reference
        raw = accounts.redis_client().get(grant_key)
        data = json.loads(raw) if raw else {}
        if str(data.get("uid")) != identity or data.get("room_id") != room_id:
            raise ValueError()
        from backend.modules.social.realtime import check_session
        check_session(data)
        with s.database() as cursor:
            cursor.execute("""SELECT m.permissions,r.kind,r.status,r.owner_id FROM social_room_members m
                JOIN social_rooms r ON r.id=m.room_id WHERE m.room_id=%s AND m.user_id=%s AND m.status='active'""", (room_id, data["uid"]))
            row = s.decode(cursor.fetchone(), "permissions")
            s.active_user(cursor, row["owner_id"])
            settings = accounts.settings()
            if row["status"] != "active" or row["kind"] == "listen" or not settings.get("social_karaoke_enabled", True) or not settings.get("social_rooms_enabled", True) or row["kind"] == "duet" and not settings.get("social_realtime_duet_enabled", False):
                raise ValueError()
            if row["owner_id"] != data["uid"] and s.blocked(cursor, row["owner_id"], data["uid"]):
                raise ValueError()
        publish = bool(row["permissions"].get("can_publish") and not row["permissions"].get("muted"))
        permission(room_id, identity, publish)
        accounts.redis_client().expire(grant_key, 300)
    except (HTTPException, ValueError, KeyError, TypeError):
        remove(room_id, identity)


def reconcile():
    if not available():
        return
    with s.database() as cursor:
        cursor.execute("SELECT id FROM social_rooms WHERE kind<>'listen' AND status='active' LIMIT 100")
        active = cursor.fetchall()
    for room in active:
        response = call(room["id"], "ListParticipants", {"room": "vd-" + room["id"]}) or {}
        for participant in response.get("participants", []):
            reconcile_participant(room["id"], participant)


def webhook(body, authorization):
    if not available():
        raise HTTPException(503, "语音服务未配置。")
    try:
        claims = jwt.decode(authorization.removeprefix("Bearer "), config()["secret"], algorithms=["HS256"],
            issuer=config()["key"], options={"verify_aud": False, "require": ["exp", "iss", "sha256"]})
        if not secrets.compare_digest(base64.b64decode(claims["sha256"], validate=True), hashlib.sha256(body).digest()):
            raise ValueError()
        event = json.loads(body)
        room_name = event.get("room", {}).get("name", "")
        if event.get("event") == "participant_joined" and room_name.startswith("vd-"):
            room_id = room_name[3:]
            if len(room_id) != 32 or any(c not in "0123456789abcdef" for c in room_id):
                raise ValueError()
            reconcile_participant(room_id, event.get("participant") or {})
    except (jwt.InvalidTokenError, ValueError, KeyError, TypeError) as exc:
        raise HTTPException(401, "回调签名无效。") from exc
    return {"ok": True}


def revoke_user(user_id):
    if not available():
        return
    with s.database() as cursor:
        cursor.execute("SELECT room_id FROM social_room_members WHERE user_id=%s AND status='active'", (user_id,))
        rooms = cursor.fetchall()
    for room in rooms:
        remove(room["room_id"], user_id)
