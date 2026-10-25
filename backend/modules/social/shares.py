"""Immutable snapshots and independently revocable grants."""
from __future__ import annotations

import hashlib
import secrets
import shutil
import time
from pathlib import Path
from urllib.parse import urlparse

from fastapi import HTTPException
from pydantic import Field
from typing import Literal
from backend.modules.social import store as s, media
from backend.modules.social.models import Input
from backend.modules.music import library
from backend.modules.accounts import service as accounts
from backend.core import storage


class ShareInput(Input):
    target_kind: Literal["track", "playlist", "rule", "performance", "room"]
    source_key: str = Field(min_length=1, max_length=64)
    scope: Literal["conversation", "friends", "public", "link"] = "conversation"
    conversation_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$")
    allow_file: bool = False
    expires_hours: int = Field(default=168, ge=1, le=8760)


def safe_track(row):
    """Never carry the sender's private paths, device references or credentials."""
    data = {k: row.get(k) for k in ("title", "artist", "provider", "identifier", "webpage_url", "thumbnail", "duration")}
    if data["provider"] not in {"qq", "netease", "kugou", "video"}:
        data.update(identifier="", webpage_url="", thumbnail=None)
    elif not data.get("webpage_url", "").startswith(("https://", "http://")):
        data["webpage_url"] = ""
    if data.get("thumbnail") and not data["thumbnail"].startswith(("https://", "http://")):
        data["thumbnail"] = None
    return data


def require(cursor, key, *, token=None, conversation_id=None):
    cursor.execute("SELECT * FROM social_shares WHERE id=%s", (key,))
    row = s.decode(cursor.fetchone(), "snapshot")
    if row["revoked_at"] is not None or row["owner_id"] is None or row["expires_at"] and row["expires_at"] <= time.time():
        raise HTTPException(410, "分享已失效。")
    s.active_user(cursor, row["owner_id"])
    if row["owner_id"] == s.uid():
        return row
    if s.blocked(cursor, s.uid(), row["owner_id"]):
        raise HTTPException(404, "分享不可访问。")
    if row["scope"] == "public":
        return row
    if row["scope"] == "friends" and s.friends(cursor, s.uid(), row["owner_id"]):
        return row
    if row["scope"] == "link" and token and secrets.compare_digest(row["token_hash"] or "", s.digest(token)):
        return row
    if row["scope"] == "conversation" and row["conversation_id"]:
        s.member(cursor, row["conversation_id"])
        # A member joining later cannot recover old share cards outside their history.
        _, membership = s.member(cursor, row["conversation_id"])
        cursor.execute("SELECT 1 FROM social_messages WHERE conversation_id=%s AND msg_type='share' AND status='sent' AND JSON_UNQUOTE(JSON_EXTRACT(body,'$.share_id'))=%s AND seq>%s LIMIT 1",
            (row["conversation_id"], key, max(membership["joined_seq"], membership["hidden_before_seq"])))
        if cursor.fetchone():
            return row
    raise HTTPException(404, "分享不存在或无权访问。")


def check_forward(cursor, row, target):
    if row["scope"] == "conversation" and row["conversation_id"] != target or row["scope"] == "link":
        raise HTTPException(403, "请由分享者创建面向该会话的新分享。")
    for user_id in s.recipients(cursor, target):
        if user_id == row["owner_id"]:
            continue
        if s.blocked(cursor, user_id, row["owner_id"]) or row["scope"] == "friends" and not s.friends(cursor, user_id, row["owner_id"]):
            raise HTTPException(403, "分享范围不包含会话中的所有成员。")


def copy_private(cursor, track):
    """Copy a local file only after its owner explicitly grants file sharing."""
    if track["provider"] != "local" or not track["webpage_url"].startswith("/api/files/"):
        raise HTTPException(422, "该来源暂不支持文件分享，请分享歌曲信息。")
    filename = Path(track["webpage_url"]).name
    accounts.resource("file", filename)
    source = storage.resource_path("file", filename)
    if not source.is_file():
        raise HTTPException(404, "原文件不存在。")
    if source.stat().st_size > s.enabled().get("social_file_mb", 50) * 1024 * 1024:
        raise HTTPException(413, "文件超过分享上限。")
    key = s.identifier()
    media.ROOT.mkdir(parents=True, exist_ok=True)
    target = media.path_for(key)
    shutil.copyfile(source, target)
    try:
        checksum = hashlib.sha256(target.read_bytes()).hexdigest()
        media.register(cursor, key, s.uid(), "shared_audio", "audio/mpeg" if source.suffix.lower() == ".mp3" else "application/octet-stream", target.stat().st_size, checksum, metadata={"filename": filename})
    except Exception:
        target.unlink(missing_ok=True)
        raise
    return key


