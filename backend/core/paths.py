"""Stable project paths shared by all modules."""
from pathlib import Path

from backend.infrastructure.runtime import ffmpeg_directory, node_executable


ROOT_DIR = Path(__file__).resolve().parents[2]
DOWNLOAD_DIR = ROOT_DIR / "downloads"
THUMBNAIL_DIR = DOWNLOAD_DIR / "_thumbnails"
SUMMARY_DIR = DOWNLOAD_DIR / "_summaries"
CONFIG_DIR = ROOT_DIR / "config"
AI_CONFIG_PATH = CONFIG_DIR / "ai.json"
FRONTEND_DIST = ROOT_DIR / "frontend" / "dist"
RUN_DIR = ROOT_DIR / ".run"
FFMPEG_DIR = ffmpeg_directory()
NODE_EXE = node_executable()
for folder in (DOWNLOAD_DIR, THUMBNAIL_DIR, SUMMARY_DIR):
    folder.mkdir(exist_ok=True)
