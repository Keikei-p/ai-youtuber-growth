from __future__ import annotations

import csv
import json
import wave
from pathlib import Path
from typing import Any

from paths import DATA_DIR


TRAINING_ROOT = DATA_DIR / "voice_training"
MODEL_ROOT = DATA_DIR / "voice_models" / "mirai-native"
METADATA_FILE = TRAINING_ROOT / "metadata.csv"
MANIFEST_FILE = TRAINING_ROOT / "manifest.jsonl"
MODEL_META_FILE = MODEL_ROOT / "model.json"


def ensure_voice_dirs() -> None:
    TRAINING_ROOT.mkdir(parents=True, exist_ok=True)
    MODEL_ROOT.mkdir(parents=True, exist_ok=True)


def _inside(root: Path, candidate: Path) -> bool:
    try:
        candidate.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _wav_info(path: Path) -> dict[str, Any]:
    with wave.open(str(path), "rb") as handle:
        rate = int(handle.getframerate())
        frames = int(handle.getnframes())
        channels = int(handle.getnchannels())
        sample_width = int(handle.getsampwidth())
    duration = frames / rate if rate > 0 else 0.0
    return {
        "sample_rate": rate,
        "frames": frames,
        "channels": channels,
        "sample_width": sample_width,
        "duration_seconds": round(duration, 3),
    }


def inspect_training_dataset() -> dict:
    ensure_voice_dirs()
    if not METADATA_FILE.is_file():
        return {
            "ready": False,
            "sample_count": 0,
            "valid_count": 0,
            "total_minutes": 0.0,
            "errors": ["metadata.csv がまだありません。"],
            "training_root": str(TRAINING_ROOT),
            "metadata_file": str(METADATA_FILE),
        }

    samples: list[dict] = []
    errors: list[str] = []
    total_seconds = 0.0

    with METADATA_FILE.open(
        "r",
        encoding="utf-8-sig",
        newline="",
    ) as handle:
        reader = csv.reader(handle, delimiter="|")
        for index, row in enumerate(reader, start=1):
            if not row or all(not str(value).strip() for value in row):
                continue
            if len(row) < 2:
                errors.append(
                    f"{index}行目: relative_wav|text 形式ではありません。"
                )
                continue

            raw_path = str(row[0]).strip()
            text_value = "|".join(row[1:]).strip()
            if not raw_path or not text_value:
                errors.append(f"{index}行目: 音声パスまたは文章が空です。")
                continue

            wav_path = (TRAINING_ROOT / raw_path).resolve()
            if not _inside(TRAINING_ROOT, wav_path):
                errors.append(f"{index}行目: 学習フォルダ外のパスです。")
                continue
            if not wav_path.is_file():
                errors.append(f"{index}行目: WAVがありません: {raw_path}")
                continue
            if wav_path.suffix.lower() != ".wav":
                errors.append(f"{index}行目: WAV以外は使用しません: {raw_path}")
                continue

            try:
                info = _wav_info(wav_path)
            except Exception as exc:
                errors.append(f"{index}行目: WAV読込失敗: {exc}")
                continue

            if info["sample_rate"] < 16000:
                errors.append(
                    f"{index}行目: サンプルレートが低すぎます: "
                    f"{info['sample_rate']}Hz"
                )
                continue
            if info["channels"] not in {1, 2}:
                errors.append(
                    f"{index}行目: チャンネル数が不正です: "
                    f"{info['channels']}"
                )
                continue
            if info["duration_seconds"] <= 0.2:
                errors.append(f"{index}行目: 音声が短すぎます。")
                continue

            total_seconds += float(info["duration_seconds"])
            samples.append(
                {
                    "audio": str(wav_path),
                    "text": text_value,
                    **info,
                }
            )

    return {
        "ready": bool(samples) and not errors,
        "sample_count": len(samples) + len(errors),
        "valid_count": len(samples),
        "total_minutes": round(total_seconds / 60.0, 2),
        "errors": errors[:50],
        "samples": samples,
        "training_root": str(TRAINING_ROOT),
        "metadata_file": str(METADATA_FILE),
        "manifest_file": str(MANIFEST_FILE),
    }


def prepare_training_manifest() -> dict:
    result = inspect_training_dataset()
    samples = result.get("samples") or []
    MANIFEST_FILE.parent.mkdir(parents=True, exist_ok=True)
    with MANIFEST_FILE.open("w", encoding="utf-8") as handle:
        for sample in samples:
            handle.write(
                json.dumps(
                    sample,
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                + "\n"
            )
    return {
        **{k: v for k, v in result.items() if k != "samples"},
        "manifest_written": str(MANIFEST_FILE),
        "manifest_rows": len(samples),
    }


def model_status() -> dict:
    ensure_voice_dirs()
    if not MODEL_META_FILE.is_file():
        return {
            "package_present": False,
            "ready": False,
            "model_root": str(MODEL_ROOT),
            "detail": "model.json がまだありません。",
        }

    try:
        meta = json.loads(
            MODEL_META_FILE.read_text(encoding="utf-8")
        )
    except Exception as exc:
        return {
            "package_present": True,
            "ready": False,
            "model_root": str(MODEL_ROOT),
            "detail": f"model.json 読込失敗: {exc}",
        }

    artifacts = [
        str(value).strip()
        for value in meta.get("artifacts") or []
        if str(value).strip()
    ]
    missing = []
    for raw in artifacts:
        candidate = (MODEL_ROOT / raw).resolve()
        if not _inside(MODEL_ROOT, candidate) or not candidate.is_file():
            missing.append(raw)

    runtime_implemented = bool(
        meta.get("runtime_implemented", False)
    )
    ready = (
        bool(meta.get("ready", False))
        and runtime_implemented
        and not missing
    )
    return {
        "package_present": True,
        "ready": ready,
        "model_root": str(MODEL_ROOT),
        "name": str(meta.get("name") or "Mirai Native Voice"),
        "version": str(meta.get("version") or ""),
        "runtime_implemented": runtime_implemented,
        "missing_artifacts": missing,
        "detail": (
            "推論可能"
            if ready
            else "モデル/推論ランタイム準備中"
        ),
    }


def native_voice_status() -> dict:
    dataset = inspect_training_dataset()
    model = model_status()
    return {
        "dataset": {
            key: value
            for key, value in dataset.items()
            if key != "samples"
        },
        "model": model,
    }
