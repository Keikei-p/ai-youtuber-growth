from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any

from ai_client import OllamaClient
from autonomy_policy import classify_action, request_approval
from config import settings
from storage import (
    analytics_history,
    connect,
    get_channel_state,
    set_channel_state,
)


def _ensure_tables() -> None:
    with connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS failure_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                stage TEXT NOT NULL,
                fingerprint TEXT NOT NULL,
                message TEXT NOT NULL,
                context_json TEXT NOT NULL DEFAULT '{}'
            );

            CREATE INDEX IF NOT EXISTS idx_failure_fingerprint
            ON failure_events(fingerprint);

            CREATE INDEX IF NOT EXISTS idx_failure_stage
            ON failure_events(stage);
            """
        )


def _normalized_message(message: str) -> str:
    value = message.lower()
    value = re.sub(r"0x[0-9a-f]+", "<hex>", value)
    value = re.sub(r"\b\d+(?:\.\d+)?\s*(?:mb|gb|ms|s)\b", "<num>", value)
    value = re.sub(r"\b\d+\b", "<num>", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value[:700]


def _fingerprint(stage: str, message: str) -> str:
    raw = f"{stage}|{_normalized_message(message)}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _classify(message: str) -> str:
    text = message.lower()
    if any(
        marker in text
        for marker in (
            "cuda",
            "cudart",
            "out of memory",
            "device(s) is/are busy",
            "device unavailable",
        )
    ):
        return "gpu"
    if "voicevox" in text or "audio" in text or "音声" in text:
        return "voice"
    if "ffmpeg" in text or "動画編集" in text or "render" in text:
        return "render"
    if "youtube" in text or "upload" in text or "投稿" in text:
        return "upload"
    if "ollama" in text or "json" in text or "台本" in text:
        return "text_ai"
    return "other"


def record_failure(
    stage: str,
    error: Exception | str,
    context: dict[str, Any] | None = None,
) -> dict:
    _ensure_tables()
    message = str(error)
    fingerprint = _fingerprint(stage, message)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    with connect() as conn:
        conn.execute(
            """
            INSERT INTO failure_events (
                created_at, stage, fingerprint, message, context_json
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                now,
                stage,
                fingerprint,
                message[:2000],
                json.dumps(context or {}, ensure_ascii=False),
            ),
        )
        row = conn.execute(
            """
            SELECT COUNT(*) AS c
            FROM failure_events
            WHERE fingerprint = ?
            """,
            (fingerprint,),
        ).fetchone()

    occurrences = int(row["c"] or 0)
    category = _classify(message)
    adaptations = _apply_safe_learning(
        stage=stage,
        category=category,
        occurrences=occurrences,
    )
    return {
        "fingerprint": fingerprint,
        "occurrences": occurrences,
        "category": category,
        "adaptations": adaptations,
    }


def _apply_safe_learning(
    *,
    stage: str,
    category: str,
    occurrences: int,
) -> list[str]:
    """
    自動適用するのは品質を壊しにくい負荷軽減だけ。
    コード変更や公開設定変更は自動では行わない。
    """
    applied: list[str] = []

    if category == "gpu" and occurrences >= 2:
        current = get_channel_state(
            "learned_scene_images_override",
            "",
        ).strip()
        if current != "1":
            set_channel_state(
                "learned_scene_images_override",
                "1",
            )
            applied.append(
                "GPU競合が繰り返されたためシーン画像数を一時的に1へ削減"
            )

    if (
        stage.startswith("ai_video")
        and category == "gpu"
        and occurrences >= 2
    ):
        if get_channel_state("ai_video_enabled", "").lower() != "false":
            set_channel_state("ai_video_enabled", "false")
            applied.append(
                "AI動画のGPU失敗が繰り返されたためAI動画を自動OFF"
            )

    return applied


def effective_scene_image_count() -> int:
    if get_channel_state("runtime_resource_mode", "").strip() == "urgent":
        return 1

    raw = get_channel_state(
        "learned_scene_images_override",
        "",
    ).strip()
    if raw:
        try:
            return max(1, min(int(raw), 4))
        except ValueError:
            pass
    return max(
        1,
        min(int(settings.studio_scene_images_per_video), 4),
    )


