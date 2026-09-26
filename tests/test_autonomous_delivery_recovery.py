from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import delivery_supervisor
import scheduler
import storage
import upload_recovery


JST = timezone(timedelta(hours=9))


class AutonomousDeliveryRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.old_db = storage.DB_PATH
        storage.DB_PATH = self.root / "delivery-recovery.db"
        storage.init_db()

    def tearDown(self) -> None:
        storage.DB_PATH = self.old_db
        self.tmp.cleanup()

    def _video(self) -> tuple[int, Path]:
        video_id = storage.save_video(
            idea="episode one",
            angle="autonomous",
            title="Episode one",
            script="safe episode one",
            description="safe",
            tags=["mirai"],
            status="rendered",
        )
        output = self.root / f"{video_id}.mp4"
        output.write_bytes(b"fake-mp4")
        storage.update_video_output(video_id, str(output), status="rendered")
        return video_id, output

    def test_first_episode_auto_upload_off_is_self_healed(self) -> None:
        video_id, _ = self._video()
        storage.set_channel_state("mirai_first_episode_video_id", str(video_id))
        storage.set_channel_state("mirai_first_episode_uploaded", "false")
        storage.set_channel_state("automation_enabled", "true")
        storage.set_channel_state("auto_upload_enabled", "false")

        with patch.object(
            delivery_supervisor,
            "get_credentials",
            return_value=object(),
        ):
            result = delivery_supervisor.self_heal_delivery_controls()

        self.assertTrue(
            storage.get_channel_state(
                "auto_upload_enabled",
                "false",
            ) == "true"
        )
        self.assertTrue(result["repairs"])

    def test_armed_production_recovers_both_switches_for_pending_episode(self) -> None:
        video_id, _ = self._video()
        storage.set_channel_state("mirai_first_episode_video_id", str(video_id))
        storage.set_channel_state("mirai_first_episode_uploaded", "false")
        storage.set_channel_state("production_autonomy_armed", "true")
        storage.set_channel_state("automation_enabled", "false")
        storage.set_channel_state("auto_upload_enabled", "false")

        with patch.object(
            delivery_supervisor,
            "get_credentials",
            return_value=object(),
        ):
            delivery_supervisor.self_heal_delivery_controls()

        self.assertEqual(
            storage.get_channel_state("automation_enabled", ""),
            "true",
        )
        self.assertEqual(
            storage.get_channel_state("auto_upload_enabled", ""),
            "true",
        )

    def test_network_failure_is_requeued_instead_of_marked_dead(self) -> None:
        video_id, _ = self._video()
        storage.queue_video(video_id, "2026-09-27T09:00+09:00")
        row = storage.queue_item_for_video(video_id)
        result = upload_recovery.recover_upload_failure(
            row,
            RuntimeError("network timeout"),
            now=datetime(2026, 9, 27, 9, 1, tzinfo=JST),
        )
        self.assertEqual(result["code"], "network")
        self.assertTrue(result["handled"])
        refreshed = storage.queue_item_for_video(video_id)
        self.assertEqual(refreshed["status"], "queued")
        self.assertIn("09:11", refreshed["scheduled_for"])

    def test_oauth_failure_requires_human_reauth_not_code_bypass(self) -> None:
        video_id, _ = self._video()
        storage.queue_video(video_id, "2026-09-27T09:00+09:00")
        row = storage.queue_item_for_video(video_id)
        result = upload_recovery.recover_upload_failure(
            row,
            RuntimeError("invalid_grant OAuth token expired"),
            now=datetime(2026, 9, 27, 9, 1, tzinfo=JST),
        )
        self.assertEqual(result["code"], "oauth")
        self.assertEqual(result["safe_action"], "reauth_required")
        self.assertEqual(
            storage.get_channel_state("youtube_auth_attention", ""),
            "true",
        )

    def test_force_repost_keeps_old_youtube_id_in_history(self) -> None:
        video_id, _ = self._video()
        storage.mark_uploaded(
            video_id,
            "youtube-old",
            privacy_status="private",
            source="test",
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
        history = storage.upload_history(video_id)
        ids = [row["youtube_video_id"] for row in history]
        self.assertIn("youtube-old", ids)
        self.assertIn("youtube-new", ids)

    def test_cross_process_upload_lock_prevents_duplicate_start(self) -> None:
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


if __name__ == "__main__":
    unittest.main()
