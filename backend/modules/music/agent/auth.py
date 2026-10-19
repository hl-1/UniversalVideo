from __future__ import annotations

import time
from contextlib import contextmanager

import jwt
from fastapi import HTTPException

from backend.modules.accounts import service as accounts
from backend.modules.music import library
from backend.modules.music.agent import store


READ_TOOLS = ["search_my_music", "list_music_categories", "search_online_playlists", "get_playlist_tracks",
              "search_online_tracks", "get_track_features", "validate_tracks"]


def check_session(uid, sid):
    session = accounts.redis_client().hgetall(accounts.session_key(sid))
    user = accounts.get_user(uid)
    if not session or session.get("uid") != str(uid) or session.get("version") != str(user["auth_version"]):
        raise HTTPException(401, "会话已失效，请重新登录后继续任务")
    if user["status"] != "active" or user["must_change_password"]:
        raise HTTPException(403, "账号当前不能执行场景歌单任务")
    return accounts.Principal(uid, user["username"], user["role"], sid, False)


def require_enabled():
    config = accounts.settings()
    if not config["music_enabled"] or not config["music_agent_enabled"]:
        raise HTTPException(403, "场景歌单已由管理员关闭")
    return config


def issue(row, *, save_version=None):
    check_session(row["user_id"], row["context"]["session_id"])
    now = int(time.time())
    return jwt.encode({"sub": str(row["user_id"]), "sid": row["context"]["session_id"], "run": row["run_id"],
                       "iat": now, "exp": now+300, "iss": "videodream", "aud": "videodream-music-mcp",
                       "scopes": ["save_playlist"] if save_version is not None else READ_TOOLS,
                       "worker": row["worker_id"],
                       "result_version": save_version}, accounts.signing_key(), algorithm="HS256")


def claims(token):
    try:
        value = jwt.decode(token, accounts.signing_key(), algorithms=["HS256"], audience="videodream-music-mcp",
                           issuer="videodream", options={"require": ["sub", "sid", "run", "scopes", "iat", "exp"]})
        uid = int(value["sub"])
    except (jwt.InvalidTokenError, ValueError, TypeError, KeyError) as exc:
        raise HTTPException(401, "任务凭证无效或已过期") from exc
    user = check_session(uid, value["sid"])
    require_enabled()
    with library.database() as cursor:
        row = store.get(cursor, value["run"], uid)
    if row["context"]["session_id"] != value["sid"]:
        raise HTTPException(401, "任务会话已改变")
    if "save_playlist" in value["scopes"]:
        if row["status"] not in {"finished", "partial"} or row["result_version"] != value["result_version"]:
            raise HTTPException(409, "保存授权的结果已改变")
    elif (row["status"] != "running" or row["worker_id"]!=value.get("worker")
          or not row["lease_until"] or row["lease_until"] <= time.time()):
        raise HTTPException(409, "任务当前不能执行工具")
    return value, user, row


def authenticate_task(token):
    return claims(token)[1]


@contextmanager
def tool_scope(token, tool):
    value, user, row = claims(token)
    if tool not in value["scopes"]:
        raise HTTPException(403, "任务没有此工具权限")
    marker = accounts.principal.set(user)
    owner_marker = accounts.data_owner.set(user.id)
    lease_marker = store.execution_lease.set(None if tool=="save_playlist" else (row["run_id"],row["worker_id"]))
    try:
        yield row, value
    finally:
        store.execution_lease.reset(lease_marker)
        accounts.data_owner.reset(owner_marker)
        accounts.principal.reset(marker)
