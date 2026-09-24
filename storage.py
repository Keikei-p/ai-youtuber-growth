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
            "SELECT id, idea, angle, title, script, status, views, avg_view_percentage, ctr "
            "FROM videos ORDER BY id DESC LIMIT ?",
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

def export_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
