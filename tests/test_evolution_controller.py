from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import storage
from mirai_engines import evolution_controller
from mirai_engines.voice_engine import _parameters
from mirai_engines.visual_learning import VisualLearningMemory


class EvolutionControllerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.old_db = storage.DB_PATH
        storage.DB_PATH = self.root / "evolution.db"
        storage.init_db()

    def tearDown(self) -> None:
        storage.DB_PATH = self.old_db
        self.tmp.cleanup()

    def _training_root(self, name: str) -> Path:
        path = self.root / "training" / name
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _video(self) -> int:
        return storage.save_video(
            idea="AIが昨日より成長した理由",
            angle="改善前後を比較",
            title="AIは1週間でどこまで成長できる？",
            script=(
                "昨日の失敗を分析しました。"
                "今日は冒頭を短くして、結論を先に伝えます。"
            ),
            description="完全AI運営の成長記録です。",
            tags=["AI", "成長"],
            status="uploaded",
        )

    def test_strong_final_video_becomes_brain_teacher(self) -> None:
        video_id = self._video()
        with patch.object(
            evolution_controller,
            "component_training_root",
            side_effect=self._training_root,
        ):
            result = evolution_controller.harvest_final_video(
                video_id,
                metrics={
                    "views": 1200,
                    "likes": 90,
                    "comments": 15,
                    "averageViewPercentage": 82,
                },
                score=84.0,
                note="冒頭とテンポが強い。",
            )

        teacher = (
            self._training_root("brain")
            / "auto"
            / f"youtube_{video_id}.txt"
        )
        self.assertEqual(result["outcome"], "strong")
        self.assertTrue(result["brain_added"])
        self.assertTrue(teacher.is_file())
        text = teacher.read_text(encoding="utf-8")
        self.assertIn("AIは1週間でどこまで成長できる？", text)
        self.assertIn("昨日の失敗を分析しました", text)
        self.assertIn("内容の丸写しではなく", text)

    def test_weak_video_is_learned_as_outcome_but_not_brain_teacher(self) -> None:
        video_id = self._video()
        with patch.object(
            evolution_controller,
            "component_training_root",
            side_effect=self._training_root,
        ):
            result = evolution_controller.harvest_final_video(
                video_id,
                metrics={
                    "views": 10,
                    "likes": 0,
                    "comments": 0,
                    "averageViewPercentage": 28,
                },
                score=31.0,
                note="離脱が多い。",
            )

        teacher = (
            self._training_root("brain")
            / "auto"
            / f"youtube_{video_id}.txt"
        )
        self.assertEqual(result["outcome"], "weak")
        self.assertFalse(result["brain_added"])
        self.assertFalse(teacher.exists())

    def test_external_image_is_not_silently_used_as_native_teacher(self) -> None:
        source = self.root / "external.png"
        source.write_bytes(b"fake")
        fake_row = {
            "backend": "diffusers-cuda",
            "accepted": True,
        }

        with (
            patch.object(
                evolution_controller,
                "component_training_root",
                side_effect=self._training_root,
            ),
            patch.object(
                VisualLearningMemory,
                "find_by_path",
                return_value=fake_row,
            ),
        ):
            added = evolution_controller._copy_native_image_teacher(
                video_id=1,
                source_raw=str(source),
                caption="test",
            )

        self.assertFalse(added)
        self.assertFalse(
            (self._training_root("image") / "metadata.csv").exists()
        )

    def test_native_image_can_become_teacher(self) -> None:
        from PIL import Image

        source = self.root / "native.png"
        Image.new("RGB", (32, 32)).save(source)
        fake_row = {
            "backend": "mirai-native-image-v0",
            "accepted": True,
        }

        with (
            patch.object(
                evolution_controller,
                "component_training_root",
                side_effect=self._training_root,
            ),
            patch.object(
                VisualLearningMemory,
                "find_by_path",
                return_value=fake_row,
            ),
        ):
            added = evolution_controller._copy_native_image_teacher(
                video_id=2,
                source_raw=str(source),
                caption="ミライの成功画像",
            )

        self.assertTrue(added)
        metadata = (
            self._training_root("image") / "metadata.csv"
        ).read_text(encoding="utf-8")
        self.assertIn("ミライの成功画像", metadata)

    def test_voice_retention_learning_changes_safe_parameters(self) -> None:
        normal = _parameters(
            "neutral",
            "こんにちは。",
            0,
            2,
            {},
        )
        weak_retention = _parameters(
            "neutral",
            "こんにちは。",
            0,
            2,
            {"last_retention": 40},
        )
        self.assertGreater(weak_retention.speed, normal.speed)
        self.assertLess(weak_retention.pause_ms, normal.pause_ms)
        self.assertLessEqual(weak_retention.speed, 1.18)
        self.assertGreaterEqual(weak_retention.pause_ms, 80)

    def test_learning_coverage_reports_all_major_stages(self) -> None:
        with (
            patch.object(
                evolution_controller,
                "corpus_status",
                return_value={"ready": False},
            ),
            patch.object(
                evolution_controller,
                "image_dataset_status",
                return_value={"ready": False},
            ),
            patch.object(
                evolution_controller,
                "video_dataset_status",
                return_value={"ready": False},
            ),
            patch.object(
                evolution_controller,
                "inspect_training_dataset",
                return_value={"ready": False},
            ),
        ):
            state = evolution_controller.evolution_status()

        coverage = state["learning_coverage"]
        for key in (
            "planning",
            "script",
            "metadata",
            "voice_strategy",
            "image_strategy",
            "video_strategy",
            "native_brain_teacher",
            "native_image_teacher",
            "native_video_teacher",
            "native_voice_teacher",
            "youtube_feedback",
            "failure_feedback",
        ):
            self.assertIn(key, coverage)


if __name__ == "__main__":
    unittest.main()
