from __future__ import annotations

"""Restore verified pending uploads into separate, conservative daily slots.

Default mode is read-only. --apply requires the Windows scheduler stopped by
the caller, a scheduler runtime lock, a SQLite backup, and strict state gates.
Nothing is uploaded here and no existing MP4 or published history is modified.
"""

import argparse
from datetime import datetime, timedelta
import json
from pathlib import Path
import sqlite3
import sys
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import settings
import storage

PRIORITY = (27, 18, 29, 30, 31, 32, 33, 34)
FAILED = frozenset((29, 30, 31, 32, 33, 34))
CREDIT = "VOICEVOX"
REPAIR_MARKER = "[RESTORED-EDITORIAL-PASS] MP4 and quality report verified"


def _now() -> datetime:
    return datetime.now(ZoneInfo(settings.app_timezone))


def _read_plan(now: datetime) -> tuple[list[dict], list[dict]]:
    if now.tzinfo is None:
        raise RuntimeError("AWARE_LOCAL_TIME_REQUIRED")
    if (now.utcoffset() or timedelta()) != timedelta(hours=9):
        raise RuntimeError("JST_REQUIRED")

    with storage.connect() as conn:
        rows = conn.execute(
            """
            SELECT q.id AS queue_id, q.video_id, q.status AS queue_status,
                   q.scheduled_for, q.error, q.attempts,
                   v.status AS video_status, v.youtube_video_id,
                   v.output_path, v.description
            FROM posting_queue q JOIN videos v ON v.id=q.video_id
            WHERE q.video_id IN (18,27,29,30,31,32,33,34)
            """
        ).fetchall()
        if len(rows) != len(PRIORITY):
            raise RuntimeError("EXPECTED_EIGHT_QUEUE_ITEMS")
        by_id = {int(row["video_id"]): dict(row) for row in rows}

        for video_id in PRIORITY:
            row = by_id[video_id]
            if row["video_status"] != "rendered":
                raise RuntimeError(f"VIDEO_{video_id}_NOT_RENDERED")
            file_path = Path(str(row["output_path"] or ""))
            if not file_path.is_file() or file_path.stat().st_size < 1024:
                raise RuntimeError(f"VIDEO_{video_id}_MP4_MISSING")
            if not row["description"] or CREDIT not in row["description"]:
                raise RuntimeError(f"VIDEO_{video_id}_VOICE_CREDIT_MISSING")
            if row["youtube_video_id"]:
                raise RuntimeError(f"VIDEO_{video_id}_ALREADY_UPLOADED")
            if int(row["attempts"] or 0) < 0:
                raise RuntimeError(f"VIDEO_{video_id}_INVALID_ATTEMPTS")
            if video_id in FAILED:
                if row["queue_status"] != "failed":
                    raise RuntimeError(f"VIDEO_{video_id}_QUEUE_CHANGED")
                report = conn.execute(
                    """
                    SELECT passed,score,report_json FROM editorial_quality_reports
                    WHERE video_id=? ORDER BY id DESC LIMIT 1
                    """,
                    (video_id,),
                ).fetchone()
                if not report:
                    raise RuntimeError(f"VIDEO_{video_id}_EDITORIAL_MISSING")
                parsed = json.loads(report["report_json"])
                dimensions = parsed.get("dimensions") or {}
                if (not report["passed"] or not parsed.get("passed")
                        or int(report["score"]) < 58
                        or int(dimensions.get("originality") or 0) < 55
                        or int(dimensions.get("title_match") or 0) < 40):
                    raise RuntimeError(f"VIDEO_{video_id}_EDITORIAL_UNSAFE")
            else:
                if row["queue_status"] != "queued":
                    raise RuntimeError(f"VIDEO_{video_id}_QUEUE_CHANGED")
                if not str(row["error"] or "").startswith("[AUTO-RECOVERY:quota]"):
                    raise RuntimeError(f"VIDEO_{video_id}_QUOTA_STATE_CHANGED")
                if int(row["attempts"] or 0) >= 5:
                    raise RuntimeError(f"VIDEO_{video_id}_ATTEMPT_LIMIT")

            for table in ("youtube_upload_receipts", "youtube_upload_history"):
                found = conn.execute(
                    f"SELECT 1 FROM {table} WHERE video_id=? LIMIT 1", (video_id,)
                ).fetchone()
                if found:
                    raise RuntimeError(f"VIDEO_{video_id}_REMOTE_HISTORY_EXISTS")

        other_days = {
            str(x[0])[:10]
            for x in conn.execute(
                """
                SELECT scheduled_for FROM posting_queue
                WHERE status IN ('queued','uploaded')
                  AND video_id NOT IN (18,27,29,30,31,32,33,34)
                """
            ).fetchall()
        }

    # Use 15:00 JST, >=30h from execution, for one upload per calendar day.
    # The extra cushion prevents the rolling channel cap retrying at midnight.
    earliest = now + timedelta(hours=30)
    cursor = earliest.date()
    plan: list[dict] = []
    for video_id in PRIORITY:
        found = None
        for _ in range(90):
            candidate = datetime(
                cursor.year, cursor.month, cursor.day,
                15, 0, tzinfo=now.tzinfo,
            )
            if candidate >= earliest and cursor.isoformat() not in other_days:
                found = candidate
                break
            cursor += timedelta(days=1)
        if found is None:
            raise RuntimeError("NO_SAFE_DAILY_SLOTS_WITHIN_90_DAYS")
        row = by_id[video_id]
        plan.append({
            "video_id": video_id,
            "queue_id": int(row["queue_id"]),
            "old_status": row["queue_status"],
            "old_scheduled_for": row["scheduled_for"],
            "new_scheduled_for": found.isoformat(timespec="minutes"),
            "old_attempts": int(row["attempts"] or 0),
        })
        other_days.add(cursor.isoformat())
        cursor += timedelta(days=1)

    return plan, [by_id[i] for i in PRIORITY]


