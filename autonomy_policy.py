from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from storage import connect, set_channel_state


AUTO_ACTIONS = {
    "knowledge_note",
    "script_guidance",
    "planner_guidance",
    "reduce_scene_images",
    "reduce_ai_video_load",
    "disable_ai_video",
    "retry_tuning",
    "prompt_tuning",
    "quality_analysis",
}

TEST_THEN_AUTO_ACTIONS = {
    "minor_code_change",
    "minor_ui_change",
    "non_destructive_refactor",
}

APPROVAL_ACTIONS = {
    "major_code_change",
    "paid_service",
    "purchase",
    "subscription",
    "credential_change",
    "security_change",
    "database_migration",
    "delete_data",
    "public_upload_change",
    "increase_resource_load",
    "large_model_download",
    "external_account_change",
}


def _ensure_table() -> None:
    with connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS approval_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                action_type TEXT NOT NULL,
                title TEXT NOT NULL,
                reason TEXT NOT NULL,
                payload_json TEXT NOT NULL DEFAULT '{}',
                status TEXT NOT NULL DEFAULT 'pending',
                resolved_at TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_approval_status
            ON approval_requests(status);
            """
        )


def classify_action(action_type: str) -> str:
    value = str(action_type or "").strip().lower()
    if value in AUTO_ACTIONS:
        return "auto"
    if value in TEST_THEN_AUTO_ACTIONS:
        return "test_then_auto"
    if value in APPROVAL_ACTIONS:
        return "approval"
    # 未知の変更は安全側で承認制。
    return "approval"


def request_approval(
    action_type: str,
    title: str,
    reason: str,
    payload: dict[str, Any] | None = None,
) -> int:
    _ensure_table()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    payload_json = json.dumps(
        payload or {},
        ensure_ascii=False,
        sort_keys=True,
    )
    with connect() as conn:
        existing = conn.execute(
            """
            SELECT id
            FROM approval_requests
            WHERE status = 'pending'
              AND action_type = ?
              AND payload_json = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (action_type, payload_json),
        ).fetchone()
        if existing:
            return int(existing["id"])

        cur = conn.execute(
            """
            INSERT INTO approval_requests (
                created_at, action_type, title, reason, payload_json
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                now,
                action_type,
                title[:300],
                reason[:2000],
                payload_json,
            ),
        )
        return int(cur.lastrowid)


def pending_approvals(limit: int = 30) -> list[dict]:
    _ensure_table()
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT
                id, created_at, action_type, title,
                reason, payload_json, status
            FROM approval_requests
            WHERE status = 'pending'
            ORDER BY id DESC
            LIMIT ?
            """,
            (max(1, min(int(limit), 100)),),
        ).fetchall()

    result: list[dict] = []
    for row in rows:
        item = dict(row)
        try:
            item["payload"] = json.loads(
                item.pop("payload_json") or "{}"
            )
        except Exception:
            item["payload"] = {}
            item.pop("payload_json", None)
        result.append(item)
    return result


def _apply_approved_payload(
    action_type: str,
    payload: dict[str, Any],
) -> str:
    """
    承認後にアプリ内だけで安全に反映できる設定変更。
    課金・コード大改修・外部アカウント操作はここでは実行しない。
    """
    if action_type == "increase_resource_load":
        value = payload.get("value")
        if isinstance(value, dict):
            payload = {**payload, **value}

        if "scene_images" in payload:
            count = max(1, min(int(payload["scene_images"]), 4))
            set_channel_state(
                "learned_scene_images_override",
                str(count),
            )
            return f"シーン画像数を{count}へ変更しました。"

        if "ai_video_enabled" in payload:
            raw = payload["ai_video_enabled"]
            enabled = (
                raw
                if isinstance(raw, bool)
                else str(raw).strip().lower()
                in {"1", "true", "yes", "on"}
            )
            set_channel_state(
                "ai_video_enabled",
                "true" if enabled else "false",
            )
            return (
                "AI動画自動生成をONにしました。"
                if enabled else "AI動画自動生成をOFFにしました。"
            )

        # 数値だけ返された場合はシーン画像数として扱う。
        if isinstance(value, (int, float, str)):
            try:
                count = max(1, min(int(value), 4))
            except (TypeError, ValueError):
                count = None
            if count is not None:
                set_channel_state(
                    "learned_scene_images_override",
                    str(count),
                )
                return f"シーン画像数を{count}へ変更しました。"

    return (
        "承認済みとして記録しました。"
        "外部契約・課金・大幅コード変更はChatGPT/Work側で実行します。"
    )


def resolve_approval(
    request_id: int,
    approve: bool,
) -> str:
    _ensure_table()
    with connect() as conn:
        row = conn.execute(
            """
            SELECT action_type, payload_json, status
            FROM approval_requests
            WHERE id = ?
            """,
            (request_id,),
        ).fetchone()
        if not row:
            raise ValueError("承認リクエストが見つかりません")
        if row["status"] != "pending":
            raise ValueError("このリクエストは処理済みです")

        status = "approved" if approve else "rejected"
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        conn.execute(
            """
            UPDATE approval_requests
            SET status = ?, resolved_at = ?
            WHERE id = ?
            """,
            (status, now, request_id),
        )

    if not approve:
        return "却下しました。"

    try:
        payload = json.loads(row["payload_json"] or "{}")
    except Exception:
        payload = {}

    return _apply_approved_payload(
        str(row["action_type"]),
        payload,
    )


def autonomy_state() -> dict:
    return {
        "policy": {
            "auto": sorted(AUTO_ACTIONS),
            "test_then_auto": sorted(TEST_THEN_AUTO_ACTIONS),
            "approval": sorted(APPROVAL_ACTIONS),
        },
        "pending": pending_approvals(),
    }
