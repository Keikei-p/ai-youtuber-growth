from __future__ import annotations
import json
import sqlite3
from datetime import datetime, timezone
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
            description TEXT NOT NULL DEFAULT '',
            tags_json TEXT NOT NULL DEFAULT '[]',
            guest_id INTEGER,
            status TEXT NOT NULL DEFAULT 'planned',
            output_path TEXT,
            youtube_video_id TEXT,
            uploaded_at TEXT,
            views INTEGER,
            likes INTEGER,
            comments INTEGER,
            avg_view_percentage REAL,
            ctr REAL
        );

        CREATE TABLE IF NOT EXISTS guests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            name TEXT NOT NULL,
            profile_json TEXT NOT NULL,
            visual_prompt TEXT NOT NULL DEFAULT '',
            image_path TEXT,
            appearances INTEGER NOT NULL DEFAULT 0,
            last_used_at TEXT,
            active INTEGER NOT NULL DEFAULT 1
        );

        CREATE TABLE IF NOT EXISTS learning_notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            video_id INTEGER,
            note TEXT NOT NULL,
            score REAL,
            FOREIGN KEY(video_id) REFERENCES videos(id)
        );

        CREATE TABLE IF NOT EXISTS analytics_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            video_id INTEGER NOT NULL,
            checkpoint_hours INTEGER NOT NULL,
            captured_at TEXT NOT NULL,
            views INTEGER NOT NULL DEFAULT 0,
            likes INTEGER NOT NULL DEFAULT 0,
            comments INTEGER NOT NULL DEFAULT 0,
            avg_view_percentage REAL NOT NULL DEFAULT 0,
            score REAL NOT NULL DEFAULT 0,
            note TEXT NOT NULL DEFAULT '',
            UNIQUE(video_id, checkpoint_hours),
            FOREIGN KEY(video_id) REFERENCES videos(id)
        );

        CREATE TABLE IF NOT EXISTS channel_state (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL
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

        video_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(videos)").fetchall()
        }
        if "uploaded_at" not in video_columns:
            conn.execute("ALTER TABLE videos ADD COLUMN uploaded_at TEXT")
        if "description" not in video_columns:
            conn.execute("ALTER TABLE videos ADD COLUMN description TEXT NOT NULL DEFAULT ''")
        if "tags_json" not in video_columns:
            conn.execute("ALTER TABLE videos ADD COLUMN tags_json TEXT NOT NULL DEFAULT '[]'")
        if "guest_id" not in video_columns:
            conn.execute("ALTER TABLE videos ADD COLUMN guest_id INTEGER")

        queue_columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(posting_queue)").fetchall()
        }
        if "attempts" not in queue_columns:
            conn.execute(
                "ALTER TABLE posting_queue ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0"
            )

        conn.execute(
            """
            UPDATE videos
            SET uploaded_at = COALESCE(
                (
                    SELECT q.uploaded_at
                    FROM posting_queue q
                    WHERE q.video_id = videos.id
                      AND q.uploaded_at IS NOT NULL
                    ORDER BY q.id DESC
                    LIMIT 1
                ),
                created_at
            )
            WHERE youtube_video_id IS NOT NULL
              AND uploaded_at IS NULL
            """
        )

def dashboard_videos(limit: int = 30) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT
                v.id, v.created_at, v.title, v.status, v.output_path,
                v.youtube_video_id, v.uploaded_at, v.views, v.likes, v.comments,
                v.avg_view_percentage, v.description, v.tags_json,
                g.name AS guest_name,
                q.scheduled_for, q.status AS queue_status, q.error AS queue_error
            FROM videos v
            LEFT JOIN guests g ON g.id = v.guest_id
            LEFT JOIN posting_queue q ON q.video_id = v.id
            ORDER BY v.id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]

def video_by_id(video_id: int) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute(
            """
            SELECT
                v.id, v.created_at, v.title, v.status, v.output_path,
                v.youtube_video_id, v.uploaded_at, v.views, v.likes,
                v.comments, v.avg_view_percentage, v.description,
                v.tags_json, g.name AS guest_name
            FROM videos v
            LEFT JOIN guests g ON g.id = v.guest_id
            WHERE v.id = ?
            """,
            (video_id,),
        ).fetchone()
    return dict(row) if row else None


def recent_videos(limit: int = 20) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT
                v.id, v.idea, v.angle, v.title, v.script, v.description,
                v.tags_json, v.guest_id, g.name AS guest_name, v.status,
                v.views, v.likes, v.comments, v.avg_view_percentage, v.ctr,
                (
                    SELECT ln.note FROM learning_notes ln
                    WHERE ln.video_id = v.id
                    ORDER BY ln.id DESC LIMIT 1
                ) AS learning_note
            FROM videos v
            LEFT JOIN guests g ON g.id = v.guest_id
            ORDER BY v.id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]

def save_video(
    idea: str,
    angle: str,
    title: str,
    script: str,
    description: str = "",
    tags: list[str] | None = None,
    guest_id: int | None = None,
    status: str = "planned",
) -> int:
    with connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO videos (
                idea, angle, title, script, description, tags_json, guest_id, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                idea,
                angle,
                title,
                script,
                description,
                json.dumps(tags or [], ensure_ascii=False),
                guest_id,
                status,
            ),
        )
        return int(cur.lastrowid)

def update_video_output(video_id: int, output_path: str, status: str = "rendered") -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE videos SET output_path = ?, status = ? WHERE id = ?",
            (output_path, status, video_id),
        )

def clear_video_output(video_id: int) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE videos SET output_path = NULL WHERE id = ?",
            (video_id,),
        )

