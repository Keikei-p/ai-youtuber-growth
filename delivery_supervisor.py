from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from runtime_control import (
    automation_enabled,
    auto_upload_enabled,
    runtime_cancel_requested,
    set_auto_upload_enabled,
    set_automation_enabled,
)
from storage import (
    get_channel_state,
    init_db,
    queue_item_for_video,
    set_channel_state,
    video_by_id,
)
from youtube.auth import get_credentials


def inspect_delivery_state() -> dict[str, Any]:
    init_db()
    blockers: list[dict[str, str]] = []

    if runtime_cancel_requested():
        blockers.append({"code": "safety_stop", "detail": "安全停止が有効です。"})
    if not automation_enabled():
        blockers.append({"code": "automation_off", "detail": "自動運転がOFFです。"})
    if not auto_upload_enabled():
        blockers.append({"code": "auto_upload_off", "detail": "YouTube自動投稿がOFFです。"})

    try:
        get_credentials(interactive=False)
    except Exception as exc:
        blockers.append({"code": "youtube_auth", "detail": str(exc)})

    raw_id = get_channel_state("mirai_first_episode_video_id", "").strip()
    first: dict[str, Any] = {
        "video_id": int(raw_id) if raw_id.isdigit() else None,
        "uploaded": get_channel_state(
            "mirai_first_episode_uploaded", "false"
        ).strip().lower() == "true",
    }
    if first["video_id"]:
        row = video_by_id(first["video_id"])
        if not row:
            blockers.append({
                "code": "episode1_record_missing",
                "detail": "第1話IDはありますが動画DBレコードがありません。",
            })
        else:
            first["youtube_video_id"] = row.get("youtube_video_id")
            first["output_path"] = row.get("output_path")
            first["file_exists"] = bool(
                row.get("output_path")
                and Path(str(row["output_path"])).is_file()
            )
            first["queue"] = queue_item_for_video(first["video_id"])
    else:
        first["status"] = "not_created"

    return {
        "ok": not blockers,
        "blockers": blockers,
        "repairs": [],
        "first_episode": first,
        "checked_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }


def self_heal_delivery_controls() -> dict[str, Any]:
    state = inspect_delivery_state()
    repairs: list[str] = []
    first = state.get("first_episode") or {}
    first_pending = bool(
        first.get("video_id") and not first.get("youtube_video_id")
    )

    if (
        first_pending
        and not runtime_cancel_requested()
        and automation_enabled()
        and not auto_upload_enabled()
    ):
        set_auto_upload_enabled(True)
        repairs.append("第1話未投稿なのに自動投稿OFFだったためONへ復旧")

    production_armed = get_channel_state(
        "production_autonomy_armed", "false"
    ).strip().lower() == "true"

    # 旧バージョンですでに「自動運転ON + 自動投稿ON」だった環境は、
    # 第1話未投稿時に限り明示設定済みとして本番arm状態へ移行する。
    if (
        first_pending
        and not production_armed
        and not runtime_cancel_requested()
        and automation_enabled()
        and auto_upload_enabled()
    ):
        set_channel_state("production_autonomy_armed", "true")
        production_armed = True
        repairs.append("既存の自動投稿ON設定を本番配送arm状態へ移行")

    if (
        first_pending
        and production_armed
        and not runtime_cancel_requested()
        and not automation_enabled()
    ):
        set_automation_enabled(True)
        set_auto_upload_enabled(True)
        repairs.append("本番自動運転の配送途中で設定OFFを検出しONへ復旧")

    refreshed = inspect_delivery_state()
    refreshed["repairs"] = repairs
    set_channel_state(
        "delivery_supervisor_last",
        json.dumps(refreshed, ensure_ascii=False),
    )
    return refreshed


def arm_production_autonomy() -> dict[str, Any]:
    set_channel_state("production_autonomy_armed", "true")
    set_automation_enabled(True)
    set_auto_upload_enabled(True)
    return self_heal_delivery_controls()


def disarm_production_autonomy() -> None:
    set_channel_state("production_autonomy_armed", "false")
