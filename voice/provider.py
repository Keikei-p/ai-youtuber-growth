from __future__ import annotations

from config import settings
from runtime_control import voice_provider_name
from voice.mirai_local import MiraiLocalClient
from voice.voicevox import VoicevoxClient


def build_voice_provider():
    name = voice_provider_name()
    if name == "voicevox":
        return VoicevoxClient()
    if name == "mirai_local":
        return MiraiLocalClient()
    raise RuntimeError(f"未対応の音声providerです: {name}.")


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
            "name": voice_provider_name(),
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


def voice_attribution_status() -> dict:
    provider_name = voice_provider_name()
    credit = voice_attribution()
    required = provider_name == "voicevox"
    return {
        "required": required,
        "credit": credit,
        "resolved": (not required) or bool(credit),
    }
