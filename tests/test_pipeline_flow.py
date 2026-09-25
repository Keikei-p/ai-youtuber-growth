from __future__ import annotations

import struct
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

import main
import production_pipeline
import storage


class FakeProvider:
    name = "fake-provider"

    def available(self) -> bool:
        return True

    def synthesize(
        self,
        text: str,
        output_path: Path,
        voice_params: dict | None = None,
    ) -> Path:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(output_path), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(24000)
            handle.writeframes(struct.pack("<h", 500) * 4800)
        return output_path


def make_wav(path: Path, seconds: float = 1.5) -> None:
    rate = 24000
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(
            struct.pack("<h", 500) * int(rate * seconds)
        )


class PipelineFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        storage.DB_PATH = self.root / "pipeline.db"
        storage.init_db()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_run_generation_one_item_connects_text_db_and_render_stage(self) -> None:
        output = self.root / "video.mp4"

        def fake_render(results, character):
            for item in results:
                output.write_bytes(b"fake-mp4")
                item["output_path"] = str(output)
                item["quality_passed"] = True
                item["status"] = "rendered"
                storage.update_video_output(
                    int(item["id"]),
                    str(output),
                    status="rendered",
                )

        with (
            patch.object(main, "ensure_runtime_dirs"),
            patch.object(main, "PLAN_DIR", self.root / "plans"),
            patch.object(main, "load_character", return_value={"name": "Mirai"}),
            patch.object(main, "recent_videos", return_value=[]),
            patch.object(
                main,
                "plan_ideas",
                return_value=[{"idea": "統合テスト", "angle": "通常運転"}],
            ),
            patch.object(main, "select_guest_for_next_video", return_value=None),
            patch.object(
                main,
                "_make_valid_script",
                return_value={
                    "title": "統合テスト",
                    "script": "これは通常運転の統合テストです。",
                },
            ),
            patch.object(
                main,
                "build_metadata",
                return_value={
                    "title": "統合テスト",
                    "description": "description",
                    "tags": ["test"],
                },
            ),
            patch.object(main, "unload_ollama_model", return_value=True),
            patch.object(main, "release_torch_cuda_cache"),
            patch.object(main, "render_results", side_effect=fake_render),
        ):
            results = main.run_generation(
                render=True,
                upload=False,
                target_override=1,
            )

        self.assertEqual(len(results), 1)
        self.assertTrue(results[0]["quality_passed"])
        row = storage.video_by_id(int(results[0]["id"]))
        self.assertIsNotNone(row)
        self.assertEqual(row["status"], "rendered")
        plans = list((self.root / "plans").glob("*.json"))
        self.assertEqual(len(plans), 1)

    def test_production_voice_stage_uses_mirai_voice_engine(self) -> None:
        item = {
            "id": 11,
            "script": "えっ！？これは重要です。理由を説明します。",
        }
        audio_dir = self.root / "audio"
        with (
            patch.object(production_pipeline, "AUDIO_DIR", audio_dir),
            patch.object(
                production_pipeline,
                "build_voice_provider",
                return_value=FakeProvider(),
            ),
            patch.object(production_pipeline, "release_torch_cuda_cache"),
        ):
            production_pipeline.synthesize_audio([item])

        self.assertTrue(Path(item["audio_path"]).is_file())
        self.assertEqual(item["voice_plan"]["provider"], "fake-provider")
        self.assertGreaterEqual(len(item["voice_plan"]["segments"]), 2)

    def test_render_stage_marks_quality_failure_and_keeps_file(self) -> None:
        audio = self.root / "voice.wav"
        make_wav(audio)
        video_id = storage.save_video(
            idea="idea",
            angle="angle",
            title="title",
            script="script",
        )
        item = {
            "id": video_id,
            "title": "title",
            "script": "短い台本です。",
            "audio_path": str(audio),
            "background_image_paths": [],
        }

        def fake_render_short(**kwargs):
            Path(kwargs["output_path"]).write_bytes(b"bad-but-reviewable")

        class FakeQuality:
            def inspect(self, *args, **kwargs):
                return {
                    "passed": False,
                    "score": 35,
                    "issues": [
                        {
                            "code": "audio_missing",
                            "severity": "critical",
                            "message": "audio missing",
                        }
                    ],
                    "metrics": {},
                }

        with (
            patch.object(production_pipeline, "VIDEO_DIR", self.root / "videos"),
            patch.object(production_pipeline, "render_short", side_effect=fake_render_short),
            patch.object(production_pipeline, "MiraiQualityEngine", return_value=FakeQuality()),
            patch.object(production_pipeline, "record_failure"),
            patch.object(production_pipeline, "release_torch_cuda_cache"),
        ):
            production_pipeline.render_videos(
                [item],
                {"name": "Mirai"},
            )

        self.assertFalse(item["quality_passed"])
        self.assertEqual(item["status"], "quality_failed")
        self.assertTrue(Path(item["output_path"]).is_file())
        row = storage.video_by_id(video_id)
        self.assertEqual(row["status"], "quality_failed")


if __name__ == "__main__":
    unittest.main()
