from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from mirai_engines.visual_learning import VisualLearningMemory
from paths import AUDIO_DIR, VIDEO_DIR
from storage import (
    active_guests,
    clear_video_output,
    connect,
    snapshot_exists,
    video_by_id,
)
from studio.asset_store import GENERATED_ROOT, prune_missing_assets


FINAL_CHECKPOINT_HOURS = 168


def _ensure_table() -> None:
    with connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS learning_media_cleanup (
                video_id INTEGER PRIMARY KEY,
                cleaned_at TEXT NOT NULL,
                removed_files INTEGER NOT NULL DEFAULT 0,
                freed_bytes INTEGER NOT NULL DEFAULT 0,
                summary_json TEXT NOT NULL DEFAULT '{}'
            )
            """
        )


def _flatten_paths(value) -> list[str]:
    result: list[str] = []
    if isinstance(value, str):
        if value.strip():
            result.append(value)
    elif isinstance(value, (list, tuple, set)):
        for item in value:
            result.extend(_flatten_paths(item))
    elif isinstance(value, dict):
        for item in value.values():
            result.extend(_flatten_paths(item))
    return result


def _safe_roots() -> tuple[Path, ...]:
    return (
        VIDEO_DIR.resolve(),
        AUDIO_DIR.resolve(),
        GENERATED_ROOT.resolve(),
    )


def _safe_path(raw: str | Path | None) -> Path | None:
    if not raw:
        return None
    try:
        path = Path(raw).resolve()
    except Exception:
        return None
    for root in _safe_roots():
        try:
            path.relative_to(root)
            return path
        except ValueError:
            continue
    return None


def _active_guest_paths() -> set[Path]:
    paths: set[Path] = set()
    for guest in active_guests(500):
        raw = str(guest.get("image_path") or "").strip()
        if not raw:
            continue
        try:
            paths.add(Path(raw).resolve())
        except Exception:
            pass
    return paths


def _pending_referenced_paths(exclude_video_id: int) -> set[Path]:
    """
    7日学習がまだ終わっていない別動画が使う素材は保護する。
    """
    memory = VisualLearningMemory()
    memory._ensure_tables()
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT p.video_id, p.profile_json
            FROM video_visual_profiles p
            WHERE p.video_id != ?
              AND NOT EXISTS (
                SELECT 1
                FROM analytics_snapshots s
                WHERE s.video_id = p.video_id
                  AND s.checkpoint_hours = ?
              )
            """,
            (int(exclude_video_id), FINAL_CHECKPOINT_HOURS),
        ).fetchall()

    protected: set[Path] = set()
    for row in rows:
        try:
            profile = json.loads(row["profile_json"] or "{}")
        except Exception:
            profile = {}
        for raw in _flatten_paths(profile.get("asset_paths") or {}):
            safe = _safe_path(raw)
            if safe is not None:
                protected.add(safe)
    return protected


def _candidate_paths(video_id: int) -> list[Path]:
    memory = VisualLearningMemory()
    profile = memory.video_profile(video_id)
    candidates: list[Path] = []

    for raw in _flatten_paths(profile.get("asset_paths") or {}):
        safe = _safe_path(raw)
        if safe is not None:
            candidates.append(safe)

    video = video_by_id(video_id) or {}
    output = _safe_path(video.get("output_path"))
    if output is not None:
        candidates.append(output)

    if AUDIO_DIR.exists():
        for audio in AUDIO_DIR.glob(f"*_{int(video_id)}.wav"):
            safe = _safe_path(audio)
            if safe is not None:
                candidates.append(safe)

    unique: list[Path] = []
    seen: set[Path] = set()
    for path in candidates:
        if path in seen:
            continue
        seen.add(path)
        unique.append(path)
    return unique


def cleanup_status(video_id: int) -> dict | None:
    _ensure_table()
    with connect() as conn:
        row = conn.execute(
            """
            SELECT cleaned_at, removed_files, freed_bytes, summary_json
            FROM learning_media_cleanup
            WHERE video_id = ?
            """,
            (int(video_id),),
        ).fetchone()
    if not row:
        return None
    try:
        summary = json.loads(row["summary_json"] or "{}")
    except Exception:
        summary = {}
    return {
        **summary,
        "status": "already_cleaned",
        "cleaned_at": row["cleaned_at"],
        "removed_files": int(row["removed_files"] or 0),
        "freed_bytes": int(row["freed_bytes"] or 0),
        "freed_mb": round(int(row["freed_bytes"] or 0) / 1024 / 1024, 2),
    }


def cleanup_after_final_learning(video_id: int) -> dict:
    """
    7日学習完了後に、学習へ不要になったローカル実ファイルだけ削除する。
    学習データ・prompt・quality score・YouTube分析はSQLiteへ残す。
    """
    video_id = int(video_id)
    _ensure_table()

    existing = cleanup_status(video_id)
    if existing:
        return existing

    if not snapshot_exists(video_id, FINAL_CHECKPOINT_HOURS):
        return {
            "status": "not_ready",
            "video_id": video_id,
            "removed_files": 0,
            "freed_bytes": 0,
            "freed_mb": 0.0,
        }

    protected = _pending_referenced_paths(video_id)
    protected.update(_active_guest_paths())
    candidates = _candidate_paths(video_id)

    removed_paths: list[str] = []
    protected_paths: list[str] = []
    freed = 0

    for path in candidates:
        if path in protected:
            protected_paths.append(str(path))
            continue
        try:
            if not path.is_file():
                continue
            size = int(path.stat().st_size)
            path.unlink()
            removed_paths.append(str(path))
            freed += size
        except OSError:
            protected_paths.append(str(path))

    video = video_by_id(video_id) or {}
    raw_output = str(video.get("output_path") or "").strip()
    if raw_output:
        safe_output = _safe_path(raw_output)
        if safe_output is not None and not safe_output.exists():
            clear_video_output(video_id)

    missing_index_rows = prune_missing_assets()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    summary = {
        "status": "cleaned",
        "video_id": video_id,
        "removed_paths": removed_paths,
        "protected_paths": protected_paths,
        "library_rows_removed": int(missing_index_rows),
        "learning_data_preserved": True,
        "final_checkpoint_hours": FINAL_CHECKPOINT_HOURS,
    }

    with connect() as conn:
        conn.execute(
            """
            INSERT INTO learning_media_cleanup (
                video_id, cleaned_at, removed_files,
                freed_bytes, summary_json
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                video_id,
                now,
                len(removed_paths),
                int(freed),
                json.dumps(summary, ensure_ascii=False),
            ),
        )

    VisualLearningMemory().update_video_profile(
        video_id,
        {
            "media_cleaned_at": now,
            "media_cleanup": {
                "removed_files": len(removed_paths),
                "freed_bytes": int(freed),
                "learning_data_preserved": True,
            },
        },
    )

    return {
        **summary,
        "cleaned_at": now,
        "removed_files": len(removed_paths),
        "freed_bytes": int(freed),
        "freed_mb": round(freed / 1024 / 1024, 2),
    }
