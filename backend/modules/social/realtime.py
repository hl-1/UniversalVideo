"""One-use tickets and an Outbox -> Redis -> WebSocket notification pipeline."""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import secrets
import time

import redis
from fastapi import HTTPException, WebSocket, WebSocketDisconnect
from backend.modules.accounts import service as accounts
from backend.modules.social import store as s
from backend.infrastructure.json_response import json_safe


logger = logging.getLogger(__name__)
HEARTBEAT_INTERVAL_SECONDS = 15
connections = {}
tasks = {}
connection_loops = {}


class TicketLogFilter(logging.Filter):
    def filter(self, record):
        message = record.getMessage()
        redacted = re.sub(r"(/api/social/[^\s\"]*[?&](?:ticket|token)=)[^&\s\"]+", r"\1[redacted]", message)
        if redacted != message:
            record.msg, record.args = redacted, ()
        return True


def ticket(request):
    s.enabled()
    s.rate("ws-ticket", 30)
    origin = request.headers.get("origin") or str(request.base_url).rstrip("/")
    allowed = {v.strip() for v in accounts.environment("VIDEODREAM_ORIGINS", "http://127.0.0.1:5174,http://localhost:5174").split(",")}
    if origin not in allowed:
        raise HTTPException(403, "连接来源不允许。")
    user = accounts.current()
    row = accounts.get_user(user.id)
    value = secrets.token_urlsafe(32)
    accounts.redis_client().setex(s.PREFIX + "ws-ticket:" + s.digest(value), 30,
        s.encode(dict(uid=user.id, sid=user.session_id, origin=origin, auth_version=row["auth_version"], expires_at=time.time() + 30)))
    return {"ticket": value, "expires_in": 30, "server_time": time.time()}


def check_session(data):
    session = accounts.redis_client().hgetall(accounts.session_key(data["sid"]))
    user = accounts.get_user(data["uid"])
    if session.get("uid") != str(data["uid"]) or session.get("version") != str(user["auth_version"]) or data["auth_version"] != user["auth_version"] or user["must_change_password"]:
        raise HTTPException(401, "会话已撤销。")
    if not accounts.settings().get("social_enabled", True):
        raise HTTPException(403, "社交功能已关闭。")


async def socket(ws: WebSocket):
    connection_id = secrets.token_hex(12)
    data = None
    try:
        value = ws.query_params.get("ticket", "")
        if not 20 <= len(value) <= 100:
            await ws.close(code=4401, reason="ticket_format")
            return
        raw = await asyncio.to_thread(accounts.redis_client().eval,
            "local value=redis.call('GET',KEYS[1]); if value then redis.call('DEL',KEYS[1]) end; return value", 1,
            s.PREFIX + "ws-ticket:" + s.digest(value))
        data = json.loads(raw) if raw else None
        if not data or data["origin"] != ws.headers.get("origin") or data["expires_at"] < time.time():
            logger.info("Social ticket invalid: exists=%s origin_match=%s", bool(data), bool(data and data["origin"] == ws.headers.get("origin")))
            await ws.close(code=4401, reason="ticket_invalid")
            return
        await asyncio.to_thread(check_session, data)
        await ws.accept()
        connections[connection_id] = (ws, data)
        event_loop = asyncio.get_running_loop()
        connection_loops[connection_id] = event_loop
        next_heartbeat = event_loop.time()
        while True:
            if event_loop.time() >= next_heartbeat:
                await asyncio.to_thread(check_session, data)
                client = accounts.redis_client()
                key = s.PREFIX + f"user-presence:{data['uid']}"
                def heartbeat():
                    with client.pipeline() as pipe:
                        pipe.zremrangebyscore(key, "-inf", time.time() - 45)
                        pipe.zadd(key, {connection_id: time.time()})
                        pipe.expire(key, 90)
                        pipe.execute()
                await asyncio.to_thread(heartbeat)
                await ws.send_json({"topic": "heartbeat", "server_time": time.time()})
                # Replies never advance or reset the server's monotonic deadline.
                next_heartbeat = event_loop.time() + HEARTBEAT_INTERVAL_SECONDS
            try:
                message = await asyncio.wait_for(ws.receive_text(), timeout=max(0, next_heartbeat - event_loop.time()))
                if len(message) > 2048:
                    await ws.close(code=1009)
                    break
            except asyncio.TimeoutError:
                pass
    except (WebSocketDisconnect, RuntimeError):
        pass
    except (HTTPException, redis.RedisError, ValueError, KeyError) as exc:
        logger.info("Social WebSocket rejected: %s", exc.detail if isinstance(exc, HTTPException) else type(exc).__name__)
        with contextlib.suppress(Exception):
            await ws.close(code=4401 if data else 1013)
    finally:
        connections.pop(connection_id, None)
        connection_loops.pop(connection_id, None)
        if data:
            with contextlib.suppress(redis.RedisError):
                await asyncio.to_thread(accounts.redis_client().zrem, s.PREFIX + f"user-presence:{data['uid']}", connection_id)


