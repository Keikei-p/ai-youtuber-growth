from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import learning_cleanup
import storage
from mirai_engines.visual_learning import VisualLearningMemory


class LearningCleanupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.old_db = storage.DB_PATH
        storage.DB_PATH = self.root / "learning-cleanup.db"
        storage.init_db()

        self.video_dir = self.root / "videos"
        self.audio_dir = self.root / "audio"
        self.generated = self.root / "generated"
        for folder in (
            self.video_dir,
            self.audio_dir,
            self.generated / "mirai",
            self.generated / "guests",
            self.generated / "backgrounds",
            self.generated / "videos",
        ):
            folder.mkdir(parents=True, exist_ok=True)

        self.patchers = [
            patch.object(learning_cleanup, "VIDEO_DIR", self.video_dir),
            patch.object(learning_cleanup, "AUDIO_DIR", self.audio_dir),
            patch.object(learning_cleanup, "GENERATED_ROOT", self.generated),
            patch.object(learning_cleanup, "prune_missing_assets", return_value=3),
        ]
        for p in self.patchers:
            p.start()

    def tearDown(self) -> None:
        for p in reversed(self.patchers):
            p.stop()
        storage.DB_PATH = self.old_db
        self.tmp.cleanup()

    def _video(self, *, uploaded: bool = True) -> int:
        video_id = storage.save_video(
            idea="cleanup",
            angle="learning",
            title="cleanup",
            script="cleanup",
            status="rendered",
        )
        output = self.video_dir / f"final_{video_id}.mp4"
        output.write_bytes(b"video" * 100)
        storage.update_video_output(video_id, str(output), status="rendered")
        if uploaded:
            storage.mark_uploaded(video_id, f"yt-{video_id}")
        return video_id

    def _finish_learning(self, video_id: int) -> None:
        storage.save_analytics_snapshot(
            video_id=video_id,
            checkpoint_hours=168,
            captured_at="2026-09-25T10:00:00+00:00",
            metrics={"views": 100, "averageViewPercentage": 61},
            note="learned",
            score=72,
        )

    def test_cleanup_waits_for_final_learning_checkpoint(self) -> None:
        video_id = self._video()
        output = Path(storage.video_by_id(video_id)["output_path"])
        result = learning_cleanup.cleanup_after_final_learning(video_id)
        self.assertEqual(result["status"], "not_ready")
        self.assertTrue(output.exists())

    def test_cleanup_deletes_used_media_but_preserves_active_guest(self) -> None:
        video_id = self._video()
        mirai = self.generated / "mirai" / "mirai.png"
        guest = self.generated / "guests" / "guest.png"
        bg = self.generated / "backgrounds" / "bg.png"
        ai = self.generated / "videos" / "ai.mp4"
        audio = self.audio_dir / f"20260925_{video_id}.wav"
        for path in (mirai, guest, bg, ai, audio):
            path.write_bytes(b"x" * 128)

        guest_id = storage.create_guest(
            "guest",
            {"role": "test"},
            "prompt",
            image_path=str(guest),
        )
        self.assertGreater(guest_id, 0)

        VisualLearningMemory().save_video_profile(
            video_id,
            {
                "asset_paths": {
                    "mirai": str(mirai),
                    "guest": str(guest),
                    "backgrounds": [str(bg)],
                    "ai_video": str(ai),
                },
                "profiles": ["detail"],
                "avg_visual_score": 80,
            },
        )
        self._finish_learning(video_id)

        output = Path(storage.video_by_id(video_id)["output_path"])
        result = learning_cleanup.cleanup_after_final_learning(video_id)

        self.assertEqual(result["status"], "cleaned")
        self.assertFalse(output.exists())
        self.assertFalse(audio.exists())
        self.assertFalse(mirai.exists())
        self.assertFalse(bg.exists())
        self.assertFalse(ai.exists())
        self.assertTrue(guest.exists())
        self.assertIsNone(storage.video_by_id(video_id)["output_path"])
        self.assertTrue(result["learning_data_preserved"])
        self.assertTrue(
            VisualLearningMemory().video_profile(video_id).get(
                "media_cleaned_at"
            )
        )

    def test_cleanup_preserves_asset_used_by_unfinished_video(self) -> None:
        first = self._video()
        second = self._video()
        shared = self.generated / "mirai" / "shared.png"
        first_bg = self.generated / "backgrounds" / "first.png"
        shared.write_bytes(b"shared")
        first_bg.write_bytes(b"first")

        VisualLearningMemory().save_video_profile(
            first,
            {
                "asset_paths": {
                    "mirai": str(shared),
                    "backgrounds": [str(first_bg)],
                }
            },
        )
        VisualLearningMemory().save_video_profile(
            second,
            {
                "asset_paths": {
                    "mirai": str(shared),
                    "backgrounds": [],
                }
            },
        )
        self._finish_learning(first)

        result = learning_cleanup.cleanup_after_final_learning(first)
        self.assertTrue(shared.exists())
        self.assertFalse(first_bg.exists())
        self.assertIn(str(shared.resolve()), result["protected_paths"])

    def test_cleanup_is_idempotent(self) -> None:
        video_id = self._video()
        self._finish_learning(video_id)
        first = learning_cleanup.cleanup_after_final_learning(video_id)
        second = learning_cleanup.cleanup_after_final_learning(video_id)
        self.assertEqual(first["removed_files"], second["removed_files"])
        self.assertEqual(second["status"], "already_cleaned")


if __name__ == "__main__":
    unittest.main()
