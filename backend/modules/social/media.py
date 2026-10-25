"""Private media; every read checks a live message/share/performance grant."""
from __future__ import annotations

import hashlib
import io
import json
import subprocess
import time
from pathlib import Path

from fastapi import HTTPException, UploadFile
from fastapi.responses import FileResponse
from PIL import Image, UnidentifiedImageError
from backend.core import paths, storage
from backend.modules.social import store as s


ROOT = paths.DOWNLOAD_DIR / "_social"


def path_for(key):
    # Storage keys are application-generated filenames, never supplied paths.
    if len(key) != 32 or any(c not in "0123456789abcdef" for c in key):
        raise HTTPException(404, "媒体不存在。")
    return storage.contained(ROOT / key)


def probe(path):
    try:
        result = subprocess.run([str(paths.FFMPEG_DIR / "ffprobe.exe") if (paths.FFMPEG_DIR / "ffprobe.exe").is_file() else "ffprobe",
            "-v", "error", "-show_entries", "format=duration:stream=codec_type", "-of", "json", str(path)],
            capture_output=True, timeout=20, check=True)
        data = json.loads(result.stdout)
        duration = float(data.get("format", {}).get("duration") or 0)
        if not duration:
            # MediaRecorder WebM streams often have no container duration.
            packets = subprocess.run([str(paths.FFMPEG_DIR / "ffprobe.exe") if (paths.FFMPEG_DIR / "ffprobe.exe").is_file() else "ffprobe",
                "-v", "error", "-select_streams", "a:0", "-show_entries", "packet=pts_time,duration_time", "-of", "csv=p=0", str(path)],
                capture_output=True, timeout=20, check=True, text=True)
            for line in packets.stdout.splitlines():
                parts = line.split(",")
                try:
                    duration = max(duration, float(parts[0]) + (float(parts[1]) if len(parts) > 1 else 0))
                except ValueError:
                    continue
        if not 0 < duration <= 3600 or not any(x.get("codec_type") == "audio" for x in data.get("streams", [])):
            raise ValueError()
        return round(duration * 1000)
    except (ValueError, KeyError, OSError, subprocess.SubprocessError) as exc:
        raise HTTPException(422, "音频无法读取或时长不支持。") from exc


def register(cursor, key, owner, kind, mime, size, checksum, duration=None, metadata=None):
    now = time.time()
    cursor.execute("""INSERT INTO social_assets
        (id,owner_id,kind,storage_key,mime,size_bytes,duration_ms,sha256,metadata,state,expires_at,created_at,updated_at)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'ready',%s,%s,%s)""",
        (key, owner, kind, key, mime, size, duration, checksum, s.encode(metadata or {}), now + 86400, now, now))


def upload(file: UploadFile, kind):
    config = s.enabled()
    s.rate("upload", 20)
    if kind not in {"image", "voice", "file", "backing", "guide", "vocal"}:
        raise HTTPException(422, "附件类型无效。")
    limit = config.get("social_image_mb", 10) if kind == "image" else config.get("social_file_mb", 50)
    key, checksum, size = s.identifier(), hashlib.sha256(), 0
    ROOT.mkdir(parents=True, exist_ok=True)
    path = path_for(key)
    try:
        with path.open("xb") as out:
            while chunk := file.file.read(65536):
                size += len(chunk)
                if size > limit * 1024 * 1024:
                    raise HTTPException(413, f"文件不能超过 {limit} MB。")
                out.write(chunk)
                checksum.update(chunk)
        if not size:
            raise HTTPException(422, "文件为空。")
        mime, duration = "application/octet-stream", None
        if kind == "image":
            try:
                with Image.open(path) as image:
                    if image.format not in {"PNG", "JPEG", "WEBP"} or image.width * image.height > 40000000:
                        raise ValueError()
                    mime = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}[image.format]
                    image.verify()
            except (UnidentifiedImageError, ValueError, OSError, Image.DecompressionBombError) as exc:
                raise HTTPException(422, "请选择有效的 PNG、JPEG 或 WebP 图片。") from exc
        elif kind in {"voice", "backing", "guide", "vocal"}:
            duration = probe(path)
            if kind == "voice" and duration > config.get("social_voice_seconds", 120) * 1000:
                raise HTTPException(422, "语音消息超过时长上限。")
            # Browser recordings may be WebM/Opus; use the verified input's advertised audio type.
            mime = file.content_type if file.content_type and file.content_type.split(";")[0] in {"audio/webm", "audio/ogg", "audio/wav", "audio/mpeg", "audio/mp4", "audio/flac"} else "application/octet-stream"
        filename = Path((file.filename or "附件").replace("\\", "/")).name[:200]
        with s.database() as cursor:
            register(cursor, key, s.uid(), kind, mime, size, checksum.hexdigest(), duration, {"filename": filename})
        return {"id": key, "kind": kind, "duration_ms": duration, "url": f"/api/social/assets/{key}/content", "filename": filename}
    except Exception:
        path.unlink(missing_ok=True)
        raise
    finally:
        file.file.close()


