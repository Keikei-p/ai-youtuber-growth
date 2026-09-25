from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import growth_engine
import main
import media_cleanup
import metadata
import planner
import self_improvement
import storage
import writer
from mirai_engines.improvement_engine import MiraiImprovementEngine


class _OfflineClient:
    def available(self) -> bool:
        return False


class _DuplicateClient:
    def available(self) -> bool:
        return True

    def generate_json(self, prompt: str):
        return [
            {
                "idea": "AIがYouTuberを始めて最初に困ったこと",
                "angle": "duplicate",
                "hook": "duplicate",
                "experiment_type": "new",
            }
        ]


class AutonomousGrowthRegressionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.old_db = storage.DB_PATH
        storage.DB_PATH = self.root / "growth-regression.db"
        storage.init_db()

    def tearDown(self) -> None:
        storage.DB_PATH = self.old_db
        self.tmp.cleanup()

    def test_offline_planner_rotates_without_repeating_recent_ideas(self) -> None:
        recent: list[dict] = []
        picked: list[str] = []
        with patch.object(planner, "OllamaClient", return_value=_OfflineClient()):
            for _ in range(8):
                item = planner.plan_ideas({"name": "Mirai"}, recent, 1)[0]
                picked.append(item["idea"])
                recent.insert(0, {"idea": item["idea"]})

        self.assertEqual(len(picked), 8)
        self.assertEqual(len(set(picked)), 8)

    def test_ai_duplicate_idea_is_filtered_and_replaced(self) -> None:
        recent = [
            {
                "idea": "AIがYouTuberを始めて最初に困ったこと",
            }
        ]
        with patch.object(planner, "OllamaClient", return_value=_DuplicateClient()):
            item = planner.plan_ideas({"name": "Mirai"}, recent, 1)[0]

        self.assertNotEqual(
            planner._idea_key(item["idea"]),
            planner._idea_key(recent[0]["idea"]),
        )

    def test_three_video_text_generation_reaches_target_without_ollama(self) -> None:
        storage.set_channel_state(main.FIRST_EPISODE_STATE_KEY, "true")
        plan_dir = self.root / "plans"

        with (
            patch.object(main, "ensure_runtime_dirs"),
            patch.object(main, "PLAN_DIR", plan_dir),
            patch.object(main, "load_character", return_value={"name": "ミライ"}),
            patch.object(main, "unload_ollama_model"),
            patch.object(main, "release_torch_cuda_cache"),
            patch.object(main, "select_guest_for_next_video", return_value=None),
            patch.object(planner, "OllamaClient", return_value=_OfflineClient()),
            patch.object(writer, "OllamaClient", return_value=_OfflineClient()),
            patch.object(metadata, "OllamaClient", return_value=_OfflineClient()),
        ):
            results = main.run_generation(
                render=False,
                upload=False,
                target_override=3,
            )

        self.assertEqual(len(results), 3)
        self.assertEqual(
            len({item["script"] for item in results}),
            3,
        )
        self.assertEqual(
            len({item["title"] for item in results}),
            3,
        )

    def test_duplicate_failure_becomes_planner_guidance(self) -> None:
        self_improvement.record_failure(
            "text.script_quality",
            "exact_duplicate_script",
            {"idea": "duplicate"},
        )
        with patch.object(
            self_improvement,
            "OllamaClient",
            return_value=_OfflineClient(),
        ):
            report = self_improvement.run_improvement_review()

        action_types = {
            item.get("action_type")
            for item in report.get("actions") or []
        }
        self.assertIn("planner_guidance", action_types)
        guidance = storage.get_channel_state(
            "autonomous_planner_guidance",
            "",
        )
        self.assertIn("直近10本", guidance)

    def test_post_upload_cleanup_waits_until_final_learning(self) -> None:
        video_id = storage.save_video(
            idea="cleanup",
            angle="guard",
            title="cleanup",
            script="cleanup",
            status="rendered",
        )
        video = self.root / "final.mp4"
        audio_dir = self.root / "audio"
        audio_dir.mkdir(parents=True, exist_ok=True)
        video.write_bytes(b"video")
        storage.update_video_output(video_id, str(video), status="rendered")
        storage.mark_uploaded(video_id, "yt-cleanup")
        audio = audio_dir / "final.wav"
        audio.write_bytes(b"audio")

        with patch.object(media_cleanup, "AUDIO_DIR", audio_dir):
            first = media_cleanup.cleanup_uploaded_media(
                video_id,
                str(video),
            )
            self.assertEqual(first, [])
            self.assertTrue(video.exists())
            self.assertTrue(audio.exists())

            storage.save_analytics_snapshot(
                video_id=video_id,
                checkpoint_hours=168,
                captured_at="2026-09-25T10:00:00+00:00",
                metrics={
                    "views": 10,
                    "averageViewPercentage": 55,
                },
                note="final",
                score=60,
            )
            second = media_cleanup.cleanup_uploaded_media(
                video_id,
                str(video),
            )

        self.assertEqual(len(second), 2)
        self.assertFalse(video.exists())
        self.assertFalse(audio.exists())
        self.assertIsNone(
            storage.video_by_id(video_id)["output_path"]
        )

    def test_final_learning_cleanup_retries_on_later_cycle(self) -> None:
        video_id = storage.save_video(
            idea="retry-cleanup",
            angle="growth",
            title="retry-cleanup",
            script="retry-cleanup",
            status="uploaded",
        )
        storage.mark_uploaded(video_id, "yt-retry")
        storage.save_analytics_snapshot(
            video_id=video_id,
            checkpoint_hours=168,
            captured_at="2026-09-25T10:00:00+00:00",
            metrics={
                "views": 100,
                "averageViewPercentage": 60,
            },
            note="learned",
            score=70,
        )

        with (
            patch.object(
                growth_engine,
                "due_snapshot_candidates",
                return_value=[],
            ),
            patch.object(
                growth_engine,
                "cleanup_status",
                return_value=None,
            ),
            patch.object(
                growth_engine,
                "cleanup_after_final_learning",
                return_value={
                    "status": "cleaned",
                    "removed_files": 1,
                    "freed_mb": 1.0,
                },
            ) as cleanup,
        ):
            captured = growth_engine.run_growth_cycle()

        self.assertEqual(captured, 0)
        cleanup.assert_called_once_with(video_id)


if __name__ == "__main__":
    unittest.main()
