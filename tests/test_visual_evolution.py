from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image

import storage
from mirai_engines.visual_learning import VisualLearningMemory
from mirai_engines.visual_quality_engine import (
    MiraiVisualQualityEngine,
    analyze_image,
    analyze_video_probe,
)
from studio import image_generator
import production_pipeline
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

    def test_highres_refine_keeps_original_when_score_drops(self) -> None:
        original = Image.effect_noise((512, 768), 70).convert("RGB")
        worse = Image.new("RGB", (768, 1152), (5, 5, 5))
        base_report = MiraiVisualQualityEngine().inspect_image(
            original,
            asset_type="mirai",
        )
        selection = {
            "image": original,
            "backend": "fake",
            "quality": base_report,
            "prompt": "test",
            "profile": "detail",
        }
        with (
            patch.object(
                image_generator,
                "visual_highres_enabled",
                return_value=True,
            ),
            patch.object(
                image_generator,
                "_refine_webui",
                return_value=worse,
            ),
        ):
            result = image_generator._highres_refine_selection(
                selection,
                asset_type="mirai",
                seed=1,
            )
        self.assertIs(result["image"], original)

    def test_highres_refine_adopts_equal_or_better_image(self) -> None:
        original = Image.effect_noise((512, 768), 45).convert("RGB")
        refined = Image.effect_noise((768, 1152), 75).convert("RGB")
        base_report = MiraiVisualQualityEngine().inspect_image(
            original,
            asset_type="mirai",
        )
        selection = {
            "image": original,
            "backend": "fake",
            "quality": base_report,
            "prompt": "test",
            "profile": "detail",
        }
        with (
            patch.object(
                image_generator,
                "visual_highres_enabled",
                return_value=True,
            ),
            patch.object(
                image_generator,
                "_refine_webui",
                return_value=refined,
            ),
        ):
            result = image_generator._highres_refine_selection(
                selection,
                asset_type="mirai",
                seed=1,
            )
        self.assertTrue(result.get("highres_refined"))
        self.assertEqual(result["image"].size, (768, 1152))

    def test_highres_refine_failure_safely_falls_back(self) -> None:
        original = Image.effect_noise((512, 768), 70).convert("RGB")
        base_report = MiraiVisualQualityEngine().inspect_image(
            original,
            asset_type="mirai",
        )
        selection = {
            "image": original,
            "backend": "fake",
            "quality": base_report,
            "prompt": "test",
            "profile": "detail",
        }
        with (
            patch.object(
                image_generator,
                "visual_highres_enabled",
                return_value=True,
            ),
            patch.object(
                image_generator,
                "_refine_webui",
                side_effect=RuntimeError("GPU busy"),
            ),
        ):
            result = image_generator._highres_refine_selection(
                selection,
                asset_type="mirai",
                seed=1,
            )
        self.assertIs(result["image"], original)
        self.assertFalse(result.get("highres_refined"))
        self.assertIn("GPU busy", result.get("highres_error", ""))

    def test_visual_engine_can_inspect_saved_image(self) -> None:
        path = Path(self.tmp.name) / "image.png"
        Image.effect_noise((512, 768), 70).convert("RGB").save(path)
        report = MiraiVisualQualityEngine().inspect_image(path, asset_type="background")
        self.assertTrue(report["passed"])
        self.assertGreaterEqual(report["score"], 60)

    def test_video_probe_rejects_bad_visual_asset(self) -> None:
        probe = {
            "streams": [
                {
                    "codec_type": "video",
                    "width": 160,
                    "height": 240,
                    "avg_frame_rate": "2/1",
                }
            ],
            "format": {"duration": "0.1"},
        }
        report = analyze_video_probe(probe, file_size=1000)
        self.assertFalse(report.passed)
        self.assertLess(report.score, 60)

    def test_pipeline_falls_back_when_ai_video_visual_quality_is_low(self) -> None:
        fake_video = Path(self.tmp.name) / "ai.mp4"
        fake_video.write_bytes(b"fake-ai-video")
        item = {
            "id": 99,
            "title": "visual gate",
            "idea": {"idea": "test", "angle": "test"},
        }
        fake_settings = SimpleNamespace(
            ai_video_license_confirmed=True,
            ai_video_backend="animatediff",
            visual_video_min_score=60,
        )
        fake_report = {
            "passed": False,
            "score": 25,
            "issues": [{"code": "very_blurry"}],
            "metrics": {},
        }
        with (
            patch.object(production_pipeline, "settings", fake_settings),
            patch.object(production_pipeline, "ai_video_enabled", return_value=True),
            patch.object(production_pipeline, "get_channel_state", return_value=""),
            patch.object(
                production_pipeline,
                "generate_animatediff_clip",
                return_value=str(fake_video),
            ),
            patch.object(
                production_pipeline.MiraiVisualQualityEngine,
                "inspect_video_asset",
                return_value=fake_report,
            ),
            patch.object(production_pipeline, "record_failure") as failure,
        ):
            result = production_pipeline._generate_ai_video_asset(item)

        self.assertIsNone(result)
        self.assertNotIn("ai_video_path", item)
        self.assertEqual(item["ai_video_visual"]["score"], 25)
        failure.assert_called_once()


if __name__ == "__main__":
    unittest.main()
