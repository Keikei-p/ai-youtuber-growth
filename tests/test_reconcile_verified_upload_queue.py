from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import storage
from tools.reconcile_verified_upload_queue import PRIORITY, reconcile

JST = timezone(timedelta(hours=9))
FIXED = datetime(2026, 11, 11, 21, 30, tzinfo=JST)


class VerifiedUploadScheduleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db = storage.DB_PATH
        self.root = Path(self.tmp.name)
        storage.DB_PATH = self.root / "data" / "memory.db"
        storage.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        storage.init_db()
        with storage.connect() as conn:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS editorial_quality_reports (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    video_id INTEGER,passed INTEGER,score INTEGER,
                    report_json TEXT
                )"""
            )
            for video_id in PRIORITY:
                output = self.root / f"{video_id}.mp4"
                output.write_bytes(b"valid surrogate bytes" * 200)
                conn.execute(
                    """
                    INSERT INTO videos
                    (id,idea,angle,title,script,description,tags_json,
                     status,output_path)
                    VALUES (?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        video_id, "idea", "angle", str(video_id),
                        "unique script", "VOICEVOX:ずんだもん",
                        "[]", "rendered", str(output),
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO posting_queue
                    (video_id,scheduled_for,status,error,attempts)
                    VALUES (?,?,?,?,0)
                    """,
                    (
                        video_id, "2026-11-12T09:00+09:00"
                        if video_id in (18, 27) else
                        "2026-09-28T09:00+09:00",
                        "queued" if video_id in (18, 27) else "failed",
                        "[AUTO-RECOVERY:quota] daily upload limit"
                        if video_id in (18, 27) else "missing media",
                    ),
                )
                if video_id not in (18, 27):
                    conn.execute(
                        """
                        INSERT INTO editorial_quality_reports
                        (video_id,passed,score,report_json)
                        VALUES (?,?,?,?)
                        """,
                        (
                            video_id, 1, 80,
                            '{"passed":true,"score":80,"dimensions":'
                            '{"originality":72,"title_match":45}}',
                        ),
                    )

    def tearDown(self) -> None:
        storage.DB_PATH = self.old_db
        self.tmp.cleanup()

    def _rows(self) -> list[tuple]:
        with storage.connect() as conn:
            return [
                tuple(row) for row in conn.execute(
                    """
                    SELECT video_id,status,scheduled_for,attempts,error
                    FROM posting_queue ORDER BY video_id
                    """
                ).fetchall()
            ]

    def test_preview_is_read_only_and_respects_quota_delay(self) -> None:
        before = self._rows()
        with patch("builtins.print"):
            r = reconcile(now=FIXED)
        self.assertEqual(r["mode"], "PREVIEW_ONLY")
        self.assertEqual(self._rows(), before)
        self.assertEqual(
            [x["video_id"] for x in r["plan"]], list(PRIORITY)
        )
        self.assertEqual(
            r["plan"][0]["new_scheduled_for"],
            "2026-11-13T15:00+09:00",
        )
        self.assertEqual(
            r["plan"][1]["new_scheduled_for"],
            "2026-11-14T15:00+09:00",
        )

    def test_apply_staggers_and_retains_all_original_video_bytes(self) -> None:
        before = {
            v: (self.root / f"{v}.mp4").read_bytes() for v in PRIORITY
        }
        with patch("builtins.print"):
            r = reconcile(apply=True, now=FIXED)
        self.assertEqual(r["mode"], "APPLIED")
        self.assertEqual(r["youtube_uploads_triggered"], 0)
        self.assertTrue(Path(r["backup"]).is_file())
        with storage.connect() as conn:
            rows = conn.execute(
                """
                SELECT q.video_id,q.status,q.scheduled_for,q.error,v.youtube_video_id
                FROM posting_queue q JOIN videos v ON v.id=q.video_id
                ORDER BY q.video_id
                """
            ).fetchall()
        self.assertEqual(len(rows), 8)
        self.assertEqual({row["status"] for row in rows}, {"queued"})
        self.assertEqual(len({row["scheduled_for"][:10] for row in rows}), 8)
        self.assertTrue(
            all(row["youtube_video_id"] is None for row in rows)
        )
        self.assertTrue(
            all((self.root / f"{v}.mp4").read_bytes() == before[v]
                for v in PRIORITY)
        )
        self.assertTrue(
            all(row["error"].startswith("[AUTO-RECOVERY:quota]")
                for row in rows if row["video_id"] in (18, 27))
        )

    def test_occupied_calendar_date_is_skipped(self) -> None:
        with storage.connect() as conn:
            conn.execute(
                """
                INSERT INTO videos
                (id,idea,title,script,description,tags_json,status)
                VALUES (90,'other','other','other','VOICEVOX','[]','rendered')
                """
            )
            conn.execute(
                """
                INSERT INTO posting_queue
                (video_id,scheduled_for,status)
                VALUES (90,'2026-11-14T09:00+09:00','queued')
                """
            )
        with patch("builtins.print"):
            r = reconcile(now=FIXED)
        self.assertEqual(
            r["plan"][0]["new_scheduled_for"],
            "2026-11-13T15:00+09:00",
        )
        self.assertEqual(
            r["plan"][1]["new_scheduled_for"],
            "2026-11-15T15:00+09:00",
        )

    def test_remote_history_blocks_any_queue_changes(self) -> None:
        before = self._rows()
        with storage.connect() as conn:
            conn.execute(
                """
                INSERT INTO youtube_upload_history
                (video_id,youtube_video_id,uploaded_at)
                VALUES (32,'already-published','2026-11-11T01:00:00Z')
                """
            )
        with self.assertRaisesRegex(RuntimeError, "REMOTE_HISTORY_EXISTS"):
            reconcile(apply=True, now=FIXED)
        self.assertEqual(self._rows(), before)

    def test_failed_editorial_report_blocks_revival(self) -> None:
        before = self._rows()
        with storage.connect() as conn:
            conn.execute(
                """
                INSERT INTO editorial_quality_reports
                (video_id,passed,score,report_json)
                VALUES (33,0,50,'{"passed":false,"dimensions":{}}')
                """
            )
        with self.assertRaisesRegex(RuntimeError, "EDITORIAL_UNSAFE"):
            reconcile(apply=True, now=FIXED)
        self.assertEqual(self._rows(), before)

    def test_missing_media_blocks_revival(self) -> None:
        before = self._rows()
        (self.root / "34.mp4").unlink()
        with self.assertRaisesRegex(RuntimeError, "VIDEO_34_MP4_MISSING"):
            reconcile(apply=True, now=FIXED)
        self.assertEqual(self._rows(), before)


if __name__ == "__main__":
    unittest.main()
