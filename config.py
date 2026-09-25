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
    voicevox_exe: str = os.getenv("VOICEVOX_EXE", "")
    mirai_voice_provider: str = os.getenv("MIRAI_VOICE_PROVIDER", "voicevox")

    app_root: str = os.getenv("APP_ROOT", ".")
    data_dir: str = os.getenv("DATA_DIR", "data")
    output_dir: str = os.getenv("OUTPUT_DIR", "output")
    character_file: str = os.getenv("CHARACTER_FILE", "character/character.json")
    youtube_client_secret_file: str = os.getenv("YOUTUBE_CLIENT_SECRET_FILE", "client_secret.json")
    youtube_token_file: str = os.getenv("YOUTUBE_TOKEN_FILE", "token.json")

    character_image: str = os.getenv("CHARACTER_IMAGE", "assets/character/default.png")
    background_dir: str = os.getenv("BACKGROUND_DIR", "assets/backgrounds")
    bgm_file: str = os.getenv("BGM_FILE", "")
    bgm_license_confirmed: bool = (
        os.getenv("BGM_LICENSE_CONFIRMED", "false").lower() == "true"
    )

    app_timezone: str = os.getenv("APP_TIMEZONE", "Asia/Tokyo")
    post_times: str = os.getenv("POST_TIMES", "09:00,15:00,21:00")
    post_grace_minutes: int = int(os.getenv("POST_GRACE_MINUTES", "20"))
    auto_upload_enabled: bool = os.getenv("AUTO_UPLOAD_ENABLED", "false").lower() == "true"
    auto_upload_privacy: str = os.getenv("AUTO_UPLOAD_PRIVACY", "private")

    cleanup_after_upload: bool = os.getenv("CLEANUP_AFTER_UPLOAD", "true").lower() == "true"

    guest_appearance_every: int = int(os.getenv("GUEST_APPEARANCE_EVERY", "7"))
    guest_new_every: int = int(os.getenv("GUEST_NEW_EVERY", "18"))
    guest_max_active: int = int(os.getenv("GUEST_MAX_ACTIVE", "6"))
    guest_image_enabled: bool = os.getenv("GUEST_IMAGE_ENABLED", "false").lower() == "true"
    sd_webui_url: str = os.getenv("SD_WEBUI_URL", "http://127.0.0.1:7860")

    # AI Studio. auto は Diffusers を優先し、未導入時はWebUI互換APIへフォールバック。
    studio_image_backend: str = os.getenv("STUDIO_IMAGE_BACKEND", "auto")
    studio_diffusers_model: str = os.getenv(
        "STUDIO_DIFFUSERS_MODEL",
        "stable-diffusion-v1-5/stable-diffusion-v1-5",
    )

    studio_gpu_retries: int = int(os.getenv("STUDIO_GPU_RETRIES", "2"))
    studio_cpu_fallback: bool = (
        os.getenv("STUDIO_CPU_FALLBACK", "true").lower() == "true"
    )

    studio_scene_images_per_video: int = int(
        os.getenv("STUDIO_SCENE_IMAGES_PER_VIDEO", "2")
    )

    # AI動画は1070では重いため初期OFF。ON時も1本ずつ直列生成。
    ai_video_enabled: bool = (
        os.getenv("AI_VIDEO_ENABLED", "false").lower() == "true"
    )
    ai_video_backend: str = os.getenv("AI_VIDEO_BACKEND", "animatediff")
    ai_video_license_confirmed: bool = (
        os.getenv("AI_VIDEO_LICENSE_CONFIRMED", "false").lower() == "true"
    )
    ai_video_frames: int = int(os.getenv("AI_VIDEO_FRAMES", "8"))
    ai_video_steps: int = int(os.getenv("AI_VIDEO_STEPS", "12"))
    ai_video_width: int = int(os.getenv("AI_VIDEO_WIDTH", "384"))
    ai_video_height: int = int(os.getenv("AI_VIDEO_HEIGHT", "576"))

    # 省負荷自動運転。手動実行時はユーザー操作を優先。
    resource_min_idle_seconds: int = int(
        os.getenv("RESOURCE_MIN_IDLE_SECONDS", "45")
    )
    resource_min_memory_mb: int = int(
        os.getenv("RESOURCE_MIN_MEMORY_MB", "2500")
    )
    resource_min_gpu_free_mb: int = int(
        os.getenv("RESOURCE_MIN_GPU_FREE_MB", "1800")
    )
    resource_urgent_minutes: int = int(
        os.getenv("RESOURCE_URGENT_MINUTES", "60")
    )

    youtube_category_id: str = os.getenv("YOUTUBE_CATEGORY_ID", "22")
    youtube_default_language: str = os.getenv("YOUTUBE_DEFAULT_LANGUAGE", "ja")
    youtube_contains_synthetic_media: bool = (
        os.getenv("YOUTUBE_CONTAINS_SYNTHETIC_MEDIA", "true").lower() == "true"
    )

    font_path: str = os.getenv("FONT_PATH", r"C:\Windows\Fonts\meiryo.ttc")

    dry_run: bool = os.getenv("DRY_RUN", "true").lower() == "true"
    youtube_privacy_status: str = os.getenv("YOUTUBE_PRIVACY_STATUS", "private")

settings = Settings()
