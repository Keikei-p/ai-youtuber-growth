from __future__ import annotations
import json
import sqlite3
from pathlib import Path
from typing import Any

DB_PATH = Path("data/memory.db")

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
        """)

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

def export_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
