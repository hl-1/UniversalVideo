from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from backend import accounts

router = APIRouter(prefix="/api/admin", tags=["administration"])


class UserCreate(accounts.Credentials):
    role: Literal["user", "super_admin"] = "user"


class UserUpdate(BaseModel):
    role: Literal["user", "super_admin"] | None = None
    status: Literal["active", "disabled"] | None = None


class BulkUpdate(UserUpdate):
    ids: list[int] = Field(min_length=1, max_length=100)


class PasswordReset(BaseModel):
    password: str = Field(min_length=8, max_length=128)


class SettingsUpdate(BaseModel):
    music_agent_enabled: bool = False
    music_agent_network_enabled: bool = True
    music_agent_sources: list[Literal["netease", "qq", "kugou"]] = Field(default_factory=lambda: ["netease", "qq", "kugou"])
    music_agent_decisions: int = Field(default=12, ge=2, le=12)
    music_agent_search_rounds: int = Field(default=3, ge=0, le=3)
    music_agent_candidates: int = Field(default=100, ge=1, le=100)
    music_agent_seconds: int = Field(default=600, ge=30, le=600)
    music_agent_tokens: int = Field(default=80000, ge=2000, le=80000)
    music_agent_concurrency: int = Field(default=2, ge=1, le=8)
    music_agent_api: Literal["auto", "chat", "responses"] = "auto"
    registration_enabled: bool
    music_enabled: bool
    video_enabled: bool
    summary_enabled: bool
    watermark_enabled: bool
    ai_base_url: str = Field(min_length=1, max_length=2048)
    ai_model: str = Field(min_length=1, max_length=100)
    ai_vision_model: str | None = Field(default=None, max_length=100)
    ai_asr_model: str | None = Field(default=None, max_length=100)
    ai_api_key: str | None = Field(default=None, max_length=4096)


@router.get("/users")
def users(q: str = Query(default="", max_length=80), status: Literal["", "active", "disabled"] = "",
          page: int = Query(default=1, ge=1), page_size: int = Query(default=20, ge=1, le=100)):
    accounts.admin()
    where = "WHERE username LIKE %s"
    args = ("%" + q + "%",)
    if status:
        where += " AND status=%s"
        args += (status,)
    with accounts.database() as cursor:
        cursor.execute("SELECT COUNT(*) AS n FROM users " + where, args)
        total = cursor.fetchone()["n"]
        cursor.execute("SELECT id,username,role,status,must_change_password,created_at FROM users " + where + " ORDER BY id LIMIT %s OFFSET %s",
                       args + (page_size, (page - 1) * page_size))
        return {"items": cursor.fetchall(), "total": total}


@router.post("/users", status_code=201)
def create_user(payload: UserCreate):
    accounts.admin()
    uid = accounts.create_user(payload, payload.role, temporary=True)
    accounts.audit("user_create", str(uid))
    return {"id": uid}


def update_users(ids: list[int], payload: UserUpdate):
    accounts.admin()
    ids = set(ids)
    with accounts.database() as cursor:
        cursor.execute("SELECT * FROM users ORDER BY id FOR UPDATE")
        rows = cursor.fetchall()
        if not ids.issubset({row["id"] for row in rows}):
            raise HTTPException(404, "账号不存在。")
        effective = [dict(row) for row in rows]
        updates = payload.model_dump(exclude_none=True, exclude={"ids"})
        for row in effective:
            if row["id"] in ids:
                row.update(updates)
        if not any(row["role"] == "super_admin" and row["status"] == "active" for row in effective):
            raise HTTPException(409, "不能停用或降级最后一个有效超级管理员。")
        for row in effective:
            if row["id"] in ids:
                cursor.execute("UPDATE users SET role=%s,status=%s,auth_version=auth_version+%s WHERE id=%s",
                               (row["role"], row["status"], int(row["status"] == "disabled"), row["id"]))
                cursor.execute("INSERT INTO audit_logs (actor_id,action,target,result,ip,created_at) VALUES (%s,'user_update',%s,'success','',%s)",
                               (accounts.current().id, f"{row['id']}:{row['role']}:{row['status']}", time.time()))
    for row in effective:
        if row["id"] in ids and row["status"] == "disabled":
            for callback in accounts.cancel_callbacks:
                callback(row["id"])
            accounts.revoke_all(row["id"])
    return {"ok": True}


@router.patch("/users/bulk")
def bulk_users(payload: BulkUpdate):
    return update_users(payload.ids, payload)


@router.patch("/users/{uid}")
def update_user(uid: int, payload: UserUpdate):
    return update_users([uid], payload)


