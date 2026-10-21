from __future__ import annotations

import json
import contextvars
import mimetypes
import re
import secrets
import shutil
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import quote, unquote, urlparse

from fastapi import HTTPException

from backend.core import paths, storage
from backend.infrastructure import network, platform_access, runtime, video_network, video_proxy
from backend.modules.accounts import service as accounts
from backend.modules.music import library, sources as music
from backend.modules.video import cache, parsing, parse_cache, urls
from backend.infrastructure.ytdlp import YoutubeDL

CHUNK = 256 * 1024
TERMINAL = {"completed", "cancelled", "failed", "handed_off", "library_saved"}
maintenance_stop = threading.Event()
maintenance_thread = None


def key(kind, uid, identity):
    return accounts.redis_prefix() + f"download:{kind}:{uid}:{identity}"


def filename(title, ext):
    title = re.sub(r'[\x00-\x1f\\/:*?"<>|]+', " ", title).strip(" .")[:120] or "download"
    return title + "." + re.sub(r"[^a-zA-Z0-9]", "", ext)


def state(plan):
    return accounts.redis_client().hgetall(key("state", plan["uid"], plan["id"]))


def update(plan, **changes):
    changes["updated_at"] = time.time()
    client = accounts.redis_client()
    state_key = key("state", plan["uid"], plan["id"])
    if changes.get("status") in TERMINAL:
        # Persist the terminal history before expiring temporary state. The
        # short advisory lock serializes completion/cancellation across workers.
        from backend.core import blobs
        with blobs.mutex(f"delivery:{plan['uid']}:{plan['id']}"):
            previous = state(plan)
            if previous.get("status") not in TERMINAL:
                try:
                    row = accounts.resource("delivery", plan["id"], check=False)
                    persisted = json.loads(row["payload"])
                    if row["user_id"] == plan["uid"] and persisted.get("status") in TERMINAL:
                        previous |= persisted
                except HTTPException as exc:
                    if exc.status_code != 404:
                        raise
            terminal = previous if previous.get("status") in TERMINAL else previous | changes
            snapshot = terminal | {name: plan.get(name) for name in ("destination", "mode", "filename", "media_identity", "task_id")}
            snapshot.update(source_url=plan.get("page") or plan.get("source_url"), parse_record_id=plan.get("request", {}).get("parse_record_id") or plan.get("parse_record_id"))
            accounts.register_resource("delivery", plan["id"], snapshot, uid=plan["uid"])
            if previous.get("status") in TERMINAL:
                client.hset(state_key, mapping={name: str(previous[name]) for name in ("status", "phase", "completion_source", "transferred_bytes") if name in previous})
                client.expire(state_key, 3600)
                return False
            client.hset(state_key, mapping={name: json.dumps(value, ensure_ascii=False) if isinstance(value, (list, dict)) else str(value) for name, value in changes.items()})
            client.expire(state_key, 3600)
            return True
    # Terminal states cannot be overwritten by late workers or stale clients.
    result = client.eval("""local s=redis.call('HGET',KEYS[1],'status');
        if s=='completed' or s=='cancelled' or s=='failed' or s=='handed_off' or s=='library_saved' then return 0 end;
        for i=1,#ARGV-1,2 do redis.call('HSET',KEYS[1],ARGV[i],ARGV[i+1]) end;
        redis.call('EXPIRE',KEYS[1],ARGV[#ARGV]); return 1""", 1, state_key,
        *[part for name, value in (changes | {"updated_at": time.time()}).items()
          for part in (name, json.dumps(value, ensure_ascii=False) if isinstance(value, (list, dict)) else str(value))],
        3600 if changes.get("status") in TERMINAL else 86400)
    return bool(result)


