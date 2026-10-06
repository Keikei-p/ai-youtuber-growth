from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from typing import Any

from config import settings
from runtime_control import post_times
from storage import (
    connect,
    get_channel_state,
    set_channel_state,
    set_queue_failed,
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
        "youtube_upload_reconcile_pending",
        "直前の投稿結果をyoutube側で確認中",
    )):
        return {
            "code": "upload_reconcile_pending",
            "retryable": True,
            "safe_action": "verify_same_video_later",
        }
    if any(x in text for x in (
        "youtube_processing_pending",
        "動画処理完了を確認できません",
        "processing pending",
    )):
        return {
            "code": "youtube_processing_pending",
            "retryable": True,
            "safe_action": "verify_same_video_later",
        }
    if any(x in text for x in (
        "youtube_processing_failed",
        "投稿後処理に失敗",
        "transcodefailed",
        "conversion",
        "invalidfile",
    )):
        return {
            "code": "youtube_processing_failed",
            "retryable": True,
            "safe_action": "regenerate_then_retry",
        }
    if any(x in text for x in (
        "youtube_metadata_mismatch",
        "投稿情報が期待値と一致",
    )):
        return {
            "code": "youtube_metadata_mismatch",
            "retryable": True,
            "safe_action": "verify_metadata_later",
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


def known_resolution(
    failure_code: str,
) -> dict[str, Any] | None:
    _ensure_table()
    with connect() as conn:
        row = conn.execute(
            """
            SELECT
                created_at, failure_code, action, result,
                message, context_json
            FROM upload_recovery_events
            WHERE failure_code = ?
              AND result = 'handled'
            ORDER BY id DESC
            LIMIT 1
            """,
            (str(failure_code),),
        ).fetchone()
    if not row:
        return None
    item = dict(row)
    try:
        item["context"] = json.loads(
            item.pop("context_json") or "{}"
        )
    except Exception:
        item["context"] = {}
        item.pop("context_json", None)
    return item


def _recovery_occurrences(
    video_id: int,
    failure_code: str,
) -> int:
    _ensure_table()
    with connect() as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) AS c
            FROM upload_recovery_events
            WHERE video_id = ?
              AND failure_code = ?
            """,
            (int(video_id), str(failure_code)),
        ).fetchone()
    return int((row or {})["c"] or 0) if row else 0


def _failure_knowledge(code: str) -> dict[str, str]:
    mapping = {
        "oauth": {
            "cause": "OAuth/refresh tokenが無効または権限不足",
            "fix": "自動突破せず再認証要求を保存",
            "prevention": "バックグラウンドでブラウザ認証を起動しない",
        },
        "network": {
            "cause": "ネットワーク未復旧・DNS・接続断",
            "fix": "ネット復帰後へ再スケジュール",
            "prevention": "wake後の疎通待機と回復トリガーを使用",
        },
        "youtube_transient": {
            "cause": "YouTube/Google APIの一時5xx",
            "fix": "再開可能upload/時間差再試行",
            "prevention": "同一障害の短時間連打を避ける",
        },
        "rate_limit": {
            "cause": "APIレート制限",
            "fix": "長めのバックオフ",
            "prevention": "短時間のAPI連打を避ける",
        },
        "quota": {
            "cause": "YouTube API日次quota/上限",
            "fix": "翌投稿枠へ延期",
            "prevention": "市場調査・投稿API回数を必要最小限にする",
        },
        "missing_file": {
            "cause": "投稿対象MP4欠損",
            "fix": "保存済み台本から動画再生成",
            "prevention": "投稿確認前にローカル動画を削除しない",
        },
        "upload_reconcile_pending": {
            "cause": "投稿完了直後の中断でlocal receipt有無が不明",
            "fix": "新規投稿せずYouTube直近投稿を再照合",
            "prevention": "upload intentを送信前に永続化して二重投稿を防ぐ",
        },
        "youtube_processing_pending": {
            "cause": "YouTube側エンコード処理が未完了",
            "fix": "新規投稿せず同じvideoIdを再確認",
            "prevention": "upload receiptでremote IDを永続化",
        },
        "youtube_processing_failed": {
            "cause": "YouTube側変換/処理失敗",
            "fix": "動画を再レンダリングして新規投稿候補へ",
            "prevention": "ffprobe品質ゲート後だけ投稿する",
        },
        "youtube_metadata_mismatch": {
            "cause": "YouTube上のメタ情報が期待値と不一致",
            "fix": "同じvideoIdを再確認し、盲目的な再投稿を避ける",
            "prevention": "投稿レシートへ期待値を保存",
        },
        "metadata": {
            "cause": "YouTubeメタデータ制約違反",
            "fix": "タイトル/説明/タグを安全化して再試行",
            "prevention": "文字数とタグ総量を投稿前に制限",
        },
        "publish_guard": {
            "cause": "公開前確認ルールで人間の承認待ち",
            "fix": "新規投稿せず一定時間後に承認状態を再確認",
            "prevention": "承認待ち中の毎分再試行を避ける",
        },
    }
    return mapping.get(code, {
        "cause": "未知の失敗",
        "fix": "ログを保存し別経路を診断",
        "prevention": "同じ操作を無限反復しない",
    })


def _adaptive_retry_delay(
    code: str,
    default_minutes: int,
    previous_count: int,
) -> int:
    """
    Improvement Engineのretry_tuningを実際の再試行へ反映する。
    adaptive時は同じ失敗を短時間連打せず、回数に応じてbackoffする。
    """
    profile = get_channel_state(
        "autonomous_retry_profile",
        "",
    ).strip().lower()
    if profile != "adaptive":
        return max(1, int(default_minutes))

    multiplier = min(
        3.0,
        1.0 + (max(0, int(previous_count)) * 0.5),
    )
    return max(
        1,
        int(round(int(default_minutes) * multiplier)),
    )


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

    previous_resolution = known_resolution(code)
    previous_count = _recovery_occurrences(video_id, code)
    knowledge = _failure_knowledge(code)
    result = {
        **diagnosis,
        "handled": False,
        "scheduled_for": None,
        "previous_resolution": previous_resolution,
        "same_failure_count": previous_count + 1,
        "knowledge": knowledge,
    }

    # 同一原因を無限反復しない。安全な短期回復を数回試した後は
    # circuit breakerで停止し、診断/実施内容/残課題を保存する。
    limits = {
        "network": 6,
        "youtube_transient": 6,
        "rate_limit": 5,
        "youtube_processing_pending": 12,
        "upload_reconcile_pending": 12,
        "youtube_metadata_mismatch": 4,
        "youtube_processing_failed": 4,
        "missing_file": 4,
        "metadata": 4,
        "quota": 3,
        "oauth": 2,
        "publish_guard": 9999,
        "unknown": 4,
    }
    limit = int(limits.get(code, 5))
    if previous_count >= limit:
        error_text = (
            f"[CIRCUIT-BREAKER:{code}] 同一原因が"
            f"{previous_count + 1}回続いたため自動反復を停止。 "
            f"原因={knowledge['cause']} / "
            f"実施={knowledge['fix']} / "
            "残課題=別の修正方法または外部状態の確認が必要"
        )
        set_queue_failed(queue_id, error_text)
        set_channel_state(
            f"upload_recovery_attention_{video_id}",
            json.dumps(
                {
                    "failure_code": code,
                    "count": previous_count + 1,
                    "cause": knowledge["cause"],
                    "fix": knowledge["fix"],
                    "prevention": knowledge["prevention"],
                    "message": message[:1000],
                },
                ensure_ascii=False,
            ),
        )
        result.update(
            handled=True,
            safe_action="circuit_breaker_attention",
            circuit_breaker=True,
        )
        _record(
            video_id=video_id or None,
            queue_id=queue_id,
            failure_code=code,
            message=message,
            action="circuit_breaker_attention",
            result="circuit_breaker",
            context={
                "same_failure_count": previous_count + 1,
                **knowledge,
            },
        )
        return result

    if (
        video_id > 0
        and code in {
            "oauth",
            "quota",
            "metadata",
            "publish_guard",
            "missing_file",
        }
    ):
        # これらはYouTube側で投稿完了している曖昧状態ではない。
        # 古いintentを残すと次回に不要なremote照合が走るため消す。
        set_channel_state(
            f"youtube_upload_intent_{video_id}",
            "",
        )

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

    if code in {
        "network",
        "youtube_transient",
        "rate_limit",
        "youtube_processing_pending",
        "youtube_metadata_mismatch",
        "upload_reconcile_pending",
    }:
        if code == "rate_limit":
            delay = 20
        elif code == "upload_reconcile_pending":
            delay = max(
                2,
                min(
                    int(
                        getattr(
                            settings,
                            "youtube_reconcile_grace_minutes",
                            3,
                        )
                    ),
                    10,
                ),
            )
        else:
            delay = 10
        delay = _adaptive_retry_delay(
            code,
            delay,
            previous_count,
        )
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
    elif code == "youtube_processing_failed":
        retry_delay = _adaptive_retry_delay(
            code,
            30,
            previous_count,
        )
        retry_at = now + timedelta(minutes=retry_delay)
        set_channel_state(
            f"video_regeneration_requested_{video_id}",
            "true",
        )
        set_queue_recovery(
            queue_id,
            scheduled_for=retry_at.isoformat(timespec="minutes"),
            error=f"[AUTO-RECOVERY:youtube_processing_failed] {message}",
            increment_attempt=True,
        )
        result.update(
            handled=True,
            scheduled_for=retry_at.isoformat(timespec="minutes"),
        )
    elif code == "missing_file":
        retry_delay = _adaptive_retry_delay(
            code,
            30,
            previous_count,
        )
        retry_at = now + timedelta(minutes=retry_delay)
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
        retry_delay = _adaptive_retry_delay(
            code,
            15,
            previous_count,
        )
        retry_at = now + timedelta(minutes=retry_delay)
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
        retry_delay = _adaptive_retry_delay(
            code,
            30,
            previous_count,
        )
        retry_at = now + timedelta(minutes=retry_delay)
        set_queue_recovery(
            queue_id,
            scheduled_for=retry_at.isoformat(timespec="minutes"),
            error=f"[ACTION-REQUIRED:publish_guard] {message}",
            increment_attempt=False,
        )
        set_channel_state(
            f"publish_guard_attention_{video_id}",
            message[:1000],
        )
        result.update(
            handled=True,
            scheduled_for=retry_at.isoformat(timespec="minutes"),
        )

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
            "previous_resolution": previous_resolution,
            "same_failure_count": previous_count + 1,
            "cause": knowledge["cause"],
            "fix": knowledge["fix"],
            "prevention": knowledge["prevention"],
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