@router.post("/users/{uid}/reset-password")
def reset_password(uid: int, payload: PasswordReset):
    accounts.admin()
    with accounts.database() as cursor:
        cursor.execute("SELECT id FROM users WHERE id=%s FOR UPDATE", (uid,))
        if not cursor.fetchone():
            raise HTTPException(404, "账号不存在。")
        cursor.execute("UPDATE users SET password_hash=%s,must_change_password=TRUE,auth_version=auth_version+1 WHERE id=%s",
                       (accounts.passwords.hash(payload.password), uid))
    accounts.audit("password_reset", str(uid))
    accounts.revoke_all(uid)
    return {"ok": True}


@router.delete("/users/{uid}")
def delete_user(uid: int):
    accounts.admin()
    with accounts.database() as cursor:
        cursor.execute("SELECT id,role,status FROM users ORDER BY id FOR UPDATE")
        rows = cursor.fetchall()
        if not any(row["id"] == uid for row in rows):
            raise HTTPException(404, "账号不存在。")
        if not any(row["id"] != uid and row["role"] == "super_admin" and row["status"] == "active" for row in rows):
            raise HTTPException(409, "不能删除最后一个有效超级管理员。")
        cursor.execute("SELECT kind,resource_key FROM resources WHERE user_id=%s", (uid,))
        files = cursor.fetchall()
        from backend import video_store
        for table in ("video_recent_links", *(item[0] for item in video_store.TABLES.values())):
            cursor.execute(f"DELETE FROM {table} WHERE user_id=%s", (uid,))
        for table in ("music_recommendation_runs", "music_track_features", "music_playlists", "music_favorites", "music_history", "music_downloads", "music_lyrics", "music_tracks", "music_theme", "music_profile", "resources"):
            cursor.execute(f"DELETE FROM {table} WHERE user_id=%s", (uid,))
        cursor.execute("DELETE FROM users WHERE id=%s", (uid,))
    for callback in accounts.cancel_callbacks:
        callback(uid)
    for row in files:
        remove_file(row["kind"], row["resource_key"])
    accounts.audit("user_delete", str(uid))
    accounts.revoke_all(uid)
    return {"ok": True}


DATA_TABLES = {"favorites": ("music_favorites", "track_id"), "history": ("music_history", "track_id"),
               "playlists": ("music_playlists", "id"), "tracks": ("music_tracks", "id"), "downloads": ("music_downloads", "id")}
ResourceKind = Literal["file", "image", "thumbnail", "summary_file", "download", "summary", "mind_map", "watermark", "batch", "music_import", "music_recommendation", "video_search", "video_parse", "video_view"]
DataKind = Literal["favorites", "history", "playlists", "tracks", "downloads", "file", "image", "thumbnail", "summary_file", "download", "summary", "mind_map", "watermark", "batch", "music_import", "music_recommendation", "video_search", "video_parse", "video_view"]


@router.get("/data")
def data(kind: DataKind = "file", user_id: int | None = None, q: str = Query(default="", max_length=100),
         page: int = Query(default=1, ge=1), page_size: int = Query(default=20, ge=1, le=100)):
    accounts.admin()
    parameters = ()
    if kind in DATA_TABLES:
        table, key = DATA_TABLES[kind]
        if kind in {"favorites", "history", "downloads"}:
            join = " JOIN music_tracks t ON t.id=d.track_id"
            label = "t.title"
        else:
            join = ""
            label = "d.name" if kind == "playlists" else "d.title"
        source = f"FROM {table} d JOIN users u ON u.id=d.user_id" + join
        where = f" WHERE {label} LIKE %s"
        parameters = ("%" + q + "%",)
        select = f"d.*,d.{key} AS resource_key,{label} AS label,u.username"
    else:
        source = "FROM resources d JOIN users u ON u.id=d.user_id"
        where = " WHERE d.kind=%s AND d.deleted=FALSE AND d.resource_key LIKE %s"
        parameters = (kind, "%" + q + "%")
        select = "d.*,d.resource_key AS label,u.username"
    if user_id is not None:
        where += " AND d.user_id=%s"
        parameters += (user_id,)
    with accounts.database() as cursor:
        cursor.execute("SELECT COUNT(*) AS n " + source + where, parameters)
        total = cursor.fetchone()["n"]
        cursor.execute("SELECT " + select + " " + source + where + " ORDER BY d.user_id,resource_key LIMIT %s OFFSET %s",
                       parameters + (page_size, (page - 1) * page_size))
        items = cursor.fetchall()
        if kind not in DATA_TABLES:
            from backend import video_store
            items = [video_store.hydrate(cursor, row) for row in items]
    from urllib.parse import quote
    for row in items:
        if "payload" in row:
            row["payload"] = json.loads(row["payload"])
        prefix = {"file": "/api/files/", "summary_file": "/api/summary-files/", "image": "/api/library/images/", "thumbnail": "/api/thumbnails/"}.get(kind)
        if prefix:
            row["download_url"] = prefix + quote(row["resource_key"], safe="")
        elif kind == "downloads":
            row["download_url"] = "/api/files/" + quote(row["filename"], safe="")
    return {"items": items, "total": total}


