from __future__ import annotations

from backend.core import storage

from pathlib import Path
from urllib.parse import unquote

from fastapi import HTTPException
from fastapi.responses import FileResponse, Response
from fastapi import APIRouter

from backend.core import paths
from backend.infrastructure import network
from backend.modules.video import thumbnails as video_thumbnails
from backend.modules.video import urls as video_urls


router = APIRouter()


@router.api_route("/api/files/{filename}", methods=["GET", "HEAD"])
def file_endpoint(filename: str, download: bool = False) -> FileResponse:
    if Path(filename).name != filename or any(char in filename for char in ("/", "\\", ":", "\x00")):
        raise HTTPException(422, "文件名无效。")
    safe_name = Path(unquote(filename)).name
    file_path = storage.resource_path("file", safe_name)
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(status_code=404, detail="文件不存在。")
    return storage.media_response(file_path, download=download)


@router.api_route("/api/summary-files/{filename}", methods=["GET", "HEAD"])
def summary_file_endpoint(filename: str, download: bool = True) -> FileResponse:
    safe_name = Path(unquote(filename)).name
    file_path = storage.resource_path("summary_file", safe_name)
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(status_code=404, detail="总结文件不存在。")
    return storage.media_response(file_path, download=download)


@router.api_route("/api/thumbnails/{filename}", methods=["GET", "HEAD"])
def thumbnail_endpoint(filename: str) -> FileResponse:
    safe_name = Path(unquote(filename)).name
    file_path = storage.resource_path("thumbnail", safe_name)
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(status_code=404, detail="封面不存在。")
    return storage.media_response(file_path)


@router.get("/api/image-proxy")
def image_proxy(url: str) -> Response:
    return video_thumbnails.fetch_image(url)