def check(plan):
    client = accounts.redis_client()
    if plan.get("sid") and not client.exists(accounts.session_key(plan["sid"])):
        raise HTTPException(401, "登录已失效，下载已停止。")
    status = client.hget(key("state", plan["uid"], plan["id"]), "status")
    if plan.get("parent_id") and client.hget(key("state", plan["uid"], plan["parent_id"]), "status") in {"cancelled", "failed"}:
        raise HTTPException(409, "批量下载已停止。")
    if status in {"cancelled", "failed"}:
        raise HTTPException(409, "下载已取消或失败，请重新下载。")
    accounts.active_worker(plan["uid"], source={"url": plan.get("page")})
    record = plan.get("request", {}).get("parse_record_id")
    if record:
        accounts.resource("video_parse", record)


def save_plan(plan):
    plan.update(id=secrets.token_hex(12), uid=accounts.owner_id(), sid=accounts.current().session_id, version=1)
    ttl = cache.media_ttl([plan.get("url", "")], ceiling=600)
    if ttl <= 0:
        raise HTTPException(410, "媒体链接已过期，请重新解析。")
    plan["expires_at"] = time.time() + ttl
    client = accounts.redis_client()
    with client.pipeline() as pipeline:
        pipeline.set(key("plan", plan["uid"], plan["id"]), json.dumps(plan, ensure_ascii=False), ex=ttl)
        pipeline.hset(key("state", plan["uid"], plan["id"]), mapping={
            "version": 1, "status": "ready", "phase": "ready", "transferred_bytes": 0,
            "updated_at": time.time(), "completion_source": "none"})
        pipeline.expire(key("state", plan["uid"], plan["id"]), 86400)
        pipeline.execute()
    accounts.register_resource("delivery", plan["id"], {
        "delivery_version": 1, "destination": plan["destination"], "delivery_mode": plan["mode"],
        "status": "ready", "suggested_filename": plan["filename"], "media_identity": plan.get("media_identity"), "task_id": plan.get("task_id"),
        "source_url": plan.get("page"), "parse_record_id": plan.get("request", {}).get("parse_record_id")})
    return plan


def load(identity, *, check_state=True):
    raw = accounts.redis_client().get(key("plan", accounts.owner_id(), identity))
    if not raw:
        raise HTTPException(410, "下载链接已过期，请重新点击下载。")
    plan = json.loads(raw)
    if plan["uid"] != accounts.owner_id() or plan["sid"] != accounts.current().session_id:
        raise HTTPException(404, "下载不存在。")
    accounts.resource("delivery", identity)
    if check_state:
        check(plan)
    return plan


