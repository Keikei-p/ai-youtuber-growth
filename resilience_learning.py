from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timezone
from typing import Any

from storage import connect


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _ensure_table() -> None:
    with connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS failure_playbooks (
                fingerprint TEXT PRIMARY KEY,
                stage TEXT NOT NULL,
                category TEXT NOT NULL,
                first_seen TEXT NOT NULL,
                last_seen TEXT NOT NULL,
                occurrences INTEGER NOT NULL DEFAULT 0,
                success_count INTEGER NOT NULL DEFAULT 0,
                last_success_at TEXT,
                attempted_actions_json TEXT NOT NULL DEFAULT '[]',
                successful_actions_json TEXT NOT NULL DEFAULT '[]',
                latest_context_json TEXT NOT NULL DEFAULT '{}',
                prevention_note TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'learning'
            );

            CREATE INDEX IF NOT EXISTS idx_failure_playbooks_stage
            ON failure_playbooks(stage, last_seen);

            CREATE INDEX IF NOT EXISTS idx_failure_playbooks_status
            ON failure_playbooks(status, occurrences);
            """
        )


def _load_list(raw: str | None) -> list[str]:
    try:
        value = json.loads(raw or "[]")
    except Exception:
        return []
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for item in value:
        text = str(item or "").strip()
        if text and text not in result:
            result.append(text)
    return result


def _merge_actions(*groups: list[str] | tuple[str, ...]) -> list[str]:
    result: list[str] = []
    for group in groups:
        for item in group:
            text = str(item or "").strip()
            if text and text not in result:
                result.append(text)
    return result[-20:]


def record_failure_pattern(
    *,
    fingerprint: str,
    stage: str,
    category: str,
    context: dict[str, Any] | None = None,
    attempted_actions: list[str] | None = None,
    prevention_note: str = "",
) -> dict[str, Any]:
    _ensure_table()
    now = _now()
    context = context or {}
    attempted_actions = attempted_actions or []

    with connect() as conn:
        row = conn.execute(
            """
            SELECT *
            FROM failure_playbooks
            WHERE fingerprint = ?
            """,
            (str(fingerprint),),
        ).fetchone()

        if row:
            previous_attempts = _load_list(
                row["attempted_actions_json"]
            )
            merged_attempts = _merge_actions(
                previous_attempts,
                attempted_actions,
            )
            conn.execute(
                """
                UPDATE failure_playbooks
                SET stage = ?,
                    category = ?,
                    last_seen = ?,
                    occurrences = occurrences + 1,
                    attempted_actions_json = ?,
                    latest_context_json = ?,
                    prevention_note = CASE
                        WHEN ? != '' THEN ?
                        ELSE prevention_note
                    END,
                    status = CASE
                        WHEN success_count > 0 THEN 'regressed'
                        ELSE 'learning'
                    END
                WHERE fingerprint = ?
                """,
                (
                    str(stage),
                    str(category),
                    now,
                    json.dumps(
                        merged_attempts,
                        ensure_ascii=False,
                    ),
                    json.dumps(
                        context,
                        ensure_ascii=False,
                    ),
                    str(prevention_note or ""),
                    str(prevention_note or ""),
                    str(fingerprint),
                ),
            )
        else:
            conn.execute(
                """
                INSERT INTO failure_playbooks (
                    fingerprint, stage, category,
                    first_seen, last_seen, occurrences,
                    success_count, attempted_actions_json,
                    successful_actions_json,
                    latest_context_json, prevention_note,
                    status
                ) VALUES (?, ?, ?, ?, ?, 1, 0, ?, '[]', ?, ?, 'learning')
                """,
                (
                    str(fingerprint),
                    str(stage),
                    str(category),
                    now,
                    now,
                    json.dumps(
                        _merge_actions(attempted_actions),
                        ensure_ascii=False,
                    ),
                    json.dumps(
                        context,
                        ensure_ascii=False,
                    ),
                    str(prevention_note or ""),
                ),
            )

    return playbook_for_fingerprint(str(fingerprint)) or {}


def record_stage_success(
    stage: str,
    *,
    action: str = "",
    context: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """
    直近の同一stage失敗を「この対処で復旧できた」成功例として保存する。
    同じstageの直近3パターンだけを対象にして、無関係な古い失敗まで
    成功扱いにしない。
    """
    _ensure_table()
    now = _now()
    context = context or {}
    updated: list[dict[str, Any]] = []

    with connect() as conn:
        rows = conn.execute(
            """
            SELECT *
            FROM failure_playbooks
            WHERE stage = ?
              AND status IN ('learning', 'regressed')
            ORDER BY last_seen DESC
            LIMIT 3
            """,
            (str(stage),),
        ).fetchall()

        for row in rows:
            successful = _load_list(
                row["successful_actions_json"]
            )
            attempted = _load_list(
                row["attempted_actions_json"]
            )
            success_action = (
                str(action or "").strip()
                or (
                    attempted[-1]
                    if attempted
                    else "同一工程が次回実行で正常完了"
                )
            )
            successful = _merge_actions(
                successful,
                [success_action],
            )
            merged_context = {}
            try:
                merged_context = json.loads(
                    row["latest_context_json"] or "{}"
                )
            except Exception:
                merged_context = {}
            if not isinstance(merged_context, dict):
                merged_context = {}
            merged_context["last_success_context"] = context

            conn.execute(
                """
                UPDATE failure_playbooks
                SET success_count = success_count + 1,
                    last_success_at = ?,
                    successful_actions_json = ?,
                    latest_context_json = ?,
                    status = 'learned'
                WHERE fingerprint = ?
                """,
                (
                    now,
                    json.dumps(
                        successful,
                        ensure_ascii=False,
                    ),
                    json.dumps(
                        merged_context,
                        ensure_ascii=False,
                    ),
                    row["fingerprint"],
                ),
            )
            updated.append(
                {
                    "fingerprint": row["fingerprint"],
                    "stage": row["stage"],
                    "category": row["category"],
                    "successful_action": success_action,
                }
            )

    return updated


def playbook_for_fingerprint(
    fingerprint: str,
) -> dict[str, Any] | None:
    _ensure_table()
    with connect() as conn:
        row = conn.execute(
            """
            SELECT *
            FROM failure_playbooks
            WHERE fingerprint = ?
            """,
            (str(fingerprint),),
        ).fetchone()
    if not row:
        return None
    item = dict(row)
    item["attempted_actions"] = _load_list(
        item.pop("attempted_actions_json", "[]")
    )
    item["successful_actions"] = _load_list(
        item.pop("successful_actions_json", "[]")
    )
    try:
        item["latest_context"] = json.loads(
            item.pop("latest_context_json", "{}")
        )
    except Exception:
        item["latest_context"] = {}
        item.pop("latest_context_json", None)
    successes = item.get("successful_actions") or []
    attempts = item.get("attempted_actions") or []
    item["preferred_next_action"] = (
        successes[-1]
        if successes
        else (
            attempts[-1]
            if attempts
            else "追加ログを収集して原因を絞る"
        )
    )
    return item


def resilience_snapshot(limit: int = 12) -> dict[str, Any]:
    _ensure_table()
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT *
            FROM failure_playbooks
            ORDER BY
                CASE status
                    WHEN 'regressed' THEN 0
                    WHEN 'learning' THEN 1
                    WHEN 'learned' THEN 2
                    ELSE 3
                END,
                occurrences DESC,
                last_seen DESC
            LIMIT ?
            """,
            (max(1, min(int(limit), 50)),),
        ).fetchall()

    playbooks: list[dict[str, Any]] = []
    for row in rows:
        item = playbook_for_fingerprint(
            str(row["fingerprint"])
        )
        if item:
            playbooks.append(item)

    status_counts = Counter(
        str(row.get("status") or "")
        for row in playbooks
    )
    total_failures = sum(
        int(row.get("occurrences") or 0)
        for row in playbooks
    )
    learned_successes = sum(
        int(row.get("success_count") or 0)
        for row in playbooks
    )
    recurring = [
        row
        for row in playbooks
        if int(row.get("occurrences") or 0) >= 2
    ]
    unresolved = [
        row
        for row in playbooks
        if row.get("status") in {"learning", "regressed"}
    ]

    return {
        "total_patterns": len(playbooks),
        "total_failures": total_failures,
        "learned_successes": learned_successes,
        "learned_patterns": int(status_counts.get("learned", 0)),
        "unresolved_patterns": len(unresolved),
        "recurring_patterns": len(recurring),
        "playbooks": playbooks,
    }
