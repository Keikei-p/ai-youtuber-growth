from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from paths import DATA_DIR


NATIVE_MODEL_ROOT = DATA_DIR / "native_models"
NATIVE_TRAINING_ROOT = DATA_DIR / "native_training"


def component_model_root(name: str) -> Path:
    path = NATIVE_MODEL_ROOT / str(name)
    path.mkdir(parents=True, exist_ok=True)
    return path


def component_training_root(name: str) -> Path:
    path = NATIVE_TRAINING_ROOT / str(name)
    path.mkdir(parents=True, exist_ok=True)
    return path


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def model_package_status(name: str) -> dict[str, Any]:
    root = component_model_root(name)
    meta_file = root / "model.json"
    if not meta_file.is_file():
        return {
            "name": name,
            "ready": False,
            "model_root": str(root),
            "detail": "model.json がまだありません。",
        }

    meta = read_json(meta_file)
    artifacts = [
        str(value).strip()
        for value in meta.get("artifacts") or []
        if str(value).strip()
    ]
    missing = [
        raw
        for raw in artifacts
        if not (root / raw).is_file()
    ]
    ready = bool(meta.get("ready")) and not missing
    return {
        "name": str(meta.get("name") or name),
        "version": str(meta.get("version") or ""),
        "ready": ready,
        "production_approved": bool(meta.get("production_approved")),
        "model_root": str(root),
        "missing_artifacts": missing,
        "architecture": meta.get("architecture") or {},
        "training": meta.get("training") or {},
        "detail": (
            "本番承認済み"
            if ready and bool(meta.get("production_approved"))
            else ("推論可能・本番承認待ち" if ready else "学習済み重みが未完成")
        ),
    }
