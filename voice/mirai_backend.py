from __future__ import annotations

"""
Mirai Local TTS native backend placeholder.

ここへ今後、ミライ専用の自作音声モデル推論を実装する。
外部音声サービスへ自動送信しない。モデル未導入時は明示的に
not readyを返し、VOICEVOX等へ黙ってフォールバックしない。
"""


def available() -> bool:
    return False


def describe() -> dict:
    return {
        "name": "mirai-native-backend",
        "ready": False,
        "detail": "自作音声モデルはまだ未導入です。",
    }


def synthesize(
    text: str,
    voice_params: dict | None = None,
) -> bytes:
    raise RuntimeError(
        "Mirai native voice model is not installed yet. "
        "Implement voice/mirai_backend.py with the self-owned model."
    )
