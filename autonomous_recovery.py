from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from typing import Any

from delivery_supervisor import self_heal_delivery_controls
from resilience_learning import resilience_snapshot
from runtime_bootstrap import bootstrap_runtime
from runtime_control import runtime_cancel_requested
from safe_code_repair import repair_known_code_invariants
from storage import (
    failed_queue_items,
    get_channel_state,
    init_db,
    revive_failed_queue,
    set_channel_state,
)
from upload_recovery import classify_upload_failure
from youtube.auth import get_credentials


_RECOVERABLE_CODES = {
    "network",
    "youtube_transient",
    "rate_limit",
    "youtube_processing_pending",
    "upload_reconcile_pending",
    "youtube_metadata_mismatch",
    "youtube_processing_failed",
    "missing_file",
    "metadata",
    "quota",
    "oauth",
}

_CODE_PATTERN = re.compile(
    r"\[(?:CIRCUIT-BREAKER|AUTO-RECOVERY|ACTION-REQUIRED):"
    r"([a-z0-9_]+)\]",
    re.I,
)


def _production_armed() -> bool:
    return (
        get_channel_state(
            "production_autonomy_armed",
            "false",
        ).strip().lower()
        == "true"
    )


def _parse_json_state(key: str) -> dict[str, Any]:
    raw = get_channel_state(key, "").strip()
    if not raw:
        return {}
    try:
        value = json.loads(raw)
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def _failure_code(error: str) -> str:
    match = _CODE_PATTERN.search(str(error or ""))
    if match:
        return match.group(1).strip().lower()
    return str(
        classify_upload_failure(error).get("code") or "unknown"
    ).strip().lower()


def _auth_ready() -> bool:
    try:
        get_credentials(interactive=False)
        return True
    except Exception:
        return False


def _revival_allowed(
    queue_id: int,
    code: str,
    now: datetime,
) -> tuple[bool, dict[str, Any]]:
    key = f"autonomous_queue_revival_{queue_id}"
    state = _parse_json_state(key)
    count = int(state.get("count") or 0)
    last_raw = str(state.get("last_at") or "").strip()

    # failed -> repair -> retry を無限ループさせない。
    if count >= 3:
        return False, state

    if last_raw:
        try:
            last = datetime.fromisoformat(last_raw)
            if last.tzinfo is None:
                last = last.replace(tzinfo=now.tzinfo)
            if now - last.astimezone(now.tzinfo) < timedelta(minutes=30):
                return False, state
        except Exception:
            pass

    if code not in _RECOVERABLE_CODES:
        return False, state

    return True, state


def _remember_revival(
    queue_id: int,
    code: str,
    now: datetime,
    state: dict[str, Any],
) -> None:
    set_channel_state(
        f"autonomous_queue_revival_{queue_id}",
        json.dumps(
            {
                "count": int(state.get("count") or 0) + 1,
                "last_at": now.isoformat(timespec="seconds"),
                "code": code,
            },
            ensure_ascii=False,
        ),
    )


def _learned_upload_action() -> str:
    try:
        snapshot = resilience_snapshot(20)
    except Exception:
        return ""

    for row in snapshot.get("playbooks") or []:
        if str(row.get("stage") or "") != "youtube.upload":
            continue
        action = str(
            row.get("preferred_next_action") or ""
        ).strip()
        if action:
            return action
    return ""


def _run_safe_test_queue() -> list[dict[str, Any]]:
    """
    以前は autonomous_test_queue へ保存するだけで実行者がいなかった。
    ここでは安全に実行可能な既知修復だけを実行し、未知変更は保留する。
    """
    raw = get_channel_state(
        "autonomous_test_queue",
        "[]",
    ).strip()
    try:
        items = json.loads(raw) if raw else []
    except Exception:
        items = []
    if not isinstance(items, list):
        items = []

    changed = False
    results: list[dict[str, Any]] = []

    for item in items:
        if not isinstance(item, dict):
            continue
        if str(item.get("status") or "") != "test_required":
            continue

        action_type = str(
            item.get("action_type") or ""
        ).strip()
        value = item.get("value")

        if (
            action_type == "minor_code_change"
            and isinstance(value, dict)
            and str(value.get("repair_id") or "")
            == "known_invariants"
        ):
            result = repair_known_code_invariants()
            status = str(result.get("status") or "")
            item["status"] = (
                "applied"
                if status in {"healthy", "applied"}
                else "blocked"
            )
            item["result"] = result
            changed = True
            results.append(
                {
                    "action_type": action_type,
                    "status": item["status"],
                    "result": result,
                }
            )
        else:
            # 実装のない提案を「改善済み」と見せない。
            item["status"] = "waiting_implementation"
            item["result"] = {
                "reason": (
                    "安全に自動実行できる実装が未定義のため保留"
                )
            }
            changed = True
            results.append(
                {
                    "action_type": action_type,
                    "status": "waiting_implementation",
                }
            )

    if changed:
        set_channel_state(
            "autonomous_test_queue",
            json.dumps(items[-50:], ensure_ascii=False),
        )

    return results


