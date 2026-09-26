from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from typing import Any

from config import settings
from native_models.brain import corpus_status
from native_models.image_v0 import dataset_status as image_dataset_status
from native_models.promotion import (
    backup_model,
    evaluate_and_promote,
    model_meta,
    restore_model,
)
from native_models.video_v0 import dataset_status as video_dataset_status
from resource_governor import background_production_decision
from storage import (
    get_channel_state,
    queued_items,
    set_channel_state,
)
from voice.model_manager import inspect_training_dataset


TRAIN_COMMANDS = {
    "brain": [
        "-m",
        "native_models.train_brain_v0",
        "--steps",
        "500",
        "--batch-size",
        "6",
    ],
    "voice": [
        "-m",
        "native_models.train_voice_v0",
        "--epochs",
        "4",
    ],
    "image": [
        "-m",
        "native_models.train_image_v0",
        "--steps",
        "800",
        "--size",
        "64",
    ],
    "video": [
        "-m",
        "native_models.train_video_v0",
        "--steps",
        "800",
        "--size",
        "64",
    ],
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except Exception:
        return None


def _dataset_state(component: str) -> dict:
    if component == "brain":
        return corpus_status()
    if component == "image":
        return image_dataset_status()
    if component == "video":
        return video_dataset_status()
    if component == "voice":
        return inspect_training_dataset()
    raise ValueError(component)


def _dataset_metric(component: str, state: dict) -> int:
    if component == "brain":
        return int(state.get("bytes") or 0)
    if component in {"image", "video", "voice"}:
        return int(state.get("valid_count") or 0)
    return 0


def _post_gap_safe(now: datetime) -> tuple[bool, str]:
    minimum_minutes = int(
        getattr(settings, "native_auto_train_post_gap_minutes", 240)
    )
    future: list[datetime] = []
    for row in queued_items():
        raw = _parse_iso(row.get("scheduled_for"))
        if raw and raw > now:
            future.append(raw)
    if not future:
        return True, "future queueなし"
    nearest = min(future)
    minutes = (nearest - now).total_seconds() / 60
    return (
        minutes >= minimum_minutes,
        f"次投稿まで{minutes:.0f}分",
    )


def _cooldown_ok(component: str, now: datetime) -> bool:
    raw = get_channel_state(
        f"native_last_train_{component}",
        "",
    )
    last = _parse_iso(raw) if raw else None
    if last is None:
        return True
    hours = float(
        getattr(settings, "native_auto_train_cooldown_hours", 168)
    )
    return now - last >= timedelta(hours=max(hours, 1.0))


def _new_data_available(component: str, state: dict) -> bool:
    current = _dataset_metric(component, state)
    previous_raw = get_channel_state(
        f"native_last_train_metric_{component}",
        "0",
    )
    try:
        previous = int(previous_raw)
    except ValueError:
        previous = 0
    return current > previous


def training_plan() -> dict[str, Any]:
    now = _now()
    gap_ok, gap_detail = _post_gap_safe(now)
    resource = background_production_decision(urgent=False)
    torch_ready = importlib.util.find_spec("torch") is not None
    components: dict[str, Any] = {}
    for component in ("brain", "voice", "image", "video"):
        state = _dataset_state(component)
        ready = bool(state.get("ready"))
        new_data = _new_data_available(component, state)
        cooldown = _cooldown_ok(component, now)
        components[component] = {
            "dataset_ready": ready,
            "dataset_metric": _dataset_metric(component, state),
            "new_data": new_data,
            "cooldown_ok": cooldown,
            "eligible": (
                ready
                and new_data
                and cooldown
                and gap_ok
                and bool(resource.allowed)
                and torch_ready
            ),
        }
    return {
        "enabled": bool(
            getattr(settings, "native_auto_train_enabled", True)
        ),
        "post_gap_ok": gap_ok,
        "post_gap_detail": gap_detail,
        "resource_allowed": bool(resource.allowed),
        "resource_reason": resource.reason,
        "torch_ready": torch_ready,
        "components": components,
    }


def maybe_run_native_retraining() -> dict:
    plan = training_plan()
    if not plan["enabled"]:
        return {"status": "disabled", "plan": plan}

    selected = next(
        (
            name
            for name in ("brain", "voice", "image", "video")
            if plan["components"][name]["eligible"]
        ),
        None,
    )
    if not selected:
        return {"status": "not_due", "plan": plan}

    previous_meta = model_meta(selected)
    backup = backup_model(selected)
    command = [
        sys.executable,
        *TRAIN_COMMANDS[selected],
    ]
    timeout = int(
        getattr(settings, "native_auto_train_timeout_seconds", 1800)
    )
    started = _now()
    set_channel_state(
        f"native_last_train_{selected}",
        started.isoformat(timespec="seconds"),
    )
    set_channel_state(
        "native_training_active",
        json.dumps(
            {
                "component": selected,
                "started_at": started.isoformat(timespec="seconds"),
            },
            ensure_ascii=False,
        ),
    )
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=max(300, timeout),
            check=False,
        )
        if completed.returncode != 0:
            restore_model(selected, backup)
            result = {
                "status": "training_failed",
                "component": selected,
                "returncode": completed.returncode,
                "stderr": (completed.stderr or "")[-3000:],
            }
            set_channel_state(
                "native_last_training_result",
                json.dumps(result, ensure_ascii=False),
            )
            return result

        promotion = evaluate_and_promote(
            selected,
            previous_meta=previous_meta,
            backup=backup,
        )
        state = _dataset_state(selected)
        metric = _dataset_metric(selected, state)
        now = _now().isoformat(timespec="seconds")
        set_channel_state(
            f"native_last_train_{selected}",
            now,
        )
        if promotion.get("promoted"):
            set_channel_state(
                f"native_last_train_metric_{selected}",
                str(metric),
            )
        result = {
            "status": (
                "promoted"
                if promotion.get("promoted")
                else "rolled_back"
            ),
            "component": selected,
            "promotion": promotion,
        }
        set_channel_state(
            "native_last_training_result",
            json.dumps(result, ensure_ascii=False),
        )
        return result
    except subprocess.TimeoutExpired as exc:
        restore_model(selected, backup)
        result = {
            "status": "timeout_rollback",
            "component": selected,
            "timeout": timeout,
            "error": str(exc),
        }
        set_channel_state(
            "native_last_training_result",
            json.dumps(result, ensure_ascii=False),
        )
        return result
    except Exception as exc:
        restore_model(selected, backup)
        result = {
            "status": "error_rollback",
            "component": selected,
            "error": str(exc),
        }
        set_channel_state(
            "native_last_training_result",
            json.dumps(result, ensure_ascii=False),
        )
        return result
    finally:
        set_channel_state("native_training_active", "")
