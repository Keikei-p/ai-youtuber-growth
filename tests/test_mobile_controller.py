from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import mobile_pairing
import storage
import webapp


class MobileControllerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db = storage.DB_PATH
        storage.DB_PATH = Path(self.tmp.name) / "mobile.db"
        storage.init_db()

    def tearDown(self) -> None:
        storage.DB_PATH = self.old_db
        self.tmp.cleanup()

    def test_pairing_token_is_hashed_and_verifiable(self) -> None:
        result = mobile_pairing.create_pairing_token()
        token = result["token"]
        stored = storage.get_channel_state(
            mobile_pairing.TOKEN_HASH_KEY,
            "",
        )

        self.assertTrue(token)
        self.assertNotEqual(stored, token)
        self.assertEqual(
            len(stored),
            64,
        )
        self.assertTrue(
            mobile_pairing.verify_pairing_token(token)
        )
        self.assertFalse(
            mobile_pairing.verify_pairing_token(
                token + "wrong"
            )
        )

    def test_revoke_invalidates_mobile_token(self) -> None:
        token = mobile_pairing.create_pairing_token()[
            "token"
        ]
        self.assertTrue(
            mobile_pairing.verify_pairing_token(token)
        )
        mobile_pairing.revoke_pairing_token()
        self.assertFalse(
            mobile_pairing.verify_pairing_token(token)
        )
        self.assertFalse(
            mobile_pairing.pairing_status()["paired"]
        )

    def test_bearer_parser_is_strict(self) -> None:
        self.assertEqual(
            mobile_pairing.bearer_token(
                "Bearer abc123"
            ),
            "abc123",
        )
        self.assertEqual(
            mobile_pairing.bearer_token(
                "Basic abc123"
            ),
            "",
        )

    def test_mobile_status_payload_is_lightweight(self) -> None:
        with (
            patch.object(
                webapp,
                "_cached_auto_post_health",
                return_value={
                    "next_queue": {
                        "video_id": 12,
                        "scheduled_for": "2026-10-07T12:00+09:00",
                        "title": "next",
                    },
                    "last_post_event": {
                        "status": "verified",
                    },
                    "problems": [],
                },
            ),
            patch.object(
                webapp,
                "product_status",
                return_value={
                    "name": "Mirai Production OS",
                    "version": "0.11.0-beta",
                },
            ),
            patch.object(
                webapp,
                "autopilot_status",
                return_value={
                    "armed": True,
                    "running": True,
                },
            ),
            patch.object(
                webapp,
                "execution_status",
                return_value={"mode": "local"},
            ),
        ):
            payload = webapp._mobile_status_payload()

        self.assertEqual(
            payload["next_queue"]["video_id"],
            12,
        )
        self.assertEqual(
            payload["last_post"]["status"],
            "verified",
        )
        self.assertNotIn("studio", payload)
        self.assertNotIn("gpu", payload)
        self.assertNotIn("videos", payload)

    def test_mobile_wake_control_does_not_start_heavy_generation(self) -> None:
        with patch.object(
            webapp._wake_event,
            "set",
        ) as wake:
            result = webapp._mobile_control("wake")
        self.assertTrue(result["ok"])
        wake.assert_called_once()

    def test_capacitor_8_source_is_present(self) -> None:
        package = json.loads(
            Path("mobile/package.json").read_text(
                encoding="utf-8",
            )
        )
        config = json.loads(
            Path(
                "mobile/capacitor.config.json"
            ).read_text(encoding="utf-8")
        )

        self.assertEqual(
            package["dependencies"][
                "@capacitor/core"
            ],
            "8.5.2",
        )
        self.assertEqual(
            package["dependencies"][
                "@capacitor/android"
            ],
            "8.5.2",
        )
        self.assertEqual(
            package["dependencies"][
                "@capacitor/ios"
            ],
            "8.5.2",
        )
        self.assertEqual(
            config["webDir"],
            "www",
        )
        self.assertFalse(
            config["android"]["allowMixedContent"]
        )

    def test_mobile_client_uses_bearer_and_https(self) -> None:
        source = Path(
            "mobile/www/app.js"
        ).read_text(encoding="utf-8")
        self.assertIn(
            "'Authorization':'Bearer '+cfg.token",
            source,
        )
        self.assertIn(
            "/^https:",
            source,
        )
        self.assertNotIn(
            "client_secret",
            source,
        )
        self.assertNotIn(
            "api_key",
            source.lower(),
        )

    def test_mobile_api_and_pairing_ui_are_wired(self) -> None:
        source = Path("webapp.py").read_text(
            encoding="utf-8",
        )
        self.assertIn(
            'path == "/api/mobile/status"',
            source,
        )
        self.assertIn(
            'path == "/api/mobile/control"',
            source,
        )
        self.assertIn(
            'path == "/api/mobile/pairing"',
            source,
        )
        self.assertIn(
            "_mobile_authorized",
            source,
        )
        self.assertIn(
            "スマホ接続コードを発行",
            source,
        )


if __name__ == "__main__":
    unittest.main()
