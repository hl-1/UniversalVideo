"""Leased, retryable FFmpeg mixes; each result contains one backing track."""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import secrets
import subprocess
import time

from backend.core import paths
from backend.modules.social import store as s, media


logger = logging.getLogger(__name__)
tasks = {}


def mix_filters(parts):
    filters = ["[0:a]aresample=48000,asetpts=PTS-STARTPTS[backing]"]
    for index, part in enumerate(parts, 1):
        offset = int(part["offset_ms"])
        shift = f"atrim=start={-offset / 1000},asetpts=PTS-STARTPTS" if offset < 0 else f"asetpts=PTS-STARTPTS,adelay={offset}:all=1"
        filters.append(f"[{index}:a]aresample=48000,{shift},volume={float(part['gain_db'])}dB[v{index}]")
    filters.append("[backing][v1][v2]amix=inputs=3:duration=first:dropout_transition=0:normalize=0,alimiter=limit=0.95:latency=1[out]")
    return ";".join(filters)


def process_one():
    worker_id = secrets.token_hex(12)
    with s.database() as cursor:
        cursor.execute("""UPDATE social_performances SET status=IF(attempts<3,'mix_pending','mix_failed'),worker_id=NULL,lease_until=NULL,last_error='工作进程中断，可重试'
            WHERE status='mixing' AND lease_until<%s""", (time.time(),))
        cursor.execute("SELECT * FROM social_performances WHERE status='mix_pending' AND attempts<3 ORDER BY updated_at LIMIT 1 FOR UPDATE SKIP LOCKED")
        row = cursor.fetchone()
        if not row:
            return
        cursor.execute("UPDATE social_performances SET status='mixing',worker_id=%s,lease_until=%s,attempts=attempts+1 WHERE id=%s", (worker_id, time.time() + 180, row["id"]))
        cursor.execute("SELECT * FROM social_performance_parts WHERE performance_id=%s ORDER BY slot", (row["id"],))
        parts = cursor.fetchall()
        cursor.execute("SELECT backing_asset_id,duration_ms FROM social_karaoke_songs WHERE id=%s", (row["karaoke_song_id"],))
        song = s.decode(cursor.fetchone())
    output_key = s.identifier()
    output = media.path_for(output_key)
    try:
        inputs = [song["backing_asset_id"], *[x["raw_asset_id"] for x in parts]]
        if len(inputs) != 3 or any(not x for x in inputs):
            raise ValueError("缺少独立人声或伴奏")
        with s.database() as cursor:
            for part in parts:
                s.active_user(cursor, part["user_id"])
            for key in inputs:
                cursor.execute("SELECT id FROM social_assets WHERE id=%s AND state='ready'", (key,))
                s.decode(cursor.fetchone())
        executable = paths.FFMPEG_DIR / "ffmpeg.exe"
        command = [str(executable) if executable.is_file() else "ffmpeg", "-nostdin", "-v", "error"]
        for key in inputs:
            command.extend(["-i", str(media.path_for(key))])
        command.extend(["-filter_complex", mix_filters(parts), "-map", "[out]", "-t", str(song["duration_ms"] / 1000), "-c:a", "libmp3lame", "-b:a", "192k", "-f", "mp3", "-y", str(output)])
        subprocess.run(command, capture_output=True, timeout=120, check=True)
        checksum = hashlib.sha256(output.read_bytes()).hexdigest()
        with s.database() as cursor:
            cursor.execute("SELECT * FROM social_performances WHERE id=%s FOR UPDATE", (row["id"],))
            current = s.decode(cursor.fetchone())
            if current["worker_id"] != worker_id or current["status"] != "mixing" or current["mix_input_hash"] != row["mix_input_hash"]:
                output.unlink(missing_ok=True)
                return
            for part in parts:
                s.active_user(cursor, part["user_id"])
            media.register(cursor, output_key, row["owner_id"], "mix", "audio/mpeg", output.stat().st_size, checksum, song["duration_ms"], {"filename": "合唱作品.mp3"})
            cursor.execute("UPDATE social_assets SET expires_at=NULL WHERE id=%s", (output_key,))
            cursor.execute("""UPDATE social_performances SET status='ready',mix_asset_id=%s,worker_id=NULL,lease_until=NULL,
                revision=revision+1,last_error=NULL,finished_at=%s,updated_at=%s WHERE id=%s""", (output_key, time.time(), time.time(), row["id"]))
            s.event(cursor, "performance", row["id"], current["revision"] + 1, [x["user_id"] for x in parts])
    except Exception:
        output.unlink(missing_ok=True)
        logger.warning("Mix failed for performance %s", row["id"], exc_info=True)
        with s.database() as cursor:
            cursor.execute("UPDATE social_performances SET status='mix_failed',worker_id=NULL,lease_until=NULL,last_error='混音失败，请检查录音后重试',updated_at=%s WHERE id=%s AND worker_id=%s AND status='mixing'", (time.time(), row["id"], worker_id))


async def loop():
    last_cleanup = 0
    while True:
        try:
            await asyncio.to_thread(process_one)
            if time.time() - last_cleanup > 60:
                await asyncio.to_thread(media.cleanup)
                last_cleanup = time.time()
        except Exception:
            logger.warning("Social audio worker deferred", exc_info=True)
        await asyncio.sleep(2)


async def maintenance():
    from backend.modules.social import rooms, rtc
    while True:
        for operation in (rooms.maintain, rtc.reconcile):
            try:
                await asyncio.to_thread(operation)
            except Exception:
                logger.warning("Social room maintenance deferred", exc_info=True)
        await asyncio.sleep(5)


def start():
    event_loop = asyncio.get_running_loop()
    if event_loop in tasks:
        tasks[event_loop][0] += 1
    else:
        tasks[event_loop] = [1, [asyncio.create_task(loop()), asyncio.create_task(maintenance())]]


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
