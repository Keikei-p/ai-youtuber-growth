from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import scheduler
import storage
from upload_recovery import (
    classify_upload_failure,
    recover_upload_failure,
)


JST = timezone(timedelta(hours=9))


class AutonomousUploadRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.old_db = storage.DB_PATH
        storage.DB_PATH = self.root / "recovery.db"
        storage.init_db()

    def tearDown(self) -> None:
        storage.DB_PATH = self.old_db
        self.tmp.cleanup()

    def _video(self, *, first_episode: bool = False) -> tuple[int, Path]:
        video_id = storage.save_video(
            idea="episode one" if first_episode else "recovery",
            angle="test",
            title="Episode one" if first_episode else "Recovery test",
            script="safe script",
            description="safe description",
            tags=["mirai", "test"],
            status="rendered",
        )
        output = self.root / f"{video_id}.mp4"
        output.write_bytes(b"fake-mp4")
        storage.update_video_output(video_id, str(output), status="rendered")
        if first_episode:
            storage.set_channel_state(
                "mirai_first_episode_video_id",
                str(video_id),
            )
            storage.set_channel_state(
                "mirai_first_episode_completed",
                "true",
            )
            storage.set_channel_state(
                "mirai_first_episode_uploaded",
                "false",
            )
        return video_id, output

    def test_first_episode_rendered_is_still_pending_until_youtube_id_exists(self) -> None:
        video_id, _ = self._video(first_episode=True)
        now = datetime(2026, 9, 27, 0, 30, tzinfo=JST)

        with patch.object(scheduler, "_now", return_value=now):
            result = scheduler.ensure_first_episode_delivery()

        self.assertEqual(result["status"], "queued_priority")
        queue = storage.queue_item_for_video(video_id)
        self.assertIsNotNone(queue)
        self.assertEqual(queue["status"], "queued")
        self.assertEqual(
            queue["scheduled_for"],
            "2026-09-27T00:30+09:00",
        )
        self.assertEqual(
            storage.get_channel_state(
                "mirai_first_episode_uploaded",
                "",
            ),
            "false",
        )

    def test_first_episode_auto_posts_even_if_general_auto_upload_is_off(self) -> None:
        video_id, _ = self._video(first_episode=True)
        now = datetime(2026, 9, 27, 0, 30, tzinfo=JST)

        with (
            patch.object(scheduler, "_now", return_value=now),
            patch.object(
                scheduler,
                "auto_upload_enabled",
                return_value=False,
            ),
            patch.object(
                scheduler,
                "automation_enabled",
                return_value=True,
            ),
            patch.object(
                scheduler,
                "_upload_runtime_block_reason",
                return_value="",
            ),
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
                return_value="youtube-episode-auto",
            ) as upload,
            patch.object(scheduler, "cleanup_uploaded_media"),
        ):
            scheduler.run_due()

        upload.assert_called_once()
        self.assertEqual(
            storage.video_by_id(video_id)["youtube_video_id"],
            "youtube-episode-auto",
        )
        self.assertEqual(
            storage.get_channel_state(
                "mirai_first_episode_uploaded",
                "",
            ),
            "true",
        )

    def test_reenabling_automation_clears_stale_safe_stop_latch(self) -> None:
        import runtime_control

        runtime_control.request_runtime_cancel()
        self.assertTrue(runtime_control.runtime_cancel_requested())
        runtime_control.set_automation_enabled(True)
        self.assertFalse(runtime_control.runtime_cancel_requested())

    def test_first_episode_upload_marks_delivery_complete(self) -> None:
        video_id, _ = self._video(first_episode=True)
        storage.mark_uploaded(
            video_id,
            "youtube-episode-one",
            privacy_status="private",
            source="auto_schedule",
        )
        self.assertEqual(
            storage.get_channel_state(
                "mirai_first_episode_uploaded",
                "",
            ),
            "true",
        )
        history = storage.upload_history(video_id)
        self.assertEqual(history[0]["youtube_video_id"], "youtube-episode-one")
        self.assertEqual(history[0]["source"], "auto_schedule")

    def test_repost_keeps_old_youtube_id_in_history(self) -> None:
        video_id, _ = self._video()
        storage.mark_uploaded(
            video_id,
            "youtube-old",
            privacy_status="private",
            source="auto_schedule",
        )

        with (
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
                return_value="youtube-new",
            ),
        ):
            result = scheduler.upload_saved_video_now(
                video_id,
                "private",
                force_reupload=True,
            )

        self.assertEqual(result["status"], "reuploaded")
        self.assertEqual(result["previous_youtube_video_id"], "youtube-old")
        self.assertEqual(
            storage.video_by_id(video_id)["youtube_video_id"],
            "youtube-new",
        )
        history = storage.upload_history(video_id)
        self.assertEqual(
            [row["youtube_video_id"] for row in history[:2]],
            ["youtube-new", "youtube-old"],
        )
        self.assertEqual(
            history[0]["replaced_youtube_video_id"],
            "youtube-old",
        )

    def test_upload_lock_blocks_second_process(self) -> None:
        video_id, _ = self._video()
        self.assertTrue(
            storage.acquire_upload_lock(video_id, owner="first")
        )
        self.assertFalse(
            storage.acquire_upload_lock(video_id, owner="second")
        )
        storage.release_upload_lock(video_id)
        self.assertTrue(
            storage.acquire_upload_lock(video_id, owner="third")
        )

    def test_network_failure_is_rescheduled_automatically(self) -> None:
        video_id, _ = self._video()
        storage.queue_video(
            video_id,
            "2026-09-27T00:20+09:00",
        )
        row = storage.queue_item_for_video(video_id)
        now = datetime(2026, 9, 27, 0, 30, tzinfo=JST)
        result = recover_upload_failure(
            row,
            "Connection reset by peer",
            now=now,
        )
        self.assertEqual(result["code"], "network")
        self.assertTrue(result["handled"])
        queue = storage.queue_item_for_video(video_id)
        self.assertEqual(
            queue["scheduled_for"],
            "2026-09-27T00:40+09:00",
        )
        self.assertEqual(int(queue["attempts"]), 1)

    def test_repeated_network_recovery_stays_below_hard_failure_cutoff(self) -> None:
        video_id, _ = self._video()
        storage.queue_video(
            video_id,
            "2026-09-27T00:20+09:00",
        )
        now = datetime(2026, 9, 27, 0, 30, tzinfo=JST)

        for _ in range(8):
            row = storage.queue_item_for_video(video_id)
            recover_upload_failure(
                row,
                "network timeout",
                now=now,
            )

        queue = storage.queue_item_for_video(video_id)
        self.assertEqual(queue["status"], "queued")
        self.assertLess(int(queue["attempts"]), 5)

    def test_oauth_failure_waits_for_human_reauthentication(self) -> None:
        video_id, _ = self._video()
        storage.queue_video(
            video_id,
            "2026-09-27T00:20+09:00",
        )
        row = storage.queue_item_for_video(video_id)
        now = datetime(2026, 9, 27, 0, 30, tzinfo=JST)
        result = recover_upload_failure(
            row,
            "invalid_grant: refresh token expired",
            now=now,
        )
        self.assertEqual(result["code"], "oauth")
        self.assertTrue(result["handled"])
        self.assertEqual(
            storage.get_channel_state("youtube_auth_attention", ""),
            "true",
        )

    def test_failure_classifier_covers_quota_and_missing_file(self) -> None:
        self.assertEqual(
            classify_upload_failure("quotaExceeded")["code"],
            "quota",
        )
        self.assertEqual(
            classify_upload_failure(
                "動画ファイルが見つかりません"
            )["code"],
            "missing_file",
        )

    def test_dry_run_blocks_real_auto_upload(self) -> None:
        video_id, _ = self._video(first_episode=True)
        now = datetime(2026, 9, 27, 0, 30, tzinfo=JST)

        with (
            patch.object(scheduler, "_now", return_value=now),
            patch.object(scheduler, "automation_enabled", return_value=True),
            patch.object(scheduler, "auto_upload_enabled", return_value=True),
            patch.object(
                scheduler,
                "_upload_runtime_block_reason",
                return_value="DRY_RUN=true",
            ),
            patch.object(scheduler, "upload_video") as upload,
        ):
            scheduler.run_due()

        upload.assert_not_called()
        self.assertIsNone(
            storage.video_by_id(video_id)["youtube_video_id"]
        )

    def test_windows_wake_cycle_prioritizes_upload_and_self_update(self) -> None:
        cycle = Path("automation/windows_cycle.ps1").read_text(
            encoding="utf-8"
        )
        install = Path("automation/install_windows_task.ps1").read_text(
            encoding="utf-8"
        )
        self.assertIn("MIRAI_DUE_FIRST", cycle)
        self.assertIn('"--run-due"', cycle)
        self.assertIn("safe_self_update.py", cycle)
        self.assertIn("MIRAI_RECOVERY_TRIGGERS", install)
        self.assertIn("@(5, 15, 30, 60)", install)

    def test_library_ui_contains_repost_flow(self) -> None:
        source = Path("webapp.py").read_text(encoding="utf-8")
        self.assertIn("repostLibraryVideo", source)
        self.assertIn("repost_library_video", source)
        self.assertIn("旧IDも履歴に残します", source)


if __name__ == "__main__":
    unittest.main()