def dispatch_batch():
    """Publish committed rows while holding their locks; duplicates are harmless."""
    with s.database() as cursor:
        cursor.execute("SELECT * FROM social_outbox WHERE status='pending' AND available_at<=%s ORDER BY id LIMIT 100 FOR UPDATE SKIP LOCKED", (time.time(),))
        for row in cursor.fetchall():
            if row["topic"] == "room":
                cursor.execute("SELECT revision,playback FROM social_rooms WHERE id=%s", (row["object_key"],))
                room = cursor.fetchone()
                if room:
                    state = s.decode(room, "playback")
                    value = s.encode(state["playback"] | {"revision": state["revision"]})
                    accounts.redis_client().eval("local old=redis.call('GET',KEYS[1]); if not old or cjson.decode(old).revision<=tonumber(ARGV[1]) then redis.call('SET',KEYS[1],ARGV[2],'EX',300) end; return 1", 1,
                        s.PREFIX + "room-state:" + row["object_key"], state["revision"], value)
            accounts.redis_client().publish(s.PREFIX + "events", row["payload"])
            cursor.execute("UPDATE social_outbox SET status='delivered',attempts=attempts+1,delivered_at=%s WHERE id=%s", (time.time(), row["id"]))


async def publisher():
    while True:
        try:
            await asyncio.to_thread(dispatch_batch)
        except Exception:
            logger.warning("Social Outbox delivery deferred", exc_info=True)
        await asyncio.sleep(1)


async def subscriber():
    while True:
        pubsub = None
        try:
            pubsub = accounts.redis_client().pubsub(ignore_subscribe_messages=True)
            await asyncio.to_thread(pubsub.subscribe, s.PREFIX + "events")
            while True:
                message = await asyncio.to_thread(pubsub.get_message, timeout=1)
                if not message:
                    await asyncio.sleep(.02)
                    continue
                event = json.loads(message["data"])
                users = event.get("recipients", [])
                for key, (ws, data) in list(connections.items()):
                    if connection_loops.get(key) is not asyncio.get_running_loop() or data["uid"] not in users:
                        continue
                    try:
                        if event["topic"] == "session_revoked" and (not event.get("sid") or event["sid"] == data["sid"]):
                            await ws.close(code=4401)
                        else:
                            # Also catches account disable before a queued notification.
                            await asyncio.to_thread(check_session, data)
                            await ws.send_json(json_safe({k: v for k, v in event.items() if k != "recipients"}))
                    except Exception:
                        connections.pop(key, None)
                        with contextlib.suppress(Exception):
                            await ws.close(code=4401)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("Social event subscriber reconnecting")
            await asyncio.sleep(2)
        finally:
            if pubsub:
                await asyncio.to_thread(pubsub.close)


def revoke(user_id, sid=None):
    # Authentication already fails closed; this additionally closes connected clients.
    try:
        accounts.redis_client().publish(s.PREFIX + "events", s.encode({"topic": "session_revoked", "recipients": [user_id], "sid": sid}))
    except redis.RedisError:
        pass


def start():
    for name in ("uvicorn.error", "uvicorn.access"):
        target = logging.getLogger(name)
        if not any(isinstance(item, TicketLogFilter) for item in target.filters):
            target.addFilter(TicketLogFilter())
    event_loop = asyncio.get_running_loop()
    if event_loop in tasks:
        tasks[event_loop][0] += 1
    else:
        tasks[event_loop] = [1, [asyncio.create_task(publisher()), asyncio.create_task(subscriber())]]


async def stop():
    event_loop = asyncio.get_running_loop()
    entry = tasks.get(event_loop)
    if not entry:
        return
    entry[0] -= 1
    if entry[0]:
        return
    del tasks[event_loop]
    for task in entry[1]:
        task.cancel()
    await asyncio.gather(*entry[1], return_exceptions=True)
    for key, (ws, _) in list(connections.items()):
        if connection_loops.get(key) is not event_loop:
            continue
        with contextlib.suppress(Exception):
            await ws.close(code=1001)
        connections.pop(key, None)
        connection_loops.pop(key, None)
