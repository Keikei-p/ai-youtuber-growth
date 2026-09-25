from __future__ import annotations

import json
import struct
import tempfile
import unittest
import wave
from pathlib import Path

import storage
from mirai_engines.composer import MiraiComposer
from mirai_engines.debug_engine import MiraiDebugEngine, diagnose
from mirai_engines.improvement_engine import MiraiImprovementEngine
from mirai_engines.quality_engine import analyze_probe_data
from mirai_engines.voice_engine import MiraiVoiceEngine


class FakeVoiceProvider:
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
        rate = 24000
        frames = max(1200, len(text) * 180)
        with wave.open(str(output_path), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(rate)
            handle.writeframes(struct.pack("<h", 0) * frames)
        return output_path


class MiraiEngineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        storage.DB_PATH = Path(self.tmp.name) / "engines.db"
        storage.init_db()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_voice_engine_plans_emotion_and_builds_wav(self) -> None:
        engine = MiraiVoiceEngine(FakeVoiceProvider())
        plan = engine.plan("えっ！？これは重要です。なぜでしょう？")
        self.assertGreaterEqual(len(plan.segments), 3)
        emotions = {segment.emotion for segment in plan.segments}
        self.assertIn("surprised", emotions)
        self.assertIn("serious", emotions)
        self.assertIn("thinking", emotions)

        output = Path(self.tmp.name) / "voice.wav"
        result = engine.synthesize(
            "えっ！？これは重要です。なぜでしょう？",
            output,
        )
        self.assertTrue(output.is_file())
        self.assertTrue(output.with_suffix(".voice.json").is_file())
        self.assertEqual(result["provider"], "fake-provider")
        with wave.open(str(output), "rb") as handle:
            self.assertGreater(handle.getnframes(), 0)
            self.assertEqual(handle.getframerate(), 24000)

        sidecar = json.loads(
            output.with_suffix(".voice.json").read_text(encoding="utf-8")
        )
        self.assertEqual(sidecar["provider"], "fake-provider")
        self.assertGreaterEqual(len(sidecar["segments"]), 3)

    def test_composer_plan_matches_audio_duration(self) -> None:
        composer = MiraiComposer()
        plan = composer.plan(
            "最初に結論です。次に理由を説明します。最後にポイントを確認します。",
            audio_duration=9.0,
            background_count=2,
            has_ai_video=True,
        )
        self.assertGreaterEqual(len(plan["scenes"]), 3)
        self.assertAlmostEqual(
            sum(scene["duration"] for scene in plan["scenes"]),
            9.0,
            places=2,
        )
        self.assertTrue(plan["scenes"][0]["use_ai_video"])
        self.assertFalse(any(composer.validate(plan, 9.0)))

    def test_quality_engine_accepts_valid_vertical_media_metadata(self) -> None:
        probe = {
            "streams": [
                {
                    "codec_type": "video",
                    "codec_name": "h264",
                    "width": 1080,
                    "height": 1920,
                    "avg_frame_rate": "30/1",
                },
                {
                    "codec_type": "audio",
                    "codec_name": "aac",
                },
            ],
            "format": {"duration": "20.0"},
        }
        plan = MiraiComposer().plan(
            "短い字幕です。次の字幕です。",
            audio_duration=20.0,
            background_count=2,
        )
        report = analyze_probe_data(
            probe,
            composition_plan=plan,
            file_size=2_000_000,
        )
        self.assertTrue(report.passed)
        self.assertGreaterEqual(report.score, 90)

    def test_quality_engine_rejects_missing_audio(self) -> None:
        probe = {
            "streams": [
                {
                    "codec_type": "video",
                    "codec_name": "h264",
                    "width": 1080,
                    "height": 1920,
                    "avg_frame_rate": "30/1",
                }
            ],
            "format": {"duration": "15.0"},
        }
        report = analyze_probe_data(probe, file_size=1_000_000)
        self.assertFalse(report.passed)
        self.assertIn(
            "audio_missing",
            [issue.code for issue in report.issues],
        )

    def test_debug_engine_identifies_gpu_memory_failures(self) -> None:
        result = diagnose(
            "image.background",
            "CUDA out of memory while allocating tensor",
            occurrences=2,
        )
        self.assertEqual(result.category, "gpu_memory")
        self.assertGreaterEqual(result.confidence, 0.9)
        self.assertTrue(result.safe_actions)

    def test_improvement_engine_reacts_to_repeated_gpu_diagnosis(self) -> None:
        debug = MiraiDebugEngine()
        debug.diagnose_and_store(
            "image.background",
            "CUDA out of memory",
            occurrences=1,
        )
        debug.diagnose_and_store(
            "image.background",
            "CUDA out of memory",
            occurrences=2,
        )
        report = MiraiImprovementEngine().review()
        action_types = {
            item.get("action_type")
            for item in report.get("actions") or []
        }
        self.assertIn("reduce_scene_images", action_types)


if __name__ == "__main__":
    unittest.main()
