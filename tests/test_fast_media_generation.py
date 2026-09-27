from __future__ import annotations

import subprocess
import unittest
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from PIL import Image

import gpu_manager
import self_improvement
from studio import image_generator
from studio.models import ImagePreset
from video import ffmpeg_encoder


class FastGpuSessionTests(unittest.TestCase):
    def test_nested_gpu_tasks_prepare_only_once(self) -> None:
        with (
            patch.object(
                gpu_manager,
                "unload_ollama_model",
                return_value=True,
            ) as unload,
            patch.object(
                gpu_manager,
                "release_torch_cuda_cache",
            ) as release,
        ):
            with gpu_manager.exclusive_gpu_task("outer"):
                with gpu_manager.exclusive_gpu_task("inner"):
                    pass

        unload.assert_called_once()
        # outerの開始/終了だけ。innerでは重い整理を繰り返さない。
        self.assertEqual(release.call_count, 2)

    def test_image_generation_session_parks_pipeline_once(self) -> None:
        @contextmanager
        def fake_gpu_task(_label):
            yield

        with (
            patch.object(
                image_generator,
                "exclusive_gpu_task",
                side_effect=fake_gpu_task,
            ),
            patch.object(
                image_generator,
                "_park_pipeline",
            ) as park,
        ):
            with image_generator.image_generation_session():
                self.assertTrue(
                    image_generator._image_batch_active()
                )
                with image_generator.image_generation_session():
                    self.assertTrue(
                        image_generator._image_batch_active()
                    )

        self.assertFalse(image_generator._image_batch_active())
        park.assert_called_once()


class FastVisualModeTests(unittest.TestCase):
    def test_fast_mode_accepts_first_good_candidate(self) -> None:
        image = Image.effect_noise(
            (512, 768),
            70,
        ).convert("RGB")
        fake_settings = SimpleNamespace(
            media_fast_mode=True,
            visual_candidate_count=2,
            visual_background_candidates=1,
            visual_retry_rounds=1,
            visual_min_score=60,
        )
        generator = MagicMock(
            side_effect=[
                (image, "fake-backend"),
                (image, "fake-backend"),
            ]
        )
        report = {
            "score": 82,
            "passed": True,
            "metrics": {},
            "issues": [],
        }

        with (
            patch.object(
                image_generator,
                "settings",
                fake_settings,
            ),
            patch.object(
                image_generator,
                "_generate",
                generator,
            ),
            patch.object(
                image_generator.MiraiVisualQualityEngine,
                "inspect_image",
                return_value=report,
            ),
            patch.object(
                image_generator,
                "_seed_base",
                return_value=100,
            ),
        ):
            selected = image_generator._generate_best_image(
                "fast candidate",
                ImagePreset(512, 768, 8, 6.0),
                asset_type="mirai",
            )

        self.assertEqual(selected["quality"]["score"], 82)
        generator.assert_called_once()

    def test_fast_mode_uses_one_new_scene_background(self) -> None:
        fake_settings = SimpleNamespace(
            media_fast_mode=True,
            studio_scene_images_per_video=4,
        )
        with (
            patch.object(
                self_improvement,
                "settings",
                fake_settings,
            ),
            patch.object(
                self_improvement,
                "get_channel_state",
                return_value="",
            ),
        ):
            self.assertEqual(
                self_improvement.effective_scene_image_count(),
                1,
            )


class FfmpegEncoderTests(unittest.TestCase):
    def setUp(self) -> None:
        ffmpeg_encoder._NVENC_AVAILABLE = True
        ffmpeg_encoder._NVENC_DISABLED = False

    def tearDown(self) -> None:
        ffmpeg_encoder._NVENC_AVAILABLE = None
        ffmpeg_encoder._NVENC_DISABLED = False

    def test_nvenc_failure_falls_back_to_x264(self) -> None:
        failure = subprocess.CalledProcessError(
            1,
            ["ffmpeg"],
        )
        with (
            patch.object(
                ffmpeg_encoder,
                "_preference",
                return_value="nvenc",
            ),
            patch.object(
                ffmpeg_encoder.subprocess,
                "run",
                side_effect=[failure, MagicMock(returncode=0)],
            ) as run,
        ):
            ffmpeg_encoder.run_video_encode(
                ["ffmpeg", "-y", "-i", "input.png"],
                ["output.mp4"],
            )

        self.assertEqual(run.call_count, 2)
        first = run.call_args_list[0].args[0]
        second = run.call_args_list[1].args[0]
        self.assertIn("h264_nvenc", first)
        self.assertIn("libx264", second)
        self.assertTrue(ffmpeg_encoder._NVENC_DISABLED)


if __name__ == "__main__":
    unittest.main()
