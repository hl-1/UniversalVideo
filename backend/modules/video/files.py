from __future__ import annotations

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


@router.get("/api/files/{filename}")
def file_endpoint(filename: str) -> FileResponse:
    if Path(filename).name != filename or any(char in filename for char in ("/", "\\", ":", "\x00")):
        raise HTTPException(422, "文件名无效。")
    safe_name = Path(unquote(filename)).name
    file_path = paths.DOWNLOAD_DIR / safe_name
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(status_code=404, detail="文件不存在。")
    return FileResponse(file_path, filename=safe_name)


@router.get("/api/summary-files/{filename}")
def summary_file_endpoint(filename: str) -> FileResponse:
    safe_name = Path(unquote(filename)).name
    file_path = paths.SUMMARY_DIR / safe_name
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(status_code=404, detail="总结文件不存在。")
    return FileResponse(file_path, filename=safe_name)


@router.get("/api/thumbnails/{filename}")
def thumbnail_endpoint(filename: str) -> FileResponse:
    safe_name = Path(unquote(filename)).name
    file_path = paths.THUMBNAIL_DIR / safe_name
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(status_code=404, detail="封面不存在。")
    return FileResponse(file_path)


@router.get("/api/image-proxy")
def image_proxy(url: str) -> Response:
    return video_thumbnails.fetch_image(url)
