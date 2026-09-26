from __future__ import annotations

import csv
import json
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from native_models.brain import corpus_status
from native_models.common import component_training_root
from native_models.image_v0 import dataset_status as image_dataset_status
from native_models.video_v0 import dataset_status as video_dataset_status
from paths import AUDIO_DIR
from storage import (
    analytics_history,
    connect,
    get_channel_state,
    set_channel_state,
)
from mirai_engines.visual_learning import VisualLearningMemory
from voice.model_manager import inspect_training_dataset


FINAL_CHECKPOINT_HOURS = 168
MIN_TEACHER_SCORE = 55.0
STRONG_TEACHER_SCORE = 70.0


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _ensure_tables() -> None:
    with connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS evolution_examples (
                video_id INTEGER PRIMARY KEY,
                harvested_at TEXT NOT NULL,
                analytics_score REAL NOT NULL DEFAULT 0,
                outcome TEXT NOT NULL,
                brain_added INTEGER NOT NULL DEFAULT 0,
                image_added INTEGER NOT NULL DEFAULT 0,
                video_added INTEGER NOT NULL DEFAULT 0,
                voice_strategy_updated INTEGER NOT NULL DEFAULT 0,
                payload_json TEXT NOT NULL DEFAULT '{}'
            );

            CREATE TABLE IF NOT EXISTS model_evolution_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                component TEXT NOT NULL,
                event_type TEXT NOT NULL,
                status TEXT NOT NULL,
                detail_json TEXT NOT NULL DEFAULT '{}'
            );

            CREATE INDEX IF NOT EXISTS idx_model_evolution_component
            ON model_evolution_events(component, created_at);
            """
        )



def _video_learning_record(video_id: int) -> dict | None:
    with connect() as conn:
        row = conn.execute(
            """
            SELECT
                id, created_at, idea, angle, title, script,
                description, tags_json, output_path,
                youtube_video_id, uploaded_at,
                views, likes, comments, avg_view_percentage
            FROM videos
            WHERE id = ?
            """,
            (int(video_id),),
        ).fetchone()
    return dict(row) if row else None


def _outcome(score: float) -> str:
    if score >= STRONG_TEACHER_SCORE:
        return "strong"
    if score >= MIN_TEACHER_SCORE:
        return "usable"
    return "weak"


def _safe_text(value: Any) -> str:
    return str(value or "").replace("\x00", "").strip()


def _append_unique_metadata(path: Path, first: str, second: str) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    existing: set[str] = set()
    if path.is_file():
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            for row in csv.reader(handle, delimiter="|"):
                if row:
                    existing.add(str(row[0]).strip())
    if first in existing:
        return False
    with path.open("a", encoding="utf-8", newline="") as handle:
        writer = csv.writer(
            handle,
            delimiter="|",
            lineterminator="\n",
        )
        writer.writerow([first, second])
    return True


def _brain_teacher(video: dict, score: float, note: str) -> bool:
    if score < MIN_TEACHER_SCORE:
        return False
    root = component_training_root("brain") / "auto"
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"youtube_{int(video['id'])}.txt"
    if path.is_file():
        return False
    payload = (
        "### 成功実績から学ぶYouTube例\n"
        f"企画: {_safe_text(video.get('idea'))}\n"
        f"切り口: {_safe_text(video.get('angle'))}\n"
        f"タイトル: {_safe_text(video.get('title'))}\n"
        f"台本:\n{_safe_text(video.get('script'))}\n"
        f"説明文:\n{_safe_text(video.get('description'))}\n"
        f"7日評価: {float(score):.2f}/100\n"
        f"学習メモ: {_safe_text(note)}\n"
        "ルール: 内容の丸写しではなく、成功した構成・フック・"
        "説明順・言葉の密度だけを別テーマへ応用する。\n"
    )
    path.write_text(payload, encoding="utf-8")
    return True


def _copy_native_image_teacher(
    *,
    video_id: int,
    source_raw: str,
    caption: str,
) -> bool:
    source = Path(source_raw)
    if not source.is_file():
        return False
    memory = VisualLearningMemory()
    row = memory.find_by_path(source)
    if not row:
        return False
    backend = str(row.get("backend") or "").lower()
    if not backend.startswith("mirai-native-image"):
        return False
    if not bool(row.get("accepted")):
        return False

    root = component_training_root("image")
    target_dir = root / "auto"
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / (
        f"youtube_{video_id}_{source.name}"
    )
    if not target.is_file():
        shutil.copy2(source, target)
    rel = target.relative_to(root).as_posix()
    return _append_unique_metadata(
        root / "metadata.csv",
        rel,
        caption,
    )


def _extract_native_video_teacher(
    *,
    video_id: int,
    source_raw: str,
    caption: str,
) -> bool:
    source = Path(source_raw)
    if not source.is_file():
        return False
    memory = VisualLearningMemory()
    row = memory.find_by_path(source)
    if not row:
        return False
    backend = str(row.get("backend") or "").lower()
    if not backend.startswith("mirai-native-video"):
        return False
    if not bool(row.get("accepted")):
        return False

    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return False

    root = component_training_root("video")
    clip_dir = root / "auto" / f"youtube_{video_id}"
    frame_paths = sorted(clip_dir.glob("frame_*.png"))
    if len(frame_paths) < 2:
        clip_dir.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            [
                ffmpeg,
                "-y",
                "-i",
                str(source),
                "-vf",
                "fps=4,scale=128:128:force_original_aspect_ratio=decrease,"
                "pad=128:128:(ow-iw)/2:(oh-ih)/2",
                "-frames:v",
                "12",
                str(clip_dir / "frame_%03d.png"),
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        frame_paths = sorted(clip_dir.glob("frame_*.png"))
    if len(frame_paths) < 2:
        return False
    rel = clip_dir.relative_to(root).as_posix()
    return _append_unique_metadata(
        root / "metadata.csv",
        rel,
        caption,
    )


def _voice_strategy(
    *,
    score: float,
    metrics: dict,
    title: str,
) -> dict:
    retention = float(
        metrics.get("averageViewPercentage") or 0.0
    )
    current_raw = get_channel_state(
        "voice_evolution_strategy",
        "",
    )
    try:
        current = (
            json.loads(current_raw)
            if current_raw
            else {}
        )
    except Exception:
        current = {}

    samples = int(current.get("samples") or 0) + 1
    avg_retention = float(
        current.get("avg_retention") or 0.0
    )
    avg_retention = (
        (avg_retention * (samples - 1) + retention)
        / samples
    )

    if retention >= 75:
        guidance = (
            "現在のテンポを維持。冒頭は短く、感情変化を"
            "入れすぎず聞き取りやすさを優先。"
        )
    elif retention >= 55:
        guidance = (
            "冒頭と文間の間を約10%短くし、重要語だけ"
            "抑揚を強めて再検証。"
        )
    else:
        guidance = (
            "話速を上げすぎず、冒頭の無音と長い間を削減。"
            "1文を短くして音声密度を上げる。"
        )

    result = {
        "samples": samples,
        "avg_retention": round(avg_retention, 2),
        "last_retention": round(retention, 2),
        "last_score": round(float(score), 2),
        "last_title": title[:200],
        "guidance": guidance,
        "updated_at": _now(),
    }
    set_channel_state(
        "voice_evolution_strategy",
        json.dumps(result, ensure_ascii=False),
    )
    return result


def _write_event(
    component: str,
    event_type: str,
    status: str,
    detail: dict,
) -> None:
    _ensure_tables()
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO model_evolution_events (
                created_at, component, event_type,
                status, detail_json
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                _now(),
                component,
                event_type,
                status,
                json.dumps(detail, ensure_ascii=False),
            ),
        )


def harvest_final_video(
    video_id: int,
    *,
    metrics: dict,
    score: float,
    note: str,
) -> dict:
    """
    7日評価が確定した動画を全工程の学習へ反映する。

    - Brain: 成功した企画/台本/metadataを自動教師化。
    - Image/Video: 自作Native backendで生成した素材だけを自動教師化。
      外部生成物を黙ってNative学習へ流さない。
    - Voice: YouTube成績から話速/間/抑揚の戦略を学習。
      WAV重み学習は権利クリア済みvoice_trainingだけを使う。
    """
    _ensure_tables()
    with connect() as conn:
        exists = conn.execute(
            "SELECT 1 FROM evolution_examples WHERE video_id = ?",
            (int(video_id),),
        ).fetchone()
    if exists:
        return {
            "video_id": int(video_id),
            "status": "already_harvested",
        }

    video = _video_learning_record(int(video_id))
    if not video:
        raise KeyError(f"video not found: {video_id}")

    outcome = _outcome(float(score))
    brain_added = _brain_teacher(
        video,
        float(score),
        note,
    )

    visual = VisualLearningMemory().video_profile(int(video_id))
    asset_paths = visual.get("asset_paths") or {}
    caption = (
        f"{_safe_text(video.get('title'))}, "
        f"{_safe_text(video.get('idea'))}"
    ).strip(", ")

    image_added_count = 0
    if float(score) >= MIN_TEACHER_SCORE:
        candidates: list[str] = []
        for key in ("mirai", "guest"):
            raw = _safe_text(asset_paths.get(key))
            if raw:
                candidates.append(raw)
        candidates.extend(
            _safe_text(value)
            for value in asset_paths.get("backgrounds") or []
            if _safe_text(value)
        )
        for raw in candidates:
            try:
                if _copy_native_image_teacher(
                    video_id=int(video_id),
                    source_raw=raw,
                    caption=caption,
                ):
                    image_added_count += 1
            except Exception as exc:
                _write_event(
                    "image",
                    "teacher_harvest",
                    "error",
                    {"video_id": video_id, "error": str(exc)},
                )

    video_added = False
    ai_video_path = _safe_text(asset_paths.get("ai_video"))
    if (
        float(score) >= MIN_TEACHER_SCORE
        and ai_video_path
    ):
        try:
            video_added = _extract_native_video_teacher(
                video_id=int(video_id),
                source_raw=ai_video_path,
                caption=caption,
            )
        except Exception as exc:
            _write_event(
                "video",
                "teacher_harvest",
                "error",
                {"video_id": video_id, "error": str(exc)},
            )

    voice = _voice_strategy(
        score=float(score),
        metrics=metrics,
        title=_safe_text(video.get("title")),
    )

    payload = {
        "score": float(score),
        "outcome": outcome,
        "metrics": metrics,
        "visual_profile": visual,
        "voice_strategy": voice,
        "rights_rule": (
            "brain=own channel text; image/video=native backend only; "
            "voice weights=rights-cleared training WAV only"
        ),
    }
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO evolution_examples (
                video_id, harvested_at, analytics_score, outcome,
                brain_added, image_added, video_added,
                voice_strategy_updated, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(video_id),
                _now(),
                float(score),
                outcome,
                int(brain_added),
                int(image_added_count),
                int(video_added),
                1,
                json.dumps(payload, ensure_ascii=False),
            ),
        )

    result = {
        "video_id": int(video_id),
        "status": "harvested",
        "outcome": outcome,
        "brain_added": brain_added,
        "image_added": image_added_count,
        "video_added": video_added,
        "voice_strategy_updated": True,
    }
    _write_event(
        "all",
        "teacher_harvest",
        "success",
        result,
    )
    return result


def harvest_pending_final_examples(limit: int = 100) -> list[dict]:
    _ensure_tables()
    history = analytics_history(limit=max(1, int(limit)))
    latest: dict[int, dict] = {}
    for row in history:
        if int(row.get("checkpoint_hours") or 0) < FINAL_CHECKPOINT_HOURS:
            continue
        latest.setdefault(int(row["video_id"]), row)

    with connect() as conn:
        harvested = {
            int(row["video_id"])
            for row in conn.execute(
                "SELECT video_id FROM evolution_examples"
            ).fetchall()
        }

    results: list[dict] = []
    for video_id, row in latest.items():
        if video_id in harvested:
            continue
        metrics = {
            "views": int(row.get("views") or 0),
            "likes": int(row.get("likes") or 0),
            "comments": int(row.get("comments") or 0),
            "averageViewPercentage": float(
                row.get("avg_view_percentage") or 0
            ),
        }
        results.append(
            harvest_final_video(
                video_id,
                metrics=metrics,
                score=float(row.get("score") or 0),
                note=_safe_text(row.get("note")),
            )
        )
    return results


def evolution_status() -> dict:
    _ensure_tables()
    with connect() as conn:
        rows = [
            dict(row)
            for row in conn.execute(
                """
                SELECT outcome, COUNT(*) AS count
                FROM evolution_examples
                GROUP BY outcome
                """
            ).fetchall()
        ]
        total = int(
            conn.execute(
                "SELECT COUNT(*) AS c FROM evolution_examples"
            ).fetchone()["c"]
            or 0
        )

    brain = corpus_status()
    image = image_dataset_status()
    video = video_dataset_status()
    voice = inspect_training_dataset()
    voice_strategy_raw = get_channel_state(
        "voice_evolution_strategy",
        "",
    )
    try:
        voice_strategy = (
            json.loads(voice_strategy_raw)
            if voice_strategy_raw
            else {}
        )
    except Exception:
        voice_strategy = {}

    return {
        "examples": total,
        "outcomes": {
            str(row["outcome"]): int(row["count"])
            for row in rows
        },
        "brain": brain,
        "image": image,
        "video": video,
        "voice": {
            "dataset": voice,
            "strategy": voice_strategy,
        },
        "learning_coverage": {
            "planning": True,
            "script": True,
            "metadata": True,
            "voice_strategy": True,
            "image_strategy": True,
            "video_strategy": True,
            "native_brain_teacher": True,
            "native_image_teacher": "native-generated-only",
            "native_video_teacher": "native-generated-only",
            "native_voice_teacher": "rights-cleared-wav-only",
            "youtube_feedback": True,
            "failure_feedback": True,
        },
    }