def remove_file(kind: str, key: str):
    directories = {"file": "", "image": "_images", "thumbnail": "_thumbnails", "summary_file": "_summaries"}
    if kind not in directories:
        return
    if Path(key).name != key or any(char in key for char in ("/", "\\", ":", "\x00")):
        raise HTTPException(422, "文件路径无效。")
    root = ROOT_DOWNLOADS() / directories[kind]
    path = root / key
    if path.resolve().parent != root.resolve():
        raise HTTPException(422, "文件路径无效。")
    path.unlink(missing_ok=True)


def ROOT_DOWNLOADS():
    from backend.library import DOWNLOAD_DIR
    return DOWNLOAD_DIR


@router.delete("/data/{kind}/{key}")
def delete_data(kind: DataKind, key: str):
    accounts.admin()
    if kind == "music_recommendation":
        from backend.music_agent.api import delete_run
        return delete_run(key, admin=True)
    if kind == "video_parse":
        from backend.video_history import delete_record
        return delete_record(key)
    if kind not in DATA_TABLES:
        row = accounts.resource(kind, key)
        for callback in accounts.cancel_callbacks:
            callback(row["user_id"], key)
    with accounts.database() as cursor:
        if kind in DATA_TABLES:
            table, field = DATA_TABLES[kind]
            cursor.execute(f"DELETE FROM {table} WHERE {field}=%s", (key,))
        else:
            from backend import video_store
            video_store.tombstone(cursor, kind, key)
            if kind == "file":
                cursor.execute("DELETE FROM music_downloads WHERE filename=%s", (key,))
    remove_file(kind, key)
    accounts.audit("data_delete", f"{kind}:{key}")
    return {"ok": True}


@router.get("/settings")
def get_settings():
    accounts.admin()
    data = accounts.settings()
    secret = data.pop("ai_api_key", "")
    return data | {"ai_api_key_configured": bool(secret), "ai_api_key_masked": "********" if secret else ""}


@router.put("/settings")
def update_settings(payload: SettingsUpdate):
    accounts.admin()
    from backend.network import public_url
    public_url(payload.ai_base_url)
    config = accounts.settings()
    config.update(payload.model_dump(exclude={"ai_api_key"}, exclude_none=True))
    if payload.ai_api_key is not None:
        config["ai_api_key"] = payload.ai_api_key
    with accounts.database() as cursor:
        cursor.execute("UPDATE system_settings SET config=%s WHERE id=1", (json.dumps(config),))
    accounts.audit("settings_update", "system")
    return get_settings()


@router.get("/logs")
def logs(action: str = Query(default="", max_length=80), user_id: int | None = None, page: int = Query(default=1, ge=1)):
    accounts.admin()
    where = " WHERE action LIKE %s"
    args = ("%" + action + "%",)
    if user_id is not None:
        where += " AND actor_id=%s"
        args += (user_id,)
    with accounts.database() as cursor:
        cursor.execute("SELECT COUNT(*) AS n FROM audit_logs" + where, args)
        total = cursor.fetchone()["n"]
        cursor.execute("SELECT * FROM audit_logs" + where + " ORDER BY id DESC LIMIT 20 OFFSET %s", args + ((page - 1) * 20,))
        return {"items": cursor.fetchall(), "total": total}


@router.get("/statistics")
def statistics():
    accounts.admin()
    with accounts.database() as cursor:
        cursor.execute("SELECT status,COUNT(*) AS count FROM users GROUP BY status")
        users = cursor.fetchall()
        cursor.execute("SELECT kind,COUNT(*) AS count FROM resources WHERE deleted=FALSE GROUP BY kind")
        resources = cursor.fetchall()
        cursor.execute("SELECT JSON_UNQUOTE(JSON_EXTRACT(payload,'$.status')) AS status,COUNT(*) AS count FROM resources WHERE deleted=FALSE AND kind IN ('download','summary','mind_map','watermark','batch','music_import') GROUP BY status")
        tasks = cursor.fetchall()
    files = [path for path in ROOT_DOWNLOADS().rglob("*") if path.is_file() and not path.is_symlink()]
    return {"users": users, "resources": resources, "tasks": tasks, "storage_bytes": sum(path.stat().st_size for path in files),
            "file_count": len(files), "mysql": "ok", "redis": "ok" if accounts.redis_client().ping() else "unavailable"}
