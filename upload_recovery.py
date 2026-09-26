from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from typing import Any

from runtime_control import post_times
from storage import (
    connect,
    set_channel_state,
    set_queue_recovery,
)


def classify_upload_failure(error: Exception | str) -> dict[str, Any]:
    message = str(error)
    text = message.lower()

    if any(x in text for x in (
        "invalid_grant", "unauthorized", "401", "refresh token",
        "credential", "token.json", "再認証", "oauth",
    )):
        return {
            "code": "oauth",
            "retryable": False,
            "safe_action": "reauth_required",
        }
    if any(x in text for x in (
        "quotaexceeded", "dailylimitexceeded", "uploadlimitexceeded",
        "quota exceeded", "daily limit",
    )):
        return {
            "code": "quota",
            "retryable": True,
            "safe_action": "reschedule_next_day",
        }
    if any(x in text for x in ("429", "ratelimitexceeded", "rate limit")):
        return {
            "code": "rate_limit",
            "retryable": True,
            "safe_action": "retry_later",
        }
    if any(x in text for x in (
        "500", "502", "503", "504", "backenderror",
        "internalerror", "service unavailable",
    )):
        return {
            "code": "youtube_transient",
            "retryable": True,
            "safe_action": "retry_later",
        }
    if any(x in text for x in (
        "timeout", "timed out", "connection reset", "connection aborted",
        "connectionerror", "name resolution", "dns", "network",
        "remote end closed",
    )):
        return {
            "code": "network",
            "retryable": True,
            "safe_action": "retry_later",
        }
    if any(x in text for x in (
        "filenotfound", "no such file", "動画ファイルが見つかりません",
        "動画ファイルのパスがありません",
    )):
        return {
            "code": "missing_file",
            "retryable": True,
            "safe_action": "regenerate_video",
        }
    if any(x in text for x in (
        "bad request", "invalid value", "invalid title", "invalid description",
        "invalid tags", "400",
    )):
        return {
            "code": "metadata",
            "retryable": True,
            "safe_action": "sanitize_metadata",
        }
    if any(x in text for x in (
        "公開前確認", "publish gate", "content_risk_publish",
    )):
        return {
            "code": "publish_guard",
            "retryable": False,
            "safe_action": "wait_for_approval",
        }
    return {
        "code": "unknown",
        "retryable": True,
        "safe_action": "standard_retry",
    }


def _ensure_table() -> None:
    with connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS upload_recovery_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                video_id INTEGER,
                queue_id INTEGER,
                failure_code TEXT NOT NULL,
                message TEXT NOT NULL,
                action TEXT NOT NULL,
                result TEXT NOT NULL,
                context_json TEXT NOT NULL DEFAULT '{}'
            );
            CREATE INDEX IF NOT EXISTS idx_upload_recovery_created
            ON upload_recovery_events(created_at);
            """
        )


def _record(
    *,
    video_id: int | None,
    queue_id: int | None,
    failure_code: str,
    message: str,
    action: str,
    result: str,
    context: dict[str, Any] | None = None,
) -> None:
    _ensure_table()
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO upload_recovery_events (
                created_at, video_id, queue_id, failure_code,
                message, action, result, context_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                datetime.now().astimezone().isoformat(timespec="seconds"),
                video_id,
                queue_id,
                failure_code,
                str(message)[:2000],
                action,
                result,
                json.dumps(context or {}, ensure_ascii=False),
            ),
        )


def recent_recovery_events(limit: int = 20) -> list[dict[str, Any]]:
    _ensure_table()
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT
                id, created_at, video_id, queue_id, failure_code,
                message, action, result, context_json
            FROM upload_recovery_events
            ORDER BY id DESC
            LIMIT ?
            """,
            (max(1, min(int(limit), 100)),),
        ).fetchall()
    result: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        try:
            item["context"] = json.loads(item.pop("context_json") or "{}")
        except Exception:
            item["context"] = {}
            item.pop("context_json", None)
        result.append(item)
    return result


