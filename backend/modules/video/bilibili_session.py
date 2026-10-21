"""Per-account Bilibili QR credentials. Provider secrets never leave this module."""
from __future__ import annotations

import base64
import io
import json
import secrets
import time
from contextlib import contextmanager
from urllib.parse import urlparse

import qrcode
import requests
from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from fastapi import HTTPException

from backend.modules.accounts import service as accounts

SESSION_TTL = 30 * 86400
QR_TTL = 180
LOCK_TTL = 30
POLL_INTERVAL = 3
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Referer": "https://www.bilibili.com/",
    "Origin": "https://www.bilibili.com",
    "Accept": "application/json, text/plain, */*",
}
COMPARE_DELETE = """
if redis.call('GET', KEYS[1]) == ARGV[1] then return redis.call('DEL', KEYS[1]) end
return 0
"""
WRITE_QR = """
if redis.call('GET', KEYS[1]) ~= ARGV[1] then return 0 end
redis.call('SET', KEYS[2], ARGV[2], 'EX', ARGV[3]); return 1
"""
UPDATE_QR = """
if redis.call('GET', KEYS[1]) ~= ARGV[1] or redis.call('GET', KEYS[2]) ~= ARGV[2] then return 0 end
redis.call('SET', KEYS[2], ARGV[3], 'EX', ARGV[4]); return 1
"""
BIND = """
if redis.call('GET', KEYS[1]) ~= ARGV[1] or redis.call('GET', KEYS[2]) ~= ARGV[2] then return 0 end
redis.call('SET', KEYS[3], ARGV[3], 'EX', ARGV[4]); redis.call('DEL', KEYS[2]); return 1
"""
UPDATE_SESSION = """
if redis.call('GET', KEYS[1]) ~= ARGV[1] then return 0 end
redis.call('SET', KEYS[1], ARGV[2], 'EX', ARGV[3]); return 1
"""


def key(uid: int, kind: str) -> str:
    return accounts.redis_prefix() + f"bilibili:{kind}:{uid}"


def _cipher() -> Fernet:
    derived = HKDF(algorithm=hashes.SHA256(), length=32, salt=None,
                   info=b"videodream/bilibili/credentials/v1").derive(accounts.signing_key().encode())
    return Fernet(base64.urlsafe_b64encode(derived))


def _seal(value: dict) -> str:
    return _cipher().encrypt(json.dumps(value, ensure_ascii=False).encode()).decode()


def _read(uid: int, kind: str) -> tuple[dict | None, str | None]:
    raw = accounts.redis_client().get(key(uid, kind))
    if not raw:
        return None, None
    try:
        value = json.loads(_cipher().decrypt(raw.encode()))
        if value.get("version") == 1 and value.get("uid") == uid and value.get("expires_at", 0) > time.time():
            return value, raw
    except (InvalidToken, ValueError, TypeError, AttributeError):
        pass
    accounts.redis_client().eval(COMPARE_DELETE, 1, key(uid, kind), raw)
    return None, None


@contextmanager
def _operation(uid: int):
    nonce = secrets.token_urlsafe(24)
    client = accounts.redis_client()
    if not client.set(key(uid, "lock"), nonce, nx=True, ex=LOCK_TTL):
        raise HTTPException(409, "B站登录状态正在更新，请稍后重试。")
    try:
        yield client, nonce
    finally:
        client.eval(COMPARE_DELETE, 1, key(uid, "lock"), nonce)


def _request(url: str, **kwargs):
    try:
        response = requests.get(url, headers=HEADERS, timeout=(4, 8), allow_redirects=False, **kwargs)
        response.raise_for_status()
        payload = response.json()
        if (not isinstance(payload, dict) or payload.get("code") not in {0, -101}
                or (payload.get("code") == 0 and not isinstance(payload.get("data"), dict))):
            raise ValueError()
        return payload, response.cookies
    except (requests.RequestException, ValueError, TypeError):
        # Exceptions and upstream bodies may contain QR keys or cookies.
        raise HTTPException(502, "B站接口暂不可用或触发访问限制，请稍后重试。") from None


def _verify(cookies: list[dict]) -> dict | None:
    jar = requests.cookies.RequestsCookieJar()
    for item in cookies:
        jar.set(item["name"], item["value"], domain=item["domain"], path=item["path"])
    payload, _ = _request("https://api.bilibili.com/x/web-interface/nav", cookies=jar)
    data = payload.get("data") or {}
    if payload.get("code") == -101 or (payload.get("code") == 0 and data.get("isLogin") is False):
        return None
    if not data.get("isLogin") or not data.get("mid"):
        raise HTTPException(502, "B站登录验证响应异常，请稍后重试。")
    return {"bilibili_uid": str(data["mid"]), "nickname": str(data.get("uname") or "B站用户")[:100]}


def session(uid: int, *, verify: bool = True) -> tuple[dict | None, str | None]:
    value, raw = _read(uid, "session")
    if value and verify and time.time() - value.get("last_verified_at", 0) >= 300:
        identity = _verify(value["cookies"])
        if identity is None:
            if accounts.redis_client().eval(COMPARE_DELETE, 1, key(uid, "session"), raw):
                return None, None
            return _read(uid, "session")
        value.update(identity, last_verified_at=time.time())
        encoded = _seal(value)
        if not accounts.redis_client().eval(UPDATE_SESSION, 1, key(uid, "session"), raw, encoded, _ttl(value)):
            # A disconnect or replacement won the race; never use the old credential.
            return _read(uid, "session")
        raw = encoded
    return value, raw


def _ttl(value: dict) -> int:
    return max(1, int(value["expires_at"] - time.time()))


