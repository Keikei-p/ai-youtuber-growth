from __future__ import annotations

import json
import re
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from difflib import SequenceMatcher
from typing import Any

from storage import connect, recent_videos


@dataclass(frozen=True)
class EditorialReport:
    passed: bool
    score: int
    dimensions: dict[str, int]
    notes: list[str]


def _keywords(text: str) -> set[str]:
    return {
        token.lower()
        for token in re.findall(
            r"[A-Za-z]{3,}|[一-龯ぁ-んァ-ヶー]{2,10}",
            str(text or ""),
        )
        if token.lower() not in {
            "youtube", "shorts", "short", "動画", "今回", "今日",
        }
    }


def _overlap_score(left: str, right: str) -> int:
    a = _keywords(left)
    b = _keywords(right)
    if not a or not b:
        return 60
    ratio = len(a & b) / max(1, min(len(a), len(b)))
    return max(35, min(100, int(45 + ratio * 65)))


def _originality(script: str, *, video_id: int | None) -> tuple[int, float]:
    normalized = re.sub(r"\s+", "", str(script or ""))
    if not normalized:
        return 0, 1.0
    worst = 0.0
    for row in recent_videos(25):
        if video_id and int(row.get("id") or 0) == int(video_id):
            continue
        old = re.sub(r"\s+", "", str(row.get("script") or ""))
        if not old:
            continue
        worst = max(
            worst,
            SequenceMatcher(None, normalized, old).ratio(),
        )
    if worst >= 0.92:
        return 20, worst
    if worst >= 0.82:
        return 50, worst
    if worst >= 0.70:
        return 72, worst
    return 95, worst


