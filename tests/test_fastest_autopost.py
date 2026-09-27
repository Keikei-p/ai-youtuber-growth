from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import delivery_supervisor
import scheduler
import storage
import upload_recovery
from video import renderer
from youtube import uploader


JST = timezone(timedelta(hours=9))


class FastestAutopostTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db = storage.DB_PATH
        storage.DB_PATH = Path(self.tmp.name) / "fastest-autopost.db"
        storage.init_db()

    def tearDown(self) -> None:
        storage.DB_PATH = self.old_db
        self.tmp.cleanup()

    def _video(self) -> int:
        video_id = storage.save_video(
            idea="autopost recovery",
            angle="test",
            title="Recovered upload",
            script="safe script",
            description="safe description",
            tags=["mirai"],
            status="rendered",
        )
        output = Path(self.tmp.name) / f"{video_id}.mp4"
        output.write_bytes(b"fake-mp4")
        storage.update_video_output(
            video_id,
            str(output),
            status="rendered",
        )
        return video_id

    def test_default_upload_chunk_is_eight_megabytes(self) -> None:
        fake = SimpleNamespace(youtube_upload_chunk_mb=8)
        with patch.object(uploader, "settings", fake):
            self.assertEqual(
                uploader._upload_chunk_size(),
                8 * 1024 * 1024,
            )
            self.assertEqual(
                uploader._upload_chunk_size() % (256 * 1024),
                0,
            )

    def test_interrupted_upload_is_reconciled_without_reupload(self) -> None:
        video_id = self._video()
        now = datetime(2026, 9, 28, 12, 0, tzinfo=JST)
        with patch.object(scheduler, "_now", return_value=now):
            scheduler._save_upload_intent(
                video_id,
                title="Recovered upload",
                description="safe description",
                privacy="private",
                queue_id=1,
            )

        with (
            patch.object(
                scheduler,
                "find_recent_matching_upload",
                return_value="youtube-recovered",
            ) as find_remote,
            patch.object(scheduler, "_now", return_value=now),
        ):
            receipt = scheduler._reconcile_upload_intent(
                video_id=video_id,
                title="Recovered upload",
                description="safe description",
                privacy="private",
                thumbnail_path=None,
                source="auto_schedule",
            )

        self.assertIsNotNone(receipt)
        self.assertEqual(
            receipt["youtube_video_id"],
            "youtube-recovered",
        )
        find_remote.assert_called_once()
        self.assertEqual(
            storage.get_channel_state(
                f"youtube_upload_intent_{video_id}",
                "missing",
            ),
            "",
        )

    def test_recent_ambiguous_upload_waits_before_retrying(self) -> None:
        video_id = self._video()
        now = datetime(2026, 9, 28, 12, 0, tzinfo=JST)
        with patch.object(scheduler, "_now", return_value=now):
            scheduler._save_upload_intent(
                video_id,
                title="Recovered upload",
                description="safe description",
                privacy="private",
                queue_id=1,
            )

        fake_settings = SimpleNamespace(
            youtube_reconcile_grace_minutes=3,
        )
        with (
            patch.object(
                scheduler,
                "find_recent_matching_upload",
                return_value=None,
            ),
            patch.object(scheduler, "_now", return_value=now),
            patch.object(scheduler, "settings", fake_settings),
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "youtube_upload_reconcile_pending",
            ):
                scheduler._reconcile_upload_intent(
                    video_id=video_id,
                    title="Recovered upload",
                    description="safe description",
                    privacy="private",
                    thumbnail_path=None,
                    source="auto_schedule",
                )

    def test_reconcile_pending_is_rescheduled_not_failed(self) -> None:
        video_id = self._video()
        storage.queue_video(
            video_id,
            "2026-09-28T12:00+09:00",
        )
        row = storage.queue_item_for_video(video_id)
        now = datetime(2026, 9, 28, 12, 1, tzinfo=JST)
        fake_settings = SimpleNamespace(
            youtube_reconcile_grace_minutes=3,
        )
        with patch.object(
            upload_recovery,
            "settings",
            fake_settings,
        ):
            result = upload_recovery.recover_upload_failure(
                row,
                RuntimeError(
                    "youtube_upload_reconcile_pending: checking"
                ),
                now=now,
            )

        self.assertEqual(
            result["code"],
            "upload_reconcile_pending",
        )
        self.assertTrue(result["handled"])
        queue = storage.queue_item_for_video(video_id)
        self.assertEqual(queue["status"], "queued")
        self.assertEqual(
            queue["scheduled_for"],
            "2026-09-28T12:04+09:00",
        )

    def test_armed_autonomy_self_heals_after_episode_one(self) -> None:
        storage.set_channel_state(
            "production_autonomy_armed",
            "true",
        )
        storage.set_channel_state(
            "mirai_first_episode_uploaded",
            "true",
        )
        storage.set_channel_state(
            "automation_enabled",
            "false",
        )
        storage.set_channel_state(
            "auto_upload_enabled",
            "false",
        )

        with patch.object(
            delivery_supervisor,
            "get_credentials",
            return_value=object(),
        ):
            result = (
                delivery_supervisor.self_heal_delivery_controls()
            )

        self.assertEqual(
            storage.get_channel_state("automation_enabled", ""),
            "true",
        )
        self.assertEqual(
            storage.get_channel_state("auto_upload_enabled", ""),
            "true",
        )
        self.assertTrue(result["repairs"])

    def test_fast_render_uses_quality_safe_24fps_floor(self) -> None:
        fake = SimpleNamespace(media_render_fps=24)
        with patch.object(renderer, "settings", fake):
            self.assertEqual(renderer._render_fps(), 24)

        fake_low = SimpleNamespace(media_render_fps=12)
        with patch.object(renderer, "settings", fake_low):
            self.assertEqual(renderer._render_fps(), 24)

    def test_gradient_builder_keeps_full_resolution(self) -> None:
        image = renderer._gradient_background(999)
        self.assertEqual(image.size, (1080, 1920))


if __name__ == "__main__":
    unittest.main()