def resolve(payload, *, origin="", allow_direct=True):
    page = urls.normalize_url(str(payload.url))
    network.public_url(page)
    platform_access.require(page)
    from backend.modules.video import history
    history.validate_relation(payload.parse_record_id, page)
    base = {"destination": payload.destination, "page": page, "request": payload.model_dump(mode="json"),
            "filename": filename(payload.title or "download", "mp4" if payload.media_type == "video" else payload.audio_format if payload.audio_format != "original" else "m4a")}
    if payload.destination == "library" or payload.compatibility:
        from backend.modules.video.api import download_endpoint
        from backend.modules.video.schemas import DownloadRequest
        request = payload.model_dump(exclude={"title"})
        result = download_endpoint(DownloadRequest.model_validate(request))
        return save_plan(base | {"mode": "task", "task_id": result.task_id})
    options = parsing._ydl_base_options(page)
    selected = payload.format_id or "bestvideo+bestaudio/best"
    media, headers, title, ext = None, options.get("http_headers", {}), payload.title or "download", "mp4"
    if selected.startswith("music:"):
        track = music.get_track(selected.partition(":")[2], page)
        base["media_identity"] = f"{track.provider}:{track.identifier}"
        known = library.downloaded_status([library.TrackInput.model_validate(track.model_dump())])[0]
        if known["file_available"]:
            source = storage.resource_path("file", known["filename"])
            if payload.audio_format == "original" or source.suffix.lstrip(".") == payload.audio_format:
                return save_plan(base | {"mode": "file", "filename": storage.download_filename(source.name), "source_filename": source.name})
        media, options, _ = music.resolve(track, options)
        headers, title = options.get("http_headers", {}), track.title
        ext = payload.audio_format if payload.audio_format != "original" else (Path(urlparse(media).path).suffix.lstrip(".") or "m4a")
        if ext not in {"mp3", "m4a", "aac", "flac", "opus", "ogg", "wav"}:
            ext = "m4a"
        base.update(expected_duration=track.duration, audio=True, output_format=ext)
    else:
        cached = parse_cache._get_sniff_cache(page)
        if selected.startswith(("sniff:", "sniffpair:")):
            if not cached or not any(item.format_id == selected for item in cached.formats):
                raise HTTPException(422, "下载格式与当前账号的解析结果不匹配，请重新解析。")
            title = cached.title
            if selected.startswith("sniff:"):
                media = unquote(selected.partition(":")[2])
                ext = Path(urlparse(media).path).suffix.lstrip(".") or "mp4"
                headers = headers | {"Referer": page}
        elif selected == "douyin-public":
            from backend.modules.video import douyin
            _, item = douyin._fetch_douyin_item(page)
            media, title = douyin._douyin_media_url(item), item.get("desc") or title
        else:
            with video_proxy.scope(page), YoutubeDL(options | {"format": selected, "skip_download": True}) as ydl:
                info = ydl.extract_info(page, download=False)
                if info.get("has_drm") or info.get("is_live"):
                    raise HTTPException(422, "暂不支持直播或 DRM 资源。")
                members = info.get("requested_formats") or [info]
                title = info.get("title") or title
                if len(members) == 1 and members[0].get("acodec") != "none":
                    member = members[0]
                    media, ext = member.get("url"), member.get("ext") or ext
                    headers = headers | member.get("http_headers", {})
                    if media:
                        cookie = ydl.cookiejar.get_cookie_header(media)
                        if cookie:
                            headers["Cookie"] = cookie
        if payload.media_type == "audio":
            media = None  # Compatibility pipeline extracts/validates an audio track.
    if not media or urlparse(media).path.lower().endswith((".m3u8", ".mpd")):
        from backend.modules.video.api import download_endpoint
        from backend.modules.video.schemas import DownloadRequest
        request = payload.model_dump(exclude={"title"})
        if request["audio_format"] == "original":
            request["audio_format"] = "m4a"
        result = download_endpoint(DownloadRequest.model_validate(request))
        return save_plan(base | {"mode": "task", "task_id": result.task_id, "filename": filename(title, "mp4" if payload.media_type == "video" else request["audio_format"])})
    video_network.validate(media)
    base.update(url=media, headers=headers, filename=filename(title, ext), mode="relay")
    # A public CORS response alone is insufficient when cookies or an overseas
    # proxy are required. The browser still probes and can fall back to relay.
    if allow_direct and origin and not base.get("audio") and not platform_access.platform(page) and not any(k.lower() in {"cookie", "authorization"} for k in headers):
        try:
            with video_network.get(media, referer=page, headers={"Origin": origin, "Range": "bytes=0-0", "Accept-Encoding": "identity"}, stream=True, timeout=(4, 5)) as response:
                if response.status_code in {200, 206} and response.headers.get("Access-Control-Allow-Origin") in {"*", origin} and response.headers.get("Content-Type", "").lower().startswith(("video/", "audio/", "application/octet-stream")):
                    base["mode"] = "direct"
        except Exception:
            pass
    return save_plan(base)


def public(plan):
    return {"download_id": plan["id"], "version": 1, "destination": plan["destination"], "mode": plan["mode"],
            "filename": plan["filename"], "expires_at": plan["expires_at"], "task_id": plan.get("task_id"),
            "url": plan["url"] if plan["mode"] == "direct" else f"/api/downloads/{plan['id']}/content",
            "fallback_url": f"/api/downloads/{plan['id']}/content"}


