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


DAILY_AUTO_PRESETS = {
    1: "12:00",
    2: "12:00,20:00",
    3: "09:00,15:00,21:00",
}


def apply_daily_auto_preset(count: int) -> dict:
    count = int(count)
    if count not in DAILY_AUTO_PRESETS:
        raise ValueError("daily auto preset must be 1, 2, or 3")
    set_posts_per_day(count)
    set_post_times(DAILY_AUTO_PRESETS[count])
    set_automation_enabled(True)
    set_auto_upload_enabled(True)
    return daily_auto_status()


def disable_daily_auto() -> dict:
    set_auto_upload_enabled(False)
    set_automation_enabled(False)
    return daily_auto_status()


def daily_auto_status() -> dict:
    enabled = automation_enabled() and auto_upload_enabled()
    current_count = posts_per_day()
    current_times = post_times()
    preset = None
    for count, times in DAILY_AUTO_PRESETS.items():
        if current_count == count and current_times == times:
            preset = count
            break
    return {
        "enabled": enabled,
        "posts_per_day": current_count,
        "post_times": current_times,
        "preset": preset,
    }


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


def voice_provider_name() -> str:
    value = get_channel_state("mirai_voice_provider", "").strip().lower()
    if value in {"voicevox", "mirai_local"}:
        return value
    configured = str(
        settings.mirai_voice_provider or "voicevox"
    ).strip().lower()
    if configured in {"voicevox", "mirai_local"}:
        return configured
    return "voicevox"


def set_voice_provider_name(value: str) -> None:
    value = str(value or "").strip().lower()
    if value not in {"voicevox", "mirai_local"}:
        raise ValueError(
            "voice provider must be voicevox or mirai_local"
        )
    set_channel_state("mirai_voice_provider", value)


def ai_video_enabled() -> bool:
    return _bool_state("ai_video_enabled", settings.ai_video_enabled)


def set_ai_video_enabled(enabled: bool) -> None:
    set_channel_state(
        "ai_video_enabled",
        "true" if enabled else "false",
    )


def ai_video_license_confirmed() -> bool:
    return _bool_state(
        "ai_video_license_confirmed",
        settings.ai_video_license_confirmed,
    )


def set_ai_video_license_confirmed(confirmed: bool) -> None:
    set_channel_state(
        "ai_video_license_confirmed",
        "true" if confirmed else "false",
    )
    if not confirmed:
        set_ai_video_enabled(False)


def mirai_identity_lock_enabled() -> bool:
    return _bool_state(
        "mirai_identity_lock_enabled",
        settings.mirai_identity_lock_enabled,
    )


def set_mirai_identity_lock_enabled(enabled: bool) -> None:
    set_channel_state(
        "mirai_identity_lock_enabled",
        "true" if enabled else "false",
    )


def mirai_identity_video_enabled() -> bool:
    return _bool_state(
        "mirai_identity_video_enabled",
        settings.mirai_identity_video_enabled,
    )


def set_mirai_identity_video_enabled(enabled: bool) -> None:
    set_channel_state(
        "mirai_identity_video_enabled",
        "true" if enabled else "false",
    )


def visual_candidate_count() -> int:
    return _int_state(
        "visual_candidate_count",
        settings.visual_candidate_count,
        1,
        4,
    )


def set_visual_candidate_count(value: int) -> None:
    set_channel_state(
        "visual_candidate_count",
        str(max(1, min(4, int(value)))),
    )


def visual_background_candidates() -> int:
    return _int_state(
        "visual_background_candidates",
        settings.visual_background_candidates,
        1,
        3,
    )


def set_visual_background_candidates(value: int) -> None:
    set_channel_state(
        "visual_background_candidates",
        str(max(1, min(3, int(value)))),
    )


def visual_retry_rounds() -> int:
    return _int_state(
        "visual_retry_rounds",
        settings.visual_retry_rounds,
        0,
        2,
    )


def set_visual_retry_rounds(value: int) -> None:
    set_channel_state(
        "visual_retry_rounds",
        str(max(0, min(2, int(value)))),
    )


def visual_min_score() -> int:
    return _int_state(
        "visual_min_score",
        settings.visual_min_score,
        40,
        95,
    )


def set_visual_min_score(value: int) -> None:
    set_channel_state(
        "visual_min_score",
        str(max(40, min(95, int(value)))),
    )


def visual_video_min_score() -> int:
    return _int_state(
        "visual_video_min_score",
        settings.visual_video_min_score,
        40,
        95,
    )


def set_visual_video_min_score(value: int) -> None:
    set_channel_state(
        "visual_video_min_score",
        str(max(40, min(95, int(value)))),
    )


def visual_highres_enabled() -> bool:
    return _bool_state(
        "visual_highres_enabled",
        settings.visual_highres_enabled,
    )


def set_visual_highres_enabled(enabled: bool) -> None:
    set_channel_state(
        "visual_highres_enabled",
        "true" if enabled else "false",
    )


def visual_runtime_settings() -> dict:
    return {
        "candidate_count": visual_candidate_count(),
        "background_candidates": visual_background_candidates(),
        "retry_rounds": visual_retry_rounds(),
        "min_score": visual_min_score(),
        "video_min_score": visual_video_min_score(),
        "mirai_identity_lock": mirai_identity_lock_enabled(),
        "mirai_identity_video": mirai_identity_video_enabled(),
        "mirai_reference_image": str(settings.mirai_reference_image),
        "highres_enabled": visual_highres_enabled(),
        "highres_scale": max(
            1.0,
            min(float(settings.visual_highres_scale), 2.0),
        ),
        "highres_strength": max(
            0.1,
            min(float(settings.visual_highres_strength), 0.5),
        ),
        "highres_steps": max(
            4,
            min(int(settings.visual_highres_steps), 20),
        ),
    }


def runtime_cancel_requested() -> bool:
    return _bool_state("runtime_cancel_requested", False)

def request_runtime_cancel() -> None:
    set_channel_state("runtime_cancel_requested", "true")

def clear_runtime_cancel() -> None:
    set_channel_state("runtime_cancel_requested", "false")
