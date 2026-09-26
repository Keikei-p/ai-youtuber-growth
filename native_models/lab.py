from __future__ import annotations

import json

from native_models.brain import brain_status
from native_models.image_v0 import image_status
from native_models.video_v0 import video_status
from voice.model_manager import native_voice_status


def native_model_status() -> dict:
    voice = native_voice_status()
    return {
        "brain": brain_status(),
        "image": image_status(),
        "video": video_status(),
        "voice": {
            "code_ready": True,
            "pretrained_dependency": False,
            "dataset": voice.get("dataset") or {},
            "model": voice.get("model") or {},
            "ready": bool((voice.get("model") or {}).get("ready")),
            "quality_stage": "research-spectrogram-v0",
        },
    }


def migration_summary() -> dict:
    status = native_model_status()
    ready = [
        name
        for name, row in status.items()
        if bool(row.get("ready"))
    ]
    code_ready = [
        name
        for name, row in status.items()
        if bool(row.get("code_ready"))
    ]
    return {
        "goal": "No pretrained third-party model weights",
        "code_ready": code_ready,
        "trained_ready": ready,
        "remaining_training": [
            name
            for name in status
            if name not in ready
        ],
        "status": status,
    }


def print_status() -> None:
    print(
        json.dumps(
            migration_summary(),
            ensure_ascii=False,
            indent=2,
        )
    )