def upstream(plan, *, range_header=None, if_range=None):
    headers = plan.get("headers", {}) | {"Accept-Encoding": "identity"}
    if range_header:
        if not re.fullmatch(r"bytes=(?:\d+-\d*|-\d+)", range_header):
            raise HTTPException(416, "Range 无效。")
        headers["Range"] = range_header
        if if_range:
            headers["If-Range"] = if_range
    with video_proxy.scope(plan["page"]):
        response = video_network.get(plan["url"], referer=plan["page"], headers=headers, stream=True, timeout=(5, 20))
    if response.status_code not in {200, 206, 416}:
        response.close()
        raise HTTPException(502, "资源返回错误，请重新解析下载。")
    if response.status_code != 416 and any(value in response.headers.get("Content-Type", "").lower() for value in ("text/html", "application/json")):
        response.close()
        raise HTTPException(502, "资源返回了网页，未作为媒体保存。")
    return response


def relay(plan, response):
    received, checked_at = 0, 0.0
    expected = response.headers.get("Content-Length")
    try:
        for chunk in response.iter_content(CHUNK):
            if time.monotonic() - checked_at >= 1:
                check(plan)
                update(plan, status="running", phase="transferring", transferred_bytes=received)
                checked_at = time.monotonic()
            if chunk:
                received += len(chunk)
                yield chunk
        if expected and received != int(expected):
            raise RuntimeError("媒体传输不完整，请重试。")
        update(plan, phase="delivered", transferred_bytes=received)
    except GeneratorExit:
        update(plan, status="cancelled", phase="cancelled")
        raise
    except Exception:
        update(plan, status="failed", phase="failed", error="媒体传输失败，请重新下载。")
        raise
    finally:
        response.close()


