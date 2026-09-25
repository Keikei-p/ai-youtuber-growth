from __future__ import annotations

import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

import runtime_control
import storage
from voice import model_manager


class DailyAutoAndVoiceModelTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.old_db = storage.DB_PATH
        storage.DB_PATH = self.root / "state.db"
        storage.init_db()

    def tearDown(self) -> None:
        storage.DB_PATH = self.old_db
        self.tmp.cleanup()

    def test_daily_auto_presets_are_atomic(self) -> None:
        state = runtime_control.apply_daily_auto_preset(2)
        self.assertTrue(state["enabled"])
        self.assertEqual(state["posts_per_day"], 2)
        self.assertEqual(state["post_times"], "12:00,20:00")
        self.assertEqual(state["preset"], 2)

        off = runtime_control.disable_daily_auto()
        self.assertFalse(off["enabled"])
        self.assertFalse(runtime_control.automation_enabled())
        self.assertFalse(runtime_control.auto_upload_enabled())

    def test_daily_auto_rejects_unknown_preset(self) -> None:
        with self.assertRaises(ValueError):
            runtime_control.apply_daily_auto_preset(4)

    def test_voice_training_manifest_is_lightweight_and_validates_wav(self) -> None:
        training = self.root / "training"
        model = self.root / "models"
        training.mkdir()
        wav = training / "sample.wav"
        with wave.open(str(wav), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(24000)
            handle.writeframes(b"\x00\x00" * 24000)
        (training / "metadata.csv").write_text(
            "sample.wav|こんにちは、ミライです。\n",
            encoding="utf-8",
        )

        with (
            patch.object(model_manager, "TRAINING_ROOT", training),
            patch.object(model_manager, "MODEL_ROOT", model),
            patch.object(model_manager, "METADATA_FILE", training / "metadata.csv"),
            patch.object(model_manager, "MANIFEST_FILE", training / "manifest.jsonl"),
            patch.object(model_manager, "MODEL_META_FILE", model / "model.json"),
        ):
            result = model_manager.prepare_training_manifest()

        self.assertEqual(result["valid_count"], 1)
        self.assertEqual(result["manifest_rows"], 1)
        self.assertGreater(result["total_minutes"], 0)
        self.assertTrue((training / "manifest.jsonl").is_file())

    def test_model_is_not_ready_without_real_runtime_and_artifacts(self) -> None:
        training = self.root / "training"
        model = self.root / "models"
        training.mkdir()
        model.mkdir()
        (model / "model.json").write_text(
            '{"ready": true, "runtime_implemented": false, '
            '"artifacts": ["voice.onnx"]}',
            encoding="utf-8",
        )
        with (
            patch.object(model_manager, "TRAINING_ROOT", training),
            patch.object(model_manager, "MODEL_ROOT", model),
            patch.object(model_manager, "METADATA_FILE", training / "metadata.csv"),
            patch.object(model_manager, "MANIFEST_FILE", training / "manifest.jsonl"),
            patch.object(model_manager, "MODEL_META_FILE", model / "model.json"),
        ):
            status = model_manager.model_status()
        self.assertFalse(status["ready"])
        self.assertIn("voice.onnx", status["missing_artifacts"])


if __name__ == "__main__":
    unittest.main()