def run_autonomous_recovery(
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """
    完全自動運用の閉ループ復旧。

    failureを記録するだけで終わらせず、
    既知原因なら安全修復 → failed queue復活 → 次run_dueで再試行まで進める。
    """
    init_db()
    now = now or datetime.now().astimezone()

    result: dict[str, Any] = {
        "checked_at": now.isoformat(timespec="seconds"),
        "status": "idle",
        "revived": [],
        "blocked": [],
        "test_actions": [],
        "runtime": {},
        "learned_action": _learned_upload_action(),
    }

    if not _production_armed():
        result["status"] = "not_armed"
        set_channel_state(
            "autonomous_recovery_last",
            json.dumps(result, ensure_ascii=False),
        )
        return result

    if runtime_cancel_requested():
        result["status"] = "safety_stop"
        set_channel_state(
            "autonomous_recovery_last",
            json.dumps(result, ensure_ascii=False),
        )
        return result

    try:
        self_heal_delivery_controls()
    except Exception as exc:
        result["blocked"].append(
            {
                "code": "delivery_supervisor",
                "detail": str(exc),
            }
        )

    try:
        result["runtime"] = bootstrap_runtime()
    except Exception as exc:
        result["runtime"] = {
            "ready": False,
            "error": str(exc),
        }

    auth_ready = _auth_ready()
    if auth_ready:
        set_channel_state(
            "youtube_auth_attention",
            "false",
        )

    for row in failed_queue_items(limit=20):
        queue_id = int(row.get("queue_id") or 0)
        video_id = int(row.get("video_id") or 0)
        error = str(row.get("error") or "")
        code = _failure_code(error)

        allowed, revival_state = _revival_allowed(
            queue_id,
            code,
            now,
        )
        if not allowed:
            result["blocked"].append(
                {
                    "queue_id": queue_id,
                    "video_id": video_id,
                    "code": code,
                    "detail": (
                        "自動復活対象外、30分cooldown中、"
                        "または復活上限3回に到達"
                    ),
                }
            )
            continue

        if code == "oauth" and not auth_ready:
            result["blocked"].append(
                {
                    "queue_id": queue_id,
                    "video_id": video_id,
                    "code": code,
                    "detail": "YouTube再認証待ち",
                }
            )
            continue

        if (
            code in {
                "missing_file",
                "youtube_processing_failed",
            }
            and not bool(
                (result.get("runtime") or {}).get(
                    "ready",
                    False,
                )
            )
        ):
            result["blocked"].append(
                {
                    "queue_id": queue_id,
                    "video_id": video_id,
                    "code": code,
                    "detail": (
                        "動画再生成が必要ですが、"
                        "Ollama/音声/FFmpegの復旧待ちです"
                    ),
                }
            )
            continue

        if code == "quota":
            scheduled_raw = str(
                row.get("scheduled_for") or ""
            ).strip()
            try:
                scheduled = datetime.fromisoformat(
                    scheduled_raw
                )
                if scheduled.tzinfo is None:
                    scheduled = scheduled.replace(
                        tzinfo=now.tzinfo
                    )
                if scheduled.astimezone(
                    now.tzinfo
                ).date() >= now.date():
                    result["blocked"].append(
                        {
                            "queue_id": queue_id,
                            "video_id": video_id,
                            "code": code,
                            "detail": "quota翌日回復待ち",
                        }
                    )
                    continue
            except Exception:
                pass

        if code == "missing_file":
            set_channel_state(
                f"video_regeneration_requested_{video_id}",
                "true",
            )
        elif code == "metadata":
            set_channel_state(
                f"metadata_sanitize_requested_{video_id}",
                "true",
            )
        elif code == "youtube_processing_failed":
            set_channel_state(
                f"video_regeneration_requested_{video_id}",
                "true",
            )

        # runtime bootstrap/修復直後の同じtickでrun_due()へ渡す。
        # 次の15分heartbeatまで不要に待たせない。
        retry_at = now.replace(second=0, microsecond=0)
        learned = result["learned_action"]
        reason = (
            f"[AUTONOMOUS-REVIVE:{code}] "
            "既知原因を安全修復後に新しい試行として復活"
        )
        if learned:
            reason += f" / learned={learned[:250]}"

        revive_failed_queue(
            queue_id,
            retry_at.isoformat(timespec="minutes"),
            reason=reason,
        )
        _remember_revival(
            queue_id,
            code,
            now,
            revival_state,
        )
        result["revived"].append(
            {
                "queue_id": queue_id,
                "video_id": video_id,
                "code": code,
                "scheduled_for": retry_at.isoformat(
                    timespec="minutes"
                ),
            }
        )

    result["test_actions"] = _run_safe_test_queue()

    if result["revived"]:
        result["status"] = "recovered"
    elif result["blocked"]:
        result["status"] = "attention"
    elif result["test_actions"]:
        result["status"] = "improved"
    else:
        result["status"] = "healthy"

    set_channel_state(
        "autonomous_recovery_last",
        json.dumps(result, ensure_ascii=False),
    )
    return result


if __name__ == "__main__":
    print(
        json.dumps(
            run_autonomous_recovery(),
            ensure_ascii=False,
            indent=2,
        )
    )
