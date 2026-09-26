from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import storage
from youtube.uploader import upload_video


class OperationalEdgeTests(unittest.TestCase):
    def test_upload_video_builds_expected_youtube_request_and_returns_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            video_path = Path(tmp) / "sample.mp4"
            video_path.write_bytes(b"fake-mp4")

            credentials = object()
            media_upload = object()
            service = MagicMock()
            request = MagicMock()
            request.next_chunk.return_value = (
                None,
                {"id": "youtube-test-id"},
            )
            service.videos.return_value.insert.return_value = request

            with (
                patch(
                    "youtube.uploader.get_credentials",
                    return_value=credentials,
                ) as auth_mock,
                patch("youtube.uploader.build", return_value=service) as build_mock,
                patch(
                    "youtube.uploader.MediaFileUpload",
                    return_value=media_upload,
                ) as media_mock,
            ):
                youtube_id = upload_video(
                    video_path=video_path,
                    title="T" * 120,
                    description="description",
                    tags=["mirai", "test"],
                    privacy_status="private",
                    category_id="22",
                    default_language="ja",
                    contains_synthetic_media=True,
                )

            self.assertEqual(youtube_id, "youtube-test-id")
            build_mock.assert_called_once_with(
                "youtube",
                "v3",
                credentials=credentials,
            )
            auth_mock.assert_called_once_with(interactive=False)
            media_mock.assert_called_once_with(
                str(video_path),
                mimetype="video/mp4",
                chunksize=-1,
                resumable=True,
            )
            insert_kwargs = service.videos.return_value.insert.call_args.kwargs
            self.assertIs(insert_kwargs["media_body"], media_upload)
            self.assertEqual(insert_kwargs["part"], "snippet,status")
            self.assertEqual(len(insert_kwargs["body"]["snippet"]["title"]), 100)
            self.assertEqual(
                insert_kwargs["body"]["snippet"]["description"],
                "description",
            )
            self.assertEqual(
                insert_kwargs["body"]["snippet"]["tags"],
                ["mirai", "test"],
            )
            self.assertEqual(
                insert_kwargs["body"]["status"]["privacyStatus"],
                "private",
            )
            self.assertFalse(
                insert_kwargs["body"]["status"]["selfDeclaredMadeForKids"]
            )
            self.assertTrue(
                insert_kwargs["body"]["status"]["containsSyntheticMedia"]
            )
            request.next_chunk.assert_called_once_with(
                num_retries=5,
            )


    def test_upload_video_rejects_missing_youtube_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            video_path = Path(tmp) / "sample.mp4"
            video_path.write_bytes(b"fake-mp4")

            service = MagicMock()
            request = MagicMock()
            request.next_chunk.return_value = (None, {})
            service.videos.return_value.insert.return_value = request

            with (
                patch(
                    "youtube.uploader.get_credentials",
                    return_value=object(),
                ),
                patch("youtube.uploader.build", return_value=service),
                patch("youtube.uploader.MediaFileUpload"),
            ):
                with self.assertRaises(RuntimeError):
                    upload_video(
                        video_path=video_path,
                        title="test",
                        description="test",
                    )

    def test_noninteractive_auth_does_not_open_browser_when_token_missing(self) -> None:
        import youtube.auth as youtube_auth

        with tempfile.TemporaryDirectory() as tmp:
            token = Path(tmp) / "missing-token.json"
            secret = Path(tmp) / "client-secret.json"
            secret.write_text("{}", encoding="utf-8")
            with (
                patch.object(youtube_auth, "TOKEN_FILE", token),
                patch.object(youtube_auth, "CLIENT_SECRET_FILE", secret),
                patch.object(
                    youtube_auth.InstalledAppFlow,
                    "from_client_secrets_file",
                ) as flow_mock,
            ):
                with self.assertRaises(FileNotFoundError):
                    youtube_auth.get_credentials(interactive=False)
            flow_mock.assert_not_called()

    def test_ai_video_license_gate_persists_and_disabling_license_disables_video(self) -> None:
        import runtime_control

        with tempfile.TemporaryDirectory() as tmp:
            old_db = storage.DB_PATH
            try:
                storage.DB_PATH = Path(tmp) / "ai-video-gate.db"
                storage.init_db()

                runtime_control.set_ai_video_license_confirmed(True)
                runtime_control.set_ai_video_enabled(True)
                self.assertTrue(runtime_control.ai_video_license_confirmed())
                self.assertTrue(runtime_control.ai_video_enabled())

                runtime_control.set_ai_video_license_confirmed(False)
                self.assertFalse(runtime_control.ai_video_license_confirmed())
                self.assertFalse(runtime_control.ai_video_enabled())
            finally:
                storage.DB_PATH = old_db

    def test_upload_video_rejects_missing_file_before_auth(self) -> None:
        missing = Path("definitely-missing-final-test.mp4")
        with patch("youtube.uploader.get_credentials") as auth_mock:
            with self.assertRaises(FileNotFoundError):
                upload_video(
                    video_path=missing,
                    title="missing",
                    description="missing",
                )
        auth_mock.assert_not_called()

    def test_posting_queue_stops_after_five_upload_failures(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            old_db = storage.DB_PATH
            try:
                storage.DB_PATH = Path(tmp) / "retry-policy.db"
                storage.init_db()
                video_id = storage.save_video(
                    idea="retry",
                    angle="final-test",
                    title="Retry policy",
                    script="retry test",
                    description="",
                    tags=["test"],
                    status="rendered",
                )
                storage.queue_video(video_id, "2026-09-25T15:00")

                with storage.connect() as conn:
                    queue_id = int(
                        conn.execute(
                            "SELECT id FROM posting_queue WHERE video_id = ?",
                            (video_id,),
                        ).fetchone()["id"]
                    )

                for attempt in range(1, 5):
                    storage.mark_queue_error(queue_id, f"failure-{attempt}")
                    with storage.connect() as conn:
                        row = conn.execute(
                            "SELECT status, attempts, error FROM posting_queue WHERE id = ?",
                            (queue_id,),
                        ).fetchone()
                    self.assertEqual(row["status"], "queued")
                    self.assertEqual(int(row["attempts"]), attempt)
                    self.assertEqual(row["error"], f"failure-{attempt}")

                storage.mark_queue_error(queue_id, "failure-5")
                with storage.connect() as conn:
                    row = conn.execute(
                        "SELECT status, attempts, error FROM posting_queue WHERE id = ?",
                        (queue_id,),
                    ).fetchone()
                self.assertEqual(row["status"], "failed")
                self.assertEqual(int(row["attempts"]), 5)
                self.assertEqual(row["error"], "failure-5")

                due = storage.due_queue("2026-09-25T16:00")
                self.assertFalse(
                    any(int(item["video_id"]) == video_id for item in due)
                )
            finally:
                storage.DB_PATH = old_db


if __name__ == "__main__":
    unittest.main()