def _tomorrow_first_slot(now: datetime) -> datetime:
    first = "09:00"
    values = [
        value.strip()
        for value in post_times().split(",")
        if value.strip()
    ]
    if values:
        first = sorted(values)[0]
    try:
        hour, minute = [int(x) for x in first.split(":", 1)]
    except Exception:
        hour, minute = 9, 0
    tomorrow = (now + timedelta(days=1)).date()
    return now.replace(
        year=tomorrow.year,
        month=tomorrow.month,
        day=tomorrow.day,
        hour=hour,
        minute=minute,
        second=0,
        microsecond=0,
    )


def recover_upload_failure(
    row: dict[str, Any],
    error: Exception | str,
    *,
    now: datetime,
) -> dict[str, Any]:
    diagnosis = classify_upload_failure(error)
    code = str(diagnosis["code"])
    queue_id = int(row.get("queue_id") or 0)
    video_id = int(row.get("video_id") or row.get("id") or 0)
    message = str(error)

    result = {
        **diagnosis,
        "handled": False,
        "scheduled_for": None,
    }

    if queue_id <= 0:
        _record(
            video_id=video_id or None,
            queue_id=None,
            failure_code=code,
            message=message,
            action=str(diagnosis["safe_action"]),
            result="recorded_without_queue",
        )
        return result

    if code in {"network", "youtube_transient", "rate_limit"}:
        delay = 20 if code == "rate_limit" else 10
        retry_at = now + timedelta(minutes=delay)
        set_queue_recovery(
            queue_id,
            scheduled_for=retry_at.isoformat(timespec="minutes"),
            error=f"[AUTO-RECOVERY:{code}] {message}",
            increment_attempt=True,
        )
        result.update(
            handled=True,
            scheduled_for=retry_at.isoformat(timespec="minutes"),
        )
    elif code == "quota":
        retry_at = _tomorrow_first_slot(now)
        set_queue_recovery(
            queue_id,
            scheduled_for=retry_at.isoformat(timespec="minutes"),
            error=f"[AUTO-RECOVERY:quota] {message}",
            increment_attempt=False,
        )
        result.update(
            handled=True,
            scheduled_for=retry_at.isoformat(timespec="minutes"),
        )
    elif code == "oauth":
        retry_at = now + timedelta(hours=6)
        set_queue_recovery(
            queue_id,
            scheduled_for=retry_at.isoformat(timespec="minutes"),
            error=f"[ACTION-REQUIRED:oauth] {message}",
            increment_attempt=False,
        )
        set_channel_state("youtube_auth_attention", "true")
        set_channel_state(
            "youtube_auth_attention_message",
            message[:1000],
        )
        result.update(
            handled=True,
            scheduled_for=retry_at.isoformat(timespec="minutes"),
        )
    elif code == "missing_file":
        retry_at = now + timedelta(minutes=30)
        set_channel_state(
            f"video_regeneration_requested_{video_id}",
            "true",
        )
        set_queue_recovery(
            queue_id,
            scheduled_for=retry_at.isoformat(timespec="minutes"),
            error=f"[AUTO-RECOVERY:regenerate] {message}",
            increment_attempt=False,
        )
        result.update(
            handled=True,
            scheduled_for=retry_at.isoformat(timespec="minutes"),
        )
    elif code == "metadata":
        retry_at = now + timedelta(minutes=15)
        set_channel_state(
            f"metadata_sanitize_requested_{video_id}",
            "true",
        )
        set_queue_recovery(
            queue_id,
            scheduled_for=retry_at.isoformat(timespec="minutes"),
            error=f"[AUTO-RECOVERY:metadata] {message}",
            increment_attempt=True,
        )
        result.update(
            handled=True,
            scheduled_for=retry_at.isoformat(timespec="minutes"),
        )
    elif code == "publish_guard":
        result["handled"] = True

    _record(
        video_id=video_id or None,
        queue_id=queue_id,
        failure_code=code,
        message=message,
        action=str(diagnosis["safe_action"]),
        result=(
            "handled"
            if result["handled"]
            else "fallback_standard_retry"
        ),
        context={
            "scheduled_for": result.get("scheduled_for"),
            "attempts": row.get("attempts"),
        },
    )
    set_channel_state(
        "upload_recovery_last",
        json.dumps(
            {
                "video_id": video_id,
                "queue_id": queue_id,
                "failure_code": code,
                "action": diagnosis["safe_action"],
                "handled": result["handled"],
                "scheduled_for": result.get("scheduled_for"),
            },
            ensure_ascii=False,
        ),
    )
    return result
