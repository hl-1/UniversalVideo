from __future__ import annotations

import json
import time
import uuid
from urllib.parse import parse_qs, urlparse

from fastapi import HTTPException
from fastapi import APIRouter

from backend.modules.music import sources as music
from backend.modules.accounts import service as accounts
from backend.modules.music import library
from backend.modules.video import cache as video_cache
from backend.modules.video import history as video_history
from backend.modules.video import store as video_store
from backend.infrastructure import browser_sessions as browser_session_store
from backend.infrastructure import network
from backend.modules.video import downloads as video_downloads
from backend.modules.video import parse_cache as video_parse_cache
from backend.modules.video import parsing as video_parsing
from backend.modules.video import schemas as video_schemas
from backend.modules.video import summary as video_summary
from backend.modules.video import tasks as video_tasks
from backend.modules.video import thumbnails as video_thumbnails
from backend.modules.video import urls as video_urls
from backend.modules.video import bilibili_session
from backend.modules.video import subtitles as video_subtitles


router = APIRouter()


@router.get("/api/videos/bilibili/session")
def bilibili_status():
    return bilibili_session.status(accounts.current().id)


@router.delete("/api/videos/bilibili/session")
def bilibili_disconnect():
    return bilibili_session.disconnect(accounts.current().id)


@router.post("/api/videos/bilibili/qr")
def bilibili_create_qr():
    return bilibili_session.create_qr(accounts.current().id)


@router.get("/api/videos/bilibili/qr/{login_id}")
def bilibili_poll_qr(login_id: str):
    return bilibili_session.poll_qr(accounts.current().id, login_id)


@router.delete("/api/videos/bilibili/qr/{login_id}")
def bilibili_cancel_qr(login_id: str):
    return bilibili_session.cancel_qr(accounts.current().id, login_id)


@router.post("/api/parse", response_model=video_schemas.ParseResponse)
def parse_endpoint(payload: video_schemas.ParseRequest) -> video_schemas.ParseResponse:
    network.public_url(str(payload.url))
    url = video_urls.normalize_url(str(payload.url))
    if not accounts.principal.get():
        return video_parsing.parse_video(url)
    record_id = video_history.create_record(url)
    try:
        if payload.force:
            video_cache.remove(video_cache.parse_key(url))
        result = video_parse_cache._get_sniff_cache(url) or video_parsing.parse_video(url)
        if result.thumbnail and not result.thumbnail.startswith("/api/thumbnails/"):
            image_url = parse_qs(urlparse(result.thumbnail).query).get("url", [result.thumbnail])[0]
            try:
                result.thumbnail = video_thumbnails._cache_thumbnail(image_url)
            except Exception:
                pass
        video_history.finish_record(record_id, result.model_dump())
        video_history.link_legacy_results(record_id)
        video_parse_cache._set_sniff_cache(url, result)
        if result.webpage_url and result.webpage_url != url:
            video_parse_cache._set_sniff_cache(result.webpage_url, result)
        return result.model_copy(update={"record_id": record_id})
    except Exception as exc:
        message = exc.detail if isinstance(exc, HTTPException) else video_urls._clean_error(exc, url)
        if isinstance(message, dict):
            message = message.get("message", str(message))
        video_history.finish_record(record_id, error=str(message))
        if isinstance(exc, HTTPException):
            detail = exc.detail if isinstance(exc.detail, dict) else {"message": str(exc.detail)}
            raise HTTPException(exc.status_code, detail | {"record_id": record_id}) from exc
        raise HTTPException(422, {"message": str(message), "record_id": record_id}) from exc


@router.post("/api/videos/records/{record_id}/formats", response_model=video_schemas.ParseResponse)
def refresh_record_formats(record_id: str, force: bool = False):
    row = video_history.owned_record(record_id)
    stored = video_history.decode(row)
    url = video_urls.normalize_url((stored.get("result") or {}).get("webpage_url") or stored["url"])
    network.public_url(url)
    if force:
        video_cache.remove(video_cache.parse_key(url))
    result = video_parse_cache._get_sniff_cache(url) or video_parsing.parse_video(url)
    video_parse_cache._set_sniff_cache(url, result)
    return result.model_copy(update={"record_id": record_id})