def create(payload):
    s.rate("share", 30)
    key, token, now = s.identifier(), secrets.token_urlsafe(32), time.time()
    assets = []
    with s.database() as cursor:
        if payload.scope == "conversation":
            if not payload.conversation_id:
                raise HTTPException(422, "请选择分享会话。")
            s.member(cursor, payload.conversation_id)
        if payload.target_kind in {"track", "playlist"}:
            if payload.target_kind == "playlist":
                library.require_playlist(cursor, payload.source_key)
                cursor.execute("SELECT name,description FROM music_playlists WHERE id=%s AND user_id=%s", (payload.source_key, s.uid()))
                playlist = s.decode(cursor.fetchone())
                cursor.execute("SELECT t.* FROM music_tracks t JOIN music_playlist_tracks p ON p.track_id=t.id WHERE p.playlist_id=%s AND t.user_id=%s ORDER BY p.position LIMIT 500", (payload.source_key, s.uid()))
                rows = cursor.fetchall()
                snapshot = {"title": playlist["name"], "description": playlist["description"]}
            else:
                cursor.execute("SELECT * FROM music_tracks WHERE id=%s AND user_id=%s", (payload.source_key, s.uid()))
                rows = [s.decode(cursor.fetchone())]
                snapshot = {"title": rows[0]["title"]}
            tracks = []
            for index, row in enumerate(rows):
                data = safe_track(row)
                if payload.allow_file and row["provider"] == "local":
                    asset = copy_private(cursor, row)
                    assets.append((asset, "audio", index))
                    data["asset_id"] = asset
                tracks.append(data)
            snapshot["tracks"] = tracks
        elif payload.target_kind == "rule":
            from backend.modules.social.rules import version
            selected = version(cursor, payload.source_key)
            if selected["status"] != "published" or selected["id"] != selected["published_version_id"]:
                raise HTTPException(403, "只能分享已经发布的规则版本。")
            snapshot = {"title": selected["title"], "summary": selected["summary"], "rule_id": selected["rule_id"], "version_id": selected["id"]}
        elif payload.target_kind == "room":
            from backend.modules.social.rooms import require as room_require, create_invite
            room, member = room_require(cursor, payload.source_key, lock=True)
            if member["role"] not in {"owner", "admin"}:
                raise HTTPException(403, "需要房间管理权限才能分享邀请。")
            invitation = create_invite(cursor, room)
            snapshot = {"title": room["title"], "room_id": room["id"], "kind": room["kind"], "code": invitation["code"], "invite_expires_at": invitation["expires_at"]}
        else:
            cursor.execute("SELECT * FROM social_performances WHERE id=%s AND owner_id=%s AND status='published'", (payload.source_key, s.uid()))
            performance = s.decode(cursor.fetchone())
            # Sharing a published work may not widen its jointly agreed visibility.
            if performance["visibility"] != "public":
                raise HTTPException(403, "双方须先确认公开作品，才能创建分享。")
            snapshot = {"title": "合唱作品", "performance_id": performance["id"], "asset_id": performance["mix_asset_id"]}
            assets.append((performance["mix_asset_id"], "mix", 0))
        cursor.execute("""INSERT INTO social_shares
            (id,owner_id,target_kind,source_key,snapshot,scope,conversation_id,token_hash,expires_at,created_at,updated_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (key, s.uid(), payload.target_kind, payload.source_key, s.encode(snapshot), payload.scope,
             payload.conversation_id if payload.scope == "conversation" else None, s.digest(token) if payload.scope == "link" else None, now + payload.expires_hours * 3600, now, now))
        for asset, purpose, index in assets:
            cursor.execute("INSERT INTO social_share_assets (share_id,asset_id,purpose,item_no) VALUES (%s,%s,%s,%s)", (key, asset, purpose, index))
            cursor.execute("UPDATE social_assets SET expires_at=NULL WHERE id=%s", (asset,))
    return {"id": key, "token": token if payload.scope == "link" else None, "snapshot": snapshot}


def get(key, token=None):
    with s.database() as cursor:
        row = require(cursor, key, token=token)
        row.pop("token_hash", None)
        return row


def revoke(key):
    with s.database() as cursor:
        cursor.execute("SELECT * FROM social_shares WHERE id=%s AND owner_id=%s FOR UPDATE", (key, s.uid()))
        row = s.decode(cursor.fetchone())
        cursor.execute("UPDATE social_shares SET revoked_at=%s,updated_at=%s WHERE id=%s", (time.time(), time.time(), key))
        users = s.recipients(cursor, row["conversation_id"]) if row["conversation_id"] else [s.uid()]
        s.event(cursor, "share_revoked", key, 0, users)
    return {"ok": True}


def copy_playlist(key, token=None):
    with s.database() as cursor:
        share = require(cursor, key, token=token)
        if share["target_kind"] not in {"track", "playlist"}:
            raise HTTPException(422, "该分享不能复制为歌单。")
        now, playlist_id, copied = time.time(), secrets.token_hex(12), 0
        cursor.execute("INSERT INTO music_playlists (id,name,description,user_id,created_at) VALUES (%s,%s,%s,%s,%s)",
            (playlist_id, share["snapshot"]["title"][:80], "从音乐分享复制", s.uid(), now))
        for track in share["snapshot"].get("tracks", []):
            if track["provider"] not in {"qq", "netease", "kugou", "video"} or not track["webpage_url"]:
                continue
            data = library.TrackInput.model_validate({k: v for k, v in track.items() if k != "asset_id"})
            track_id = library.save_track(cursor, data)
            cursor.execute("INSERT IGNORE INTO music_playlist_tracks (playlist_id,track_id,position,added_at) VALUES (%s,%s,%s,%s)", (playlist_id, track_id, copied, now))
            copied += 1
        return {"playlist_id": playlist_id, "copied": copied, "information_only": len(share["snapshot"].get("tracks", [])) - copied}
