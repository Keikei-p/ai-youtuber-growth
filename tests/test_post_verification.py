from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from youtube.post_verifier import (
    UploadVerificationError,
    verify_uploaded_video,
)


def _service_with_responses(responses):
    execute = MagicMock(side_effect=list(responses))
    request = MagicMock()
    request.execute = execute
    videos = MagicMock()
    videos.list.return_value = request
    service = MagicMock()
    service.videos.return_value = videos
    return service


class PostVerificationTests(unittest.TestCase):
    def test_processing_then_success_is_verified(self) -> None:
        service = _service_with_responses([
            {
                "items": [{
                    "id": "yt-1",
                    "snippet": {
                        "title": "Title",
                        "description": "Description",
                        "thumbnails": {"default": {"url": "x"}},
                    },
                    "status": {
                        "uploadStatus": "uploaded",
                        "privacyStatus": "private",
                    },
                    "processingDetails": {
                        "processingStatus": "processing",
                    },
                    "contentDetails": {"duration": "PT20S"},
                }]
            },
            {
                "items": [{
                    "id": "yt-1",
                    "snippet": {
                        "title": "Title",
                        "description": "Description",
                        "thumbnails": {"default": {"url": "x"}},
                    },
                    "status": {
                        "uploadStatus": "processed",
                        "privacyStatus": "private",
                    },
                    "processingDetails": {
                        "processingStatus": "succeeded",
                    },
                    "contentDetails": {"duration": "PT20S"},
                }]
            },
        ])
        with (
            patch("youtube.post_verifier.build", return_value=service),
            patch(
                "youtube.post_verifier.get_credentials",
                return_value=object(),
            ),
            patch("youtube.post_verifier.time.sleep"),
        ):
            result = verify_uploaded_video(
                "yt-1",
                expected_title="Title",
                expected_description="Description",
                expected_privacy="private",
                max_attempts=3,
                poll_seconds=0.01,
            )

        self.assertTrue(result["verified"])
        self.assertTrue(result["playback_ready"])
        self.assertEqual(result["attempt"], 2)

    def test_metadata_mismatch_is_not_success(self) -> None:
        service = _service_with_responses([{
            "items": [{
                "id": "yt-2",
                "snippet": {
                    "title": "Wrong",
                    "description": "Description",
                    "thumbnails": {"default": {"url": "x"}},
                },
                "status": {
                    "uploadStatus": "processed",
                    "privacyStatus": "private",
                },
                "processingDetails": {
                    "processingStatus": "succeeded",
                },
                "contentDetails": {"duration": "PT20S"},
            }]
        }])
        with (
            patch("youtube.post_verifier.build", return_value=service),
            patch(
                "youtube.post_verifier.get_credentials",
                return_value=object(),
            ),
        ):
            with self.assertRaises(UploadVerificationError) as ctx:
                verify_uploaded_video(
                    "yt-2",
                    expected_title="Title",
                    expected_description="Description",
                    expected_privacy="private",
                    max_attempts=1,
                    poll_seconds=0,
                )
        self.assertEqual(ctx.exception.code, "youtube_metadata_mismatch")
        self.assertFalse(ctx.exception.terminal)

    def test_processing_failure_is_terminal(self) -> None:
        service = _service_with_responses([{
            "items": [{
                "id": "yt-3",
                "snippet": {
                    "title": "Title",
                    "description": "Description",
                    "thumbnails": {"default": {"url": "x"}},
                },
                "status": {
                    "uploadStatus": "failed",
                    "privacyStatus": "private",
                    "failureReason": "conversion",
                },
                "processingDetails": {
                    "processingStatus": "failed",
                    "processingFailureReason": "transcodeFailed",
                },
                "contentDetails": {"duration": ""},
            }]
        }])
        with (
            patch("youtube.post_verifier.build", return_value=service),
            patch(
                "youtube.post_verifier.get_credentials",
                return_value=object(),
            ),
        ):
            with self.assertRaises(UploadVerificationError) as ctx:
                verify_uploaded_video(
                    "yt-3",
                    expected_title="Title",
                    expected_description="Description",
                    expected_privacy="private",
                    max_attempts=1,
                    poll_seconds=0,
                )
        self.assertEqual(ctx.exception.code, "youtube_processing_failed")
        self.assertTrue(ctx.exception.terminal)


if __name__ == "__main__":
    unittest.main()
