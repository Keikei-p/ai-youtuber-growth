from __future__ import annotations

from config import settings
from voice.voicevox import VoicevoxClient


def build_voice_provider():
    name = str(settings.mirai_voice_provider or "voicevox").strip().lower()
    if name == "voicevox":
        return VoicevoxClient()
    raise RuntimeError(
        f"未対応の音声providerです: {name}. "
        "voice/provider.pyへproviderを追加してください。"
    )


def voice_provider_status() -> dict:
    try:
        provider = build_voice_provider()
        return {
            "name": getattr(provider, "name", provider.__class__.__name__),
            "available": bool(provider.available()),
            "error": "",
        }
    except Exception as exc:
        return {
            "name": str(settings.mirai_voice_provider or "unknown"),
            "available": False,
            "error": str(exc),
        }


def voice_attribution() -> str:
    try:
        provider = build_voice_provider()
        method = getattr(provider, "attribution", None)
        if callable(method):
            return str(method() or "").strip()
    except Exception:
        pass
    return ""
