"""Fixed backing versions, two independent vocal takes and joint publication."""
from __future__ import annotations

import hashlib
import secrets
import time
from typing import Literal
from fastapi import HTTPException
from pydantic import Field
from backend.modules.social import store as s, media
from backend.modules.social.models import Input


class LyricLine(Input):
    start_ms: int = Field(ge=0)
    end_ms: int = Field(gt=0)
    text: str = Field(max_length=500)
    role: Literal["A", "B", "both"] = "both"


class SongInput(Input):
    title: str = Field(min_length=1, max_length=255)
    artist: str = Field(default="", max_length=255)
    backing_asset_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    guide_asset_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$")
    lyrics: list[LyricLine] = Field(default_factory=list, max_length=2000)
    rights_note: str = Field(min_length=1, max_length=300)
    visibility: Literal["private", "public"] = "private"


class PerformanceInput(Input):
    karaoke_song_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    slot: Literal[1, 2] = 1


class PerformanceAction(Input):
    revision: int = Field(ge=0)
    action: Literal["invite", "take", "align", "mix", "confirm", "withdraw", "swap", "release"]
    asset_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$")
    slot: Literal[1, 2] | None = None
    invitee_id: int | None = Field(default=None, gt=0)
    offset_ms: int = Field(default=0, ge=-10000, le=10000)
    gain_db: float = Field(default=0, ge=-30, le=12)
    record_consent: bool = False
    visibility: Literal["private", "public"] = "private"


def songs():
    with s.database() as cursor:
        cursor.execute("SELECT * FROM social_karaoke_songs WHERE state='ready' AND (visibility='public' OR creator_id=%s) ORDER BY created_at DESC LIMIT 100", (s.uid(),))
        return {"items": [s.decode(x, "track_snapshot", "lyrics") for x in cursor.fetchall()]}


def create_song(payload):
    s.enabled("social_karaoke_enabled")
    with s.database() as cursor:
        backing = media.require(cursor, payload.backing_asset_id)
        if backing["owner_id"] != s.uid() or backing["kind"] != "backing":
            raise HTTPException(403, "请选择自己的固定伴奏。")
        if payload.guide_asset_id:
            guide = media.require(cursor, payload.guide_asset_id)
            if guide["owner_id"] != s.uid() or guide["kind"] != "guide":
                raise HTTPException(403, "导唱文件不可用。")
        for line in payload.lyrics:
            if line.start_ms >= line.end_ms or line.end_ms > backing["duration_ms"] + 1000:
                raise HTTPException(422, "歌词时间须位于伴奏时间范围内。")
        key, now = s.identifier(), time.time()
        cursor.execute("""INSERT INTO social_karaoke_songs (id,creator_id,track_snapshot,backing_asset_id,guide_asset_id,lyrics,duration_ms,content_hash,rights_note,visibility,state,created_at,updated_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'ready',%s,%s)""", (key, s.uid(), s.encode({"title": payload.title, "artist": payload.artist}), payload.backing_asset_id, payload.guide_asset_id,
            s.encode([x.model_dump() for x in payload.lyrics]), backing["duration_ms"], backing["sha256"], payload.rights_note, payload.visibility, now, now))
        cursor.execute("UPDATE social_assets SET expires_at=NULL WHERE id IN (%s,%s)", (payload.backing_asset_id, payload.guide_asset_id))
        return {"id": key}


def require(cursor, key, lock=False):
    cursor.execute("SELECT * FROM social_performances WHERE id=%s" + (" FOR UPDATE" if lock else ""), (key,))
    row = s.decode(cursor.fetchone())
    cursor.execute("SELECT * FROM social_performance_parts WHERE performance_id=%s ORDER BY slot", (key,))
    parts = [s.decode(x, "metadata") for x in cursor.fetchall()]
    own = next((x for x in parts if x["user_id"] == s.uid()), None)
    if row["status"] == "withdrawn" or not own and not (row["status"] == "published" and row["visibility"] == "public"):
        raise HTTPException(404, "作品不可访问。")
    if row["owner_id"] and row["owner_id"] != s.uid() and s.blocked(cursor, s.uid(), row["owner_id"]):
        raise HTTPException(403, "作品不可访问。")
    return row, parts, own


def get(key):
    with s.database() as cursor:
        row, parts, own = require(cursor, key)
        cursor.execute("SELECT * FROM social_karaoke_songs WHERE id=%s", (row["karaoke_song_id"],))
        row["song"] = s.decode(cursor.fetchone(), "track_snapshot", "lyrics")
        # Private raw takes are visible only to the actual participants.
        row["parts"] = parts if own else [{k: x[k] for k in ("slot", "user_id", "status")} for x in parts]
        return row


def listing():
    with s.database() as cursor:
        cursor.execute("""SELECT DISTINCT p.*,k.track_snapshot FROM social_performances p
            JOIN social_karaoke_songs k ON k.id=p.karaoke_song_id JOIN social_performance_parts n ON n.performance_id=p.id
            WHERE (n.user_id=%s OR p.owner_id=%s) AND p.status<>'withdrawn' ORDER BY p.updated_at DESC LIMIT 100""", (s.uid(), s.uid()))
        return {"items": [s.decode(x, "track_snapshot") for x in cursor.fetchall()]}


