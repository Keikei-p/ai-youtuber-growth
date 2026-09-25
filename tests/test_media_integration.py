from __future__ import annotations

import shutil
import struct
import tempfile
import unittest
import wave
from pathlib import Path

from PIL import Image

from mirai_engines.composer import MiraiComposer
from mirai_engines.quality_engine import MiraiQualityEngine
from video.renderer import render_short


@unittest.skipUnless(
    shutil.which("ffmpeg") and shutil.which("ffprobe"),
    "FFmpeg integration test requires ffmpeg/ffprobe",
)
class MediaIntegrationTests(unittest.TestCase):
    def test_real_mp4_render_and_quality_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            background = root / "background.png"
            Image.new("RGB", (720, 1280), (24, 38, 68)).save(background)

            audio = root / "voice.wav"
            rate = 24000
            seconds = 2.4
            frame_count = int(rate * seconds)
            with wave.open(str(audio), "wb") as handle:
                handle.setnchannels(1)
                handle.setsampwidth(2)
                handle.setframerate(rate)
                handle.writeframes(
                    struct.pack("<h", 0) * frame_count
                )

            script = "これは統合テストです。完成動画の品質を確認します。"
            composer = MiraiComposer()
            plan = composer.plan(
                script,
                audio_duration=seconds,
                background_count=1,
                has_ai_video=False,
            )

            output = root / "test_short.mp4"
            render_short(
                title="Mirai統合テスト",
                script=script,
                audio_path=audio,
                output_path=output,
                character_name="Mirai",
                background_image_path=str(background),
                composition_plan=plan,
            )

            self.assertTrue(output.is_file())
            self.assertGreater(output.stat().st_size, 50_000)

            report = MiraiQualityEngine().inspect(
                output,
                composition_plan=plan,
            )
            self.assertTrue(
                report["passed"],
                msg=str(report),
            )
            self.assertEqual(report["metrics"]["width"], 1080)
            self.assertEqual(report["metrics"]["height"], 1920)
            self.assertGreaterEqual(report["metrics"]["fps"], 24)
            self.assertEqual(report["metrics"]["audio_codec"], "aac")


if __name__ == "__main__":
    unittest.main()
