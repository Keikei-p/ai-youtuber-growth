from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import ai_client
import native_models.brain as brain_module
from native_models.brain import BrainConfig, ByteTokenizer
from native_models import promotion
from native_models.image_v0 import ImageConfig, image_status
from native_models.lab import migration_summary
from native_models.video_v0 import VideoConfig, video_status
from voice import mirai_backend
from studio import image_generator, video_generator


class NativeModelTests(unittest.TestCase):
    def test_byte_tokenizer_round_trip_japanese(self) -> None:
        tokenizer = ByteTokenizer()
        text = "ミライは自分で学習して成長します。AI🤖"
        encoded = tokenizer.encode(text, bos=True, eos=True)
        self.assertEqual(encoded[0], tokenizer.bos_id)
        self.assertEqual(encoded[-1], tokenizer.eos_id)
        self.assertEqual(tokenizer.decode(encoded), text)

    def test_native_configs_validate_without_pretrained_models(self) -> None:
        BrainConfig().validate()
        ImageConfig().validate()
        VideoConfig().validate()

    def test_native_statuses_report_code_ready(self) -> None:
        image = image_status()
        video = video_status()
        self.assertTrue(image["code_ready"])
        self.assertTrue(video["code_ready"])
        self.assertFalse(image["pretrained_dependency"])
        self.assertFalse(video["pretrained_dependency"])

    def test_text_auto_prefers_native_when_trained(self) -> None:
        fake_settings = SimpleNamespace(
            mirai_text_provider="auto",
            ollama_url="http://127.0.0.1:11434",
            ollama_model="migration-only",
        )
        with (
            patch.object(ai_client, "settings", fake_settings),
            patch.object(
                ai_client,
                "brain_status",
                return_value={"ready": True, "production_ready": True},
            ),
            patch.object(
                ai_client,
                "_ollama_available",
                return_value=False,
            ),
            patch.object(
                ai_client._NATIVE,
                "generate",
                return_value="native-answer",
            ) as native_generate,
            patch.object(ai_client.requests, "post") as http_post,
        ):
            client = ai_client.OllamaClient()
            self.assertTrue(client.available())
            self.assertEqual(client.provider_name(), "mirai_native")
            self.assertEqual(client.generate("test"), "native-answer")
        native_generate.assert_called_once_with("test")
        http_post.assert_not_called()

    def test_auto_mode_does_not_use_unapproved_native_model(self) -> None:
        fake_settings = SimpleNamespace(
            mirai_text_provider="auto",
            ollama_url="http://127.0.0.1:11434",
            ollama_model="migration-only",
        )
        with (
            patch.object(ai_client, "settings", fake_settings),
            patch.object(
                ai_client,
                "brain_status",
                return_value={
                    "ready": True,
                    "production_ready": False,
                },
            ),
            patch.object(
                ai_client,
                "_ollama_available",
                return_value=True,
            ),
            patch.object(
                ai_client._NATIVE,
                "generate",
            ) as native_generate,
        ):
            client = ai_client.OllamaClient()
            self.assertEqual(client.provider_name(), "ollama")
        native_generate.assert_not_called()

    def test_brain_corpus_includes_auto_subdirectory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            auto = root / "auto"
            auto.mkdir(parents=True)
            (auto / "youtube_1.txt").write_text(
                "ミライの成功台本です。" * 300,
                encoding="utf-8",
            )
            model_root = root / "model"
            with (
                patch.object(
                    brain_module,
                    "component_training_root",
                    return_value=root,
                ),
                patch.object(
                    brain_module,
                    "component_model_root",
                    return_value=model_root,
                ),
            ):
                status = brain_module.corpus_status()
        self.assertTrue(status["ready"])
        self.assertEqual(status["files"], 1)

    def test_failed_benchmark_rolls_back_new_model(self) -> None:
        with (
            patch.object(
                promotion,
                "model_meta",
                return_value={
                    "ready": True,
                    "training": {"final_loss": 1.0},
                },
            ),
            patch.object(
                promotion,
                "benchmark_component",
                return_value={"passed": False, "score": 20},
            ),
            patch.object(
                promotion,
                "restore_model",
            ) as restore,
        ):
            result = promotion.evaluate_and_promote(
                "brain",
                previous_meta={
                    "training": {"final_loss": 0.9},
                },
                backup=Path("backup"),
            )
        self.assertFalse(result["promoted"])
        self.assertTrue(result["rolled_back"])
        restore.assert_called_once()

    def test_native_only_does_not_silently_fallback_to_ollama(self) -> None:
        fake_settings = SimpleNamespace(
            mirai_text_provider="mirai_native",
            ollama_url="http://127.0.0.1:11434",
            ollama_model="migration-only",
        )
        with (
            patch.object(ai_client, "settings", fake_settings),
            patch.object(
                ai_client,
                "brain_status",
                return_value={"ready": False},
            ),
            patch.object(
                ai_client,
                "_ollama_available",
                return_value=True,
            ),
            patch.object(ai_client.requests, "post") as http_post,
        ):
            client = ai_client.OllamaClient()
            self.assertFalse(client.available())
            with self.assertRaises(RuntimeError):
                client.generate("must-not-fallback")
        http_post.assert_not_called()

    def test_native_image_backend_does_not_use_external_refine(self) -> None:
        fake_settings = SimpleNamespace(
            studio_image_backend="native",
        )
        selection = {
            "backend": "mirai-native-image-v0",
            "image": object(),
            "quality": {"score": 50},
            "prompt": "native",
        }
        with (
            patch.object(image_generator, "settings", fake_settings),
            patch.object(
                image_generator,
                "visual_highres_enabled",
                return_value=True,
            ),
            patch.object(
                image_generator,
                "_refine_webui",
            ) as external_refine,
        ):
            self.assertEqual(
                image_generator._configured_backend(),
                "native",
            )
            result = image_generator._highres_refine_selection(
                selection,
                asset_type="mirai",
                seed=1,
            )
        self.assertIs(result, selection)
        external_refine.assert_not_called()

    def test_native_video_dispatch_does_not_load_animatediff(self) -> None:
        fake_settings = SimpleNamespace(
            ai_video_backend="native",
        )
        with (
            patch.object(video_generator, "settings", fake_settings),
            patch.object(
                video_generator,
                "_generate_native_clip",
                return_value="native.mp4",
            ) as native_clip,
        ):
            result = video_generator.generate_animatediff_clip(
                "native prompt"
            )
        self.assertEqual(result, "native.mp4")
        native_clip.assert_called_once_with("native prompt")

    def test_native_voice_runtime_is_implemented_but_requires_weights(self) -> None:
        self.assertTrue(mirai_backend.RUNTIME_IMPLEMENTED)
        with patch.object(
            mirai_backend,
            "available",
            return_value=False,
        ):
            with self.assertRaises(RuntimeError):
                mirai_backend.synthesize("こんにちは")

    def test_migration_summary_has_four_owned_components(self) -> None:
        status = migration_summary()
        self.assertEqual(
            set(status["status"]),
            {"brain", "image", "video", "voice"},
        )
        self.assertEqual(
            status["goal"],
            "No pretrained third-party model weights",
        )


if __name__ == "__main__":
    unittest.main()