def create(payload):
    s.enabled("social_karaoke_enabled")
    key, now = s.identifier(), time.time()
    with s.database() as cursor:
        cursor.execute("SELECT id FROM social_karaoke_songs WHERE id=%s AND state='ready' AND (creator_id=%s OR visibility='public')", (payload.karaoke_song_id, s.uid()))
        s.decode(cursor.fetchone())
        cursor.execute("""INSERT INTO social_performances (id,karaoke_song_id,owner_id,kind,created_at,updated_at)
            VALUES (%s,%s,%s,'recorded_duet',%s,%s)""", (key, payload.karaoke_song_id, s.uid(), now, now))
        for slot in (1, 2):
            cursor.execute("INSERT INTO social_performance_parts (performance_id,slot,user_id,status,metadata,created_at,updated_at) VALUES (%s,%s,%s,%s,'{}',%s,%s)", (key, slot, s.uid() if slot == payload.slot else None, "reserved" if slot == payload.slot else "empty", now, now))
    return get(key)


def invalidate(cursor, row):
    cursor.execute("UPDATE social_performances SET status='draft',visibility='private',mix_input_hash=NULL,revision=revision+1,updated_at=%s WHERE id=%s", (time.time(), row["id"]))
    cursor.execute("UPDATE social_shares SET revoked_at=%s WHERE source_key=%s AND target_kind='performance'", (time.time(), row["id"]))
    cursor.execute("UPDATE social_performance_parts SET publish_consent_hash=NULL WHERE performance_id=%s", (row["id"],))


def mix_hash(row, parts):
    return s.digest(s.encode({"song": row["karaoke_song_id"], "parts": [{k: str(x[k]) for k in ("slot", "user_id", "raw_asset_id", "offset_ms", "gain_db")} for x in parts]}))


def action(key, payload):
    s.rate("performance", 60)
    with s.database() as cursor:
        row, parts, own = require(cursor, key, True)
        if not own or row["revision"] != payload.revision:
            raise HTTPException(409, "作品状态已更新或当前没有演唱位置。")
        if own["reserved_until"] and own["reserved_until"] <= time.time() and not own["raw_asset_id"]:
            raise HTTPException(409, "演唱位置预约已过期，请重新领取。")
        if row["status"] == "mixing":
            raise HTTPException(409, "混音处理中，请稍后再试。")
        now = time.time()
        if payload.action == "invite":
            if row["owner_id"] != s.uid():
                raise HTTPException(403, "仅发起者可以创建邀请。")
            slot = payload.slot or (2 if own["slot"] == 1 else 1)
            target = next(x for x in parts if x["slot"] == slot)
            if target["user_id"] and target["reserved_until"] and target["reserved_until"] > now or target["raw_asset_id"] or target["user_id"] == s.uid():
                raise HTTPException(409, "该位置已被占用。")
            if payload.invitee_id:
                s.require_friend(cursor, s.uid(), payload.invitee_id)
            token, invite = secrets.token_urlsafe(24), s.identifier()
            cursor.execute("""INSERT INTO social_duet_invites (id,performance_id,slot,inviter_id,invitee_id,token_hash,expires_at,created_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""", (invite, key, slot, s.uid(), payload.invitee_id, s.digest(token), now + 7 * 86400, now))
            if payload.invitee_id:
                s.notify(cursor, payload.invitee_id, "duet_invite", invite, {"performance_id": key, "code": token})
            return {"code": token, "performance_id": key}
        if payload.action == "take":
            if not payload.record_consent:
                raise HTTPException(422, "请先同意保存本次录音。")
            asset = media.require(cursor, payload.asset_id)
            if asset["owner_id"] != s.uid() or asset["kind"] != "vocal":
                raise HTTPException(403, "只能使用自己的独立人声录音。")
            cursor.execute("SELECT duration_ms FROM social_karaoke_songs WHERE id=%s", (row["karaoke_song_id"],))
            duration = cursor.fetchone()["duration_ms"]
            if asset["duration_ms"] > duration + 30000:
                raise HTTPException(422, "录音时长与伴奏不匹配。")
            cursor.execute("""UPDATE social_performance_parts SET raw_asset_id=%s,status='recorded',record_consent_at=%s,
                reserved_until=NULL,offset_ms=%s,gain_db=%s,updated_at=%s WHERE performance_id=%s AND user_id=%s""", (payload.asset_id, now, payload.offset_ms, payload.gain_db, now, key, s.uid()))
            cursor.execute("UPDATE social_assets SET expires_at=NULL WHERE id=%s", (payload.asset_id,))
            if own["raw_asset_id"] and own["raw_asset_id"] != payload.asset_id:
                cursor.execute("UPDATE social_assets SET expires_at=%s WHERE id=%s", (now + 86400, own["raw_asset_id"]))
            invalidate(cursor, row)
        elif payload.action == "align":
            cursor.execute("UPDATE social_performance_parts SET offset_ms=%s,gain_db=%s,updated_at=%s WHERE performance_id=%s AND user_id=%s", (payload.offset_ms, payload.gain_db, now, key, s.uid()))
            invalidate(cursor, row)
        elif payload.action == "swap":
            if any(x["raw_asset_id"] for x in parts):
                raise HTTPException(409, "录音后不能交换角色，请重新发起作品。")
            cursor.execute("UPDATE social_performance_parts SET slot=slot+2 WHERE performance_id=%s", (key,))
            cursor.execute("UPDATE social_performance_parts SET slot=5-slot WHERE performance_id=%s", (key,))
            invalidate(cursor, row)
        elif payload.action == "mix":
            if any(not x["raw_asset_id"] or not x["record_consent_at"] for x in parts):
                raise HTTPException(409, "两人均录制并同意保存后才能混音。")
            checksum = mix_hash(row, parts)
            cursor.execute("UPDATE social_performances SET status='mix_pending',mix_input_hash=%s,attempts=0,worker_id=NULL,lease_until=NULL,last_error=NULL,revision=revision+1,updated_at=%s WHERE id=%s", (checksum, now, key))
            cursor.execute("UPDATE social_performance_parts SET publish_consent_hash=NULL WHERE performance_id=%s", (key,))
        elif payload.action == "confirm":
            if row["status"] not in {"ready", "published"} or not row["mix_asset_id"] or row["mix_input_hash"] != mix_hash(row, parts):
                raise HTTPException(409, "请先生成当前录音版本的成品并试听。")
            consent = s.digest(row["mix_input_hash"] + ":" + payload.visibility)
            cursor.execute("UPDATE social_performance_parts SET publish_consent_hash=%s,metadata=%s,updated_at=%s WHERE performance_id=%s AND user_id=%s", (consent, s.encode(own["metadata"] | {"publish_visibility": payload.visibility}), now, key, s.uid()))
            cursor.execute("SELECT publish_consent_hash FROM social_performance_parts WHERE performance_id=%s", (key,))
            both = all(x["publish_consent_hash"] == consent for x in cursor.fetchall())
            cursor.execute("UPDATE social_performances SET status=%s,visibility=%s,revision=revision+1,updated_at=%s WHERE id=%s", ("published" if both else "ready", payload.visibility if both else "private", now, key))
        elif payload.action == "withdraw":
            cursor.execute("UPDATE social_performances SET status='withdrawn',visibility='private',revision=revision+1,updated_at=%s WHERE id=%s", (now, key))
            cursor.execute("UPDATE social_shares SET revoked_at=%s WHERE source_key=%s AND target_kind='performance'", (now, key))
        elif payload.action == "release":
            if own["raw_asset_id"] or row["owner_id"] == s.uid():
                raise HTTPException(409, "已录音或发起者的位置不可释放。")
            cursor.execute("UPDATE social_performance_parts SET user_id=NULL,status='empty',reserved_until=NULL WHERE performance_id=%s AND user_id=%s", (key, s.uid()))
            invalidate(cursor, row)
            return {"released": True}
        s.event(cursor, "performance", key, row["revision"] + 1, [x["user_id"] for x in parts if x["user_id"]])
    return get(key)


