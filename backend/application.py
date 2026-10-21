from __future__ import annotations

import logging


logger = logging.getLogger("videodream.summary")

import math
from fastapi import FastAPI, HTTPException
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from backend.modules.watermark.api import router as watermark_router
from backend.modules.music import sources as music
from backend.modules.music.lyrics import router as music_lyrics_router
from backend.modules.music.media import router as music_library_media_router
from backend.modules.accounts import service as accounts
from backend.modules.music import library
from backend.modules.music import imports as music_imports
from backend.modules.video import cache as video_cache
from backend.modules.video import history as video_history
from backend.modules.admin.api import router as administration_router
from backend.modules.workspace.api import router as workspace_router
from backend.modules.music.agent import api as music_agent_api, mcp as music_agent_mcp, runtime as music_agent_runtime
from backend.core import paths
from backend.infrastructure import browser_sessions as browser_session_store
from backend.modules.music import batch as music_batch
from backend.modules.video import api as video_api
from backend.modules.video import files as video_files
from backend.modules.video import preview as video_preview
from backend.modules.video import preview_assets
from backend.modules.video import tasks as video_tasks
from backend.modules.video import bilibili_session
from backend.modules.delivery.api import router as delivery_router
app = FastAPI(title="VideoDream API", version="1.0.0", lifespan=music_agent_mcp.lifespan)


@app.exception_handler(RequestValidationError)
async def validation_error_response(request, exc):
    def finite(value):
        if isinstance(value, float) and not math.isfinite(value):
            return str(value)
        if isinstance(value, dict):
            return {key: finite(item) for key, item in value.items()}
        if isinstance(value, list):
            return [finite(item) for item in value]
        return value
    return JSONResponse({"detail": finite(jsonable_encoder(exc.errors()))}, status_code=422)
app.include_router(watermark_router)
app.include_router(music.router)
app.include_router(music_agent_api.router)
app.router.routes.extend(music_agent_mcp.http_app.routes)
app.include_router(music_imports.router)
app.include_router(music_lyrics_router)
app.include_router(music_library_media_router)
app.include_router(library.router)
app.include_router(accounts.router)
app.include_router(administration_router)
app.include_router(workspace_router)
app.include_router(video_history.router)
app.include_router(video_api.router)
app.include_router(video_preview.router)
app.include_router(video_files.router)
app.include_router(music_batch.router)
app.include_router(delivery_router)
app.add_middleware(accounts.AuthenticationMiddleware)


if (paths.FRONTEND_DIST / "assets").is_dir():
    app.mount("/assets", StaticFiles(directory=paths.FRONTEND_DIST / "assets"), name="frontend-assets")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:5174",
        "http://127.0.0.1:5174",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/", include_in_schema=False)
def frontend_endpoint() -> FileResponse:
    index = paths.FRONTEND_DIST / "index.html"
    if not index.is_file():
        raise HTTPException(404, "前端尚未构建，请在 frontend 目录执行 npm run build。")
    return FileResponse(index)
accounts.cancel_callbacks.append(video_tasks.cancel_user_tasks)
accounts.cancel_callbacks.append(bilibili_session.disconnect)
accounts.cancel_callbacks.append(browser_session_store.sessions.cancel_user)
accounts.cancel_callbacks.append(music_agent_runtime.cancel_user)
accounts.cancel_callbacks.append(preview_assets.cancel_user)
from backend.modules.delivery import service as delivery_service
accounts.cancel_callbacks.append(delivery_service.cancel_user)


@app.on_event("shutdown")
def close_browser_sessions():
    from backend.modules.delivery import service as delivery
    delivery.stop_maintenance()
    preview_assets.stop()
    browser_session_store.sessions.shutdown()
    video_history.stop_worker()


@app.on_event("startup")
def restore_task_state():
    from backend.modules.delivery import service as delivery
    delivery.start_maintenance()
    preview_assets.start()
    video_history.start_worker()
    try:
        video_history.restore_interrupted()
    except video_cache.redis.RedisError:
        logger.warning("Redis unavailable; interrupted task reconciliation deferred")


@app.get("/auth-worker.js", include_in_schema=False)
def authentication_worker():
    return FileResponse(paths.ROOT_DIR / "frontend" / "public" / "auth-worker.js", media_type="text/javascript", headers={"Cache-Control": "no-cache"})
