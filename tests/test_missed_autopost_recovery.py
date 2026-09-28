from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

import automation_health
import scheduler
import storage


JST = ZoneInfo("Asia/Tokyo")


class MissedAutopostRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.old_db = storage.DB_PATH
        storage.DB_PATH = self.root / "missed-autopost.db"
        storage.init_db()
        storage.set_channel_state(
            "production_autonomy_armed",
            "true",
        )
        storage.set_channel_state(
            "automation_enabled",
            "true",
        )
        storage.set_channel_state(
            "auto_upload_enabled",
            "true",
        )
        storage.set_channel_state(
            "runtime_cancel_requested",
            "false",
        )

    def tearDown(self) -> None:
        storage.DB_PATH = self.old_db
        self.tmp.cleanup()

    def _video(self, title: str) -> tuple[int, Path]:
        video_id = storage.save_video(
            idea=title,
            angle="test",
            title=title,
            script="safe",
            description="safe",
            tags=["mirai"],
            status="rendered",
        )
        output = self.root / f"{video_id}.mp4"
        output.write_bytes(b"fake-mp4")
        storage.update_video_output(
            video_id,
            str(output),
            status="rendered",
        )
        return video_id, output

    def test_empty_today_queue_generates_immediate_catchup_after_missed_slot(self) -> None:
        now = datetime(2026, 9, 28, 19, 10, tzinfo=JST)

        # 明日分はすでに準備済み。以前はこれだけでmissing=0となり、
        # 今日12:00の未投稿が翌日へ持ち越されていた。
        future_id, _ = self._video("future")
        storage.queue_video(
            future_id,
            "2026-09-29T12:00+09:00",
        )

        generated_id, output = self._video("catchup")
        item = {
            "id": generated_id,
            "title": "catchup",
            "output_path": str(output),
            "quality_passed": True,
        }
        decision = SimpleNamespace(
            allowed=True,
            mode="normal",
            reason="test",
            snapshot={},
        )

        with (
            patch.object(scheduler, "_now", return_value=now),
            patch.object(
                scheduler,
                "post_times",
                return_value="12:00",
            ),
            patch.object(
                scheduler,
                "posts_per_day",
                return_value=1,
            ),
            patch.object(
                scheduler,
                "ensure_first_episode_delivery",
                return_value={"status": "uploaded"},
            ),
            patch.object(
                scheduler,
                "_generation_runtime_ready",
                return_value=True,
            ),
            patch.object(
                scheduler,
                "background_production_decision",
                return_value=decision,
            ),
            patch.object(
                scheduler,
                "run_generation",
                return_value=[item],
            ) as generation,
            patch.object(
                scheduler,
                "reschedule_missed",
                return_value=0,
            ),
        ):
            scheduler.prepare_upcoming()

        generation.assert_called_once()
        queue = storage.queue_item_for_video(generated_id)
        self.assertIsNotNone(queue)
        self.assertEqual(
            queue["scheduled_for"],
            "2026-09-28T19:10+09:00",
        )
        catchup = storage.get_channel_state(
            "autopost_catchup_last",
            "",
        )
        self.assertIn("scheduled_immediate", catchup)

    def test_no_catchup_when_today_quota_already_uploaded(self) -> None:
        now = datetime(2026, 9, 28, 19, 10, tzinfo=JST)
        uploaded_id, _ = self._video("already uploaded")
        storage.mark_uploaded(
            uploaded_id,
            "youtube-done",
            uploaded_at="2026-09-28T13:00:00+09:00",
            source="test",
        )
        future_id, _ = self._video("future")
        storage.queue_video(
            future_id,
            "2026-09-29T12:00+09:00",
        )

        with (
            patch.object(scheduler, "_now", return_value=now),
            patch.object(
                scheduler,
                "post_times",
                return_value="12:00",
            ),
            patch.object(
                scheduler,
                "posts_per_day",
                return_value=1,
            ),
            patch.object(
                scheduler,
                "ensure_first_episode_delivery",
                return_value={"status": "uploaded"},
            ),
            patch.object(
                scheduler,
                "_generation_runtime_ready",
                return_value=True,
            ),
            patch.object(
                scheduler,
                "run_generation",
            ) as generation,
            patch.object(
                scheduler,
                "reschedule_missed",
                return_value=0,
            ),
        ):
            scheduler.prepare_upcoming()

        generation.assert_not_called()

    def test_armed_missed_queue_stays_due_within_catchup_window(self) -> None:
        now = datetime(2026, 9, 28, 19, 10, tzinfo=JST)
        video_id, _ = self._video("missed queue")
        storage.queue_video(
            video_id,
            "2026-09-28T12:00+09:00",
        )
        fake_settings = SimpleNamespace(
            post_grace_minutes=20,
            post_sleep_catchup_hours=24,
        )

        with (
            patch.object(scheduler, "_now", return_value=now),
            patch.object(scheduler, "settings", fake_settings),
        ):
            moved = scheduler.reschedule_missed()

        self.assertEqual(moved, 0)
        queue = storage.queue_item_for_video(video_id)
        self.assertEqual(
            queue["scheduled_for"],
            "2026-09-28T12:00+09:00",
        )

    def test_run_due_writes_persistent_scheduler_heartbeat(self) -> None:
        now = datetime(2026, 9, 28, 19, 10, tzinfo=JST)

        with (
            patch.object(scheduler, "_now", return_value=now),
            patch.object(
                scheduler,
                "self_heal_delivery_controls",
                return_value={"repairs": []},
            ),
            patch.object(
                scheduler,
                "runtime_cancel_requested",
                return_value=False,
            ),
            patch.object(
                scheduler,
                "ensure_first_episode_delivery",
                return_value={"status": "uploaded"},
            ),
            patch.object(
                scheduler,
                "due_queue",
                return_value=[],
            ),
        ):
            scheduler.run_due()

        self.assertEqual(
            storage.get_channel_state(
                "scheduler_last_run_due_at",
                "",
            ),
            "2026-09-28T19:10:00+09:00",
        )

    def test_health_flags_stale_scheduler_when_autopilot_is_armed(self) -> None:
        old = (
            datetime.now().astimezone()
            - timedelta(hours=2)
        ).isoformat(timespec="seconds")
        storage.set_channel_state(
            "scheduler_last_run_due_at",
            old,
        )

        health = automation_health.collect_auto_post_health(
            self.root,
            platform_name="posix",
        )

        codes = {
            row["code"]
            for row in health["problems"]
        }
        self.assertIn(
            "scheduler_heartbeat_stale",
            codes,
        )


if __name__ == "__main__":
    unittest.main()
