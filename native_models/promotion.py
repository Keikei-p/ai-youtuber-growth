from __future__ import annotations

import shutil
import tempfile
import wave
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from PIL import Image, ImageChops, ImageStat

from native_models.common import (
    component_model_root,
    read_json,
    write_json,
)
from paths import DATA_DIR
from voice.model_manager import MODEL_ROOT


COMPONENT_ROOTS = {
    "brain": lambda: component_model_root("brain-v0"),
    "image": lambda: component_model_root("image-v0"),
    "video": lambda: component_model_root("video-v0"),
    "voice": lambda: MODEL_ROOT,
}
BENCHMARK_MIN_SCORE = {
    "brain": 70,
    "image": 70,
    "video": 65,
    "voice": 70,
}


def component_root(component: str) -> Path:
    key = str(component or "").strip().lower()
    if key not in COMPONENT_ROOTS:
        raise ValueError(f"unknown native component: {component}")
    root = Path(COMPONENT_ROOTS[key]())
    root.mkdir(parents=True, exist_ok=True)
    return root


def model_meta(component: str) -> dict[str, Any]:
    return read_json(component_root(component) / "model.json")


def backup_model(component: str) -> Path | None:
    root = component_root(component)
    if not any(root.iterdir()):
        return None
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    target = DATA_DIR / "native_model_backups" / component / stamp
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(root, target)
    return target


def restore_model(component: str, backup: Path | None) -> None:
    root = component_root(component)
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True, exist_ok=True)
    if backup is None:
        return
    for child in backup.iterdir():
        target = root / child.name
        if child.is_dir():
            shutil.copytree(child, target)
        else:
            shutil.copy2(child, target)


def _loss_ok(previous: dict, current: dict) -> tuple[bool, dict]:
    old_loss = (previous.get("training") or {}).get("final_loss")
    new_loss = (current.get("training") or {}).get("final_loss")
    detail = {
        "previous_loss": old_loss,
        "new_loss": new_loss,
    }
    if old_loss is None or new_loss is None:
        return True, detail
    try:
        old = float(old_loss)
        new = float(new_loss)
    except Exception:
        return True, detail
    return new <= old * 1.10, detail


def _brain_benchmark() -> dict:
    from native_models.brain import MiraiNativeBrainClient

    client = MiraiNativeBrainClient()
    prompt = (
        "日本語で80〜180文字。AIが昨日の失敗から改善した話を、"
        "結論を先にして短く説明してください。"
    )
    try:
        value = client.generate(
            prompt,
            max_new_tokens=220,
            temperature=0.7,
        )
    except Exception as exc:
        return {"passed": False, "score": 0, "error": str(exc)}

    length = len(value)
    japanese = sum(
        1
        for ch in value
        if (
            "\u3040" <= ch <= "\u30ff"
            or "\u4e00" <= ch <= "\u9fff"
        )
    )
    ratio = japanese / max(length, 1)
    score = 0
    if 60 <= length <= 260:
        score += 45
    elif 30 <= length <= 360:
        score += 25
    if ratio >= 0.35:
        score += 35
    elif ratio >= 0.15:
        score += 20
    if any(mark in value for mark in ("。", "！", "？")):
        score += 10
    if not any(
        bad in value
        for bad in ("�", "\x00", "<unk>")
    ):
        score += 10
    return {
        "passed": score >= BENCHMARK_MIN_SCORE["brain"],
        "score": score,
        "sample": value[:500],
        "length": length,
        "japanese_ratio": round(ratio, 3),
    }


def _image_benchmark() -> dict:
    from native_models.image_v0 import generate
    from mirai_engines.visual_quality_engine import (
        MiraiVisualQualityEngine,
    )

    try:
        image = generate(
            "friendly futuristic AI character, clean blue studio",
            seed=1234,
        )
        image = image.convert("RGB").resize(
            (512, 768),
            Image.Resampling.LANCZOS,
        )
        report = MiraiVisualQualityEngine().inspect_image(
            image,
            asset_type="mirai",
        )
        score = int(report.get("score") or 0)
        return {
            "passed": bool(report.get("passed"))
            and score >= BENCHMARK_MIN_SCORE["image"],
            "score": score,
            "report": report,
        }
    except Exception as exc:
        return {"passed": False, "score": 0, "error": str(exc)}


