"""Transactional helpers. MySQL is authoritative; Redis only wakes clients."""
from __future__ import annotations

import hashlib
import json
import secrets
import time

from fastapi import HTTPException
from backend.modules.accounts import service as accounts
from backend.infrastructure.database import database


PREFIX = "videodream:social:"


def uid():
    return accounts.current().id


def identifier():
    return secrets.token_hex(16)


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def decode(row, *fields):
    if row is None:
        raise HTTPException(404, "内容不存在或无权访问。")
    row = dict(row)
    for field in fields:
        if field in row and isinstance(row[field], str):
            row[field] = json.loads(row[field])
    return row


def enabled(feature="social_enabled"):
    config = accounts.settings()
    if not config.get("social_enabled", True) or not config.get(feature, True):
        raise HTTPException(403, "此功能已由管理员关闭。")
    return config


def active_user(cursor, user_id):
    cursor.execute("SELECT id,username,status FROM users WHERE id=%s", (user_id,))
    row = decode(cursor.fetchone())
    if row["status"] != "active":
        raise HTTPException(404, "用户不可用。")
    return row


def blocked(cursor, first, second):
    cursor.execute("""SELECT 1 FROM social_relations WHERE kind='block' AND status='active'
        AND ((user_id=%s AND target_user_id=%s) OR (user_id=%s AND target_user_id=%s)) LIMIT 1""",
        (first, second, second, first))
    return bool(cursor.fetchone())


def friends(cursor, first, second):
    cursor.execute("SELECT 1 FROM social_relations WHERE user_id=%s AND target_user_id=%s AND kind='friend' AND status='accepted'",
                   (first, second))
    accepted = bool(cursor.fetchone())
    return accepted and not blocked(cursor, first, second)


def require_friend(cursor, first, second):
    active_user(cursor, second)
    if first == second or not friends(cursor, first, second):
        raise HTTPException(403, "需要先成为好友，且双方不能处于拉黑状态。")


def member(cursor, conversation_id, user_id=None, *, lock=False, manage=False):
    user_id = uid() if user_id is None else user_id
    cursor.execute("SELECT * FROM social_conversations WHERE id=%s" + (" FOR UPDATE" if lock else ""), (conversation_id,))
    conversation = decode(cursor.fetchone(), "settings")
    cursor.execute("SELECT * FROM social_conversation_members WHERE conversation_id=%s AND user_id=%s AND status='active'",
                   (conversation_id, user_id))
    membership = decode(cursor.fetchone())
    if conversation["status"] != "active" or manage and membership["role"] not in {"owner", "admin"}:
        raise HTTPException(403, "当前没有操作权限。")
    if conversation["kind"] == "direct":
        cursor.execute("SELECT user_id FROM social_conversation_members WHERE conversation_id=%s AND user_id<>%s", (conversation_id, user_id))
        other = cursor.fetchone()
        if other and blocked(cursor, user_id, other["user_id"]):
            raise HTTPException(403, "对方暂不可联系。")
    elif conversation["kind"] == "room":
        cursor.execute("SELECT id FROM social_rooms WHERE conversation_id=%s", (conversation_id,))
        room = decode(cursor.fetchone())
        from backend.modules.social.rooms import require as room_require
        room_require(cursor, room["id"])
    return conversation, membership


def recipients(cursor, conversation_id):
    cursor.execute("SELECT user_id FROM social_conversation_members WHERE conversation_id=%s AND status='active'", (conversation_id,))
    return [r["user_id"] for r in cursor.fetchall()]


def event(cursor, topic, object_key, revision, users, payload=None):
    event_id = identifier()
    body = dict(payload or {}, event_id=event_id, topic=topic, object_key=object_key,
                revision=int(revision), recipients=list(set(users)))
    cursor.execute("""INSERT INTO social_outbox
        (topic,object_key,revision,dedupe_key,payload,available_at,created_at)
        VALUES (%s,%s,%s,%s,%s,%s,%s)""",
        (topic, object_key, revision, digest(event_id), encode(body), time.time(), time.time()))
    return event_id


def notify(cursor, user_id, event_type, object_key, payload):
    key = identifier()
    cursor.execute("""INSERT INTO social_notifications
        (id,user_id,event_type,object_key,payload,dedupe_key,created_at) VALUES (%s,%s,%s,%s,%s,%s,%s)""",
        (key, user_id, event_type, object_key, encode(payload), digest(key), time.time()))
    event(cursor, "notification", key, 0, [user_id])


def rate(action, limit=60):
    # Increment and expire atomically, including the first request.
    client = accounts.redis_client()
    key = f"{PREFIX}rate:{action}:{uid()}:{int(time.time()) // 60}"
    count = client.eval("local n=redis.call('INCR',KEYS[1]); if n==1 then redis.call('EXPIRE',KEYS[1],61) end; return n", 1, key)
    if count > limit:
        raise HTTPException(429, "操作过于频繁，请稍后重试。")


def clean_user(cursor, user_id):
    """Run before deleting an account, inside the account deletion transaction."""
    now = time.time()
    from backend.modules.social import rtc
    if rtc.available():
        cursor.execute("SELECT r.id,r.owner_id FROM social_rooms r JOIN social_room_members m ON m.room_id=r.id WHERE m.user_id=%s AND m.status='active' AND r.kind<>'listen'", (user_id,))
        for room in cursor.fetchall():
            if room["owner_id"] == user_id:
                rtc.delete_room(room["id"])
            else:
                rtc.remove(room["id"], user_id)
    cursor.execute("DELETE FROM social_rule_run_bindings WHERE run_id IN (SELECT run_id FROM music_recommendation_runs WHERE user_id=%s)", (user_id,))
    cursor.execute("UPDATE social_shares SET revoked_at=%s WHERE owner_id=%s AND revoked_at IS NULL", (now, user_id))
    cursor.execute("""UPDATE social_shares SET revoked_at=%s WHERE target_kind='performance' AND source_key IN
        (SELECT p.id FROM social_performances p WHERE p.owner_id=%s OR EXISTS
        (SELECT 1 FROM social_performance_parts n WHERE n.performance_id=p.id AND n.user_id=%s))""", (now, user_id, user_id))
    cursor.execute("UPDATE social_conversations c JOIN social_rooms r ON r.conversation_id=c.id SET c.status='closed' WHERE r.owner_id=%s", (user_id,))
    cursor.execute("UPDATE social_rooms SET status='closed',closed_at=%s,revision=revision+1 WHERE owner_id=%s", (now, user_id))
    cursor.execute("UPDATE social_conversations SET status='closed' WHERE kind='group' AND owner_id=%s", (user_id,))
    cursor.execute("UPDATE social_performances SET status='withdrawn',revision=revision+1 WHERE owner_id=%s OR id IN (SELECT performance_id FROM social_performance_parts WHERE user_id=%s)", (user_id, user_id))
    cursor.execute("UPDATE social_assets SET state='deleted',expires_at=%s WHERE owner_id=%s", (now, user_id))
    cursor.execute("UPDATE social_rules SET status='withdrawn' WHERE author_id=%s", (user_id,))
