from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

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


    def test_metadata_recovery_uses_stricter_safe_limits(self) -> None:
        video_id = self._video()
        storage.set_channel_state(
            f"metadata_sanitize_requested_{video_id}",
            "true",
        )
        row = storage.video_by_id(video_id)
        row["title"] = ("長いタイトル" * 30) + "\x00"
        row["description"] = ("説明文" * 2000) + "\x00"
        row["tags_json"] = json.dumps(
            [f"タグ{i}" * 20 for i in range(30)],
            ensure_ascii=False,
        )

        title, description, tags = scheduler._safe_upload_metadata(
            row
        )

        self.assertLessEqual(len(title), 90)
        self.assertLessEqual(len(description), 4500)
        self.assertLessEqual(len(tags), 15)
        self.assertNotIn("\x00", title)
        self.assertNotIn("\x00", description)
        self.assertTrue(all(len(tag) <= 40 for tag in tags))

    def test_publish_guard_waits_thirty_minutes_not_every_minute(self) -> None:
        video_id = self._video()
        storage.queue_video(
            video_id,
            "2026-09-28T12:00+09:00",
        )
        row = storage.queue_item_for_video(video_id)
        now = datetime(2026, 9, 28, 12, 1, tzinfo=JST)

        result = upload_recovery.recover_upload_failure(
            row,
            RuntimeError(
                "公開前確認待ちのため投稿を停止しました。"
            ),
            now=now,
        )

        self.assertEqual(result["code"], "publish_guard")
        self.assertTrue(result["handled"])
        queue = storage.queue_item_for_video(video_id)
        self.assertEqual(queue["status"], "queued")
        self.assertEqual(
            queue["scheduled_for"],
            "2026-09-28T12:31+09:00",
        )
        self.assertIn(
            "ACTION-REQUIRED:publish_guard",
            str(queue.get("error") or ""),
        )

    def test_remote_reconciliation_cutoff_prefers_intent_time(self) -> None:
        fake_settings = SimpleNamespace(
            youtube_reconcile_lookback_minutes=30,
        )
        channel_call = MagicMock()
        channel_call.list.return_value.execute.return_value = {
            "items": [
                {
                    "contentDetails": {
                        "relatedPlaylists": {
                            "uploads": "UPLOADS",
                        }
                    }
                }
            ]
        }
        playlist_call = MagicMock()
        playlist_call.list.return_value.execute.return_value = {
            "items": [
                {
                    "snippet": {
                        "title": "Same",
                        "description": "Desc",
                        "publishedAt": "2026-09-28T02:40:00Z",
                    },
                    "contentDetails": {
                        "videoId": "old-duplicate",
                    },
                }
            ]
        }
        youtube = SimpleNamespace(
            channels=lambda: channel_call,
            playlistItems=lambda: playlist_call,
        )
        with (
            patch.object(uploader, "settings", fake_settings),
            patch.object(
                uploader,
                "get_credentials",
                return_value=object(),
            ),
            patch.object(uploader, "build", return_value=youtube),
            patch.object(uploader, "datetime") as dt,
        ):
            dt.now.return_value = datetime(
                2026, 9, 28, 3, 0,
                tzinfo=timezone.utc,
            )
            dt.fromisoformat.side_effect = datetime.fromisoformat
            result = uploader.find_recent_matching_upload(
                title="Same",
                description="Desc",
                started_at="2026-09-28T03:00:00+00:00",
            )

        self.assertIsNone(result)


    def test_due_cycle_recovers_remote_upload_without_second_upload(self) -> None:
        video_id = self._video()
        storage.queue_video(
            video_id,
            "2026-09-28T12:00+09:00",
        )
        now = datetime(2026, 9, 28, 12, 1, tzinfo=JST)
        with patch.object(scheduler, "_now", return_value=now):
            scheduler._save_upload_intent(
                video_id,
                title="Recovered upload",
                description="safe description",
                privacy="private",
                queue_id=1,
            )

        verified = {
            "verified": True,
            "exists": True,
            "processing_status": "succeeded",
            "upload_status": "processed",
            "privacy_status": "private",
            "title_match": True,
            "description_match": True,
            "privacy_match": True,
            "duration_present": True,
            "thumbnail_present": True,
            "playback_ready": True,
        }
        with (
            patch.object(scheduler, "_now", return_value=now),
            patch.object(
                scheduler,
                "auto_upload_enabled",
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
                "find_recent_matching_upload",
                return_value="youtube-recovered-due",
            ),
            patch.object(
                scheduler,
                "upload_video",
            ) as upload,
            patch.object(
                scheduler,
                "verify_uploaded_video",
                return_value=verified,
            ),
            patch.object(
                scheduler,
                "cleanup_uploaded_media",
            ),
        ):
            scheduler.run_due()

        upload.assert_not_called()
        row = storage.video_by_id(video_id)
        self.assertEqual(
            row["youtube_video_id"],
            "youtube-recovered-due",
        )
        queue = storage.queue_item_for_video(video_id)
        self.assertEqual(queue["status"], "uploaded")



if __name__ == "__main__":
    unittest.main()
