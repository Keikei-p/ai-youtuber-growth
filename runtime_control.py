from __future__ import annotations

from config import settings
from storage import get_channel_state, set_channel_state

TRUE_VALUES = {"1", "true", "yes", "on"}

def _bool_state(key: str, default: bool) -> bool:
    raw = get_channel_state(key, "")
    if not raw:
        return default
    return raw.strip().lower() in TRUE_VALUES

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
    raw = get_channel_state("web_interval_seconds", "").strip()
    try:
        return max(60, int(raw)) if raw else 600
    except ValueError:
        return 600

def set_web_interval_seconds(seconds: int) -> None:
    set_channel_state("web_interval_seconds", str(max(60, int(seconds))))
