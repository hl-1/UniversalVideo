from __future__ import annotations

import hashlib
import inspect
import json
import logging
import os
import re
import secrets
import threading
import time
import zipfile
from contextvars import ContextVar, copy_context
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import jwt
import redis
from argon2 import PasswordHasher
from argon2.exceptions import VerificationError, InvalidHashError
from dotenv import dotenv_values
from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.concurrency import run_in_threadpool

from backend.core.paths import ROOT_DIR as PROJECT_ROOT


ROOT = PROJECT_ROOT
LOCAL_CONFIG_PATH = ROOT / ".env.universal-video.local"
ACCESS_TTL = 900
SESSION_TTL = 604800
COOKIE = "videodream_refresh"
MEDIA_COOKIE = "videodream_media"
MEDIA_AUDIENCE = "videodream-media"
logger = logging.getLogger("videodream.auth")
passwords = PasswordHasher()
dummy_hash = passwords.hash(secrets.token_urlsafe(32))
router = APIRouter(prefix="/api/auth", tags=["accounts"])
cancel_callbacks = []
schema_lock = threading.RLock()


def cancel_resources(uid: int, key: str):
    for callback in cancel_callbacks:
        # User-only cleanup (for example disconnecting a platform account)
        # must not run when just one file or task is deleted.
        try:
            inspect.signature(callback).bind(uid, key)
        except TypeError:
            continue
        callback(uid, key)


@dataclass(frozen=True)
class Principal:
    id: int
    username: str
    role: str
    session_id: str = ""
    must_change_password: bool = False
principal: ContextVar[Principal | None] = ContextVar("videodream_principal", default=None)
data_owner: ContextVar[int | None] = ContextVar("videodream_data_owner", default=None)


def current() -> Principal:
    user = principal.get()
    if not user:
        raise HTTPException(401, "请先登录。")
    return user


def owner_id() -> int:
    return data_owner.get() or current().id


def admin() -> Principal:
    user = current()
    if user.role != "super_admin":
        raise HTTPException(403, "需要超级管理员权限。")
    return user


def local_environment() -> dict:
    from backend.core.configuration import local_environment as load
    return load()


def environment(name: str, default=None):
    return os.getenv(name, local_environment().get(name, default))


def signing_key() -> str:
    key = environment("VIDEODREAM_JWT_SECRET", "")
    if len(key) < 32:
        raise HTTPException(503, "认证密钥尚未配置，请运行本地账号初始化脚本。")
    return key


def redis_client():
    try:
        return redis.Redis(host=environment("REDIS_HOST", "localhost"), port=int(environment("REDIS_PORT", 6379)),
                           password=environment("REDIS_PASSWORD") or None, db=int(environment("REDIS_DATABASE", 0)),
                           decode_responses=True, socket_connect_timeout=2, socket_timeout=2)
    except (ValueError, TypeError) as exc:
        raise HTTPException(503, "Redis 配置无效。") from exc


def session_key(sid: str) -> str:
    return redis_prefix() + "session:" + sid


def redis_prefix() -> str:
    return "videodream:"


def user_sessions_key(uid: int) -> str:
    return redis_prefix() + f"user-sessions:{uid}"


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def database():
    from backend.infrastructure.database import database as connect
    return connect()


def public_user(row: dict) -> dict:
    return {key: row[key] for key in ("id", "username", "role", "status", "must_change_password", "created_at")}


def get_user(uid: int) -> dict:
    with database() as cursor:
        cursor.execute("SELECT * FROM users WHERE id=%s", (uid,))
        row = cursor.fetchone()
    if not row or row["status"] != "active":
        raise HTTPException(401, "账号已停用或不存在，请重新登录。")
    return row


def active_worker(uid: int, *, source=None):
    if uid:
        user = get_user(uid)
        if source is not None and user["role"] != "super_admin":
            from backend.infrastructure import platform_access
            if platform_access.source(source) or (isinstance(source, dict) and platform_access.metadata_for(source)):
                raise HTTPException(403, "该平台任务需要超级管理员权限，任务已停止。")


