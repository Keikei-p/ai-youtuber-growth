from __future__ import annotations

import io
import tempfile
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import storage
from runtime_control import set_voice_provider_name, voice_provider_name
from voice.mirai_local import MiraiLocalClient
from voice.provider import build_voice_provider


def make_wav_bytes() -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(24000)
        handle.writeframes(b"\x00\x00" * 2400)
    return buffer.getvalue()


class MiraiLocalVoiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db = storage.DB_PATH
        storage.DB_PATH = Path(self.tmp.name) / "voice.db"
        storage.init_db()

    def tearDown(self) -> None:
        storage.DB_PATH = self.old_db
        self.tmp.cleanup()

    def test_provider_can_switch_without_restart(self) -> None:
        set_voice_provider_name("mirai_local")
        self.assertEqual(voice_provider_name(), "mirai_local")
        provider = build_voice_provider()
        self.assertIsInstance(provider, MiraiLocalClient)

        set_voice_provider_name("voicevox")
        self.assertEqual(voice_provider_name(), "voicevox")

    def test_mirai_local_health(self) -> None:
        response = Mock()
        response.ok = True
        response.json.return_value = {
            "ok": True,
            "engine": "mirai-tts",
        }
        with patch(
            "voice.mirai_local.requests.get",
            return_value=response,
        ):
            self.assertTrue(
                MiraiLocalClient(
                    "http://127.0.0.1:50150"
                ).available()
            )

    def test_mirai_local_writes_valid_wav(self) -> None:
        response = Mock()
        response.content = make_wav_bytes()
        response.raise_for_status.return_value = None
        target = Path(self.tmp.name) / "voice.wav"
        fake_settings = SimpleNamespace(
            mirai_tts_timeout_seconds=30,
        )
        with (
            patch(
                "voice.mirai_local.requests.post",
                return_value=response,
            ) as post,
            patch(
                "voice.mirai_local.settings",
                fake_settings,
            ),
        ):
            result = MiraiLocalClient(
                "http://127.0.0.1:50150"
            ).synthesize(
                "こんにちは",
                target,
                {"speed": 1.0, "pitch": 0.0},
            )
        self.assertEqual(result, target)
        self.assertTrue(target.is_file())
        payload = post.call_args.kwargs["json"]
        self.assertEqual(payload["text"], "こんにちは")
        self.assertIn("voice_params", payload)

    def test_mirai_local_rejects_invalid_audio(self) -> None:
        response = Mock()
        response.content = b"not-a-wav"
        response.raise_for_status.return_value = None
        fake_settings = SimpleNamespace(
            mirai_tts_timeout_seconds=30,
            mirai_tts_url="http://127.0.0.1:50150",
        )
        with (
            patch(
                "voice.mirai_local.requests.post",
                return_value=response,
            ),
            patch(
                "voice.mirai_local.settings",
                fake_settings,
            ),
        ):
            with self.assertRaises(RuntimeError):
                MiraiLocalClient().synthesize(
                    "test",
                    Path(self.tmp.name) / "bad.wav",
                )


if __name__ == "__main__":
    unittest.main()