def audio_stream(plan):
    """Guarded input and bounded OS pipes; no media is staged on disk."""
    response = upstream(plan)
    ext = plan["output_format"]
    format_name = {"m4a": "mp4", "aac": "adts", "opus": "ogg"}.get(ext, ext)
    codec = {"mp3": ["-c:a", "libmp3lame", "-q:a", "2"], "m4a": ["-c:a", "aac", "-b:a", "192k"]}.get(ext, ["-c:a", "copy"])
    source_type = response.headers.get("Content-Type", "").partition(";")[0]
    source_ext = Path(urlparse(plan["url"]).path).suffix.lower()
    if plan["request"]["audio_format"] == "original" or (ext == "mp3" and (source_type == "audio/mpeg" or source_ext == ".mp3")) or (ext == "m4a" and (source_type == "audio/mp4" or source_ext == ".m4a")):
        codec = ["-c:a", "copy"]
    command = [str(paths.FFMPEG_DIR / runtime.executable_name("ffmpeg")), "-nostdin", "-hide_banner", "-loglevel", "error",
               "-progress", "pipe:2", "-protocol_whitelist", "pipe", "-i", "pipe:0", "-map", "0:a:0", "-vn", *codec]
    if format_name == "mp4":
        command += ["-movflags", "frag_keyframe+empty_moov"]
    process = subprocess.Popen(command + ["-f", format_name, "pipe:1"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    duration, errors, stopped = [0.0], [], threading.Event()

    def feed():
        consumed = 0
        try:
            for chunk in response.iter_content(CHUNK):
                if stopped.is_set():
                    break
                consumed += len(chunk)
                if consumed > 1024 * 1024 * 1024:
                    raise ValueError("单曲源文件超过 1 GB。")
                process.stdin.write(chunk)
            expected_size = response.headers.get("Content-Length")
            if expected_size and consumed != int(expected_size) and not stopped.is_set():
                raise ValueError("音频源数据不完整。")
        except Exception as exc:
            errors.append(exc)
        finally:
            try:
                process.stdin.close()
            except OSError:
                pass
            response.close()

    def watch():
        try:
            while not stopped.wait(1):
                check(plan)
        except Exception as exc:
            errors.append(exc)
            process.kill()
            response.close()

    def progress():
        for line in process.stderr:
            if line.startswith(b"out_time_us="):
                try:
                    duration[0] = int(line.partition(b"=")[2]) / 1_000_000
                except ValueError:
                    pass

    threads = [threading.Thread(target=contextvars.copy_context().run, args=(target,), daemon=True) for target in (feed, progress, watch)]
    for thread in threads:
        thread.start()
    received = 0
    try:
        while chunk := process.stdout.read(CHUNK):
            received += len(chunk)
            update(plan, status="running", phase="transferring", transferred_bytes=received)
            yield chunk
        code = process.wait(timeout=20)
        threads[1].join(timeout=2)
        expected = plan.get("expected_duration")
        if code or errors or not received or (expected and abs(duration[0] - expected) > max(3, expected * .03)):
            raise RuntimeError("音轨不完整或仅提供试听，未完成保存。")
        update(plan, phase="delivered", transferred_bytes=received)
    except GeneratorExit:
        update(plan, status="cancelled", phase="cancelled")
        raise
    except Exception:
        update(plan, status="failed", phase="failed", error="音轨不完整或传输失败，请重试。")
        raise
    finally:
        stopped.set()
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)
        response.close()
        process.stdout.close()
        threads[1].join(timeout=1)
        process.stderr.close()


def task_path(task_id):
    from backend.modules.video import tasks
    row = accounts.resource("download", task_id)
    task = tasks.tasks.get(task_id)
    payload = json.loads(row["payload"])
    if payload.get("destination") != "device" or payload.get("status") != "finished":
        raise HTTPException(409, "本机下载文件尚未准备好。")
    path = storage.directory("temp", uid=row["user_id"], create=False) / "delivery" / task_id / "file" / storage.filename(payload["filename"])
    if not storage.contained(path).is_file():
        raise HTTPException(410, "临时文件已清理，请重新下载。")
    return path


def cleanup_task(task_id, uid):
    from backend.modules.video import tasks
    task = tasks.tasks.get(task_id)
    if task:
        uid = task.user_id
    else:
        try:
            uid = accounts.resource("download", task_id)["user_id"]
        except HTTPException:
            pass
    if task and task.destination == "device":
        task.cancel.set()
        if task.process and task.process.poll() is None:
            task.process.kill()
        if task.status in {"running", "pending"}:
            return  # Worker cleans its directory after stopping.
    directory = storage.directory("temp", uid=uid, create=False) / "delivery" / storage.filename(task_id)
    if directory.exists():
        shutil.rmtree(storage.contained(directory))


def collect_temporary():
    from backend.modules.video import tasks
    cutoff = time.time() - 3600
    for folder in paths.DOWNLOAD_DIR.glob("users/*/tmp/delivery/*"):
        task = tasks.tasks.get(folder.name)
        if task and task.status in {"running", "pending"} and not task.cancel.is_set():
            continue
        if folder.is_dir() and not folder.is_symlink() and folder.stat().st_mtime < cutoff:
            shutil.rmtree(storage.contained(folder))


def start_maintenance():
    global maintenance_thread
    if maintenance_thread and maintenance_thread.is_alive():
        return
    maintenance_stop.clear()

    def maintain():
        import logging
        while not maintenance_stop.wait(60):
            try:
                collect_temporary()
                from backend.core import blobs
                blobs.collect()
            except Exception:
                logging.getLogger(__name__).exception("Download cleanup deferred")
    maintenance_thread = threading.Thread(target=maintain, daemon=True, name="delivery-cleanup")
    maintenance_thread.start()


def stop_maintenance():
    maintenance_stop.set()


def cancel_user(uid, key_to_cancel=None):
    client = accounts.redis_client()
    for plan_key in client.scan_iter(key("plan", uid, "*")):
        raw = client.get(plan_key)
        if not raw:
            continue
        plan = json.loads(raw)
        if key_to_cancel and key_to_cancel not in {plan["id"], plan.get("task_id"), plan.get("source_filename"), plan.get("request", {}).get("parse_record_id")}:
            continue
        update(plan, status="cancelled", phase="cancelled")
        if plan.get("task_id") and plan["destination"] == "device":
            cleanup_task(plan["task_id"], uid)