def audit(action: str, target: str, result: str = "success", *, actor: int | None = None, ip: str = ""):
    with database() as cursor:
        cursor.execute("INSERT INTO audit_logs (actor_id,action,target,result,ip,created_at) VALUES (%s,%s,%s,%s,%s,%s)",
                       (actor if actor is not None else (principal.get().id if principal.get() else None),
                        action[:80], target[:255], result[:40], ip[:64], time.time()))
AUTH_SCHEMA = [
    """CREATE TABLE IF NOT EXISTS users (id BIGINT PRIMARY KEY AUTO_INCREMENT, username VARCHAR(40) UNIQUE NOT NULL,
    password_hash VARCHAR(255) NOT NULL, role VARCHAR(16) NOT NULL DEFAULT 'user', status VARCHAR(16) NOT NULL DEFAULT 'active',
    must_change_password BOOLEAN NOT NULL DEFAULT FALSE, auth_version INT NOT NULL DEFAULT 0,
    created_at DOUBLE NOT NULL) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS system_settings (id INT PRIMARY KEY, config MEDIUMTEXT NOT NULL)
    ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS audit_logs (id BIGINT PRIMARY KEY AUTO_INCREMENT, actor_id BIGINT,
    action VARCHAR(80) NOT NULL,target VARCHAR(255) NOT NULL,result VARCHAR(40) NOT NULL,ip VARCHAR(64) NOT NULL,
    created_at DOUBLE NOT NULL, INDEX audit_actor(actor_id,created_at)) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS resources (kind VARCHAR(24) NOT NULL, resource_key VARCHAR(255) NOT NULL,
    user_id BIGINT NOT NULL, payload MEDIUMTEXT NOT NULL, created_at DOUBLE NOT NULL, updated_at DOUBLE NOT NULL,
    deleted BOOLEAN NOT NULL DEFAULT FALSE,
    PRIMARY KEY(kind,resource_key), INDEX resource_owner(user_id,kind)) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS account_migrations (id VARCHAR(40) PRIMARY KEY, completed_at DOUBLE NOT NULL,
    backup_path TEXT NOT NULL) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
]
DEFAULT_SETTINGS = {"registration_enabled": False, "music_enabled": True, "video_enabled": True,
                    "summary_enabled": True, "watermark_enabled": True,
                    "ai_base_url": "https://api.openai.com/v1", "ai_model": "gpt-4.1-mini", "ai_api_key": "",
                    "ai_vision_base_url": "", "ai_vision_model": "", "ai_vision_api_key": "",
                    "ai_asr_base_url": "", "ai_asr_model": "", "ai_asr_api_key": ""}
AI_GROUPS = {"text": ("ai", "LLM", "文字处理"),
             "vision": ("ai_vision", "LLM_VISION", "截图识别"),
             "speech": ("ai_asr", "LLM_ASR", "语音转写")}
DEFAULT_SETTINGS.update(music_agent_enabled=False, music_agent_network_enabled=True,
                        music_agent_sources=["netease", "qq", "kugou"], music_agent_decisions=6,
                        music_agent_network_requests=300, music_agent_search_rounds=5, music_agent_candidates=100, music_agent_seconds=600,
                        music_agent_tokens=150000, music_agent_model_calls=40, music_agent_concurrency=2, music_agent_api="auto")


def resolve_ai_settings(saved: dict, *, use_environment: bool = True) -> dict:
    config = DEFAULT_SETTINGS | saved
    environment_values = (local_environment() | dict(os.environ)) if use_environment else {}
    sources = {}
    for modality, (prefix, variable, _) in AI_GROUPS.items():
        fields = ("base_url", "model", "api_key")
        inherited = modality != "text" and not any(
            f"{prefix}_{field}" in saved or environment_values.get(f"{variable}_{field.upper()}") is not None
            for field in ("base_url", "api_key"))
        # Only old configurations without an independent connection inherit text settings.
        # A dedicated address with an empty key must never borrow another provider's key.
        if inherited:
            for field in ("base_url", "api_key"):
                config[f"{prefix}_{field}"] = config[f"ai_{field}"]
        from_environment = False
        for field in fields:
            value = environment_values.get(f"{variable}_{field.upper()}")
            if value is not None:
                config[f"{prefix}_{field}"] = str(value).strip()
                from_environment = True
        if from_environment or (inherited and sources["text"] == "environment"):
            sources[modality] = "environment"
        else:
            sources[modality] = "administrator" if any(f"{prefix}_{field}" in saved for field in fields) else "default"
    config["ai_sources"] = sources
    return config


def local_ai_config(modality: Literal["text", "vision", "speech"] = "text") -> dict[str, str]:
    config = resolve_ai_settings({})
    prefix = AI_GROUPS[modality][0]
    return {field: config[f"{prefix}_{field}"] for field in ("base_url", "model", "api_key")}


def migrate(cursor):
    from backend.modules.music import library
    for statement in AUTH_SCHEMA:
        cursor.execute(statement)
    cursor.execute("SELECT * FROM account_migrations WHERE id='accounts-v1'")
    if cursor.fetchone():
        return
    cursor.execute("SELECT GET_LOCK('videodream-accounts-migration', 30) AS acquired")
    if cursor.fetchone()["acquired"] != 1:
        raise HTTPException(503, "账号数据迁移正在进行。")
    try:
        cursor.execute("SELECT * FROM account_migrations WHERE id='accounts-v1'")
        if cursor.fetchone():
            return
        backup = library.backup_library(cursor)
        archive = backup.with_suffix(".files.zip")
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_STORED) as output:
            for path in library.DOWNLOAD_DIR.rglob("*"):
                if path.is_file() and not path.is_symlink():
                    output.write(path, arcname=str(path.relative_to(library.DOWNLOAD_DIR)))
        cursor.execute("INSERT IGNORE INTO users (id,username,password_hash,role,must_change_password,created_at) VALUES (1,'admin','!uninitialized','super_admin',TRUE,%s)", (time.time(),))
        for table in ("music_profile", "music_tracks", "music_favorites", "music_history", "music_playlists",
                      "music_downloads", "music_lyrics", "music_theme"):
            cursor.execute(f"SHOW COLUMNS FROM `{table}` LIKE 'user_id'")
            if not cursor.fetchone():
                cursor.execute(f"ALTER TABLE `{table}` ADD COLUMN user_id BIGINT NOT NULL DEFAULT 1, ADD INDEX {table}_owner(user_id)")
        initial = dict(DEFAULT_SETTINGS)
        ai = ROOT / "config" / "ai.json"
        if ai.is_file():
            previous = json.loads(ai.read_text(encoding="utf-8"))
            for prefix, _, _ in AI_GROUPS.values():
                legacy_prefix = prefix.removeprefix("ai_") + "_" if prefix != "ai" else ""
                for field in ("base_url", "model", "api_key"):
                    fallback = previous.get(field, initial[f"ai_{field}"]) if field != "model" else initial[f"{prefix}_model"]
                    initial[f"{prefix}_{field}"] = previous.get(legacy_prefix + field, fallback)
        cursor.execute("INSERT IGNORE INTO system_settings VALUES (1,%s)", (json.dumps(initial),))
        mapped = 0
        for path in library.DOWNLOAD_DIR.rglob("*"):
            if not path.is_file() or path.is_symlink():
                continue
            relative = path.relative_to(library.DOWNLOAD_DIR)
            kind = {"_images": "image", "_thumbnails": "thumbnail", "_summaries": "summary_file"}.get(relative.parts[0])
            if len(relative.parts) == 1:
                kind = "file"
            if kind:
                payload = json.dumps({"filename": path.name, "size": path.stat().st_size, "migrated": True})
                cursor.execute("INSERT IGNORE INTO resources (kind,resource_key,user_id,payload,created_at,updated_at) VALUES (%s,%s,1,%s,%s,%s)", (kind, path.name, payload, time.time(), time.time()))
                mapped += 1
        cursor.execute("INSERT INTO account_migrations VALUES ('accounts-v1',%s,%s)", (time.time(), str(backup)))
        cursor.execute("INSERT INTO audit_logs (actor_id,action,target,result,ip,created_at) VALUES (1,'migration','admin','success','local',%s)", (time.time(),))
        (backup.parent / (backup.stem + ".migration.json")).write_text(
            json.dumps({"owner": "admin", "database_backup": str(backup), "file_backup": str(archive),
                        "indexed_files": mapped, "unmapped": [str(p.relative_to(library.DOWNLOAD_DIR))
                            for p in library.DOWNLOAD_DIR.rglob("*") if p.is_file() and len(p.relative_to(library.DOWNLOAD_DIR).parts) > 1
                            and p.relative_to(library.DOWNLOAD_DIR).parts[0] not in {"_images", "_thumbnails", "_summaries"}]},
                       ensure_ascii=False, indent=2), encoding="utf-8")
    finally:
        cursor.execute("SELECT RELEASE_LOCK('videodream-accounts-migration')")


def settings(*, use_environment: bool = True) -> dict:
    with database() as cursor:
        cursor.execute("SELECT config FROM system_settings WHERE id=1")
        row = cursor.fetchone()
    saved = json.loads(row["config"]) if row else {}
    config = resolve_ai_settings(saved, use_environment=use_environment)
    if row and "music_agent_network_requests" not in saved:
        config["music_agent_network_requests"] = 160
    # Old stored settings remain readable; new tasks always obey the current ceiling.
    config["music_agent_tokens"] = min(150000, config["music_agent_tokens"])
    config["music_agent_decisions"] = min(6, config["music_agent_decisions"])
    config["music_agent_model_calls"] = min(40, config["music_agent_model_calls"])
    return config


def register_resource(kind: str, key: str, payload: dict | None = None, *, uid: int | None = None):
    uid = uid or (owner_id() if principal.get() else None)
    if uid is None:
        return
    from backend.infrastructure import platform_access
    if platform_access._cache.get() is not None:
        platform_access._cache.get().clear()
    payload = dict(payload or {})
    inherited = platform_access.metadata_for(payload)
    if inherited:
        payload.update(inherited)
    with database() as cursor:
        cursor.execute("SELECT id FROM users WHERE id=%s", (uid,))
        if not cursor.fetchone():
            return
        from backend.modules.video import store as video_store
        if kind in video_store.KINDS:
            stored = video_store.get(cursor, kind, key, lock=True, include_deleted=True)
            if stored and stored["deleted"]:
                return
            video_store.save(cursor, kind, key, uid, payload or {})
            return
        cursor.execute("SELECT payload FROM resources WHERE kind=%s AND resource_key=%s", (kind, key))
        previous = cursor.fetchone()
        if previous:
            saved = json.loads(previous["payload"])
            if saved.get("access_scope") == "super_admin":
                payload.update({name: saved[name] for name in ("access_scope", "source_platform", "source_task_id", "source_filename", "parse_record_id") if name in saved})
        cursor.execute("""INSERT INTO resources (kind,resource_key,user_id,payload,created_at,updated_at) VALUES (%s,%s,%s,%s,%s,%s)
            ON DUPLICATE KEY UPDATE payload=VALUES(payload),updated_at=VALUES(updated_at)""",
                       (kind, key, uid, json.dumps(payload or {}, ensure_ascii=False, default=str), time.time(), time.time()))


def resource(kind: str, key: str, *, check: bool = True) -> dict:
    with database() as cursor:
        from backend.modules.video import store as video_store
        if kind in video_store.KINDS:
            row = video_store.get(cursor, kind, key)
        else:
            cursor.execute("SELECT * FROM resources WHERE kind=%s AND resource_key=%s AND deleted=FALSE", (kind, key))
            row = cursor.fetchone()
    if not row or (check and current().role != "super_admin" and row["user_id"] != current().id):
        raise HTTPException(404, "资源不存在。")
    if check:
        from backend.infrastructure import platform_access
        platform_access.require(metadata=platform_access.resource_metadata(kind, key))
    return row


def permitted_files(kind="file") -> set[str]:
    user = current()
    with database() as cursor:
        query = "SELECT * FROM resources WHERE kind=%s AND deleted=FALSE"
        args = (kind,)
        if user.role != "super_admin":
            query += " AND user_id=%s"
            args += (user.id,)
        cursor.execute(query, args)
        rows = cursor.fetchall()
    from backend.infrastructure import platform_access
    return {row["resource_key"] for row in rows if platform_access.visible(row)}


def submit(pool, function, *args):
    context = copy_context()
    return pool.submit(context.run, function, *args)


def issue_access(user: dict, sid: str):
    now = int(time.time())
    return jwt.encode({"sub": str(user["id"]), "sid": sid, "iat": now, "exp": now + ACCESS_TTL,
                       "iss": "videodream", "aud": "videodream-web"}, signing_key(), algorithm="HS256")


def authenticate(token: str, *, audience: str = "videodream-web") -> Principal:
    try:
        claims = jwt.decode(token, signing_key(), algorithms=["HS256"], issuer="videodream", audience=audience,
                            options={"require": ["sub", "sid", "iat", "exp"]})
        session = redis_client().hgetall(session_key(claims["sid"]))
        if not session or session.get("uid") != claims["sub"]:
            raise HTTPException(401, "会话已失效，请重新登录。")
        user = get_user(int(claims["sub"]))
        if session.get("version") != str(user["auth_version"]):
            raise HTTPException(401, "会话已撤销，请重新登录。")
        return Principal(user["id"], user["username"], user["role"], claims["sid"], bool(user["must_change_password"]))
    except (jwt.InvalidTokenError, ValueError, KeyError, TypeError) as exc:
        raise HTTPException(401, "登录凭证已过期或无效。") from exc


def cookie_options() -> dict:
    return {"httponly": True, "secure": environment("VIDEODREAM_COOKIE_SECURE", "false").lower() == "true",
            "samesite": "strict", "path": "/api/auth"}


def media_cookie_options() -> dict:
    return cookie_options() | {"path": "/api/"}


def set_media_cookie(response: Response, user: dict, sid: str, ttl: int):
    # Do not extend the session, and never issue a cookie for an expired session.
    ttl = min(SESSION_TTL, ttl)
    if ttl <= 0:
        raise HTTPException(401, "会话已失效，请重新登录。")
    now = int(time.time())
    token = jwt.encode({"sub": str(user["id"]), "sid": sid, "iat": now, "exp": now + ttl,
                        "iss": "videodream", "aud": MEDIA_AUDIENCE}, signing_key(), algorithm="HS256")
    response.set_cookie(MEDIA_COOKIE, token, max_age=ttl, **media_cookie_options())


def media_read(path: str, method: str) -> bool:
    if method not in {"GET", "HEAD"}:
        return False
    return any(re.fullmatch(pattern, path) for pattern in (
        r"/api/files/[^/]+", r"/api/summary-files/[^/]+", r"/api/thumbnails/[^/]+",
        r"/api/library/images/[^/]+", r"/api/image-proxy", r"/api/music/playback/[^/]+/content",
        r"/api/previews/[^/]+/content", r"/api/watermarks/videos/[^/]+",
        r"/api/music/batches/[^/]+/archive",
    ))


def require_csrf(request: Request):
    origin = request.headers.get("origin", "")
    allowed = {value.strip() for value in environment("VIDEODREAM_ORIGINS", "http://127.0.0.1:5174,http://localhost:5174").split(",")}
    if origin not in allowed or request.headers.get("x-videodream-csrf") != "1":
        raise HTTPException(403, "请求来源校验失败。")


def revoke_all(uid: int):
    client = redis_client()
    key = user_sessions_key(uid)
    ids = client.smembers(key)
    with client.pipeline() as pipeline:
        if ids:
            pipeline.delete(*(session_key(sid) for sid in ids))
        pipeline.delete(key)
        pipeline.execute()


class Credentials(BaseModel):
    username: str = Field(pattern=r"^[A-Za-z0-9_][A-Za-z0-9_.-]{2,39}$")
    password: str = Field(min_length=8, max_length=128)


class PasswordChange(BaseModel):
    old_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=8, max_length=128)


def verify_password(encoded: str, password: str) -> bool:
    try:
        return passwords.verify(encoded if encoded.startswith("$argon2id$") else dummy_hash, password) and encoded.startswith("$argon2id$")
    except (VerificationError, InvalidHashError):
        return False


def create_user(payload: Credentials, role="user", temporary=False) -> int:
    import pymysql
    try:
        with database() as cursor:
            cursor.execute("INSERT INTO users (username,password_hash,role,must_change_password,created_at) VALUES (%s,%s,%s,%s,%s)",
                           (payload.username, passwords.hash(payload.password), role, temporary, time.time()))
            uid = cursor.lastrowid
            cursor.execute("INSERT INTO music_profile (id,nickname,user_id) VALUES (%s,%s,%s)", (uid, payload.username, uid))
        return uid
    except HTTPException as exc:
        if isinstance(exc.__cause__, pymysql.err.IntegrityError):
            raise HTTPException(409, "用户名已存在。") from exc
        raise


@router.get("/status")
def status():
    with database() as cursor:
        cursor.execute("SELECT password_hash FROM users WHERE id=1")
        user = cursor.fetchone()
    return {"initialized": bool(user and user["password_hash"].startswith("$argon2id$")),
            "registration_enabled": settings()["registration_enabled"]}


@router.post("/register", status_code=201)
def register(payload: Credentials, request: Request):
    require_csrf(request)
    if not settings()["registration_enabled"]:
        raise HTTPException(403, "公开注册尚未开启。")
    rate_limit("register:" + (request.client.host if request.client else "unknown"), 5, 3600)
    uid = create_user(payload)
    audit("register", str(uid), actor=uid)
    return {"ok": True}


def rate_limit(key: str, limit: int, period: int):
    result = redis_client().eval("local n=redis.call('INCR',KEYS[1]); if n==1 then redis.call('EXPIRE',KEYS[1],ARGV[1]) end; return n",
                                 1, redis_prefix() + "rate:" + digest(key), period)
    if result > limit:
        raise HTTPException(429, "尝试次数过多，请稍后重试。", headers={"Retry-After": str(period)})


@router.post("/login")
def login(payload: Credentials, request: Request, response: Response):
    require_csrf(request)
    signing_key()
    ip = request.client.host if request.client else "unknown"
    rate_limit("login-ip:" + ip, 50, 900)
    if int(redis_client().get(redis_prefix() + "rate:" + digest("login-user:" + payload.username.casefold())) or 0) >= 10:
        raise HTTPException(429, "尝试次数过多，请稍后重试。", headers={"Retry-After": "900"})
    with database() as cursor:
        cursor.execute("SELECT * FROM users WHERE username=%s", (payload.username,))
        user = cursor.fetchone()
        valid = verify_password(user["password_hash"] if user else dummy_hash, payload.password)
        if not user or not valid or user["status"] != "active":
            rate_limit("login-user:" + payload.username.casefold(), 10, 900)
            audit("login", payload.username, "denied", ip=ip)
            raise HTTPException(401, "用户名或密码错误，或账号已停用。")
        # Serialize session creation against disabling and password resets.
        cursor.execute("SELECT * FROM users WHERE id=%s FOR UPDATE", (user["id"],))
        latest = cursor.fetchone()
        if latest["password_hash"] != user["password_hash"] or latest["status"] != "active":
            raise HTTPException(401, "账号状态已变化，请重新登录。")
        sid, secret = secrets.token_urlsafe(24), secrets.token_urlsafe(48)
        with redis_client().pipeline() as pipeline:
            for field, value in {"uid": user["id"], "digest": digest(secret), "version": user["auth_version"]}.items():
                pipeline.hset(session_key(sid), field, value)
            pipeline.expire(session_key(sid), SESSION_TTL)
            pipeline.sadd(user_sessions_key(user['id']), sid)
            pipeline.expire(user_sessions_key(user['id']), SESSION_TTL)
            pipeline.execute()
    response.set_cookie(COOKIE, sid + "." + secret, max_age=SESSION_TTL, **cookie_options())
    set_media_cookie(response, user, sid, redis_client().ttl(session_key(sid)))
    audit("login", str(user["id"]), actor=user["id"], ip=ip)
    return {"access_token": issue_access(user, sid), "expires_in": ACCESS_TTL, "user": public_user(user)}


@router.post("/refresh")
def refresh(request: Request, response: Response):
    require_csrf(request)
    signing_key()
    cookie = request.cookies.get(COOKIE, "")
    if not re.fullmatch(r"[A-Za-z0-9_-]{32}\.[A-Za-z0-9_-]{64}", cookie):
        raise HTTPException(401, "请重新登录。")
    sid, old = cookie.split(".", 1)
    client = redis_client()
    session = client.hgetall(session_key(sid))
    if not session:
        raise HTTPException(401, "会话已失效，请重新登录。")
    user = get_user(int(session["uid"]))
    if session.get("version") != str(user["auth_version"]):
        raise HTTPException(401, "会话已撤销，请重新登录。")
    secret = secrets.token_urlsafe(48)
    ttl = client.eval("""if redis.call('HGET',KEYS[1],'digest') ~= ARGV[1] then return -1 end
        local ttl=redis.call('TTL',KEYS[1]); if ttl<=0 then return -1 end
        redis.call('HSET',KEYS[1],'digest',ARGV[2]); return ttl""", 1, session_key(sid), digest(old), digest(secret))
    if ttl <= 0:
        raise HTTPException(401, "刷新凭证已失效，请重新登录。")
    response.set_cookie(COOKIE, sid + "." + secret, max_age=ttl, **cookie_options())
    set_media_cookie(response, user, sid, ttl)
    return {"access_token": issue_access(user, sid), "expires_in": ACCESS_TTL, "user": public_user(user)}


@router.post("/logout")
def logout(request: Request, response: Response):
    require_csrf(request)
    cookie = request.cookies.get(COOKIE, "")
    if re.fullmatch(r"[A-Za-z0-9_-]{32}\.[A-Za-z0-9_-]{64}", cookie):
        sid, secret = cookie.split(".", 1)
        client = redis_client()
        session = client.hgetall(session_key(sid))
        if session and secrets.compare_digest(session.get("digest", ""), digest(secret)):
            client.delete(session_key(sid))
            client.srem(user_sessions_key(int(session['uid'])), sid)
    response.delete_cookie(COOKIE, **cookie_options())
    response.delete_cookie(MEDIA_COOKIE, **media_cookie_options())
    return {"ok": True}


@router.get("/me")
def me():
    return public_user(get_user(current().id))


@router.post("/password")
def change_password(payload: PasswordChange, response: Response):
    if payload.old_password == payload.new_password:
        raise HTTPException(422, "新密码不能与原密码相同。")
    with database() as cursor:
        cursor.execute("SELECT * FROM users WHERE id=%s FOR UPDATE", (current().id,))
        user = cursor.fetchone()
        if not verify_password(user["password_hash"], payload.old_password):
            raise HTTPException(422, "原密码不正确。")
        cursor.execute("UPDATE users SET password_hash=%s,must_change_password=FALSE,auth_version=auth_version+1 WHERE id=%s",
                       (passwords.hash(payload.new_password), user["id"]))
    revoke_all(user["id"])
    response.delete_cookie(COOKIE, **cookie_options())
    response.delete_cookie(MEDIA_COOKIE, **media_cookie_options())
    audit("password_change", str(user["id"]))
    return {"ok": True}


@router.get("/login-records")
def login_records(page: int = 1):
    page = max(1, page)
    with database() as cursor:
        cursor.execute("SELECT COUNT(*) AS n FROM audit_logs WHERE action='login' AND (actor_id=%s OR target=%s)", (current().id, current().username))
        total = cursor.fetchone()["n"]
        cursor.execute("SELECT result,ip,created_at FROM audit_logs WHERE action='login' AND (actor_id=%s OR target=%s) ORDER BY id DESC LIMIT 20 OFFSET %s",
                       (current().id, current().username, (page - 1) * 20))
        return {"items": cursor.fetchall(), "total": total}


def check_path(path: str):
    rules = [(r"/api/files/([^/]+)", "file"), (r"/api/summary-files/([^/]+)", "summary_file"),
             (r"/api/thumbnails/([^/]+)", "thumbnail"), (r"/api/library/images/([^/]+)", "image"),
             (r"/api/tasks/([^/]+)", "download"), (r"/api/summaries/([^/]+)(?:/mind-map)?", "summary"),
             (r"/api/music/batches/([^/]+)(?:/(?:cancel|retry|archive))?", "batch"),
             (r"/api/music/imports/([^/]+)(?:/(?:search|cancel|download))?", "music_import"),
             (r"/api/watermarks/tasks/([^/]+)", "watermark"),
             (r"/api/watermarks/videos/([^/]+)(?:/info)?", "file"),
             (r"/api/music/lyrics/([^/]+)", "file"),
             (r"/api/music/playback/([^/]+)/content", "playback")]
    for pattern, kind in rules:
        match = re.fullmatch(pattern, path)
        if match and path not in {"/api/music/lyrics/fetch", "/api/music/lyrics/online", "/api/music/imports/text", "/api/music/imports/screenshots"}:
            resource(kind, match[1])
            break


class AuthenticationMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        path = request.url.path
        if not path.startswith("/api/") or path in {"/api/health", "/api/auth/status", "/api/auth/login", "/api/auth/register", "/api/auth/refresh", "/api/auth/logout"} or request.method == "OPTIONS":
            try:
                return await call_next(request)
            except redis.RedisError:
                return JSONResponse({"detail": "认证服务暂不可用，请稍后重试。"}, status_code=503)
        marker = None
        request_id = secrets.token_hex(8)
        auth_source = "none"
        from backend.infrastructure import platform_access
        access_marker = platform_access._cache.set({})
        try:
            header = request.headers.get("authorization", "")
            if path.rstrip("/") == "/api/music/mcp":
                if not header.startswith("Bearer "):
                    raise HTTPException(401, "请先登录。")
                auth_source = "task_bearer"
                from backend.modules.music.agent.auth import authenticate_task
                user = await run_in_threadpool(authenticate_task, header[7:])
            elif header.startswith("Bearer "):
                auth_source = "bearer"
                user = await run_in_threadpool(authenticate, header[7:])
            elif not header and media_read(path, request.method) and request.cookies.get(MEDIA_COOKIE):
                auth_source = "media_cookie"
                user = await run_in_threadpool(authenticate, request.cookies[MEDIA_COOKIE], audience=MEDIA_AUDIENCE)
            else:
                raise HTTPException(401, "请先登录。")
            marker = principal.set(user)
            if user.must_change_password and path not in {"/api/auth/me", "/api/auth/password"}:
                raise HTTPException(403, "请先修改临时密码。")
            if path.startswith("/api/admin/"):
                admin()
            await run_in_threadpool(platform_access.require_payload, dict(request.query_params))
            if request.headers.get("content-type", "").split(";", 1)[0] == "application/json":
                try:
                    body = await request.json()
                except (ValueError, UnicodeError):
                    body = None
                await run_in_threadpool(platform_access.require_payload, body)
            await run_in_threadpool(check_path, path)
            config = await run_in_threadpool(settings)
            feature = "music" if path.startswith(("/api/music", "/api/library")) else "watermark" if path.startswith("/api/watermarks") else "summary" if path.startswith("/api/summaries") else "video" if path in {"/api/parse", "/api/download"} or path.startswith(("/api/browser-session", "/api/previews", "/api/videos")) else None
            if feature and not config[f"{feature}_enabled"]:
                raise HTTPException(403, "此功能已由管理员关闭。")
            response = await call_next(request)
            if user.role != "super_admin" and response.status_code < 400 and response.headers.get("content-type", "").startswith("application/json"):
                raw = b"".join([chunk async for chunk in response.body_iterator])
                data = json.loads(raw)
                filtered = await run_in_threadpool(platform_access.filter_response, data)
                headers = {key: value for key, value in response.headers.items() if key.lower() not in {"content-length", "content-type"}}
                response = JSONResponse(filtered, status_code=response.status_code, headers=headers, background=response.background)
            response.headers["Cache-Control"] = "private, no-store"
            response.headers["X-Content-Type-Options"] = "nosniff"
            response.headers["X-Request-ID"] = request_id
            if request.method == "DELETE":
                await run_in_threadpool(audit, "delete", path, "success" if response.status_code < 400 else "denied")
            return response
        except HTTPException as exc:
            logger.info("request_id=%s method=%s area=%s auth=%s status=%s reason=%s", request_id,
                        request.method, "/".join(path.split("/")[:3]), auth_source, exc.status_code, exc.detail)
            return JSONResponse({"detail": exc.detail}, status_code=exc.status_code,
                                headers={"Cache-Control": "private, no-store", "X-Request-ID": request_id} | (exc.headers or {}))
        except redis.RedisError:
            logger.warning("request_id=%s auth=%s status=503 reason=redis_unavailable", request_id, auth_source)
            return JSONResponse({"detail": "认证服务暂不可用，请稍后重试。"}, status_code=503,
                                headers={"Cache-Control": "private, no-store", "X-Request-ID": request_id})
        finally:
            platform_access._cache.reset(access_marker)
            if marker is not None:
                principal.reset(marker)
