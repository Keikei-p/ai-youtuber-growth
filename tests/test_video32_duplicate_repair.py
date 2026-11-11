from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import storage
from tools import repair_video32_duplicate as repair


OLD_SCRIPT = (
    "今日もやる気を保ちたい？AIが教える効率アップ法をチェック！"
    "タスクをリスト化して管理したり、集中力を高めるテクニック。"
    "一緒に効率の良い一日を！#AIYouTuber #効率アップ"
)


class Video32DuplicateRepairTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.old_db = storage.DB_PATH
        storage.DB_PATH = self.root / "data" / "memory.db"
        storage.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        storage.init_db()
        self.old_mp4 = self.root / "original-32.mp4"
        self.old_mp4.write_bytes(b"original video payload")
        with storage.connect() as conn:
            for video_id, title, script, status, path in (
                (26, "Original tips", OLD_SCRIPT, "uploaded", ""),
                (32, "Duplicate tips", OLD_SCRIPT, "editorial_failed",
                 str(self.old_mp4)),
                (33, "Learning", "違う勉強の仕組みを説明する", "rendered", ""),
            ):
                conn.execute(
                    """
                    INSERT INTO videos (
                        id,idea,angle,title,script,description,tags_json,
                        status,output_path
                    ) VALUES (?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        video_id, "アイデア", "角度", title, script,
                        "VOICEVOX:ずんだもん", '["AIYouTuber"]',
                        status, path,
                    ),
                )
        storage.queue_video(32, "2026-11-11T15:00+09:00")
        queue = storage.queue_item_for_video(32)
        storage.set_queue_failed(int(queue["queue_id"]), "original duplicate")

    def tearDown(self) -> None:
        storage.DB_PATH = self.old_db
        self.temp.cleanup()

    def test_preview_is_read_only_and_distinct(self) -> None:
        with patch("builtins.print"):
            result = repair.inspect()
        self.assertGreaterEqual(result["old_similarity_with_26"], 0.90)
        self.assertLess(result["new_max_similarity"], 0.70)
        self.assertGreaterEqual(result["draft_originality"], 55)
        self.assertEqual(storage.video_by_id(32)["title"], "Duplicate tips")
        self.assertEqual(storage.queue_item_for_video(32)["status"], "failed")

    def test_apply_preserves_original_media_and_failed_queue(self) -> None:
        def fake_render(items, _character):
            import production_pipeline

            path = production_pipeline.VIDEO_DIR / "test_32.mp4"
            path.write_bytes(b"replacement video payload")
            storage.update_video_output(32, str(path), status="rendered")
            items[0]["quality_passed"] = True
            items[0]["editorial_quality"] = {"passed": True, "score": 83}

        with (
            patch("runtime_control.execution_mode", return_value="local"),
            patch.object(repair, "execution_mode", return_value="local"),
            patch.object(repair, "ai_video_backend_name", return_value="local"),
            patch.object(repair, "runtime_cancel_requested", return_value=False),
            patch("main.load_character", return_value={"name": "Mirai"}),
            patch("main.render_results", side_effect=fake_render),
            patch("builtins.print"),
        ):
            result = repair.apply()

        updated = storage.video_by_id(32)
        self.assertEqual(updated["script"], repair.SCRIPT)
        self.assertEqual(updated["title"], repair.TITLE)
        self.assertEqual(updated["status"], "rendered")
        self.assertIn("VOICEVOX:ずんだもん", updated["description"])
        self.assertEqual(storage.queue_item_for_video(32)["status"], "failed")
        self.assertIsNone(updated["youtube_video_id"])
        self.assertEqual(self.old_mp4.read_bytes(), b"original video payload")
        self.assertEqual(Path(updated["output_path"]).read_bytes(),
                         b"replacement video payload")
        self.assertTrue((Path(result["backup"]) / "memory.db").is_file())
        self.assertEqual((Path(result["backup"]) / "original-32.mp4").read_bytes(),
                         b"original video payload")

    def test_failed_render_restores_existing_metadata(self) -> None:
        def fake_failed_render(items, _character):
            import production_pipeline

            path = production_pipeline.VIDEO_DIR / "failed_32.mp4"
            path.write_bytes(b"rejected output")
            storage.update_video_output(32, str(path), status="editorial_failed")
            raise RuntimeError("simulated editorial failure")

        with (
            patch.object(repair, "execution_mode", return_value="local"),
            patch.object(repair, "ai_video_backend_name", return_value="local"),
            patch.object(repair, "runtime_cancel_requested", return_value=False),
            patch("main.load_character", return_value={"name": "Mirai"}),
            patch("main.render_results", side_effect=fake_failed_render),
            patch("builtins.print"),
        ):
            with self.assertRaisesRegex(RuntimeError, "simulated"):
                repair.apply()

        updated = storage.video_by_id(32)
        self.assertEqual(updated["title"], "Duplicate tips")
        self.assertEqual(updated["script"], OLD_SCRIPT)
        self.assertEqual(updated["status"], "editorial_failed")
        self.assertEqual(updated["output_path"], str(self.old_mp4))
        self.assertEqual(storage.queue_item_for_video(32)["status"], "failed")
        self.assertEqual(self.old_mp4.read_bytes(), b"original video payload")

    def test_remote_video_cannot_be_repaired(self) -> None:
        with storage.connect() as conn:
            conn.execute(
                "UPDATE videos SET youtube_video_id='already-posted' WHERE id=32"
            )
        with self.assertRaisesRegex(RuntimeError, "REMOTE_RECORD"):
            repair.inspect()


if __name__ == "__main__":
    unittest.main()
