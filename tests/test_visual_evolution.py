from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image

import storage
from mirai_engines.visual_learning import VisualLearningMemory
from mirai_engines.visual_quality_engine import MiraiVisualQualityEngine, analyze_image
from studio import image_generator
from studio.models import ImagePreset


class VisualEvolutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        storage.DB_PATH = Path(self.tmp.name) / "visual.db"
        storage.init_db()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_visual_quality_prefers_detailed_image(self) -> None:
        bad = Image.new("RGB", (512, 768), (5, 5, 5))
        good = Image.effect_noise((512, 768), 70).convert("RGB")
        bad_report = analyze_image(bad, asset_type="mirai")
        good_report = analyze_image(good, asset_type="mirai")
        self.assertLess(bad_report.score, good_report.score)
        self.assertFalse(bad_report.passed)
        self.assertTrue(good_report.passed)

    def test_learning_memory_prefers_higher_scoring_profile(self) -> None:
        memory = VisualLearningMemory()
        for score in (84, 91, 88):
            memory.record_result(asset_type="mirai", path="", prompt="detail", backend="fake",
                                 profile="detail", score=score, passed=True, accepted=True)
        for score in (68, 70):
            memory.record_result(asset_type="mirai", path="", prompt="balanced", backend="fake",
                                 profile="balanced", score=score, passed=True, accepted=True)
        self.assertEqual(memory.recommended_profile("mirai"), "detail")

    def test_youtube_performance_builds_visual_strategy(self) -> None:
        memory = VisualLearningMemory()
        rows = [
            (1, "detail", "smile", 72.0),
            (2, "detail", "smile", 68.0),
            (3, "balanced", "normal", 41.0),
            (4, "cinematic", "serious", 45.0),
        ]
        for video_id, profile, expression, retention in rows:
            memory.save_video_profile(video_id, {
                "profiles": [profile], "expression": expression,
                "avg_visual_score": 80, "ai_video_used": False,
            })
            memory.record_performance(video_id=video_id, checkpoint_hours=24,
                                      avg_view_percentage=retention, views=100,
                                      analytics_score=retention)
        strategy = memory.build_strategy()
        self.assertEqual(strategy["status"], "active")
        self.assertEqual(strategy["preferred_profile"], "detail")
        self.assertEqual(strategy["preferred_expression"], "smile")

    def test_image_generator_selects_best_candidate(self) -> None:
        bad = Image.new("RGB", (512, 768), (3, 3, 3))
        good = Image.effect_noise((512, 768), 70).convert("RGB")
        fake_settings = SimpleNamespace(
            visual_candidate_count=2, visual_background_candidates=1,
            visual_retry_rounds=0, visual_min_score=60,
        )
        with (
            patch.object(image_generator, "settings", fake_settings),
            patch.object(image_generator, "_generate",
                         side_effect=[(bad, "fake-backend"), (good, "fake-backend")]),
            patch.object(image_generator, "_seed_base", return_value=100),
        ):
            selected = image_generator._generate_best_image(
                "base prompt", ImagePreset(512, 768, 8, 6.0),
                asset_type="mirai", meta={"expression": "normal"},
            )
        self.assertGreaterEqual(selected["quality"]["score"], 60)
        self.assertIs(selected["image"], good)

    def test_visual_engine_can_inspect_saved_image(self) -> None:
        path = Path(self.tmp.name) / "image.png"
        Image.effect_noise((512, 768), 70).convert("RGB").save(path)
        report = MiraiVisualQualityEngine().inspect_image(path, asset_type="background")
        self.assertTrue(report["passed"])
        self.assertGreaterEqual(report["score"], 60)


if __name__ == "__main__":
    unittest.main()
