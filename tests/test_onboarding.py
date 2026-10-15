from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import onboarding
import storage


def valid_client_secret() -> dict:
    return {
        "installed": {
            "client_id": "example.apps.googleusercontent.com",
            "project_id": "mirai-test",
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "auth_provider_x509_cert_url": (
                "https://www.googleapis.com/oauth2/v1/certs"
            ),
            "client_secret": "super-secret-test-value",
            "redirect_uris": ["http://localhost"],
        }
    }


class OnboardingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

        self.old_db = storage.DB_PATH
        storage.DB_PATH = self.root / "onboarding.db"
        storage.init_db()

        self.secret_patch = patch.object(
            onboarding,
            "CLIENT_SECRET_FILE",
            self.root / "client_secret.json",
        )
        self.token_patch = patch.object(
            onboarding,
            "TOKEN_FILE",
            self.root / "token.json",
        )
        self.secret_patch.start()
        self.token_patch.start()

    def tearDown(self) -> None:
        self.token_patch.stop()
        self.secret_patch.stop()
        storage.DB_PATH = self.old_db
        self.tmp.cleanup()

    def test_save_valid_desktop_oauth_json_without_leaking_secret(self) -> None:
        payload = valid_client_secret()
        status = onboarding.save_youtube_client_secret(
            payload
        )

        saved = json.loads(
            onboarding.CLIENT_SECRET_FILE.read_text(
                encoding="utf-8",
            )
        )
        self.assertEqual(
            saved["installed"]["client_id"],
            payload["installed"]["client_id"],
        )
        self.assertTrue(
            status["youtube"]["client_secret"][
                "valid"
            ]
        )

        public_status = json.dumps(
            status,
            ensure_ascii=False,
        )
        self.assertNotIn(
            payload["installed"]["client_secret"],
            public_status,
        )
        self.assertNotIn(
            payload["installed"]["client_id"],
            public_status,
        )

    def test_rejects_non_desktop_oauth_json(self) -> None:
        with self.assertRaises(ValueError):
            onboarding.save_youtube_client_secret(
                {
                    "web": {
                        "client_id": "web-client",
                        "client_secret": "secret",
                    }
                }
            )

    def test_replacing_oauth_json_invalidates_old_token(self) -> None:
        onboarding.TOKEN_FILE.write_text(
            "old-token",
            encoding="utf-8",
        )
        onboarding.save_youtube_client_secret(
            valid_client_secret()
        )
        self.assertFalse(
            onboarding.TOKEN_FILE.exists()
        )

    def test_disconnect_removes_token_but_keeps_oauth_config(self) -> None:
        onboarding.save_youtube_client_secret(
            valid_client_secret()
        )
        onboarding.TOKEN_FILE.write_text(
            "{}",
            encoding="utf-8",
        )

        onboarding.disconnect_youtube(
            remove_client_secret=False,
        )

        self.assertFalse(
            onboarding.TOKEN_FILE.exists()
        )
        self.assertTrue(
            onboarding.CLIENT_SECRET_FILE.exists()
        )

    def test_interactive_connect_marks_setup_complete(self) -> None:
        onboarding.save_youtube_client_secret(
            valid_client_secret()
        )

        def fake_get_credentials(*, interactive: bool = True):
            if interactive:
                onboarding.TOKEN_FILE.write_text(
                    "{}",
                    encoding="utf-8",
                )
            return object()

        with patch.object(
            onboarding,
            "get_credentials",
            side_effect=fake_get_credentials,
        ):
            result = (
                onboarding.connect_youtube_interactive()
            )

        self.assertTrue(result["complete"])
        self.assertTrue(
            result["youtube"]["ready"]
        )
        self.assertEqual(
            storage.get_channel_state(
                onboarding.SETUP_COMPLETE_KEY,
                "",
            ),
            "true",
        )

    def test_webapp_wires_onboarding_without_secret_echo(self) -> None:
        source = Path("webapp.py").read_text(
            encoding="utf-8",
        )
        self.assertIn(
            'path == "/api/onboarding/client-secret"',
            source,
        )
        self.assertIn(
            'path == "/api/onboarding/youtube-connect"',
            source,
        )
        self.assertIn(
            'path == "/api/onboarding/disconnect"',
            source,
        )
        self.assertIn(
            "saveYouTubeClientSecret",
            source,
        )
        self.assertNotIn(
            'self._json({"client_secret": payload',
            source,
        )

    def test_dashboard_startup_does_not_boot_ai_services(self) -> None:
        source = Path("webapp.py").read_text(
            encoding="utf-8",
        )
        start = source.index("def run(open_browser")
        run_block = source[start:]
        self.assertNotIn(
            "_ensure_local_services()",
            run_block,
        )
        self.assertIn(
            "server.serve_forever()",
            run_block,
        )


if __name__ == "__main__":
    unittest.main()
