from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import scheduler
import storage


JST = timezone(timedelta(hours=9))


class SleepWakeSchedulerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.old_db = storage.DB_PATH
        storage.DB_PATH = self.root / "sleep-wake.db"
        storage.init_db()

    def tearDown(self) -> None:
        storage.DB_PATH = self.old_db
        self.tmp.cleanup()

    def _queued_video(self, scheduled_for: str) -> tuple[int, int]:
        video_id = storage.save_video(
            idea="sleep wake",
            angle="catchup",
            title="Sleep wake test",
            script="sleep wake test",
            description="description",
            tags=["test"],
            status="rendered",
        )
        output = self.root / f"{video_id}.mp4"
        output.write_bytes(b"fake-mp4")
        storage.update_video_output(
            video_id,
            str(output),
            status="rendered",
        )
        storage.queue_video(video_id, scheduled_for)
        with storage.connect() as conn:
            row = conn.execute(
                "SELECT id FROM posting_queue WHERE video_id = ?",
                (video_id,),
            ).fetchone()
        return video_id, int(row["id"])

    def test_sleep_resume_37_minutes_late_still_uploads(self) -> None:
        video_id, queue_id = self._queued_video(
            "2026-09-26T09:00+09:00"
        )
        now = datetime(2026, 9, 26, 9, 37, tzinfo=JST)
        fake_settings = SimpleNamespace(
            post_sleep_catchup_hours=6,
            post_grace_minutes=20,
            youtube_category_id="22",
            youtube_default_language="ja",
        )

        with (
            patch.object(scheduler, "settings", fake_settings),
            patch.object(scheduler, "_now", return_value=now),
            patch.object(scheduler, "_full_test_state", return_value={}),
            patch.object(scheduler, "auto_upload_enabled", return_value=True),
            patch.object(scheduler, "upload_privacy", return_value="private"),
            patch.object(
                scheduler,
                "voice_attribution_status",
                return_value={"resolved": True, "credit": ""},
            ),
            patch.object(
                scheduler,
                "publish_gate",
                return_value={"allowed": True, "risks": []},
            ),
            patch.object(
                scheduler,
                "upload_video",
                return_value="youtube-sleep-wake",
            ) as upload,
            patch.object(scheduler, "cleanup_uploaded_media"),
        ):
            scheduler.run_due()

        upload.assert_called_once()
        row = storage.video_by_id(video_id)
        self.assertEqual(
            row["youtube_video_id"],
            "youtube-sleep-wake",
        )
        with storage.connect() as conn:
            queue = conn.execute(
                "SELECT status FROM posting_queue WHERE id = ?",
                (queue_id,),
            ).fetchone()
        self.assertEqual(queue["status"], "uploaded")

    def test_failed_upload_is_kept_for_resume_retry_inside_catchup_window(self) -> None:
        _, queue_id = self._queued_video(
            "2026-09-26T09:00+09:00"
        )
        storage.mark_queue_error(queue_id, "network not ready")
        now = datetime(2026, 9, 26, 9, 37, tzinfo=JST)
        fake_settings = SimpleNamespace(
            post_sleep_catchup_hours=6,
            post_grace_minutes=20,
        )

        with (
            patch.object(scheduler, "settings", fake_settings),
            patch.object(scheduler, "_now", return_value=now),
            patch.object(scheduler, "_full_test_state", return_value={}),
        ):
            moved = scheduler.reschedule_missed()

        self.assertEqual(moved, 0)
        with storage.connect() as conn:
            row = conn.execute(
                "SELECT scheduled_for, attempts, status "
                "FROM posting_queue WHERE id = ?",
                (queue_id,),
            ).fetchone()
        self.assertEqual(
            row["scheduled_for"],
            "2026-09-26T09:00+09:00",
        )
        self.assertEqual(int(row["attempts"]), 1)
        self.assertEqual(row["status"], "queued")

    def test_normal_tick_checks_due_upload_before_heavy_work(self) -> None:
        calls: list[str] = []

        with (
            patch.object(scheduler, "_full_test_state", return_value={}),
            patch.object(
                scheduler,
                "run_due",
                side_effect=lambda: calls.append("due"),
            ),
            patch.object(
                scheduler,
                "run_growth_cycle",
                side_effect=lambda: calls.append("growth"),
            ),
            patch.object(
                scheduler,
                "maybe_run_improvement_review",
                side_effect=lambda **kwargs: calls.append("improvement"),
            ),
            patch.object(
                scheduler,
                "prepare_upcoming",
                side_effect=lambda: calls.append("prepare"),
            ),
        ):
            scheduler.tick()

        self.assertEqual(
            calls,
            ["due", "growth", "improvement", "prepare"],
        )

    def test_windows_wake_task_has_post_prepare_and_recovery_triggers(self) -> None:
        install = Path(
            "automation/install_windows_task.ps1"
        ).read_text(encoding="utf-8")
        cycle = Path(
            "automation/windows_cycle.ps1"
        ).read_text(encoding="utf-8")

        self.assertIn("New-ScheduledTaskTrigger -Daily", install)
        self.assertIn("$PrepareMinutes = 90", install)
        self.assertIn("$RecoveryMinutes = 15", install)
        self.assertIn("WakeToRun", install)
        self.assertIn("/SETACVALUEINDEX", install)
        self.assertIn("/SETDCVALUEINDEX", install)
        self.assertIn("SetThreadExecutionState", cycle)
        self.assertIn("www.googleapis.com", cycle)
        self.assertIn("network not ready after wake", cycle)


if __name__ == "__main__":
    unittest.main()