def _video_benchmark() -> dict:
    from config import settings
    from native_models.video_v0 import generate_frames

    source = Path(settings.mirai_reference_image)
    if not source.is_file():
        return {
            "passed": False,
            "score": 0,
            "error": "reference image missing",
        }
    try:
        with Image.open(source) as raw:
            start = raw.convert("RGB")
        frames = generate_frames(
            start,
            "Mirai moves naturally in a futuristic studio",
        )
        if len(frames) < 4:
            return {
                "passed": False,
                "score": 20,
                "frames": len(frames),
            }
        diffs: list[float] = []
        for left, right in zip(frames, frames[1:]):
            diff = ImageChops.difference(
                left.convert("RGB"),
                right.convert("RGB"),
            )
            stat = ImageStat.Stat(diff)
            diffs.append(
                sum(stat.mean) / max(len(stat.mean), 1)
            )
        motion = sum(diffs) / max(len(diffs), 1)
        score = 45
        if len(frames) >= 8:
            score += 25
        if 0.2 <= motion <= 40:
            score += 30
        elif motion > 0:
            score += 15
        return {
            "passed": score >= BENCHMARK_MIN_SCORE["video"],
            "score": score,
            "frames": len(frames),
            "mean_frame_delta": round(motion, 3),
        }
    except Exception as exc:
        return {"passed": False, "score": 0, "error": str(exc)}


def _voice_benchmark() -> dict:
    from native_models.voice_v0 import synthesize_wav_bytes

    try:
        payload = synthesize_wav_bytes(
            "こんにちは。私はミライです。昨日の結果から少し成長しました。"
        )
        with tempfile.NamedTemporaryFile(
            suffix=".wav",
            delete=False,
        ) as handle:
            handle.write(payload)
            temp = Path(handle.name)
        try:
            with wave.open(str(temp), "rb") as wav:
                rate = int(wav.getframerate())
                channels = int(wav.getnchannels())
                frames = int(wav.getnframes())
                duration = frames / max(rate, 1)
        finally:
            temp.unlink(missing_ok=True)

        score = 0
        if rate >= 16000:
            score += 30
        if channels in (1, 2):
            score += 20
        if 1.0 <= duration <= 20.0:
            score += 40
        if len(payload) >= 20_000:
            score += 10
        return {
            "passed": score >= BENCHMARK_MIN_SCORE["voice"],
            "score": score,
            "sample_rate": rate,
            "channels": channels,
            "duration": round(duration, 3),
        }
    except Exception as exc:
        return {"passed": False, "score": 0, "error": str(exc)}


def benchmark_component(component: str) -> dict:
    key = str(component or "").strip().lower()
    if key == "brain":
        return _brain_benchmark()
    if key == "image":
        return _image_benchmark()
    if key == "video":
        return _video_benchmark()
    if key == "voice":
        return _voice_benchmark()
    raise ValueError(f"unknown native component: {component}")


def evaluate_and_promote(
    component: str,
    *,
    previous_meta: dict,
    backup: Path | None,
) -> dict:
    current = model_meta(component)
    loss_ok, loss_detail = _loss_ok(previous_meta, current)
    benchmark = benchmark_component(component)
    passed = (
        bool(current.get("ready"))
        and loss_ok
        and bool(benchmark.get("passed"))
    )

    if not passed:
        restore_model(component, backup)
        return {
            "component": component,
            "promoted": False,
            "rolled_back": True,
            "loss": loss_detail,
            "benchmark": benchmark,
        }

    current["production_approved"] = True
    current["approved_at"] = datetime.now(
        timezone.utc
    ).isoformat(timespec="seconds")
    current["approval_evidence"] = {
        "loss": loss_detail,
        "benchmark": benchmark,
    }
    write_json(
        component_root(component) / "model.json",
        current,
    )
    return {
        "component": component,
        "promoted": True,
        "rolled_back": False,
        "loss": loss_detail,
        "benchmark": benchmark,
    }
