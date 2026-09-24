from __future__ import annotations
from pathlib import Path
from config import settings

ROOT_DIR = Path(settings.app_root).resolve()
DATA_DIR = Path(settings.data_dir).resolve()
OUTPUT_DIR = Path(settings.output_dir).resolve()
CHARACTER_FILE = Path(settings.character_file).resolve()
CLIENT_SECRET_FILE = Path(settings.youtube_client_secret_file).resolve()
TOKEN_FILE = Path(settings.youtube_token_file).resolve()

AUDIO_DIR = OUTPUT_DIR / "audio"
VIDEO_DIR = OUTPUT_DIR / "videos"
PLAN_DIR = OUTPUT_DIR / "plans"

def ensure_runtime_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    VIDEO_DIR.mkdir(parents=True, exist_ok=True)
    PLAN_DIR.mkdir(parents=True, exist_ok=True)
