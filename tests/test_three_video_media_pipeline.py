from __future__ import annotations

import math
import struct
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

from PIL import Image

import production_pipeline
import storage


class FakeVoiceProvider:
    name = "server-fake-voice"

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
        seconds = max(1.4, min(2.2, len(text) / 24))
        frames = int(rate * seconds)
        samples = bytearray()
        for index in range(frames):
            value = int(
                1400 * math.sin(
                    2 * math.pi * 210 * index / rate
                )
            )
            samples.extend(struct.pack("<h", value))
        with wave.open(str(output_path), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(rate)
            handle.writeframes(bytes(samples))
        return output_path


@unittest.skipUnless(
    production_pipeline.shutil.which("ffmpeg")
    if hasattr(production_pipeline, "shutil")
    else True,
    "FFmpeg required",
)
class ThreeVideoMediaPipelineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        storage.DB_PATH = self.root / "three-media.db"
        storage.init_db()
        self.audio_dir = self.root / "audio"
        self.video_dir = self.root / "videos"
        self.visual_dir = self.root / "visuals"
        self.visual_dir.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _visual(self, name: str, rgb: tuple[int, int, int]) -> str:
        path = self.visual_dir / name
        Image.new("RGB", (720, 1280), rgb).save(path)
        return str(path)

    def test_three_videos_complete_voice_composer_render_and_quality(self) -> None:
        character_path = self._visual(
            "mirai.png",
            (220, 235, 250),
        )
        backgrounds = [
            self._visual("bg1.png", (20, 35, 70)),
            self._visual("bg2.png", (45, 30, 75)),
        ]

        items: list[dict] = []
        for index in range(1, 4):
            script = (
                f"これはミライのサーバー統合テスト{index}本目です。"
                "音声設計、動画構成、字幕、編集、品質検査まで確認します。"
            )
            video_id = storage.save_video(
                idea=f"server media {index}",
                angle="full media pipeline",
                title=f"Mirai media test {index}",
                script=script,
                description=(
                    "この動画はAIを使って企画・音声・画像/映像を制作しています。\n\n"
                    "VOICEVOX:ずんだもん"
                ),
                tags=["mirai", "server-test"],
                status="planned",
            )
            items.append(
                {
                    "id": video_id,
                    "title": f"Mirai media test {index}",
                    "script": script,
                    "idea": {
                        "idea": f"server media {index}",
                        "angle": "full media pipeline",
                    },
                    "guest": None,
                }
            )

        def fake_mirai(item):
            item["character_image_path"] = character_path
            item["mirai_expression"] = "normal"
            return character_path

        def fake_backgrounds(item):
            item["background_image_paths"] = list(backgrounds)
            item["background_image_path"] = backgrounds[0]
            return list(backgrounds)

        with (
            patch.object(
                production_pipeline,
                "AUDIO_DIR",
                self.audio_dir,
            ),
            patch.object(
                production_pipeline,
                "VIDEO_DIR",
                self.video_dir,
            ),
            patch.object(
                production_pipeline,
                "build_voice_provider",
                return_value=FakeVoiceProvider(),
            ),
            patch.object(
                production_pipeline,
                "_ensure_mirai_visual",
                side_effect=fake_mirai,
            ),
            patch.object(
                production_pipeline,
                "_ensure_guest_visual",
                return_value=None,
            ),
            patch.object(
                production_pipeline,
                "_generate_backgrounds",
                side_effect=fake_backgrounds,
            ),
            patch.object(
                production_pipeline,
                "_generate_ai_video_asset",
                return_value=None,
            ),
            patch.object(
                production_pipeline,
                "unload_ollama_model",
                return_value=True,
            ),
            patch.object(
                production_pipeline,
                "release_torch_cuda_cache",
            ),
        ):
            production_pipeline.produce_media(
                items,
                {"name": "Mirai"},
            )

        self.assertEqual(len(items), 3)
        for item in items:
            self.assertTrue(
                item.get("quality_passed"),
                msg=str(item),
            )
            output = Path(item["output_path"])
            self.assertTrue(output.is_file())
            self.assertGreater(output.stat().st_size, 50_000)
            self.assertEqual(item["status"], "rendered")
            self.assertGreaterEqual(
                int(item["quality"]["score"]),
                70,
            )
            self.assertTrue(Path(item["audio_path"]).is_file())
            self.assertTrue(item.get("composition_plan"))

            row = storage.video_by_id(int(item["id"]))
            self.assertEqual(row["status"], "rendered")
            self.assertEqual(
                Path(row["output_path"]),
                output,
            )


if __name__ == "__main__":
    unittest.main()
