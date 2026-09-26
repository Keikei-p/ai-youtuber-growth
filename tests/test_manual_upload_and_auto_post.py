from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import scheduler
import storage


JST = timezone(timedelta(hours=9))


class ManualUploadAndAutoPostTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.old_db = storage.DB_PATH
        storage.DB_PATH = self.root / "manual-upload.db"
        storage.init_db()

    def tearDown(self) -> None:
        storage.DB_PATH = self.old_db
        self.tmp.cleanup()

    def _rendered_video(self) -> tuple[int, Path]:
        video_id = storage.save_video(
            idea="manual upload",
            angle="library",
            title="Manual upload test",
            script="safe test script",
            description="safe description",
            tags=["test", "mirai"],
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

    def test_library_upload_works_even_when_auto_upload_is_off(self) -> None:
        video_id, _ = self._rendered_video()
        storage.queue_video(
            video_id,
            "2026-09-26T21:00+09:00",
        )

        with (
            patch.object(
                scheduler,
                "_now",
                return_value=datetime(2026, 9, 26, 20, 0, tzinfo=JST),
            ),
            patch.object(
                scheduler,
                "auto_upload_enabled",
                return_value=False,
            ),
            patch.object(
                scheduler,
                "voice_attribution_status",
                return_value={
                    "resolved": True,
                    "credit": "",
                },
            ),
            patch.object(
                scheduler,
                "publish_gate",
                return_value={
                    "allowed": True,
                    "risks": [],
                },
            ),
            patch.object(
                scheduler,
                "upload_video",
                return_value="youtube-manual-1",
            ) as upload,
        ):
            result = scheduler.upload_saved_video_now(
                video_id,
                "private",
            )

        self.assertEqual(result["status"], "uploaded")
        self.assertEqual(
            result["youtube_video_id"],
            "youtube-manual-1",
        )
        upload.assert_called_once()

        row = storage.video_by_id(video_id)
        self.assertEqual(
            row["youtube_video_id"],
            "youtube-manual-1",
        )
        with storage.connect() as conn:
            queue = conn.execute(
                """
                SELECT status, error
                FROM posting_queue
                WHERE video_id = ?
                """,
                (video_id,),
            ).fetchone()
        self.assertEqual(queue["status"], "uploaded")
        self.assertIsNone(queue["error"])

    def test_library_upload_is_idempotent_after_success(self) -> None:
        video_id, _ = self._rendered_video()

        with (
            patch.object(
                scheduler,
                "voice_attribution_status",
                return_value={
                    "resolved": True,
                    "credit": "",
                },
            ),
            patch.object(
                scheduler,
                "publish_gate",
                return_value={
                    "allowed": True,
                    "risks": [],
                },
            ),
            patch.object(
                scheduler,
                "upload_video",
                return_value="youtube-manual-2",
            ) as upload,
        ):
            first = scheduler.upload_saved_video_now(
                video_id,
                "unlisted",
            )
            second = scheduler.upload_saved_video_now(
                video_id,
                "unlisted",
            )

        self.assertEqual(first["status"], "uploaded")
        self.assertEqual(
            second["status"],
            "already_uploaded",
        )
        self.assertEqual(upload.call_count, 1)

    def test_due_queue_never_returns_already_uploaded_video(self) -> None:
        video_id, _ = self._rendered_video()
        storage.queue_video(
            video_id,
            "2026-09-26T09:00+09:00",
        )
        storage.mark_uploaded(
            video_id,
            "youtube-existing",
        )

        rows = storage.due_queue(
            "2026-09-26T10:00+09:00",
            "2026-09-26T00:00+09:00",
        )
        self.assertEqual(rows, [])

    def test_library_upload_missing_file_fails_without_marking_uploaded(self) -> None:
        video_id, output = self._rendered_video()
        output.unlink()

        with self.assertRaises(FileNotFoundError):
            scheduler.upload_saved_video_now(
                video_id,
                "private",
            )

        row = storage.video_by_id(video_id)
        self.assertFalse(row["youtube_video_id"])

    def test_web_ui_contains_library_upload_and_wake_sync(self) -> None:
        source = Path("webapp.py").read_text(encoding="utf-8")
        self.assertIn("YouTubeへ投稿", source)
        self.assertIn("upload_library_video", source)
        self.assertIn("Windows起床タスクも同期済み", source)
        self.assertIn("_install_wake_task(60)", source)


if __name__ == "__main__":
    unittest.main()