def mark_uploaded(
    video_id: int,
    youtube_video_id: str,
    uploaded_at: str | None = None,
) -> None:
    uploaded_at = uploaded_at or datetime.now(timezone.utc).isoformat(timespec="seconds")
    with connect() as conn:
        conn.execute(
            """
            UPDATE videos
            SET youtube_video_id = ?, status = 'uploaded',
                uploaded_at = COALESCE(uploaded_at, ?)
            WHERE id = ?
            """,
            (youtube_video_id, uploaded_at, video_id),
        )

def uploaded_videos(limit: int = 20) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT
                id, idea, angle, title, script, description, tags_json, guest_id,
                output_path, youtube_video_id, uploaded_at,
                views, likes, comments, avg_view_percentage
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

def snapshot_exists(video_id: int, checkpoint_hours: int) -> bool:
    with connect() as conn:
        row = conn.execute(
            """
            SELECT 1 FROM analytics_snapshots
            WHERE video_id = ? AND checkpoint_hours = ?
            LIMIT 1
            """,
            (video_id, checkpoint_hours),
        ).fetchone()
    return row is not None

def due_snapshot_candidates(
    checkpoint_hours: int,
    now_iso: str,
    limit: int = 20,
) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT
                v.id, v.idea, v.angle, v.title, v.script,
                v.youtube_video_id, v.uploaded_at
            FROM videos v
            WHERE v.youtube_video_id IS NOT NULL
              AND v.uploaded_at IS NOT NULL
              AND ((julianday(?) - julianday(v.uploaded_at)) * 24.0) >= ?
              AND NOT EXISTS (
                  SELECT 1 FROM analytics_snapshots s
                  WHERE s.video_id = v.id
                    AND s.checkpoint_hours = ?
              )
            ORDER BY v.uploaded_at ASC
            LIMIT ?
            """,
            (now_iso, checkpoint_hours, checkpoint_hours, limit),
        ).fetchall()
    return [dict(r) for r in rows]

def save_analytics_snapshot(
    video_id: int,
    checkpoint_hours: int,
    captured_at: str,
    metrics: dict,
    note: str,
    score: float,
) -> None:
    with connect() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO analytics_snapshots (
                video_id, checkpoint_hours, captured_at,
                views, likes, comments, avg_view_percentage, score, note
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                video_id,
                checkpoint_hours,
                captured_at,
                int(metrics.get("views") or 0),
                int(metrics.get("likes") or 0),
                int(metrics.get("comments") or 0),
                float(metrics.get("averageViewPercentage") or 0),
                float(score),
                note,
            ),
        )

def analytics_history(limit: int = 60) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT
                s.video_id, s.checkpoint_hours, s.captured_at,
                s.views, s.likes, s.comments, s.avg_view_percentage,
                s.score, s.note,
                v.idea, v.angle, v.title
            FROM analytics_snapshots s
            JOIN videos v ON v.id = s.video_id
            ORDER BY s.id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]

def set_channel_state(key: str, value: str) -> None:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO channel_state (key, value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                updated_at = excluded.updated_at
            """,
            (key, value, now),
        )

def get_channel_state(key: str, default: str = "") -> str:
    with connect() as conn:
        row = conn.execute(
            "SELECT value FROM channel_state WHERE key = ?",
            (key,),
        ).fetchone()
    return str(row["value"]) if row else default

def video_count() -> int:
    with connect() as conn:
        row = conn.execute("SELECT COUNT(*) AS c FROM videos").fetchone()
    return int(row["c"] or 0)

def active_guests(limit: int = 20) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT id, name, profile_json, visual_prompt, image_path,
                   appearances, last_used_at, active
            FROM guests
            WHERE active = 1
            ORDER BY appearances DESC, id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]

def create_guest(
    name: str,
    profile: dict,
    visual_prompt: str,
    image_path: str | None = None,
) -> int:
    with connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO guests (name, profile_json, visual_prompt, image_path)
            VALUES (?, ?, ?, ?)
            """,
            (
                name,
                json.dumps(profile, ensure_ascii=False),
                visual_prompt,
                image_path,
            ),
        )
        return int(cur.lastrowid)

def set_guest_image(guest_id: int, image_path: str) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE guests SET image_path = ? WHERE id = ?",
            (image_path, guest_id),
        )

def mark_guest_used(guest_id: int) -> None:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with connect() as conn:
        conn.execute(
            """
            UPDATE guests
            SET appearances = appearances + 1, last_used_at = ?
            WHERE id = ?
            """,
            (now, guest_id),
        )

def retire_guest(guest_id: int) -> None:
    with connect() as conn:
        conn.execute("UPDATE guests SET active = 0 WHERE id = ?", (guest_id,))

def guest_performance() -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT
                g.id, g.name, g.appearances, g.last_used_at,
                COALESCE(AVG(s.score), 0) AS avg_score,
                COUNT(s.id) AS snapshot_count
            FROM guests g
            LEFT JOIN videos v ON v.guest_id = g.id
            LEFT JOIN analytics_snapshots s ON s.video_id = v.id
            WHERE g.active = 1
            GROUP BY g.id
            ORDER BY avg_score DESC, g.appearances DESC, g.id DESC
            """
        ).fetchall()
    return [dict(r) for r in rows]

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
                q.attempts, v.title, v.description, v.tags_json, v.guest_id,
                v.output_path
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
                    v.title, v.description, v.tags_json, v.guest_id,
                    v.script, v.output_path
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
                    v.title, v.description, v.tags_json, v.guest_id,
                    v.script, v.output_path
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