def recent_failures(limit: int = 20) -> list[dict]:
    _ensure_tables()
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT
                id, created_at, stage, fingerprint,
                message, context_json
            FROM failure_events
            ORDER BY id DESC
            LIMIT ?
            """,
            (max(1, min(int(limit), 100)),),
        ).fetchall()

    result: list[dict] = []
    for row in rows:
        item = dict(row)
        try:
            item["context"] = json.loads(
                item.pop("context_json") or "{}"
            )
        except Exception:
            item["context"] = {}
            item.pop("context_json", None)
        result.append(item)
    return result


def failure_summary() -> list[dict]:
    _ensure_tables()
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT
                stage,
                fingerprint,
                MAX(message) AS message,
                COUNT(*) AS occurrences,
                MAX(created_at) AS last_seen
            FROM failure_events
            GROUP BY stage, fingerprint
            ORDER BY occurrences DESC, last_seen DESC
            LIMIT 15
            """
        ).fetchall()
    return [dict(row) for row in rows]


def _process_improvement_actions(data: dict) -> tuple[list[str], list[int], list[dict]]:
    applied: list[str] = []
    approvals: list[int] = []
    queued_tests: list[dict] = []

    actions = data.get("actions") or []
    if not isinstance(actions, list):
        return applied, approvals, queued_tests

    for raw in actions[:12]:
        if not isinstance(raw, dict):
            continue

        action_type = str(raw.get("action_type") or "").strip()
        if not action_type:
            continue

        mode = classify_action(action_type)
        value = raw.get("value")
        title = str(
            raw.get("title")
            or raw.get("action")
            or action_type
        )
        reason = str(raw.get("reason") or "")

        if mode == "auto":
            if action_type == "script_guidance":
                set_channel_state(
                    "autonomous_script_guidance",
                    str(value or title)[:2000],
                )
                applied.append("話し方・台本改善を自動反映")
            elif action_type == "planner_guidance":
                set_channel_state(
                    "autonomous_planner_guidance",
                    str(value or title)[:2000],
                )
                applied.append("企画改善を自動反映")
            elif action_type == "reduce_scene_images":
                try:
                    count = max(1, min(int(value), 4))
                except Exception:
                    count = 1
                current = effective_scene_image_count()
                count = min(count, current)
                set_channel_state(
                    "learned_scene_images_override",
                    str(count),
                )
                applied.append(
                    f"省負荷のためシーン画像数を{count}へ自動調整"
                )
            elif action_type == "disable_ai_video":
                set_channel_state("ai_video_enabled", "false")
                applied.append("AI動画を自動OFFして負荷を軽減")
            elif action_type in {
                "knowledge_note",
                "prompt_tuning",
                "quality_analysis",
                "retry_tuning",
                "reduce_ai_video_load",
            }:
                notes_raw = get_channel_state(
                    "autonomous_learning_notes",
                    "[]",
                )
                try:
                    notes = json.loads(notes_raw)
                    if not isinstance(notes, list):
                        notes = []
                except Exception:
                    notes = []
                notes.append(
                    {
                        "created_at": datetime.now(
                            timezone.utc
                        ).isoformat(timespec="seconds"),
                        "type": action_type,
                        "value": value,
                        "reason": reason,
                    }
                )
                set_channel_state(
                    "autonomous_learning_notes",
                    json.dumps(
                        notes[-50:],
                        ensure_ascii=False,
                    ),
                )
                applied.append(f"{action_type}を学習メモへ保存")
            continue

        if mode == "test_then_auto":
            queued = {
                "action_type": action_type,
                "title": title,
                "reason": reason,
                "value": value,
                "status": "test_required",
            }
            queued_tests.append(queued)
            continue

        request_id = request_approval(
            action_type=action_type,
            title=title,
            reason=reason,
            payload={
                "value": value,
                **(
                    value
                    if isinstance(value, dict)
                    else {}
                ),
            },
        )
        approvals.append(request_id)

    if queued_tests:
        set_channel_state(
            "autonomous_test_queue",
            json.dumps(
                queued_tests,
                ensure_ascii=False,
            ),
        )

    return applied, approvals, queued_tests


