from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from PIL import Image

import production_pipeline
from studio import google_video_generator
from studio import video_generator


def _response(status_code: int, payload: dict | None = None, content: bytes = b""):
    response = MagicMock()
    response.status_code = status_code
    response.json.return_value = payload or {}
    response.content = content
    response.text = ""
    return response


class GoogleVeoVideoTests(unittest.TestCase):
    def _settings(self, **overrides):
        values = {
            "google_ai_api_key": "test-key",
            "google_video_model": "veo-3.1-fast-generate-preview",
            "google_video_duration_seconds": 4,
            "google_video_resolution": "720p",
            "google_video_aspect_ratio": "9:16",
            "google_video_timeout_seconds": 120,
            "google_video_poll_seconds": 3,
            "google_video_max_per_batch": 3,
            "ai_video_backend": "google",
            "ai_video_enabled": True,
            "ai_video_frames": 8,
            "ai_video_steps": 12,
            "ai_video_width": 384,
            "ai_video_height": 576,
            "studio_diffusers_model": "unused",
        }
        values.update(overrides)
        return SimpleNamespace(**values)

    def test_google_status_never_exposes_key(self) -> None:
        settings = self._settings()
        with patch.object(
            google_video_generator,
            "settings",
            settings,
        ):
            status = google_video_generator.google_video_status()
        self.assertTrue(status["available"])
        self.assertTrue(status["api_key_configured"])
        self.assertNotIn("test-key", str(status))
        self.assertEqual(
            status["model"],
            "veo-3.1-fast-generate-preview",
        )

    def test_image_to_video_uses_official_long_running_shape_and_downloads(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            image = root / "mirai.png"
            Image.new("RGB", (720, 1280), (80, 100, 140)).save(image)

            start = _response(
                200,
                {"name": "operations/veo-test-1"},
            )
            processing = _response(
                200,
                {"name": "operations/veo-test-1", "done": False},
            )
            done = _response(
                200,
                {
                    "name": "operations/veo-test-1",
                    "done": True,
                    "response": {
                        "generateVideoResponse": {
                            "generatedSamples": [
                                {
                                    "video": {
                                        "uri": "https://example.invalid/video"
                                    }
                                }
                            ]
                        }
                    },
                },
            )
            download = _response(
                200,
                content=b"x" * 30_000,
            )

            with (
                patch.object(
                    google_video_generator,
                    "settings",
                    self._settings(),
                ),
                patch.object(
                    google_video_generator,
                    "GENERATED_ROOT",
                    root / "generated",
                ),
                patch.object(
                    google_video_generator,
                    "record_asset",
                ) as record_asset,
                patch.object(
                    google_video_generator.requests,
                    "post",
                    return_value=start,
                ) as post,
                patch.object(
                    google_video_generator.requests,
                    "get",
                    side_effect=[processing, done, download],
                ) as get,
                patch.object(
                    google_video_generator.time,
                    "sleep",
                ),
            ):
                path = google_video_generator.generate_google_veo_clip(
                    "A cinematic portrait shot of Mirai looking at the camera.",
                    image_path=image,
                )

            output = Path(path)
            self.assertTrue(output.is_file())
            self.assertGreater(output.stat().st_size, 20_000)

            body = post.call_args.kwargs["json"]
            instance = body["instances"][0]
            params = body["parameters"]
            self.assertEqual(params["aspectRatio"], "9:16")
            self.assertEqual(params["durationSeconds"], "4")
            self.assertEqual(params["resolution"], "720p")
            self.assertEqual(params["personGeneration"], "allow_adult")
            self.assertIn("image", instance)
            self.assertIn(
                "inlineData",
                instance["image"],
            )
            self.assertEqual(get.call_count, 3)
            record_asset.assert_called_once()

    def test_reference_images_force_eight_seconds(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            image = Path(tmp) / "ref.png"
            Image.new("RGB", (512, 512), (1, 2, 3)).save(image)
            with (
                patch.object(
                    google_video_generator,
                    "settings",
                    self._settings(),
                ),
                patch.object(
                    google_video_generator.requests,
                    "post",
                    return_value=_response(
                        200,
                        {"name": "operations/ref"},
                    ),
                ) as post,
                patch.object(
                    google_video_generator.requests,
                    "get",
                    side_effect=[
                        _response(
                            200,
                            {
                                "done": True,
                                "response": {
                                    "generateVideoResponse": {
                                        "generatedSamples": [{
                                            "video": {
                                                "uri": "https://example.invalid/ref"
                                            }
                                        }]
                                    }
                                },
                            },
                        ),
                        _response(
                            200,
                            content=b"v" * 30_000,
                        ),
                    ],
                ),
                patch.object(
                    google_video_generator,
                    "GENERATED_ROOT",
                    Path(tmp) / "generated",
                ),
                patch.object(
                    google_video_generator,
                    "record_asset",
                ),
            ):
                google_video_generator.generate_google_veo_clip(
                    "reference test",
                    reference_images=[image],
                )

            body = post.call_args.kwargs["json"]
            self.assertEqual(
                body["parameters"]["durationSeconds"],
                "8",
            )
            self.assertEqual(
                len(
                    body["instances"][0]["referenceImages"]
                ),
                1,
            )

    def test_missing_key_fails_without_network(self) -> None:
        with (
            patch.object(
                google_video_generator,
                "settings",
                self._settings(google_ai_api_key=""),
            ),
            patch.dict(
                "os.environ",
                {
                    "GEMINI_API_KEY": "",
                    "GOOGLE_API_KEY": "",
                },
                clear=False,
            ),
            patch.object(
                google_video_generator.requests,
                "post",
            ) as post,
        ):
            with self.assertRaises(
                google_video_generator.GoogleVideoGenerationError
            ) as ctx:
                google_video_generator.generate_google_veo_clip(
                    "test",
                )
        self.assertEqual(
            ctx.exception.code,
            "google_video_api_key_missing",
        )
        post.assert_not_called()

    def test_video_generator_routes_google_backend(self) -> None:
        settings = self._settings()
        with (
            patch.object(video_generator, "settings", settings),
            patch.object(
                video_generator,
                "generate_google_veo_clip",
                return_value="google.mp4",
            ) as google,
        ):
            result = video_generator.generate_animatediff_clip(
                "hello",
                image_path="mirai.png",
            )
        self.assertEqual(result, "google.mp4")
        google.assert_called_once_with(
            "hello",
            image_path="mirai.png",
            reference_images=None,
        )

    def test_google_failure_falls_back_to_identity_motion(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            identity = Path(tmp) / "mirai.png"
            identity.write_bytes(b"fake")
            motion = Path(tmp) / "motion.mp4"
            motion.write_bytes(b"m" * 30_000)
            settings = self._settings()

            with (
                patch.object(production_pipeline, "settings", settings),
                patch.object(
                    production_pipeline,
                    "ai_video_enabled",
                    return_value=True,
                ),
                patch.object(
                    production_pipeline,
                    "ai_video_license_confirmed",
                    return_value=True,
                ),
                patch.object(
                    production_pipeline,
                    "mirai_identity_video_enabled",
                    return_value=True,
                ),
                patch.object(
                    production_pipeline,
                    "get_channel_state",
                    return_value="",
                ),
                patch.object(
                    production_pipeline,
                    "generate_animatediff_clip",
                    side_effect=RuntimeError("google down"),
                ) as google,
                patch.object(
                    production_pipeline,
                    "generate_motion_clip",
                    return_value=str(motion),
                ) as fallback,
                patch.object(
                    production_pipeline.MiraiVisualQualityEngine,
                    "inspect_video_asset",
                    return_value={
                        "passed": True,
                        "score": 90,
                        "metrics": {},
                    },
                ),
                patch.object(
                    production_pipeline.VisualLearningMemory,
                    "record_result",
                ),
                patch.object(
                    production_pipeline,
                    "record_failure",
                ),
            ):
                item = {
                    "id": 1,
                    "title": "test",
                    "script": "test",
                    "idea": {
                        "idea": "test",
                        "angle": "test",
                    },
                    "character_image_path": str(identity),
                }
                result = production_pipeline._generate_ai_video_asset(
                    item
                )

            self.assertEqual(result, str(motion))
            google.assert_called_once()
            fallback.assert_called_once()
            self.assertEqual(
                item["ai_video_backend"],
                "ffmpeg-identity-motion",
            )

    def test_batch_cost_guard_limits_google_calls(self) -> None:
        settings = self._settings(
            google_video_max_per_batch=1,
        )
        items = [
            {"id": 1},
            {"id": 2},
            {"id": 3},
        ]

        def fake_video(item):
            if not item.get("skip_google_video"):
                item["google_video_used"] = True

        with (
            patch.object(production_pipeline, "settings", settings),
            patch.object(
                production_pipeline,
                "unload_ollama_model",
            ),
            patch.object(
                production_pipeline,
                "release_torch_cuda_cache",
            ),
            patch.object(
                production_pipeline,
                "_ensure_mirai_visual",
            ),
            patch.object(
                production_pipeline,
                "_ensure_guest_visual",
            ),
            patch.object(
                production_pipeline,
                "_generate_backgrounds",
            ),
            patch.object(
                production_pipeline,
                "_generate_ai_video_asset",
                side_effect=fake_video,
            ),
            patch.object(
                production_pipeline,
                "_save_video_visual_profile",
            ),
        ):
            production_pipeline.prepare_visuals(items)

        self.assertFalse(items[0].get("skip_google_video", False))
        self.assertTrue(items[1]["skip_google_video"])
        self.assertTrue(items[2]["skip_google_video"])


if __name__ == "__main__":
    unittest.main()
