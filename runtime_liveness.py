from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from runtime_control import (
    automation_enabled,
    auto_upload_enabled,
    runtime_cancel_requested,
)
from storage import get_channel_state


def _read_time(raw: str, now: datetime) -> datetime | None:
    try:
        value = datetime.fromisoformat(str(raw).strip())
        if value.tzinfo is None:
            value = value.replace(tzinfo=now.tzinfo)
        value = value.astimezone(now.tzinfo)
        if value > now + timedelta(minutes=2):
            return None
        return value
    except (TypeError, ValueError, OverflowError):
        return None


def _latest(*items: datetime | None) -> datetime | None:
    values = [item for item in items if item is not None]
    return max(values) if values else None


def operational_liveness(
    *,
    now: datetime | None = None,
    heartbeat_limit_minutes: int = 45,
    cycle_limit_minutes: int = 180,
) -> dict[str, Any]:
    """
    設定フラグと実行実績を分離する。armしただけでは「稼働中」にしない。
    schedulerの開始/完了/失敗時刻を使い、実行中や停止を判定する。
    """
    now = now or datetime.now().astimezone()
    if now.tzinfo is None:
        raise ValueError("now must have a timezone")

    raw = {
        "last_due_at": get_channel_state("scheduler_last_run_due_at", ""),
        "last_tick_at": get_channel_state("scheduler_last_tick_at", ""),
        "cycle_started_at": get_channel_state("scheduler_cycle_started_at", ""),
        "cycle_completed_at": get_channel_state("scheduler_cycle_completed_at", ""),
        "cycle_failed_at": get_channel_state("scheduler_cycle_failed_at", ""),
    }
    parsed = {
        key: _read_time(value, now)
        for key, value in raw.items()
    }
    heartbeat = _latest(
        parsed["last_due_at"], parsed["last_tick_at"]
    )
    started = parsed["cycle_started_at"]
    completed = parsed["cycle_completed_at"]
    failed = parsed["cycle_failed_at"]

    armed = (
        get_channel_state("production_autonomy_armed", "false")
        .strip().lower() == "true"
    )
    configured = (
        armed and automation_enabled() and auto_upload_enabled()
    )
    stopped = runtime_cancel_requested()

    status = "stopped"
    detail = "完全自動運用は停止中です。"
    if stopped:
        status = "safety_stop"
        detail = "明示的な安全停止が有効です。"
    elif not configured:
        status = "not_configured"
        detail = "完全自動運用の設定が有効になっていません。"
    elif heartbeat is None:
        status = "unverified"
        detail = "運用設定はONですが、スケジューラの実行履歴がありません。"
    elif now - heartbeat > timedelta(minutes=heartbeat_limit_minutes):
        status = "stale"
        detail = "スケジューラの実行が途絶えています。"
    elif failed is not None and (
        (completed is None or failed > completed)
        and (started is None or failed >= started)
    ):
        status = "failed"
        detail = "直近の動画生成・投稿サイクルが異常終了しました。"
    elif started is not None and (
        completed is None or started > completed
    ):
        if now - started > timedelta(minutes=cycle_limit_minutes):
            status = "stalled"
            detail = "実行中サイクルが長時間完了していません。"
        else:
            status = "processing"
            detail = "現在のサイクルは開始済みですが、まだ完了していません。"
    elif completed is None:
        status = "starting"
        detail = "投稿確認の実行は確認済みですが、フルサイクル完了は未確認です。"
    elif now - completed > timedelta(minutes=cycle_limit_minutes):
        status = "degraded"
        detail = "投稿確認は動いていますが、フルサイクル完了が長時間ありません。"
    else:
        status = "running"
        detail = "最近のスケジューラ実行とサイクル完了を確認しました。"

    return {
        "status": status,
        "running": status in {"running", "processing"},
        "configured": configured,
        "detail": detail,
        **raw,
        "heartbeat_at": (
            heartbeat.isoformat(timespec="seconds")
            if heartbeat is not None else ""
        ),
    }