def require(cursor, key, *, share_id=None, token=None):
    cursor.execute("SELECT * FROM social_assets WHERE id=%s AND state='ready'", (key,))
    row = s.decode(cursor.fetchone(), "metadata")
    if row["owner_id"] == s.uid():
        return row
    if share_id:
        from backend.modules.social import shares
        share = shares.require(cursor, share_id, token=token)
        if share["target_kind"] == "performance":
            from backend.modules.social.karaoke import require as performance_require
            performance_require(cursor, share["source_key"])
        cursor.execute("SELECT 1 FROM social_share_assets WHERE share_id=%s AND asset_id=%s", (share_id, key))
        if cursor.fetchone():
            return row
    cursor.execute("""SELECT a.conversation_id,a.seq FROM social_message_assets a
        JOIN social_messages m ON m.conversation_id=a.conversation_id AND m.seq=a.seq
        JOIN social_conversation_members n ON n.conversation_id=a.conversation_id AND n.user_id=%s
        WHERE a.asset_id=%s AND m.status='sent' AND n.status='active'
        AND m.seq>GREATEST(n.joined_seq,n.hidden_before_seq)""", (s.uid(), key))
    for grant in cursor.fetchall():
        try:
            s.member(cursor, grant["conversation_id"])
            return row
        except HTTPException:
            continue
    cursor.execute("""SELECT p.id,p.owner_id,p.visibility,p.status FROM social_performances p
        WHERE (p.mix_asset_id=%s OR EXISTS (SELECT 1 FROM social_performance_parts n WHERE n.performance_id=p.id AND n.raw_asset_id=%s))
        AND (EXISTS (SELECT 1 FROM social_performance_parts n WHERE n.performance_id=p.id AND n.user_id=%s)
        OR (p.mix_asset_id=%s AND p.status='published' AND p.visibility='public'))""", (key, key, s.uid(), key))
    performance = cursor.fetchone()
    if performance:
        from backend.modules.social.karaoke import require as performance_require
        performance_require(cursor, performance["id"])
        return row
    cursor.execute("""SELECT r.id FROM social_rooms r JOIN social_room_members m ON m.room_id=r.id
        JOIN social_karaoke_songs k ON k.id=JSON_UNQUOTE(JSON_EXTRACT(r.playback,'$.karaoke_song_id'))
        WHERE m.user_id=%s AND m.status='active' AND r.status='active' AND k.state='ready'
        AND (k.backing_asset_id=%s OR k.guide_asset_id=%s) LIMIT 1""", (s.uid(), key, key))
    for grant in cursor.fetchall():
        try:
            from backend.modules.social.rooms import require as room_require
            room_require(cursor, grant["id"])
            return row
        except HTTPException:
            continue
    cursor.execute("""SELECT 1 FROM social_karaoke_songs k WHERE (k.backing_asset_id=%s OR k.guide_asset_id=%s)
        AND k.state='ready' AND (k.creator_id=%s OR k.visibility='public' OR EXISTS
        (SELECT 1 FROM social_performances p JOIN social_performance_parts n ON n.performance_id=p.id
         WHERE p.karaoke_song_id=k.id AND n.user_id=%s)) LIMIT 1""", (key, key, s.uid(), s.uid()))
    if cursor.fetchone():
        return row
    raise HTTPException(404, "媒体不存在或授权已失效。")


def content(key, share_id=None, token=None):
    with s.database() as cursor:
        row = require(cursor, key, share_id=share_id, token=token)
    path = path_for(row["storage_key"])
    if not path.is_file():
        raise HTTPException(404, "媒体文件不存在。")
    inline = row["mime"].startswith(("image/", "audio/"))
    return FileResponse(path, media_type=row["mime"], filename=row["metadata"].get("filename", "媒体"),
        content_disposition_type="inline" if inline else "attachment",
        headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"})


def cleanup():
    with s.database() as cursor:
        cursor.execute("SELECT id,storage_key FROM social_assets WHERE expires_at<=%s AND state IN ('ready','deleted') LIMIT 100 FOR UPDATE SKIP LOCKED", (time.time(),))
        for row in cursor.fetchall():
            path_for(row["storage_key"]).unlink(missing_ok=True)
            cursor.execute("UPDATE social_assets SET state='deleted',expires_at=NULL WHERE id=%s", (row["id"],))
