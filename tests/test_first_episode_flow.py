from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import main
import storage


class FirstEpisodeFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db = storage.DB_PATH
        storage.DB_PATH = Path(self.tmp.name) / "first-episode.db"
        storage.init_db()

    def tearDown(self) -> None:
        storage.DB_PATH = self.old_db
        self.tmp.cleanup()

    def _normal_idea(self) -> list[dict]:
        return [{
            "idea": "第2話の通常企画",
            "angle": "学習型の通常企画",
            "hook": "第2話です",
            "experiment_type": "new",
        }]

    def _normal_written(self) -> dict:
        return {
            "title": "通常の第2話",
            "script": "これは通常の学習型自動企画です。",
        }

    def _normal_metadata(self) -> dict:
        return {
            "title": "通常の第2話",
            "description": "通常説明",
            "tags": ["通常", "学習型"],
        }

    def _patch_generation(self):
        return (
            patch.object(main, "ensure_runtime_dirs"),
            patch.object(main, "load_character", return_value={"name": "ミライ"}),
            patch.object(main, "unload_ollama_model"),
            patch.object(main, "release_torch_cuda_cache"),
            patch.object(main, "plan_ideas", return_value=self._normal_idea()),
            patch.object(main, "select_guest_for_next_video", return_value=None),
            patch.object(main, "_make_valid_script", return_value=self._normal_written()),
            patch.object(main, "build_metadata", return_value=self._normal_metadata()),
        )

    def test_first_episode_package_has_launch_metadata(self) -> None:
        package = main._first_episode_package()
        self.assertIn("完全AIユーチューバー", package["written"]["title"])
        self.assertIn("今日", package["written"]["script"])
        self.assertIn("投稿するたび", package["written"]["script"])
        self.assertIn("AI", package["metadata"]["description"])
        self.assertIn("AIユーチューバー", package["metadata"]["tags"])

    def test_only_first_item_is_special_then_normal_planner_runs(self) -> None:
        patches = self._patch_generation()
        with patches[0], patches[1], patches[2], patches[3], \
             patches[4] as planner, patches[5], patches[6], patches[7]:
            results = main.run_generation(
                render=False,
                upload=False,
                target_override=2,
            )

        self.assertEqual(len(results), 2)
        self.assertTrue(results[0]["first_episode"])
        self.assertFalse(results[1]["first_episode"])
        self.assertIn("完全AIユーチューバー", results[0]["title"])
        self.assertEqual(results[1]["title"], "通常の第2話")
        planner.assert_called_once()

    def test_successful_first_render_switches_future_runs_to_learning_mode(self) -> None:
        def fake_render(results, character):
            for item in results:
                item["output_path"] = str(Path(self.tmp.name) / f"{item['id']}.mp4")
                item["quality_passed"] = True

        patches = self._patch_generation()
        with patches[0], patches[1], patches[2], patches[3], \
             patches[4], patches[5], patches[6], patches[7], \
             patch.object(main, "render_results", side_effect=fake_render):
            first = main.run_generation(
                render=True,
                upload=False,
                target_override=1,
            )
        self.assertTrue(first[0]["first_episode"])
        self.assertEqual(
            storage.get_channel_state(main.FIRST_EPISODE_STATE_KEY, ""),
            "true",
        )

        patches = self._patch_generation()
        with patches[0], patches[1], patches[2], patches[3], \
             patches[4] as planner, patches[5], patches[6], patches[7]:
            second = main.run_generation(
                render=False,
                upload=False,
                target_override=1,
            )
        self.assertFalse(second[0]["first_episode"])
        self.assertEqual(second[0]["title"], "通常の第2話")
        planner.assert_called_once()

    def test_failed_first_render_remains_pending(self) -> None:
        def fake_render(results, character):
            for item in results:
                item["quality_passed"] = False
                item["output_path"] = None

        patches = self._patch_generation()
        with patches[0], patches[1], patches[2], patches[3], \
             patches[4], patches[5], patches[6], patches[7], \
             patch.object(main, "render_results", side_effect=fake_render):
            results = main.run_generation(
                render=True,
                upload=False,
                target_override=1,
            )

        self.assertTrue(results[0]["first_episode"])
        self.assertNotEqual(
            storage.get_channel_state(main.FIRST_EPISODE_STATE_KEY, ""),
            "true",
        )


if __name__ == "__main__":
    unittest.main()
