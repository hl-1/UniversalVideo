"""Short-lived, single-use image challenges for public authentication."""
from __future__ import annotations

import base64
import hashlib
import hmac
import io
import json
import secrets

from fastapi import HTTPException
from PIL import Image, ImageDraw, ImageFont


TTL = 120
ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"


def make_code() -> str:
    return "".join(secrets.choice(ALPHABET) for _ in range(4))


def code_digest(secret: str, identity: str, purpose: str, code: str) -> str:
    message = f"{identity}:{purpose}:{code.strip().upper()}".encode()
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def render(code: str) -> str:
    image = Image.new("RGB", (160, 56), "#edf5ef")
    draw = ImageDraw.Draw(image)
    for _ in range(55):
        x, y = secrets.randbelow(160), secrets.randbelow(56)
        draw.ellipse((x, y, x + 1, y + 1), fill="#a7c5b5")
    font = ImageFont.load_default(size=32)
    for index, character in enumerate(code):
        tile = Image.new("RGBA", (40, 56))
        ImageDraw.Draw(tile).text((8, 7), character, font=font,
                                  fill=secrets.choice(("#204e3c", "#294c66", "#58445e")))
        tile = tile.rotate(secrets.randbelow(25) - 12, resample=Image.Resampling.BICUBIC)
        image.paste(tile, (index * 36 + 6, 0), tile)
    for _ in range(3):
        draw.line([(0, secrets.randbelow(56)), (80, secrets.randbelow(56)),
                   (159, secrets.randbelow(56))], fill="#86ab98", width=1)
    output = io.BytesIO()
    image.save(output, format="PNG")
    return "data:image/png;base64," + base64.b64encode(output.getvalue()).decode("ascii")


def issue(client, prefix: str, secret: str, purpose: str) -> dict:
    identity, code = secrets.token_urlsafe(24), make_code()
    image = render(code)
    client.set(prefix + "captcha:" + identity,
               json.dumps({"digest": code_digest(secret, identity, purpose, code), "purpose": purpose}), ex=TTL)
    return {"captcha_id": identity, "image": image, "expires_in": TTL}


def verify(client, prefix: str, secret: str, identity: str, code: str, purpose: str):
    # Read and consume together, including incorrect answers and wrong purposes.
    raw = client.eval("""local value=redis.call('GET',KEYS[1]);
        if value then redis.call('DEL',KEYS[1]) end; return value""", 1, prefix + "captcha:" + identity)
    saved = json.loads(raw) if raw else {}
    expected = code_digest(secret, identity, purpose, code)
    if saved.get("purpose") != purpose or not hmac.compare_digest(saved.get("digest", ""), expected):
        raise HTTPException(400, "验证码错误或已失效，请重新输入。")
