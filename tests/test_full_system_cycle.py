from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import scheduler
import storage
from runtime_control import (
    set_auto_upload_enabled,
    set_upload_privacy,
)


class FullSystemCycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        storage.DB_PATH = self.root / "system-cycle.db"
        storage.init_db()
        set_auto_upload_enabled(True)
        set_upload_privacy("private")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _make_ready_video(self, index: int) -> int:
        video_id = storage.save_video(
            idea=f"idea-{index}",
            angle="system-test",
            title=f"Mirai system test {index}",
            script="ミライが自分の動画制作システムを検証しています。",
            description=(
                "この動画はAIを使って企画・音声・画像/映像を制作しています。\n\n"
                "VOICEVOX:ずんだもん"
            ),
            tags=["mirai", "test"],
            status="rendered",
        )
        output = self.root / f"video-{index}.mp4"
        output.write_bytes(b"simulated-rendered-video")
        storage.update_video_output(
            video_id,
            str(output),
            status="rendered",
        )
        return video_id

    def test_three_ready_videos_flow_through_queue_legal_gate_and_upload_state(self) -> None:
        tz = ZoneInfo("Asia/Tokyo")
        now = datetime(2026, 9, 25, 15, 0, tzinfo=tz)

        video_ids = [
            self._make_ready_video(index)
            for index in range(1, 4)
        ]

        for offset, video_id in enumerate(video_ids, start=1):
            scheduled = now - timedelta(minutes=offset)
            storage.queue_video(
                video_id,
                scheduled.isoformat(timespec="minutes"),
            )

        uploaded_calls: list[dict] = []

        def fake_upload_video(**kwargs):
            uploaded_calls.append(kwargs)
            return f"yt-test-{len(uploaded_calls)}"

        with (
            patch.object(scheduler, "_now", return_value=now),
            patch.object(scheduler, "reschedule_missed", return_value=0),
            patch.object(
                scheduler,
                "voice_attribution_status",
                return_value={
                    "required": True,
                    "credit": "VOICEVOX:ずんだもん",
                    "resolved": True,
                },
            ),
            patch.object(
                scheduler,
                "upload_video",
                side_effect=fake_upload_video,
            ),
            patch.object(scheduler, "cleanup_uploaded_media"),
            patch.object(scheduler, "_maybe_finish_full_test"),
        ):
            scheduler.run_due()

        self.assertEqual(len(uploaded_calls), 3)
        self.assertTrue(
            all(
                call["privacy_status"] == "private"
                for call in uploaded_calls
            )
        )
        self.assertTrue(
            all(
                call["contains_synthetic_media"] is True
                for call in uploaded_calls
            )
        )

        uploaded_ids = set()
        for video_id in video_ids:
            row = storage.video_by_id(video_id)
            self.assertTrue(
                str(row["youtube_video_id"]).startswith("yt-test-"),
                msg=str(row),
            )
            uploaded_ids.add(row["youtube_video_id"])
            self.assertEqual(row["status"], "uploaded")

        self.assertEqual(
            uploaded_ids,
            {"yt-test-1", "yt-test-2", "yt-test-3"},
        )

        with storage.connect() as conn:
            rows = conn.execute(
                """
                SELECT video_id, status, attempts, error
                FROM posting_queue
                ORDER BY video_id ASC
                """
            ).fetchall()

        self.assertEqual(len(rows), 3)
        self.assertTrue(
            all(row["status"] == "uploaded" for row in rows)
        )
        self.assertTrue(
            all(int(row["attempts"]) == 0 for row in rows)
        )
        self.assertTrue(
            all(row["error"] is None for row in rows)
        )

    def test_risky_video_is_not_sent_to_youtube(self) -> None:
        tz = ZoneInfo("Asia/Tokyo")
        now = datetime(2026, 9, 25, 15, 0, tzinfo=tz)
        video_id = storage.save_video(
            idea="risk",
            angle="system-test",
            title="選挙について",
            script="今日は選挙と候補者について話します。",
            description=(
                "この動画はAIを使って企画・音声・画像/映像を制作しています。\n\n"
                "VOICEVOX:ずんだもん"
            ),
            tags=["test"],
            status="rendered",
        )
        output = self.root / "risky.mp4"
        output.write_bytes(b"simulated-rendered-video")
        storage.update_video_output(video_id, str(output), status="rendered")
        storage.queue_video(
            video_id,
            (now - timedelta(minutes=1)).isoformat(timespec="minutes"),
        )

        with (
            patch.object(scheduler, "_now", return_value=now),
            patch.object(scheduler, "reschedule_missed", return_value=0),
            patch.object(
                scheduler,
                "voice_attribution_status",
                return_value={
                    "required": True,
                    "credit": "VOICEVOX:ずんだもん",
                    "resolved": True,
                },
            ),
            patch.object(scheduler, "upload_video") as uploader,
            patch.object(scheduler, "_maybe_finish_full_test"),
        ):
            scheduler.run_due()

        uploader.assert_not_called()
        row = storage.video_by_id(video_id)
        self.assertIsNone(row["youtube_video_id"])
        with storage.connect() as conn:
            queue_row = conn.execute(
                "SELECT status, attempts FROM posting_queue WHERE video_id = ?",
                (video_id,),
            ).fetchone()
        self.assertEqual(queue_row["status"], "queued")
        self.assertEqual(int(queue_row["attempts"]), 0)


if __name__ == "__main__":
    unittest.main()
