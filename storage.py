from __future__ import annotations
import json
import sqlite3
from pathlib import Path
from typing import Any
from paths import DATA_DIR

DB_PATH = DATA_DIR / "memory.db"

def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db() -> None:
    with connect() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS videos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            idea TEXT NOT NULL,
            angle TEXT,
            title TEXT NOT NULL,
            script TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'planned',
            output_path TEXT,
            youtube_video_id TEXT,
            views INTEGER,
            likes INTEGER,
            comments INTEGER,
            avg_view_percentage REAL,
            ctr REAL
        );

        CREATE TABLE IF NOT EXISTS learning_notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            video_id INTEGER,
            note TEXT NOT NULL,
            score REAL,
            FOREIGN KEY(video_id) REFERENCES videos(id)
        );

        CREATE TABLE IF NOT EXISTS posting_queue (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            video_id INTEGER NOT NULL UNIQUE,
            scheduled_for TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'queued',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            uploaded_at TEXT,
            error TEXT,
            attempts INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY(video_id) REFERENCES videos(id)
        );
        """)

        columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(posting_queue)").fetchall()
        }
        if "attempts" not in columns:
            conn.execute(
                "ALTER TABLE posting_queue ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0"
            )

def recent_videos(limit: int = 20) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT
                v.id, v.idea, v.angle, v.title, v.script, v.status,
                v.views, v.likes, v.comments, v.avg_view_percentage, v.ctr,
                (
                    SELECT ln.note FROM learning_notes ln
                    WHERE ln.video_id = v.id
                    ORDER BY ln.id DESC LIMIT 1
                ) AS learning_note
            FROM videos v
            ORDER BY v.id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]

def save_video(idea: str, angle: str, title: str, script: str, status: str = "planned") -> int:
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO videos (idea, angle, title, script, status) VALUES (?, ?, ?, ?, ?)",
            (idea, angle, title, script, status),
        )
        return int(cur.lastrowid)

def update_video_output(video_id: int, output_path: str, status: str = "rendered") -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE videos SET output_path = ?, status = ? WHERE id = ?",
            (output_path, status, video_id),
        )

def mark_uploaded(video_id: int, youtube_video_id: str) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE videos SET youtube_video_id = ?, status = 'uploaded' WHERE id = ?",
            (youtube_video_id, video_id),
        )

def uploaded_videos(limit: int = 20) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT id, title, script, youtube_video_id, views, likes, comments, avg_view_percentage
            FROM videos
            WHERE youtube_video_id IS NOT NULL
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]

def update_metrics(video_id: int, metrics: dict) -> None:
    with connect() as conn:
        conn.execute(
            """
            UPDATE videos
            SET views = ?, likes = ?, comments = ?, avg_view_percentage = ?
            WHERE id = ?
            """,
            (
                int(metrics.get("views") or 0),
                int(metrics.get("likes") or 0),
                int(metrics.get("comments") or 0),
                float(metrics.get("averageViewPercentage") or 0),
                video_id,
            ),
        )

def save_learning_note(video_id: int, note: str, score: float) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO learning_notes (video_id, note, score) VALUES (?, ?, ?)",
            (video_id, note, score),
        )

def queue_video(video_id: int, scheduled_for: str) -> None:
    with connect() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO posting_queue (video_id, scheduled_for, status)
            VALUES (?, ?, 'queued')
            """,
            (video_id, scheduled_for),
        )

def queue_for_day(day_prefix: str) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT
                q.id AS queue_id, q.video_id, q.scheduled_for, q.status,
                q.uploaded_at, q.error, q.attempts,
                v.title, v.script, v.output_path, v.youtube_video_id
            FROM posting_queue q
            JOIN videos v ON v.id = q.video_id
            WHERE q.scheduled_for LIKE ?
            ORDER BY q.scheduled_for ASC
            """,
            (f"{day_prefix}%",),
        ).fetchall()
    return [dict(r) for r in rows]

def queued_items() -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT
                q.id AS queue_id, q.video_id, q.scheduled_for, q.status,
                q.attempts, v.title, v.output_path
            FROM posting_queue q
            JOIN videos v ON v.id = q.video_id
            WHERE q.status = 'queued'
            ORDER BY q.scheduled_for ASC
            """
        ).fetchall()
    return [dict(r) for r in rows]

def occupied_schedule_times() -> set[str]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT scheduled_for
            FROM posting_queue
            WHERE status IN ('queued', 'uploaded')
            """
        ).fetchall()
    return {str(r["scheduled_for"]) for r in rows}

def update_queue_schedule(queue_id: int, scheduled_for: str) -> None:
    with connect() as conn:
        conn.execute(
            """
            UPDATE posting_queue
            SET scheduled_for = ?, error = NULL
            WHERE id = ?
            """,
            (scheduled_for, queue_id),
        )

def due_queue(now_iso: str, oldest_allowed_iso: str | None = None) -> list[dict[str, Any]]:
    with connect() as conn:
        if oldest_allowed_iso:
            rows = conn.execute(
                """
                SELECT
                    q.id AS queue_id, q.video_id, q.scheduled_for, q.attempts,
                    v.title, v.script, v.output_path
                FROM posting_queue q
                JOIN videos v ON v.id = q.video_id
                WHERE q.status = 'queued'
                  AND q.scheduled_for <= ?
                  AND q.scheduled_for >= ?
                  AND q.attempts < 5
                ORDER BY q.scheduled_for ASC
                """,
                (now_iso, oldest_allowed_iso),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT
                    q.id AS queue_id, q.video_id, q.scheduled_for, q.attempts,
                    v.title, v.script, v.output_path
                FROM posting_queue q
                JOIN videos v ON v.id = q.video_id
                WHERE q.status = 'queued'
                  AND q.scheduled_for <= ?
                  AND q.attempts < 5
                ORDER BY q.scheduled_for ASC
                """,
                (now_iso,),
            ).fetchall()
    return [dict(r) for r in rows]

def mark_queue_uploaded(queue_id: int, uploaded_at: str) -> None:
    with connect() as conn:
        conn.execute(
            """
            UPDATE posting_queue
            SET status = 'uploaded', uploaded_at = ?, error = NULL
            WHERE id = ?
            """,
            (uploaded_at, queue_id),
        )

def mark_queue_error(queue_id: int, error: str) -> None:
    with connect() as conn:
        conn.execute(
            """
            UPDATE posting_queue
            SET attempts = attempts + 1,
                status = CASE WHEN attempts + 1 >= 5 THEN 'failed' ELSE 'queued' END,
                error = ?
            WHERE id = ?
            """,
            (error[:1000], queue_id),
        )

def export_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
