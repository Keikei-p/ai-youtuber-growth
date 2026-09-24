from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any

from ai_client import OllamaClient
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
  "requires_code_change":false
}}
"""
    try:
        data = client.generate_json(prompt)
        if not isinstance(data, dict):
            raise ValueError("invalid improvement review")
    except Exception:
        data = fallback

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
        "recent_failures": recent_failures(8),
    }