def claim(code):
    s.rate("duet-claim", 20)
    now = time.time()
    with s.database() as cursor:
        cursor.execute("SELECT performance_id FROM social_duet_invites WHERE token_hash=%s", (s.digest(code),))
        found = s.decode(cursor.fetchone())
        cursor.execute("SELECT * FROM social_performances WHERE id=%s FOR UPDATE", (found["performance_id"],))
        row = s.decode(cursor.fetchone())
        cursor.execute("SELECT * FROM social_duet_invites WHERE token_hash=%s FOR UPDATE", (s.digest(code),))
        invite = s.decode(cursor.fetchone())
        if row["status"] in {"withdrawn", "mixing", "mix_pending"} or invite["status"] != "pending" or invite["expires_at"] <= now or invite["inviter_id"] is None or invite["invitee_id"] not in {None, s.uid()} or s.blocked(cursor, s.uid(), invite["inviter_id"]):
            raise HTTPException(403, "补唱邀请已失效。")
        cursor.execute("SELECT * FROM social_performance_parts WHERE performance_id=%s", (row["id"],))
        parts = cursor.fetchall()
        target = next(x for x in parts if x["slot"] == invite["slot"])
        if any(x["user_id"] == s.uid() for x in parts) or target["raw_asset_id"] or target["user_id"] and (not target["reserved_until"] or target["reserved_until"] > now):
            raise HTTPException(409, "演唱位置已被占用。")
        cursor.execute("UPDATE social_performance_parts SET user_id=%s,status='reserved',reserved_until=%s,updated_at=%s WHERE performance_id=%s AND slot=%s", (s.uid(), now + 1800, now, row["id"], invite["slot"]))
        cursor.execute("UPDATE social_duet_invites SET status='claimed',claimed_by=%s,claimed_at=%s WHERE id=%s", (s.uid(), now, invite["id"]))
        invalidate(cursor, row)
        s.notify(cursor, row["owner_id"], "duet_claimed", row["id"], {"performance_id": row["id"], "user_id": s.uid()})
    return get(row["id"])
