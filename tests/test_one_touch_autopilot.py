from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import autopilot_controller
import delivery_supervisor
import storage


class OneTouchAutopilotTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db = storage.DB_PATH
        storage.DB_PATH = Path(self.tmp.name) / "autopilot.db"
        storage.init_db()

    def tearDown(self) -> None:
        storage.DB_PATH = self.old_db
        self.tmp.cleanup()

    def test_start_autopilot_arms_everything_and_clears_safety_stop(self) -> None:
        storage.set_channel_state(
            "runtime_cancel_requested",
            "true",
        )
        with (
            patch.object(
                autopilot_controller,
                "get_credentials",
                return_value=object(),
            ),
            patch.object(
                delivery_supervisor,
                "get_credentials",
                return_value=object(),
            ),
        ):
            status = autopilot_controller.start_autopilot()

        self.assertTrue(status["armed"])
        self.assertTrue(status["running"])
        self.assertEqual(
            storage.get_channel_state(
                "production_autonomy_armed",
                "",
            ),
            "true",
        )
        self.assertEqual(
            storage.get_channel_state(
                "automation_enabled",
                "",
            ),
            "true",
        )
        self.assertEqual(
            storage.get_channel_state(
                "auto_upload_enabled",
                "",
            ),
            "true",
        )
        self.assertEqual(
            storage.get_channel_state(
                "runtime_cancel_requested",
                "",
            ),
            "false",
        )
        self.assertEqual(
            storage.get_channel_state(
                "full_autopilot_enabled",
                "",
            ),
            "true",
        )

    def test_heal_autopilot_restores_switches_after_restart_drift(self) -> None:
        storage.set_channel_state(
            "production_autonomy_armed",
            "true",
        )
        storage.set_channel_state(
            "automation_enabled",
            "false",
        )
        storage.set_channel_state(
            "auto_upload_enabled",
            "false",
        )
        storage.set_channel_state(
            "runtime_cancel_requested",
            "false",
        )

        with patch.object(
            delivery_supervisor,
            "get_credentials",
            return_value=object(),
        ):
            status = autopilot_controller.heal_autopilot()

        self.assertTrue(status["running"])
        self.assertEqual(
            storage.get_channel_state(
                "automation_enabled",
                "",
            ),
            "true",
        )
        self.assertEqual(
            storage.get_channel_state(
                "auto_upload_enabled",
                "",
            ),
            "true",
        )
        self.assertEqual(
            status["last_event"]["status"],
            "repaired",
        )

    def test_heal_autopilot_never_bypasses_explicit_safety_stop(self) -> None:
        storage.set_channel_state(
            "production_autonomy_armed",
            "true",
        )
        storage.set_channel_state(
            "automation_enabled",
            "false",
        )
        storage.set_channel_state(
            "auto_upload_enabled",
            "false",
        )
        storage.set_channel_state(
            "runtime_cancel_requested",
            "true",
        )

        status = autopilot_controller.heal_autopilot()

        self.assertFalse(status["running"])
        self.assertTrue(status["runtime_cancel_requested"])
        self.assertEqual(
            storage.get_channel_state(
                "automation_enabled",
                "",
            ),
            "false",
        )
        self.assertEqual(
            status["last_event"]["status"],
            "blocked",
        )

    def test_stop_autopilot_disarms_persistent_operation(self) -> None:
        storage.set_channel_state(
            "production_autonomy_armed",
            "true",
        )
        storage.set_channel_state(
            "automation_enabled",
            "true",
        )
        storage.set_channel_state(
            "auto_upload_enabled",
            "true",
        )

        status = autopilot_controller.stop_autopilot()

        self.assertFalse(status["armed"])
        self.assertFalse(status["running"])
        self.assertFalse(status["automation_enabled"])
        self.assertFalse(status["auto_upload_enabled"])
        self.assertEqual(
            storage.get_channel_state(
                "full_autopilot_enabled",
                "",
            ),
            "false",
        )

    def test_start_autopilot_requires_real_youtube_auth_once(self) -> None:
        with patch.object(
            autopilot_controller,
            "get_credentials",
            side_effect=RuntimeError("token missing"),
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "YouTube認証",
            ):
                autopilot_controller.start_autopilot()

        self.assertFalse(
            autopilot_controller.production_autonomy_armed()
        )
        event = autopilot_controller.autopilot_status()[
            "last_event"
        ]
        self.assertEqual(event["status"], "blocked")
        self.assertEqual(event["code"], "youtube_auth")

    def test_dashboard_exposes_single_primary_autopilot_control(self) -> None:
        source = Path("webapp.py").read_text(
            encoding="utf-8",
        )
        self.assertIn("▶ 完全自動運用開始", source)
        self.assertIn("/api/autopilot", source)
        self.assertIn("toggleFullAutopilot()", source)
        self.assertIn(
            "'自動運転':'settings'",
            source,
        )
        self.assertIn(
            "'毎日自動投稿':'settings'",
            source,
        )
        self.assertIn(
            'launcher = ROOT / "web_background.pyw"',
            source,
        )

    def test_background_launcher_does_not_open_browser(self) -> None:
        source = Path("web_background.pyw").read_text(
            encoding="utf-8",
        )
        self.assertIn("run(open_browser=False)", source)

    def test_cycle_self_heals_before_automation_gate(self) -> None:
        source = Path("webapp.py").read_text(
            encoding="utf-8",
        )
        start = source.index("def _cycle_worker()")
        end = source.index("def _read_log_tail", start)
        cycle = source[start:end]
        self.assertLess(
            cycle.index("heal_autopilot()"),
            cycle.index("if automation_enabled():"),
        )


if __name__ == "__main__":
    unittest.main()
