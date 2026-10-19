"""Local environment configuration, shared by authentication and infrastructure."""
from pathlib import Path

from dotenv import dotenv_values

from backend.core.paths import ROOT_DIR


LOCAL_CONFIG_PATH = ROOT_DIR / ".env.universal-video.local"

def local_environment(root: Path | None = None, local_path: Path | None = None) -> dict:
    root = root if root is not None else ROOT_DIR
    local_path = local_path if local_path is not None else LOCAL_CONFIG_PATH
    return dotenv_values(root / ".env") | dotenv_values(local_path, interpolate=False)
