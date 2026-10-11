from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path


def executable_name(name: str) -> str:
    return f"{name}.exe" if sys.platform == "win32" else name


def ffmpeg_directory() -> Path:
    configured = os.environ.get("VIDEODREAM_FFMPEG_DIR")
    if configured:
        return Path(configured).expanduser()
    location = shutil.which("ffmpeg") or shutil.which("ffprobe")
    if location:
        return Path(location).parent
    if sys.platform == "win32":
        candidates = (Path(r"C:\softWare\environment\ffmpeg"),)
    elif sys.platform == "darwin":
        candidates = (Path("/opt/homebrew/bin"), Path("/usr/local/bin"))
    else:
        candidates = (Path("/usr/bin"), Path("/usr/local/bin"))
    for directory in candidates:
        if any((directory / executable_name(name)).is_file() for name in ("ffmpeg", "ffprobe")):
            return directory
    return candidates[0]


def node_executable() -> Path | None:
    configured = os.environ.get("VIDEODREAM_NODE_EXE")
    if configured:
        return Path(configured).expanduser()
    location = shutil.which("node")
    if location:
        return Path(location)
    legacy = Path(r"C:\softWare\environment\nodejs\node.exe")
    if sys.platform == "win32" and legacy.is_file():
        return legacy
    return None


def browser_executable() -> Path | None:
    configured = os.environ.get("VIDEODREAM_BROWSER_EXE")
    if configured:
        return Path(configured).expanduser()
    candidates = []
    if sys.platform == "win32":
        local = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        program_files = Path(os.environ.get("PROGRAMFILES", r"C:\Program Files"))
        program_files_x86 = Path(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"))
        for directory in (local, program_files, program_files_x86):
            candidates.extend((directory / "Google/Chrome/Application/chrome.exe",
                               directory / "Microsoft/Edge/Application/msedge.exe"))
        candidates.append(Path(r"C:\Program Files (x86)\Microsoft\EdgeCore\126.0.2592.113\msedge.exe"))
    elif sys.platform == "darwin":
        for directory in (Path("/Applications"), Path.home() / "Applications"):
            candidates.extend((directory / "Google Chrome.app/Contents/MacOS/Google Chrome",
                               directory / "Microsoft Edge.app/Contents/MacOS/Microsoft Edge"))
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    for name in ("chrome", "msedge", "google-chrome", "chromium", "chromium-browser"):
        location = shutil.which(name)
        if location:
            return Path(location)
    return None