def run_improvement_review() -> dict:
    failures = failure_summary()
    analytics = analytics_history(12)
    strategy = get_channel_state("growth_strategy", "")

    fallback = {
        "summary": (
            "失敗履歴と動画成績を蓄積中です。"
            "同じGPU失敗が繰り返された場合は負荷を自動で下げます。"
        ),
        "recommendations": [],
        "actions": [],
        "requires_code_change": False,
    }

    client = OllamaClient()
    if not client.available():
        set_channel_state(
            "ai_improvement_report",
            json.dumps(fallback, ensure_ascii=False),
        )
        return fallback

    prompt = f"""
あなたは自律型AI YouTuber制作アプリの改善担当AIです。
失敗を繰り返さず、PC負荷を抑え、動画品質と会話品質を上げる改善案を出します。

現在の失敗集計:
{json.dumps(failures, ensure_ascii=False)}

最近の動画分析:
{json.dumps(analytics, ensure_ascii=False)}

現在の成長戦略:
{strategy}

重要ルール:
- 同時に重いGPU処理を実行しない
- GTX 1070 8GBを前提にする
- 投稿公開設定や課金を勝手に変更しない
- コード変更が必要な案はrequires_code_change=trueにする
- 同じ失敗への再発防止を優先
- 動画品質、字幕、画像、話し方、テンポの改善も考える
- 安全な学習/企画/話し方/負荷軽減はactionsに構造化する
- 負荷増加・大型モデル・課金・外部契約・大幅コード変更・削除・公開範囲変更は必ず承認対象にする
- 小さい非破壊コード改善はminor_code_changeとして提案できるが、実行前テストが必要
- JSONだけ返す

{{
  "summary":"...",
  "recommendations":[
    {{
      "area":"resource|video|image|voice|script|upload|code",
      "priority":"high|medium|low",
      "action":"...",
      "reason":"..."
    }}
  ],
  "actions":[
    {
      "action_type":"script_guidance|planner_guidance|reduce_scene_images|disable_ai_video|knowledge_note|prompt_tuning|quality_analysis|retry_tuning|reduce_ai_video_load|minor_code_change|major_code_change|paid_service|purchase|subscription|credential_change|security_change|database_migration|delete_data|public_upload_change|increase_resource_load|large_model_download|external_account_change",
      "title":"...",
      "value":"文字列・数値・またはJSON",
      "reason":"..."
    }
  ],
  "requires_code_change":false
}}
"""
    try:
        data = client.generate_json(prompt)
        if not isinstance(data, dict):
            raise ValueError("invalid improvement review")
    except Exception:
        data = fallback

    applied, approvals, queued_tests = _process_improvement_actions(data)
    data["applied_actions"] = applied
    data["approval_request_ids"] = approvals
    data["test_then_auto_queue"] = queued_tests

    set_channel_state(
        "ai_improvement_report",
        json.dumps(data, ensure_ascii=False),
    )
    set_channel_state(
        "ai_improvement_reviewed_at",
        datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )
    return data


def improvement_state() -> dict:
    raw = get_channel_state("ai_improvement_report", "")
    try:
        report = json.loads(raw) if raw else {}
    except Exception:
        report = {}

    return {
        "report": report,
        "reviewed_at": get_channel_state(
            "ai_improvement_reviewed_at",
            "",
        ),
        "scene_images": effective_scene_image_count(),
        "script_guidance": get_channel_state(
            "autonomous_script_guidance",
            "",
        ),
        "planner_guidance": get_channel_state(
            "autonomous_planner_guidance",
            "",
        ),
        "test_queue": get_channel_state(
            "autonomous_test_queue",
            "[]",
        ),
        "recent_failures": recent_failures(8),
    }


def maybe_run_improvement_review(min_hours: int = 12) -> dict | None:
    raw = get_channel_state("ai_improvement_reviewed_at", "").strip()
    if raw:
        try:
            last = datetime.fromisoformat(raw)
            if last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
            age_hours = (
                datetime.now(timezone.utc) - last
            ).total_seconds() / 3600
            if age_hours < max(1, int(min_hours)):
                return None
        except Exception:
            pass
    return run_improvement_review()