@router.post("/api/browser-session/open")
def open_browser_session(payload: video_schemas.ParseRequest):
    url = video_urls.normalize_url(str(payload.url))
    network.public_url(url)
    if not video_urls._is_douyin_url(url) or video_urls._is_douyin_search_page(url):
        raise HTTPException(422, "验证窗口仅支持具体的抖音视频或分享链接。")
    return browser_session_store.sessions.open(accounts.current().id, url)


@router.get("/api/browser-session")
def browser_session_status():
    return browser_session_store.sessions.status(accounts.current().id)


@router.delete("/api/browser-session")
def close_browser_session(reset: bool = False):
    result = browser_session_store.sessions.close(accounts.current().id, reset=reset)
    if reset:
        video_cache.clear_user(accounts.current().id)
    return result


@router.post("/api/music/parse")
def parse_music_endpoint(payload: video_schemas.ParseRequest):
    url = str(payload.url)
    network.public_url(url)
    host = video_urls._host(url)
    if music.identify(url) or any(host == domain or host.endswith(f".{domain}") for domain in ("music.163.com", "y.qq.com", "kugou.com")):
        return music.parse_url(url)
    result = video_parsing.parse_video(url)
    if not result.formats:
        raise HTTPException(422, "请使用单个视频或歌曲链接，当前链接没有可用音轨。")
    return result.model_dump()


@router.post("/api/download", response_model=video_schemas.DownloadResponse)
def download_endpoint(payload: video_schemas.DownloadRequest) -> video_schemas.DownloadResponse:
    network.public_url(str(payload.url))
    relation = video_history.validate_relation(payload.parse_record_id, video_urls.normalize_url(str(payload.url)))
    if payload.format_id and payload.format_id.startswith(("sniff:", "sniffpair:")):
        cached = video_parse_cache._get_sniff_cache(video_urls.normalize_url(str(payload.url)))
        if not cached or not any(item.format_id == payload.format_id for item in cached.formats):
            raise HTTPException(422, "下载格式与当前账号的解析结果不匹配，请重新解析。")
    task_id = uuid.uuid4().hex[:12]
    task = video_tasks.DownloadTask(
        task_id=task_id,
        url=str(payload.url),
        format_id=payload.format_id,
        media_type=payload.media_type,
        audio_format=payload.audio_format,
    )
    task.parse_record_id = payload.parse_record_id
    if relation:
        task.user_id = relation["user_id"]
    with video_tasks.tasks_lock:
        video_tasks.tasks[task_id] = task
    video_tasks._persist_task(task, "download", force=True)
    owner_marker = accounts.data_owner.set(task.user_id)
    try:
        accounts.submit(video_tasks.executor, video_downloads._run_download, task)
    finally:
        accounts.data_owner.reset(owner_marker)
    return video_schemas.DownloadResponse(task_id=task_id)


@router.post("/api/summaries", response_model=video_schemas.SummaryResponse)
def summary_endpoint(payload: video_schemas.SummaryRequest) -> video_schemas.SummaryResponse:
    network.public_url(str(payload.url))
    relation = video_history.validate_relation(payload.parse_record_id, video_urls.normalize_url(str(payload.url)))
    task_id = uuid.uuid4().hex[:12]
    task = video_tasks.SummaryTask(task_id=task_id, url=str(payload.url))
    if accounts.principal.get() and video_urls._is_bilibili_url(task.url):
        task.bilibili_prepared = True
        if payload.bilibili_subtitle_policy == "prompt":
            # Use the actor's credentials, including when an admin opens another user's record.
            uid = accounts.current().id
            cookie_header, session_raw = "", None
            try:
                # Count automated probing, but never count the human QR wait or persist a failed admission.
                with task.timing.stage("source", "字幕与转写准备"):
                    cookie_header, session_raw = bilibili_session.cookie_header(uid)
                    task.prepared_source = video_subtitles._extract_bilibili_api_summary_source(
                        task.url, cookie_header=cookie_header, strict=True,
                    )
            except video_subtitles.BilibiliLoginRequired as exc:
                if not (cookie_header and bilibili_session.still_valid(uid, session_raw)):
                    bilibili_session.invalidate(uid, session_raw)
                    raise HTTPException(409, {"code": "bilibili_login_required", "message": str(exc)}) from None
                # An independently verified login with no caption tracks may offer speech.
                task.timing.update("source", status="finished")
            except video_subtitles.BilibiliSubtitleError as exc:
                raise HTTPException(502, {"code": "bilibili_subtitle_unavailable", "message": str(exc)}) from None
            if task.prepared_source is None:
                raise HTTPException(409, {
                    "code": "bilibili_speech_confirmation_required",
                    "message": "本视频暂无可用字幕，需使用语音转写，耗时通常更长。是否继续？",
                })
            task.timing.update("queue", started_at_ms=task.timing.now(), elapsed_ms=0)
    task.generate_mind_map = payload.generate_mind_map
    task.parse_record_id = payload.parse_record_id
    if relation:
        task.user_id = relation["user_id"]
    with video_tasks.summary_tasks_lock:
        video_tasks.summary_tasks[task_id] = task
    video_tasks._persist_task(task, "summary", force=True)
    if payload.generate_mind_map:
        video_summary.create_pipeline_map(task)
        video_tasks._persist_task(task, "summary", force=True)
    owner_marker = accounts.data_owner.set(task.user_id)
    try:
        accounts.submit(video_tasks.ai_executor, video_summary._run_summary, task)
    finally:
        accounts.data_owner.reset(owner_marker)
    return video_schemas.SummaryResponse(task_id=task_id, mind_map_task_id=task.mind_map_task_id)


