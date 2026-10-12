from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import autopilot_controller
import runtime_liveness
import scheduler
import storage


JST = timezone(timedelta(hours=9))
NOW = datetime(2026, 10, 12, 12, 0, tzinfo=JST)


class TrueLivenessAndEpisodeGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db = storage.DB_PATH
        storage.DB_PATH = Path(self.tmp.name) / "liveness.db"
        storage.init_db()
        storage.set_channel_state("production_autonomy_armed", "true")
        storage.set_channel_state("automation_enabled", "true")
        storage.set_channel_state("auto_upload_enabled", "true")
        storage.set_channel_state("runtime_cancel_requested", "false")

    def tearDown(self) -> None:
        storage.DB_PATH = self.old_db
        self.tmp.cleanup()

    def _stamp(self, minutes_before: int = 0) -> str:
        return (NOW - timedelta(minutes=minutes_before)).isoformat(
            timespec="seconds"
        )

    def test_armed_without_execution_is_not_running(self) -> None:
        state = runtime_liveness.operational_liveness(now=NOW)
        self.assertEqual(state["status"], "unverified")
        self.assertFalse(state["running"])

    def test_heartbeat_without_completed_cycle_is_not_running(self) -> None:
        storage.set_channel_state("scheduler_last_run_due_at", self._stamp())
        state = runtime_liveness.operational_liveness(now=NOW)
        self.assertEqual(state["status"], "starting")
        self.assertFalse(state["running"])

    def test_recent_completed_cycle_is_running(self) -> None:
        storage.set_channel_state("scheduler_last_tick_at", self._stamp())
        storage.set_channel_state("scheduler_cycle_started_at", self._stamp(4))
        storage.set_channel_state("scheduler_cycle_completed_at", self._stamp(1))
        state = runtime_liveness.operational_liveness(now=NOW)
        self.assertEqual(state["status"], "running")
        self.assertTrue(state["running"])

    def test_old_cycle_even_with_recent_due_check_is_degraded(self) -> None:
        storage.set_channel_state("scheduler_last_run_due_at", self._stamp())
        storage.set_channel_state("scheduler_cycle_completed_at", self._stamp(220))
        state = runtime_liveness.operational_liveness(now=NOW)
        self.assertEqual(state["status"], "degraded")
        self.assertFalse(state["running"])

    def test_stale_scheduler_is_not_running(self) -> None:
        storage.set_channel_state("scheduler_last_run_due_at", self._stamp(62))
        storage.set_channel_state("scheduler_cycle_completed_at", self._stamp(63))
        state = runtime_liveness.operational_liveness(now=NOW)
        self.assertEqual(state["status"], "stale")
        self.assertFalse(state["running"])

    def test_cycle_failure_is_not_masked_by_due_heartbeat(self) -> None:
        storage.set_channel_state("scheduler_last_run_due_at", self._stamp())
        storage.set_channel_state("scheduler_cycle_started_at", self._stamp(10))
        storage.set_channel_state("scheduler_cycle_failed_at", self._stamp(8))
        state = runtime_liveness.operational_liveness(now=NOW)
        self.assertEqual(state["status"], "failed")
        self.assertFalse(state["running"])

    def test_cancellation_cannot_be_marked_running(self) -> None:
        storage.set_channel_state("scheduler_last_tick_at", self._stamp())
        storage.set_channel_state("scheduler_cycle_completed_at", self._stamp())
        storage.set_channel_state("runtime_cancel_requested", "true")
        self.assertEqual(
            runtime_liveness.operational_liveness(now=NOW)["status"],
            "safety_stop",
        )

    def test_lock_contention_does_not_fake_heartbeat(self) -> None:
        with patch.object(
            scheduler, "acquire_runtime_lock", return_value=False
        ):
            scheduler.tick()
        self.assertEqual(
            storage.get_channel_state("scheduler_last_tick_at", ""), ""
        )

    def test_tick_success_records_completed_cycle(self) -> None:
        with (
            patch.object(scheduler, "_now", return_value=NOW),
            patch.object(scheduler, "acquire_runtime_lock", return_value=True),
            patch.object(scheduler, "release_runtime_lock"),
            patch.object(scheduler, "_tick_unlocked"),
        ):
            scheduler.tick()
        self.assertEqual(
            storage.get_channel_state("scheduler_cycle_completed_at", ""),
            self._stamp(),
        )
        self.assertEqual(
            storage.get_channel_state("scheduler_cycle_last_outcome", ""),
            "completed",
        )

    def test_tick_failure_records_error_and_is_rethrown(self) -> None:
        with (
            patch.object(scheduler, "_now", return_value=NOW),
            patch.object(scheduler, "acquire_runtime_lock", return_value=True),
            patch.object(scheduler, "release_runtime_lock"),
            patch.object(
                scheduler,
                "_tick_unlocked",
                side_effect=RuntimeError("test failure"),
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "test failure"):
                scheduler.tick()
        self.assertEqual(
            storage.get_channel_state("scheduler_cycle_last_outcome", ""),
            "failed:RuntimeError",
        )
        self.assertEqual(
            storage.get_channel_state("scheduler_cycle_completed_at", ""), ""
        )

    def _first_episode(self) -> int:
        video_id = storage.save_video(
            idea="episode1",
            angle="test",
            title="Episode 1",
            script="safe",
            description="safe",
            tags=["mirai"],
            status="rendered",
        )
        p = Path(self.tmp.name) / f"{video_id}.mp4"
        p.write_bytes(b"fake-mp4")
        storage.update_video_output(video_id, str(p), status="rendered")
        storage.set_channel_state("mirai_first_episode_video_id", str(video_id))
        return video_id

    def test_failed_first_episode_waits_for_guarded_recovery(self) -> None:
        video_id = self._first_episode()
        storage.queue_video(video_id, self._stamp(10))
        first = storage.queue_item_for_video(video_id)
        storage.set_queue_failed(
            first["queue_id"],
            "[CIRCUIT-BREAKER:oauth] reauth required",
        )
        with patch.object(
            scheduler,
            "regenerate_saved_video",
        ) as regeneration:
            state = scheduler.ensure_first_episode_delivery()
        self.assertEqual(state["status"], "recovery_required")
        latest = storage.queue_item_for_video(video_id)
        self.assertEqual(latest["status"], "failed")
        self.assertGreaterEqual(int(latest["attempts"]), 5)
        regeneration.assert_not_called()

    def test_first_episode_retry_backoff_is_preserved(self) -> None:
        video_id = self._first_episode()
        future = (NOW + timedelta(minutes=35)).isoformat(timespec="minutes")
        storage.queue_video(video_id, future)
        item = storage.queue_item_for_video(video_id)
        storage.set_queue_recovery(
            item["queue_id"],
            scheduled_for=future,
            error="[AUTO-RECOVERY:network] wait",
        )
        with patch.object(scheduler, "_now", return_value=NOW):
            state = scheduler.ensure_first_episode_delivery()
        self.assertEqual(state["status"], "queued_priority")
        self.assertEqual(
            storage.queue_item_for_video(video_id)["scheduled_for"], future
        )

    def test_episode_safety_stop_must_not_reschedule(self) -> None:
        video_id = self._first_episode()
        storage.set_channel_state("runtime_cancel_requested", "true")
        with patch.object(scheduler, "regenerate_saved_video") as regeneration:
            state = scheduler.ensure_first_episode_delivery()
        self.assertEqual(state["status"], "safety_stop")
        self.assertIsNone(storage.queue_item_for_video(video_id))
        regeneration.assert_not_called()


if __name__ == "__main__":
    unittest.main()
