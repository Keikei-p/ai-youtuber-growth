from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from delivery_supervisor import (
    arm_production_autonomy,
    disarm_production_autonomy,
    self_heal_delivery_controls,
)
from runtime_control import (
    auto_upload_enabled,
    automation_enabled,
    post_times,
    posts_per_day,
    runtime_cancel_requested,
    set_auto_upload_enabled,
    set_automation_enabled,
    upload_privacy,
)
from storage import (
    get_channel_state,
    init_db,
    set_channel_state,
)
from youtube.auth import get_credentials


def production_autonomy_armed() -> bool:
    return (
        get_channel_state(
            "production_autonomy_armed",
            "false",
        ).strip().lower()
        == "true"
    )


def _last_event() -> dict[str, Any]:
    raw = get_channel_state(
        "full_autopilot_last_event",
        "",
    ).strip()
    if not raw:
        return {}
    try:
        value = json.loads(raw)
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def _save_event(
    status: str,
    *,
    detail: str = "",
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "status": str(status),
        "checked_at": datetime.now().astimezone().isoformat(
            timespec="seconds"
        ),
        "detail": str(detail or "")[:2000],
    }
    if extra:
        payload.update(extra)
    set_channel_state(
        "full_autopilot_last_event",
        json.dumps(payload, ensure_ascii=False),
    )
    return payload


def autopilot_status() -> dict[str, Any]:
    init_db()
    armed = production_autonomy_armed()
    auth_attention = (
        get_channel_state(
            "youtube_auth_attention",
            "false",
        ).strip().lower()
        == "true"
    )
    human_action = ""
    if auth_attention:
        human_action = (
            "YouTubeの再認証が必要です。"
            "それ以外の自動運転設定は維持します。"
        )

    return {
        "armed": armed,
        "running": (
            armed
            and automation_enabled()
            and auto_upload_enabled()
            and not runtime_cancel_requested()
        ),
        "automation_enabled": automation_enabled(),
        "auto_upload_enabled": auto_upload_enabled(),
        "runtime_cancel_requested": runtime_cancel_requested(),
        "posts_per_day": posts_per_day(),
        "post_times": post_times(),
        "privacy": upload_privacy(),
        "human_action_required": bool(human_action),
        "human_action": human_action,
        "last_event": _last_event(),
    }


def start_autopilot() -> dict[str, Any]:
    """
    人間が最初に1回だけ開始する入口。

    YouTube OAuthだけは本人同意が必要なので、認証が無い時は
    自動突破せず開始を止める。それ以外の自動運転スイッチは
    production armにまとめて永続化する。
    """
    init_db()
    try:
        get_credentials(interactive=False)
    except Exception as exc:
        _save_event(
            "blocked",
            detail="YouTube認証が必要です。",
            extra={"code": "youtube_auth"},
        )
        raise RuntimeError(
            "完全自動運用を開始する前にYouTube認証が必要です。"
            "一度だけ認証を完了してください。 "
            + str(exc)
        ) from exc

    # set_automation_enabled(True) 内で safety stop も解除される。
    delivery = arm_production_autonomy()
    set_channel_state("full_autopilot_enabled", "true")
    set_channel_state(
        "youtube_auth_attention",
        "false",
    )
    _save_event(
        "running",
        detail="完全自動運用を開始しました。",
        extra={
            "post_times": post_times(),
            "posts_per_day": posts_per_day(),
            "privacy": upload_privacy(),
            "repairs": list(
                delivery.get("repairs") or []
            ),
        },
    )
    return autopilot_status()


def stop_autopilot() -> dict[str, Any]:
    init_db()
    disarm_production_autonomy()
    set_auto_upload_enabled(False)
    set_automation_enabled(False)
    set_channel_state("full_autopilot_enabled", "false")
    _save_event(
        "stopped",
        detail="完全自動運用を停止しました。",
    )
    return autopilot_status()


def heal_autopilot() -> dict[str, Any]:
    """
    アプリ起動直後/常駐ループ先頭で呼ぶ自己修復。

    armが残っている限り、途中でautomation/uploadの片方がOFFに
    崩れても人間操作なしで元へ戻す。安全停止時だけは勝手に解除しない。
    """
    init_db()
    if not production_autonomy_armed():
        return autopilot_status()

    if runtime_cancel_requested():
        _save_event(
            "blocked",
            detail="安全停止中のため自動復旧しません。",
            extra={"code": "safety_stop"},
        )
        return autopilot_status()

    before = (
        automation_enabled(),
        auto_upload_enabled(),
    )
    delivery = self_heal_delivery_controls()
    after = (
        automation_enabled(),
        auto_upload_enabled(),
    )
    repairs = list(delivery.get("repairs") or [])

    if before != after or repairs:
        _save_event(
            "repaired",
            detail="完全自動運用の設定ずれを自己修復しました。",
            extra={"repairs": repairs},
        )
    else:
        _save_event(
            "running",
            detail="完全自動運用を継続中です。",
        )
    return autopilot_status()