@router.get("/api/tasks/{task_id}", response_model=video_schemas.TaskResponse)
def task_endpoint(task_id: str) -> video_schemas.TaskResponse:
    with video_tasks.tasks_lock:
        task = video_tasks.tasks.get(task_id)
    if not task:
        if accounts.principal.get():
            return video_schemas.TaskResponse.model_validate(json.loads(accounts.resource("download", task_id)["payload"]))
        raise HTTPException(status_code=404, detail="任务不存在或服务已重启。")
    return task.to_response()


@router.get("/api/summaries/{task_id}", response_model=video_schemas.SummaryTaskResponse)
def summary_task_endpoint(task_id: str) -> video_schemas.SummaryTaskResponse:
    with video_tasks.summary_tasks_lock:
        task = video_tasks.summary_tasks.get(task_id)
    if not task:
        if accounts.principal.get():
            return video_schemas.SummaryTaskResponse.model_validate(json.loads(accounts.resource("summary", task_id)["payload"]))
        raise HTTPException(status_code=404, detail="总结任务不存在或服务已重启。")
    return task.to_response()


@router.post("/api/summaries/{task_id}/mind-map", response_model=video_schemas.MindMapResponse)
def create_mind_map_endpoint(task_id: str, regenerate: bool = False) -> video_schemas.MindMapResponse:
    stored_row = accounts.resource("summary", task_id) if accounts.principal.get() else None
    with video_tasks.summary_tasks_lock:
        summary = video_tasks.summary_tasks.get(task_id)
    if not summary:
        if not accounts.principal.get():
            raise HTTPException(status_code=404, detail="总结任务不存在或服务已重启。")
        stored = json.loads(stored_row["payload"])
        summary = video_tasks.SummaryTask(task_id, (stored.get("result") or {}).get("webpage_url", "https://example.com"))
        summary.user_id = stored_row["user_id"]
        summary.status = stored["status"]
        summary.result = video_schemas.SummaryResult.model_validate(stored["result"]) if stored.get("result") else None
        summary.transcript = [video_schemas.SubtitleSegment.model_validate(row) for row in stored.get("transcript", [])]
        summary.parse_record_id = stored.get("parse_record_id")
        summary.timing = stored.get("timing")
        summary.model_info = stored.get("model_info")
        summary.mind_map_task_id = stored.get("mind_map_task_id")
        video_tasks.summary_tasks[task_id] = summary
    if summary.status != "finished" or not summary.result:
        raise HTTPException(status_code=409, detail="总结完成后才能生成思维导图。")
    if accounts.principal.get():
        with accounts.database() as cursor:
            record_id = json.loads(stored_row["payload"]).get("parse_record_id")
            if record_id:
                # Match task persistence and deletion: lock the parse record before its results.
                cursor.execute("SELECT deleted FROM video_parse_records WHERE record_id=%s AND user_id=%s FOR UPDATE", (record_id, stored_row["user_id"]))
                record = cursor.fetchone()
                if not record or record["deleted"]:
                    raise HTTPException(404, "解析记录已删除。")
            parent = video_store.get(cursor, "summary", task_id, lock=True)
            if not parent:
                raise HTTPException(404, "总结任务不存在。")
            if json.loads(parent["payload"])["status"] != "finished":
                raise HTTPException(409, "总结完成后才能生成思维导图。")
            cursor.execute("SELECT * FROM video_mind_maps WHERE user_id=%s AND deleted=FALSE AND summary_task_id=%s ORDER BY created_at DESC,task_id DESC", (parent["user_id"], task_id))
            versions = [json.loads(video_store.adapt("mind_map", row)["payload"]) for row in cursor.fetchall()]
            active = next((row for row in versions if row["status"] in {"pending", "running"}), None)
            finished = next((row for row in versions if row["status"] == "finished"), None)
            existing = active or (finished if not regenerate else None)
            if existing:
                return video_schemas.MindMapResponse(task_id=existing["task_id"])
            task = video_tasks.MindMapTask(uuid.uuid4().hex[:12], task_id)
            task.user_id = parent["user_id"]
            task.parse_record_id = json.loads(parent["payload"]).get("parse_record_id")
            cursor.execute("SELECT COALESCE(MAX(version),0) AS version FROM video_mind_maps WHERE summary_task_id=%s", (task_id,))
            task.version = cursor.fetchone()["version"] + 1
            value = task.to_response().model_dump() | {"parse_record_id": task.parse_record_id, "worker_id": video_history.WORKER_ID}
            now = time.time()
            video_store.save(cursor, "mind_map", task.task_id, task.user_id, value, created=now)
        with video_tasks.mind_map_tasks_lock:
            video_tasks.mind_map_tasks[task_id] = task
        owner_marker = accounts.data_owner.set(task.user_id)
        try:
            accounts.submit(video_tasks.ai_executor, video_summary._run_mind_map, task, summary)
        finally:
            accounts.data_owner.reset(owner_marker)
        return video_schemas.MindMapResponse(task_id=task.task_id)
    with video_tasks.mind_map_tasks_lock:
        existing = video_tasks.mind_map_tasks.get(task_id)
        if existing and not regenerate and existing.status in {"pending", "running", "finished"}:
            return video_schemas.MindMapResponse(task_id=existing.task_id)
        task = video_tasks.MindMapTask(uuid.uuid4().hex[:12], task_id)
        task.user_id = summary.user_id
        video_tasks.mind_map_tasks[task_id] = task
    video_tasks._persist_task(task, "mind_map", force=True)
    owner_marker = accounts.data_owner.set(summary.user_id)
    try:
        accounts.submit(video_tasks.ai_executor, video_summary._run_mind_map, task, summary)
    finally:
        accounts.data_owner.reset(owner_marker)
    return video_schemas.MindMapResponse(task_id=task.task_id)


