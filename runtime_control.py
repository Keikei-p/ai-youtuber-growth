from __future__ import annotations

from config import settings
from storage import get_channel_state, set_channel_state

TRUE_VALUES = {"1", "true", "yes", "on"}

def _bool_state(key: str, default: bool) -> bool:
    raw = get_channel_state(key, "")
    if not raw:
        return default
    return raw.strip().lower() in TRUE_VALUES

def _int_state(
    key: str,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    raw = get_channel_state(key, "").strip()
    try:
        value = int(raw) if raw else int(default)
    except ValueError:
        value = int(default)
    return max(minimum, min(maximum, value))

def automation_enabled() -> bool:
    return _bool_state("automation_enabled", False)

def set_automation_enabled(enabled: bool) -> None:
    set_channel_state("automation_enabled", "true" if enabled else "false")

def auto_upload_enabled() -> bool:
    return _bool_state("auto_upload_enabled", settings.auto_upload_enabled)

def set_auto_upload_enabled(enabled: bool) -> None:
    set_channel_state("auto_upload_enabled", "true" if enabled else "false")

def upload_privacy() -> str:
    value = get_channel_state("auto_upload_privacy", "").strip().lower()
    if value in {"private", "unlisted", "public"}:
        return value
    return settings.auto_upload_privacy

def set_upload_privacy(value: str) -> None:
    value = value.strip().lower()
    if value not in {"private", "unlisted", "public"}:
        raise ValueError("privacy must be private, unlisted, or public")
    set_channel_state("auto_upload_privacy", value)

def web_interval_seconds() -> int:
    return _int_state("web_interval_seconds", 600, 60, 3600)

def set_web_interval_seconds(seconds: int) -> None:
    set_channel_state(
        "web_interval_seconds",
        str(max(60, min(3600, int(seconds)))),
    )

def posts_per_day() -> int:
    return _int_state(
        "posts_per_day",
        settings.posts_per_day,
        1,
        10,
    )

def set_posts_per_day(value: int) -> None:
    set_channel_state("posts_per_day", str(max(1, min(10, int(value)))))

def post_times() -> str:
    value = get_channel_state("post_times", "").strip()
    return value or settings.post_times

def _normalize_post_times(value: str) -> str:
    times: list[str] = []
    for raw in value.split(","):
        raw = raw.strip()
        if not raw:
            continue
        parts = raw.split(":")
        if len(parts) != 2:
            raise ValueError("投稿時刻は HH:MM 形式で指定してください")
        hour = int(parts[0])
        minute = int(parts[1])
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError("投稿時刻が不正です")
        normalized = f"{hour:02d}:{minute:02d}"
        if normalized not in times:
            times.append(normalized)

    if not times:
        raise ValueError("投稿時刻を1つ以上設定してください")

    return ",".join(sorted(times))

def set_post_times(value: str) -> None:
    set_channel_state("post_times", _normalize_post_times(value))

def guest_appearance_every() -> int:
    return _int_state(
        "guest_appearance_every",
        settings.guest_appearance_every,
        0,
        100,
    )

def set_guest_appearance_every(value: int) -> None:
    set_channel_state(
        "guest_appearance_every",
        str(max(0, min(100, int(value)))),
    )

def guest_new_every() -> int:
    return _int_state(
        "guest_new_every",
        settings.guest_new_every,
        0,
        500,
    )

def set_guest_new_every(value: int) -> None:
    set_channel_state(
        "guest_new_every",
        str(max(0, min(500, int(value)))),
    )


def guest_image_auto_enabled() -> bool:
    return _bool_state("guest_image_auto_enabled", settings.guest_image_enabled)

def set_guest_image_auto_enabled(enabled: bool) -> None:
    set_channel_state(
        "guest_image_auto_enabled",
        "true" if enabled else "false",
    )


def ai_video_enabled() -> bool:
    return _bool_state("ai_video_enabled", settings.ai_video_enabled)

def set_ai_video_enabled(enabled: bool) -> None:
    set_channel_state(
        "ai_video_enabled",
        "true" if enabled else "false",
    )


def runtime_cancel_requested() -> bool:
    return _bool_state("runtime_cancel_requested", False)

def request_runtime_cancel() -> None:
    set_channel_state("runtime_cancel_requested", "true")

def clear_runtime_cancel() -> None:
    set_channel_state("runtime_cancel_requested", "false")
