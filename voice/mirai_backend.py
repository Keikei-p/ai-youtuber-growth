from __future__ import annotations

"""
Mirai Local TTS native backend placeholder.

ここへ今後、ミライ専用の自作音声モデル推論を実装する。
外部音声サービスへ自動送信しない。モデル未導入時は明示的に
not readyを返し、VOICEVOX等へ黙ってフォールバックしない。
"""


RUNTIME_IMPLEMENTED = False


def available() -> bool:
    # モデル管理までは実装済み。実推論コードを入れるまでは
    # 「利用可能」と誤表示しない。
    return False


def describe() -> dict:
    from voice.model_manager import native_voice_status
    state = native_voice_status()
    model = state["model"]
    dataset = state["dataset"]
    return {
        "name": "mirai-native-backend",
        "ready": bool(model.get("ready")) and RUNTIME_IMPLEMENTED,
        "detail": model.get("detail"),
        "model": model,
        "dataset": {
            "valid_count": dataset.get("valid_count", 0),
            "total_minutes": dataset.get("total_minutes", 0.0),
            "errors": len(dataset.get("errors") or []),
        },
    }


def synthesize(
    text: str,
    voice_params: dict | None = None,
) -> bytes:
    raise RuntimeError(
        "Mirai native voice model is not installed yet. "
        "Implement voice/mirai_backend.py with the self-owned model."
    )