@router.get("/api/summaries/{task_id}/mind-map", response_model=video_schemas.MindMapTaskResponse)
def mind_map_task_endpoint(task_id: str, map_id: str | None = None) -> video_schemas.MindMapTaskResponse:
    if accounts.principal.get():
        parent = accounts.resource("summary", task_id)
        with library.database() as cursor:
            args: tuple = (parent["user_id"], task_id)
            query = "SELECT * FROM video_mind_maps WHERE deleted=FALSE AND user_id=%s AND summary_task_id=%s"
            if map_id:
                query += " AND task_id=%s"
                args += (map_id,)
            query += " ORDER BY (status='finished') DESC,created_at DESC,task_id DESC LIMIT 1"
            cursor.execute(query, args)
            stored = cursor.fetchone()
        if not stored:
            raise HTTPException(404, "思维导图任务不存在。")
        with video_tasks.mind_map_tasks_lock:
            live = video_tasks.mind_map_tasks.get(task_id)
            if live and live.task_id == stored["task_id"] and live.user_id == parent["user_id"]:
                if stored["status"] == "cancelled" and live.status in {"pending", "running"}:
                    live.cancel.set()
                    live.status = "cancelled"
                    live.timing.finish("cancelled")
                return live.to_response()
        return video_schemas.MindMapTaskResponse.model_validate(json.loads(video_store.adapt("mind_map", stored)["payload"]))
    with video_tasks.summary_tasks_lock:
        if task_id not in video_tasks.summary_tasks:
            raise HTTPException(status_code=404, detail="总结任务不存在或服务已重启。")
    with video_tasks.mind_map_tasks_lock:
        task = video_tasks.mind_map_tasks.get(task_id)
        if not task:
            raise HTTPException(status_code=404, detail="思维导图任务不存在。")
        return task.to_response()
