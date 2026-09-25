from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from storage import connect, set_channel_state


@dataclass(frozen=True)
class QualityIssue:
    code: str
    severity: str
    message: str


@dataclass(frozen=True)
class QualityReport:
    passed: bool
    score: int
    issues: list[QualityIssue]
    metrics: dict[str, Any]


def _rate(value: str | None) -> float:
    raw = str(value or "0/1")
    try:
        if "/" in raw:
            left, right = raw.split("/", 1)
            denom = float(right)
            return float(left) / denom if denom else 0.0
        return float(raw)
    except Exception:
        return 0.0


def analyze_probe_data(
    probe: dict,
    *,
    composition_plan: dict | None = None,
    file_size: int | None = None,
) -> QualityReport:
    issues: list[QualityIssue] = []
    score = 100

    streams = probe.get("streams") or []
    video = next((row for row in streams if row.get("codec_type") == "video"), None)
    audio = next((row for row in streams if row.get("codec_type") == "audio"), None)
    fmt = probe.get("format") or {}

    if video is None:
        issues.append(QualityIssue("video_missing", "critical", "映像ストリームがありません。"))
        score -= 60
    if audio is None:
        issues.append(QualityIssue("audio_missing", "critical", "音声ストリームがありません。"))
        score -= 45

    width = int((video or {}).get("width") or 0)
    height = int((video or {}).get("height") or 0)
    fps = _rate((video or {}).get("avg_frame_rate") or (video or {}).get("r_frame_rate"))
    try:
        duration = float(fmt.get("duration") or 0)
    except Exception:
        duration = 0.0

    if video is not None and not (height > width > 0):
        issues.append(QualityIssue("not_vertical", "critical", "縦動画になっていません。"))
        score -= 35

    if video is not None and (width < 720 or height < 1280):
        issues.append(QualityIssue("low_resolution", "warning", "解像度がShorts向け基準より低めです。"))
        score -= 12

    if video is not None and fps < 24:
        issues.append(QualityIssue("low_fps", "warning", "フレームレートが24fps未満です。"))
        score -= 10

    if duration <= 1.0:
        issues.append(QualityIssue("duration_too_short", "critical", "動画尺が1秒以下です。"))
        score -= 50
    elif duration > 180:
        issues.append(QualityIssue("duration_long", "warning", "自動Shortsとしては動画尺が長めです。"))
        score -= 8

    if file_size is not None and file_size < 50_000:
        issues.append(QualityIssue("file_too_small", "critical", "完成動画ファイルが小さすぎます。"))
        score -= 40

    scenes = (composition_plan or {}).get("scenes") or []
    if composition_plan is not None:
        if not scenes:
            issues.append(QualityIssue("scene_plan_missing", "critical", "動画構成シーンがありません。"))
            score -= 35
        if len(scenes) > 24:
            issues.append(QualityIssue("too_many_scenes", "warning", "場面切替が多すぎます。"))
            score -= 7
        for scene in scenes:
            text = str(scene.get("text") or "")
            if len(text) > 42:
                issues.append(QualityIssue("subtitle_too_long", "warning", "1画面の字幕が長すぎます。"))
                score -= 8
                break
        if scenes:
            durations = [float(scene.get("duration") or 0) for scene in scenes]
            if min(durations) < 0.45:
                issues.append(QualityIssue("scene_too_fast", "warning", "一部シーンの表示時間が短すぎます。"))
                score -= 6

    score = max(0, min(int(score), 100))
    passed = not any(issue.severity == "critical" for issue in issues) and score >= 70

    return QualityReport(
        passed=passed,
        score=score,
        issues=issues,
        metrics={
            "width": width,
            "height": height,
            "fps": round(fps, 3),
            "duration": round(duration, 3),
            "file_size": file_size,
            "video_codec": (video or {}).get("codec_name"),
            "audio_codec": (audio or {}).get("codec_name"),
            "scene_count": len(scenes),
        },
    )


class MiraiQualityEngine:
    def _ensure_table(self) -> None:
        with connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS quality_reports (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    video_id INTEGER,
                    passed INTEGER NOT NULL,
                    score INTEGER NOT NULL,
                    report_json TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_quality_video
                ON quality_reports(video_id);

                CREATE INDEX IF NOT EXISTS idx_quality_created
                ON quality_reports(created_at);
                """
            )

    def inspect(
        self,
        video_path: str | Path,
        *,
        video_id: int | None = None,
        composition_plan: dict | None = None,
    ) -> dict:
        path = Path(video_path)
        if not path.is_file():
            report = QualityReport(
                passed=False,
                score=0,
                issues=[QualityIssue("file_missing", "critical", "完成動画ファイルがありません。")],
                metrics={"path": str(path)},
            )
            return self._save(video_id, report)

        ffprobe = shutil.which("ffprobe")
        if not ffprobe:
            report = QualityReport(
                passed=False,
                score=20,
                issues=[QualityIssue("ffprobe_missing", "critical", "ffprobeが見つからず品質検査できません。")],
                metrics={"path": str(path), "file_size": path.stat().st_size},
            )
            return self._save(video_id, report)

        result = subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-show_streams",
                "-show_format",
                "-of",
                "json",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )
        probe = json.loads(result.stdout or "{}")
        report = analyze_probe_data(
            probe,
            composition_plan=composition_plan,
            file_size=path.stat().st_size,
        )
        return self._save(video_id, report)

    def _save(self, video_id: int | None, report: QualityReport) -> dict:
        self._ensure_table()
        payload = {
            "passed": report.passed,
            "score": report.score,
            "issues": [asdict(issue) for issue in report.issues],
            "metrics": report.metrics,
        }
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with connect() as conn:
            conn.execute(
                """
                INSERT INTO quality_reports (
                    created_at, video_id, passed, score, report_json
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    now,
                    video_id,
                    1 if report.passed else 0,
                    report.score,
                    json.dumps(payload, ensure_ascii=False),
                ),
            )

        if not report.passed:
            codes = [issue.code for issue in report.issues]
            set_channel_state(
                "last_quality_failure",
                json.dumps(
                    {
                        "video_id": video_id,
                        "score": report.score,
                        "issues": codes,
                        "created_at": now,
                    },
                    ensure_ascii=False,
                ),
            )
        return payload

    def recent(self, limit: int = 20) -> list[dict]:
        self._ensure_table()
        with connect() as conn:
            rows = conn.execute(
                """
                SELECT created_at, video_id, passed, score, report_json
                FROM quality_reports
                ORDER BY id DESC
                LIMIT ?
                """,
                (max(1, min(int(limit), 100)),),
            ).fetchall()
        result: list[dict] = []
        for row in rows:
            try:
                report = json.loads(row["report_json"])
            except Exception:
                report = {}
            report.update(
                {
                    "created_at": row["created_at"],
                    "video_id": row["video_id"],
                    "passed": bool(row["passed"]),
                    "score": int(row["score"]),
                }
            )
            result.append(report)
        return result