def reconcile(*, apply: bool = False, now: datetime | None = None) -> dict:
    now = now or _now()
    if not apply:
        plan, _ = _read_plan(now)
        result = {"mode": "PREVIEW_ONLY", "plan": plan}
        print("SAFE_UPLOAD_SCHEDULE=" + json.dumps(result, ensure_ascii=False))
        return result

    if not storage.acquire_runtime_lock(
        "scheduler_tick", owner="safe-upload-queue-reconcile"
    ):
        raise RuntimeError("SCHEDULER_CYCLE_RUNNING")
    try:
        plan, old_rows = _read_plan(now)
        backup_path = storage.DB_PATH.with_name(
            "memory-before-safe-upload-plan-"
            + now.strftime("%Y%m%d-%H%M%S-%f") + ".db"
        )
        if backup_path.exists():
            raise RuntimeError("BACKUP_PATH_ALREADY_EXISTS")
        source = sqlite3.connect(storage.DB_PATH, timeout=15)
        dest = sqlite3.connect(backup_path)
        try:
            source.backup(dest)
        finally:
            dest.close()
            source.close()
        print("DB_BACKUP=" + str(backup_path), flush=True)
        changes: list[dict] = []
        with storage.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            for item, prior in zip(plan, old_rows):
                video_id = item["video_id"]
                if video_id in FAILED:
                    sql = """
                        UPDATE posting_queue
                        SET scheduled_for=?,status='queued',attempts=0,error=?
                        WHERE id=? AND video_id=? AND status='failed'
                          AND scheduled_for=?
                    """
                    args = (
                        item["new_scheduled_for"], REPAIR_MARKER,
                        item["queue_id"], video_id, item["old_scheduled_for"],
                    )
                else:
                    sql = """
                        UPDATE posting_queue
                        SET scheduled_for=?
                        WHERE id=? AND video_id=? AND status='queued'
                          AND scheduled_for=? AND error=?
                    """
                    args = (
                        item["new_scheduled_for"], item["queue_id"],
                        video_id, item["old_scheduled_for"], prior["error"],
                    )
                changed = conn.execute(sql, args).rowcount
                if changed != 1:
                    raise RuntimeError(f"VIDEO_{video_id}_QUEUE_RACE_ABORT")
                if conn.execute(
                    "SELECT 1 FROM youtube_upload_history WHERE video_id=?",
                    (video_id,),
                ).fetchone() or conn.execute(
                    "SELECT 1 FROM youtube_upload_receipts WHERE video_id=?",
                    (video_id,),
                ).fetchone():
                    raise RuntimeError(f"VIDEO_{video_id}_REMOTE_STATE_CHANGED")
                if conn.execute(
                    "SELECT youtube_video_id FROM videos WHERE id=?",
                    (video_id,),
                ).fetchone()[0]:
                    raise RuntimeError(f"VIDEO_{video_id}_REMOTE_STATE_CHANGED")
                changes.append({
                    "video_id": video_id,
                    "slot": item["new_scheduled_for"],
                })
            dup = conn.execute(
                """
                SELECT substr(scheduled_for,1,10) AS day,count(*) AS n
                FROM posting_queue
                WHERE status='queued' AND scheduled_for>=?
                GROUP BY substr(scheduled_for,1,10)
                HAVING count(*) > 1
                """,
                (plan[0]["new_scheduled_for"][:10],),
            ).fetchall()
            if dup:
                raise RuntimeError("FUTURE_DAILY_SLOT_COLLISION")
        result = {
            "mode": "APPLIED",
            "backup": str(backup_path),
            "plan": changes,
            "youtube_uploads_triggered": 0,
        }
        print("SAFE_UPLOAD_SCHEDULE=" + json.dumps(result, ensure_ascii=False))
        return result
    finally:
        storage.release_runtime_lock("scheduler_tick")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--apply", action="store_true",
        help="Apply backed-up queue changes (no YouTube API calls).",
    )
    arguments = parser.parse_args()
    reconcile(apply=arguments.apply)


if __name__ == "__main__":
    main()