def evaluate_editorial_quality(
    item: dict,
    *,
    technical_quality: dict | None = None,
) -> dict[str, Any]:
    title = str(item.get("title") or "").strip()
    script = str(item.get("script") or "").strip()
    idea = item.get("idea") or {}
    if not isinstance(idea, dict):
        idea = {"idea": str(idea)}
    hook = str(idea.get("hook") or "").strip()
    thumbnail = item.get("thumbnail") or {}
    composition = item.get("composition_plan") or {}
    technical = technical_quality or item.get("quality") or {}

    sentences = [
        value.strip()
        for value in re.split(r"[。！？!?]", script)
        if value.strip()
    ]
    first = sentences[0] if sentences else script[:60]
    avg_sentence = (
        sum(len(value) for value in sentences) / len(sentences)
        if sentences else len(script)
    )

    hook_score = 55
    if hook and _overlap_score(hook, first) >= 65:
        hook_score = 92
    elif len(first) <= 42:
        hook_score = 78
    if len(first) > 70:
        hook_score -= 20

    interest_score = 62
    if any(mark in script[:100] for mark in ("？", "?", "まさか", "結果", "実験", "失敗")):
        interest_score += 16
    if any(mark in title for mark in ("？", "?", "結果", "理由", "どうなる")):
        interest_score += 8
    interest_score = min(100, interest_score)

    pacing_score = 90 if 90 <= len(script) <= 280 else 65
    if avg_sentence <= 42:
        pacing_score += 5
    elif avg_sentence > 65:
        pacing_score -= 18
    pacing_score = max(30, min(100, pacing_score))

    retention_score = int(round(
        hook_score * 0.55 + pacing_score * 0.45
    ))

    visual_score = int(
        technical.get("score")
        or thumbnail.get("score")
        or 60
    )
    audio_score = 90 if item.get("audio_path") else 45

    composition_warnings = item.get("composition_warnings") or []
    scenes = composition.get("scenes") or []
    subtitle_score = 92
    if composition_warnings:
        subtitle_score -= min(35, len(composition_warnings) * 9)
    if any(
        len(str(scene.get("text") or "")) > 42
        for scene in scenes
        if isinstance(scene, dict)
    ):
        subtitle_score -= 18
    subtitle_score = max(35, subtitle_score)

    story_score = 60
    if len(sentences) >= 3:
        story_score += 18
    if len(sentences) >= 5:
        story_score += 8
    if any(word in script[-90:] for word in ("次", "コメント", "教えて", "見守", "どう思")):
        story_score += 8
    story_score = min(100, story_score)

    title_source = " ".join([
        str(idea.get("idea") or ""),
        str(idea.get("angle") or ""),
        script,
    ])
    title_match = _overlap_score(title, title_source)

    thumbnail_score = int(thumbnail.get("score") or 60)
    if item.get("thumbnail_path"):
        thumbnail_score = max(thumbnail_score, 72)

    originality_score, duplicate_ratio = _originality(
        script,
        video_id=int(item.get("id") or 0) or None,
    )

    next_episode_score = 55
    if any(
        word in script[-100:]
        for word in ("次", "コメント", "教えて", "見守", "続き")
    ):
        next_episode_score = 88

    dimensions = {
        "hook": max(0, min(100, hook_score)),
        "interest": interest_score,
        "pacing": pacing_score,
        "retention_expectation": retention_score,
        "visual": max(0, min(100, visual_score)),
        "audio": audio_score,
        "subtitle": subtitle_score,
        "story": story_score,
        "title_match": title_match,
        "thumbnail": max(0, min(100, thumbnail_score)),
        "originality": originality_score,
        "next_episode": next_episode_score,
    }

    weights = {
        "hook": 1.25,
        "interest": 1.0,
        "pacing": 1.1,
        "retention_expectation": 1.15,
        "visual": 1.0,
        "audio": 0.85,
        "subtitle": 0.8,
        "story": 0.9,
        "title_match": 1.0,
        "thumbnail": 0.85,
        "originality": 1.15,
        "next_episode": 0.7,
    }
    weighted = sum(
        dimensions[key] * weights[key]
        for key in dimensions
    )
    total_weight = sum(weights.values())
    score = int(round(weighted / total_weight))

    notes: list[str] = []
    if dimensions["hook"] < 60:
        notes.append("冒頭フックを短く強くする")
    if dimensions["pacing"] < 60:
        notes.append("文章と1文の長さを圧縮する")
    if dimensions["title_match"] < 55:
        notes.append("タイトルと内容の一致を強める")
    if dimensions["originality"] < 55:
        notes.append("直近動画との重複を避ける")
    if dimensions["thumbnail"] < 55:
        notes.append("サムネイル主役と文字量を見直す")

    # 強い重複、極端なタイトル不一致、低総合点だけを投稿禁止。
    critical = (
        dimensions["originality"] < 35
        or dimensions["title_match"] < 40
        or score < 58
    )
    return {
        "passed": not critical,
        "score": max(0, min(100, score)),
        "dimensions": dimensions,
        "notes": notes,
        "duplicate_ratio": round(duplicate_ratio, 3),
    }


class MiraiEditorialQualityEngine:
    def _ensure_table(self) -> None:
        with connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS editorial_quality_reports (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    video_id INTEGER,
                    passed INTEGER NOT NULL,
                    score INTEGER NOT NULL,
                    report_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_editorial_quality_video
                ON editorial_quality_reports(video_id, id DESC);
                """
            )

    def inspect(
        self,
        item: dict,
        *,
        technical_quality: dict | None = None,
    ) -> dict[str, Any]:
        report = evaluate_editorial_quality(
            item,
            technical_quality=technical_quality,
        )
        self._ensure_table()
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with connect() as conn:
            conn.execute(
                """
                INSERT INTO editorial_quality_reports (
                    created_at, video_id, passed, score, report_json
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    now,
                    int(item.get("id") or 0) or None,
                    1 if report["passed"] else 0,
                    int(report["score"]),
                    json.dumps(report, ensure_ascii=False),
                ),
            )
        return report

    def recent(self, limit: int = 20) -> list[dict[str, Any]]:
        self._ensure_table()
        with connect() as conn:
            rows = conn.execute(
                """
                SELECT created_at, video_id, passed, score, report_json
                FROM editorial_quality_reports
                ORDER BY id DESC
                LIMIT ?
                """,
                (max(1, min(int(limit), 100)),),
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            try:
                report = json.loads(row["report_json"] or "{}")
            except Exception:
                report = {}
            report.update({
                "created_at": row["created_at"],
                "video_id": row["video_id"],
                "passed": bool(row["passed"]),
                "score": int(row["score"]),
            })
            result.append(report)
        return result
