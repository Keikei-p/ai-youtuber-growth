from __future__ import annotations

"""
Mirai Native Voice v0 runtime.

外部TTSモデルの重みは使わず、data/voice_training の権利クリアWAVから
自前学習した model.pt を読み込んでWAVを生成する。
"""


RUNTIME_IMPLEMENTED = True


def available() -> bool:
    try:
        from native_models.voice_v0 import weights_path
        from voice.model_manager import model_status
        state = model_status()
        if not bool(state.get("ready")):
            return False
        if not weights_path().is_file():
            return False
        import torch  # noqa: F401
        return True
    except Exception:
        return False


def describe() -> dict:
    from voice.model_manager import native_voice_status
    state = native_voice_status()
    model = state["model"]
    dataset = state["dataset"]
    return {
        "name": "mirai-native-backend",
        "ready": available(),
        "detail": (
            "自作Voice v0推論可能"
            if available()
            else model.get("detail")
        ),
        "model": model,
        "dataset": {
            "valid_count": dataset.get("valid_count", 0),
            "total_minutes": dataset.get("total_minutes", 0.0),
            "errors": len(dataset.get("errors") or []),
        },
        "pretrained_dependency": False,
    }


def synthesize(
    text: str,
    voice_params: dict | None = None,
) -> bytes:
    if not available():
        raise RuntimeError(
            "Mirai Native Voice v0はまだ学習済みではありません。"
            " data/voice_training を準備して "
            "python -m native_models.train_voice_v0 を実行してください。"
        )
    from native_models.voice_v0 import synthesize_wav_bytes
    return synthesize_wav_bytes(text, voice_params)
