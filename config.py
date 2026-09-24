from dataclasses import dataclass
import os
from dotenv import load_dotenv

load_dotenv()

@dataclass(frozen=True)
class Settings:
    posts_per_day: int = int(os.getenv("POSTS_PER_DAY", "3"))
    ollama_url: str = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434")
    ollama_model: str = os.getenv("OLLAMA_MODEL", "qwen2.5:7b")
    voicevox_url: str = os.getenv("VOICEVOX_URL", "http://127.0.0.1:50021")
    voicevox_speaker: int = int(os.getenv("VOICEVOX_SPEAKER", "3"))
    font_path: str = os.getenv("FONT_PATH", r"C:\Windows\Fonts\meiryo.ttc")
    dry_run: bool = os.getenv("DRY_RUN", "true").lower() == "true"
    youtube_privacy_status: str = os.getenv("YOUTUBE_PRIVACY_STATUS", "private")

settings = Settings()