def status(uid: int) -> dict:
    value, _ = session(uid)
    return {"bound": bool(value), "nickname": value["nickname"] if value else "",
            "expires_at": value["expires_at"] if value else None}


def cookie_header(uid: int) -> tuple[str, str | None]:
    value, raw = session(uid)
    if not value:
        return "", None
    return "; ".join(f'{item["name"]}={item["value"]}' for item in value["cookies"]), raw


def invalidate(uid: int, raw: str | None):
    if raw:
        accounts.redis_client().eval(COMPARE_DELETE, 1, key(uid, "session"), raw)


def still_valid(uid: int, raw: str | None) -> bool:
    value, current_raw = _read(uid, "session")
    return bool(value and raw == current_raw and _verify(value["cookies"]))


def _qr_public(value: dict, *, image: str | None = None) -> dict:
    result = {"login_id": value["login_id"], "status": value["status"],
              "expires_at": value["expires_at"], "poll_after": POLL_INTERVAL}
    if image:
        result["qr_image"] = image
    return result


def create_qr(uid: int) -> dict:
    with _operation(uid) as (client, nonce):
        payload, _ = _request("https://passport.bilibili.com/x/passport-login/web/qrcode/generate",
                              params={"source": "main-fe-header"})
        data = payload.get("data") or {}
        target = str(data.get("url") or "")
        parsed = urlparse(target)
        if (not data.get("qrcode_key") or parsed.scheme != "https" or parsed.hostname != "account.bilibili.com"
                or parsed.username or parsed.password or parsed.port not in {None, 443}):
            raise HTTPException(502, "B站返回的二维码无效，请刷新重试。")
        now = time.time()
        lifetime = min(QR_TTL, max(1, int(data.get("expires_in") or QR_TTL)))
        value = {"version": 1, "uid": uid, "login_id": secrets.token_urlsafe(24),
                 "provider_key": data["qrcode_key"], "status": "waiting_scan", "created_at": now,
                 "expires_at": now + lifetime, "last_poll_at": 0}
        image = qrcode.make(target)
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        if not client.eval(WRITE_QR, 2, key(uid, "lock"), key(uid, "qr"), nonce, _seal(value), lifetime):
            raise HTTPException(409, "登录已取消，请重新获取二维码。")
        return _qr_public(value, image="data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode())


def poll_qr(uid: int, login_id: str) -> dict:
    with _operation(uid) as (client, nonce):
        value, raw = _read(uid, "qr")
        if not value or value["login_id"] != login_id:
            return {"login_id": login_id, "status": "expired", "poll_after": POLL_INTERVAL}
        if time.time() - value["last_poll_at"] < POLL_INTERVAL:
            return _qr_public(value)
        payload, jar = _request("https://passport.bilibili.com/x/passport-login/web/qrcode/poll",
                                params={"qrcode_key": value["provider_key"], "source": "main-fe-header"})
        data = payload.get("data") or {}
        code = data.get("code")
        if code == 86038:
            client.eval(COMPARE_DELETE, 1, key(uid, "qr"), raw)
            return {"login_id": login_id, "status": "expired", "poll_after": POLL_INTERVAL}
        if code == 0:
            cookies = []
            now = time.time()
            for cookie in jar:
                domain = str(cookie.domain).lower().lstrip(".")
                if (domain != "bilibili.com" or cookie.path != "/" or not cookie.value
                        or (cookie.expires is not None and cookie.expires <= now)):
                    continue
                cookies.append({"name": cookie.name, "value": cookie.value, "domain": cookie.domain,
                                "path": cookie.path, "expires": cookie.expires})
            if not any(item["name"] == "SESSDATA" for item in cookies):
                raise HTTPException(502, "B站未返回有效登录凭据，请刷新二维码重试。")
            identity = _verify(cookies)
            if not identity:
                raise HTTPException(502, "B站登录尚未生效，请刷新二维码重试。")
            expiry = min([now + SESSION_TTL] + [item["expires"] for item in cookies if item["expires"]])
            bound = {"version": 1, "uid": uid, **identity, "cookies": cookies, "created_at": now,
                     "expires_at": expiry, "last_verified_at": now}
            accounts.active_worker(uid)
            if not client.eval(BIND, 3, key(uid, "lock"), key(uid, "qr"), key(uid, "session"),
                               nonce, raw, _seal(bound), _ttl(bound)):
                return {"login_id": login_id, "status": "expired", "poll_after": POLL_INTERVAL}
            return {"login_id": login_id, "status": "confirmed", "nickname": bound["nickname"], "bound": True}
        if code not in {86101, 86090}:
            raise HTTPException(502, "B站扫码状态异常，请稍后重试或刷新二维码。")
        value.update(status="waiting_confirm" if code == 86090 else "waiting_scan", last_poll_at=time.time())
        if not client.eval(UPDATE_QR, 2, key(uid, "lock"), key(uid, "qr"), nonce, raw, _seal(value), _ttl(value)):
            return {"login_id": login_id, "status": "expired", "poll_after": POLL_INTERVAL}
        return _qr_public(value)


def cancel_qr(uid: int, login_id: str):
    for _ in range(4):
        value, raw = _read(uid, "qr")
        if not value or value["login_id"] != login_id:
            return {"cancelled": True}
        if accounts.redis_client().eval(COMPARE_DELETE, 1, key(uid, "qr"), raw):
            return {"cancelled": True}
        # A waiting-scan -> waiting-confirm update may have raced this delete.
    raise HTTPException(409, "扫码状态正在更新，请重试取消。")


def disconnect(uid: int):
    accounts.redis_client().delete(key(uid, "session"), key(uid, "qr"), key(uid, "lock"))
    return {"bound": False, "nickname": "", "expires_at": None}
