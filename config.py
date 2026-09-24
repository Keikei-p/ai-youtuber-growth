from dataclasses import dataclass
import os
from dotenv import load_dotenv

load_dotenv()

@dataclass(frozen=True)
class Settings:
    posts_per_day: int = int(os.getenv("POSTS_PER_DAY", "3"))
    max_script_retries: int = int(os.getenv("MAX_SCRIPT_RETRIES", "3"))
    max_generation_rounds: int = int(os.getenv("MAX_GENERATION_ROUNDS", "3"))

    ollama_url: str = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434")
    ollama_model: str = os.getenv("OLLAMA_MODEL", "qwen2.5:7b")

    voicevox_url: str = os.getenv("VOICEVOX_URL", "http://127.0.0.1:50021")
    voicevox_speaker: int = int(os.getenv("VOICEVOX_SPEAKER", "3"))

    app_root: str = os.getenv("APP_ROOT", ".")
    data_dir: str = os.getenv("DATA_DIR", "data")
    output_dir: str = os.getenv("OUTPUT_DIR", "output")
    character_file: str = os.getenv("CHARACTER_FILE", "character/character.json")
    youtube_client_secret_file: str = os.getenv("YOUTUBE_CLIENT_SECRET_FILE", "client_secret.json")
    youtube_token_file: str = os.getenv("YOUTUBE_TOKEN_FILE", "token.json")

    character_image: str = os.getenv("CHARACTER_IMAGE", "assets/character/default.png")
    background_dir: str = os.getenv("BACKGROUND_DIR", "assets/backgrounds")
    bgm_file: str = os.getenv("BGM_FILE", "")

    font_path: str = os.getenv("FONT_PATH", r"C:\Windows\Fonts\meiryo.ttc")

    dry_run: bool = os.getenv("DRY_RUN", "true").lower() == "true"
    youtube_privacy_status: str = os.getenv("YOUTUBE_PRIVACY_STATUS", "private")

settings = Settings()
