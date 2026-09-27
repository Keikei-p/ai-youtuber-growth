from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import automation_health
import storage


class AutomationHealthTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.old_db = storage.DB_PATH
        storage.DB_PATH = self.root / "health.db"
        storage.init_db()
        storage.set_channel_state("automation_enabled", "true")
        storage.set_channel_state("auto_upload_enabled", "true")
        storage.set_channel_state("production_autonomy_armed", "true")

    def tearDown(self) -> None:
        storage.DB_PATH = self.old_db
        self.tmp.cleanup()

    def test_cycle_status_reads_runtime_json(self) -> None:
        status_dir = self.root / "automation"
        status_dir.mkdir(parents=True)
        status_file = status_dir / "last_cycle_status.json"
        status_file.write_text(
            json.dumps(
                {
                    "status": "success",
                    "stage": "complete",
                    "detail": "ok",
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        result = automation_health.read_cycle_status(self.root)
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["stage"], "complete")

    def test_windows_task_status_parses_json_mode(self) -> None:
        script_dir = self.root / "automation"
        script_dir.mkdir(parents=True)
        script = script_dir / "windows_task_status.ps1"
        script.write_text("# test", encoding="utf-8")
        payload = {
            "registered": True,
            "enabled": True,
            "wake_to_run": True,
            "start_when_available": True,
            "triggers_count": 19,
            "last_task_result": 0,
            "wake_timer_present": True,
        }
        completed = SimpleNamespace(
            returncode=0,
            stdout=json.dumps(payload),
            stderr="",
        )
        with (
            patch.object(
                automation_health.shutil,
                "which",
                return_value="powershell.exe",
            ),
            patch.object(
                automation_health.subprocess,
                "run",
                return_value=completed,
            ) as run,
        ):
            result = automation_health.windows_task_status(
                self.root,
                platform_name="nt",
            )

        self.assertTrue(result["registered"])
        self.assertTrue(result["wake_to_run"])
        self.assertEqual(result["triggers_count"], 19)
        command = run.call_args.args[0]
        self.assertIn("-Json", command)

    def test_ready_health_requires_real_wake_task_and_no_post_blocker(self) -> None:
        task = {
            "supported": True,
            "registered": True,
            "enabled": True,
            "wake_to_run": True,
            "start_when_available": True,
            "wake_timer_present": True,
            "last_task_result": 0,
        }
        with (
            patch.object(
                automation_health,
                "windows_task_status",
                return_value=task,
            ),
            patch.object(
                automation_health,
                "read_cycle_status",
                return_value={
                    "status": "success",
                    "stage": "complete",
                },
            ),
            patch.object(
                automation_health,
                "read_auto_post_event",
                return_value={
                    "status": "verified",
                    "video_id": 1,
                },
            ),
            patch.object(
                automation_health,
                "read_automation_log_tail",
                return_value="cycle ok",
            ),
            patch.object(
                automation_health,
                "recent_recovery_events",
                return_value=[],
            ),
        ):
            health = automation_health.collect_auto_post_health(
                self.root,
                platform_name="nt",
            )

        self.assertEqual(health["overall"], "ready")
        self.assertTrue(health["ready"])
        self.assertEqual(health["problems"], [])

    def test_missing_wake_task_is_visible_as_attention(self) -> None:
        with (
            patch.object(
                automation_health,
                "windows_task_status",
                return_value={
                    "supported": True,
                    "registered": False,
                    "state": "missing",
                },
            ),
            patch.object(
                automation_health,
                "read_cycle_status",
                return_value={},
            ),
            patch.object(
                automation_health,
                "read_auto_post_event",
                return_value={},
            ),
            patch.object(
                automation_health,
                "read_automation_log_tail",
                return_value="",
            ),
            patch.object(
                automation_health,
                "recent_recovery_events",
                return_value=[],
            ),
        ):
            health = automation_health.collect_auto_post_health(
                self.root,
                platform_name="nt",
            )

        self.assertEqual(health["overall"], "attention")
        codes = {row["code"] for row in health["problems"]}
        self.assertIn("wake_task_missing", codes)

    def test_blocked_post_event_is_visible_as_attention(self) -> None:
        with (
            patch.object(
                automation_health,
                "windows_task_status",
                return_value={
                    "supported": True,
                    "registered": True,
                    "enabled": True,
                    "wake_to_run": True,
                    "start_when_available": True,
                    "wake_timer_present": True,
                },
            ),
            patch.object(
                automation_health,
                "read_cycle_status",
                return_value={
                    "status": "success",
                    "stage": "complete",
                },
            ),
            patch.object(
                automation_health,
                "read_auto_post_event",
                return_value={
                    "status": "blocked",
                    "code": "oauth",
                    "detail": "再認証が必要です。",
                },
            ),
            patch.object(
                automation_health,
                "read_automation_log_tail",
                return_value="",
            ),
            patch.object(
                automation_health,
                "recent_recovery_events",
                return_value=[],
            ),
        ):
            health = automation_health.collect_auto_post_health(
                self.root,
                platform_name="nt",
            )

        self.assertEqual(health["overall"], "attention")
        self.assertTrue(
            any(
                row["code"] == "oauth"
                for row in health["problems"]
            )
        )


class AutomationObservabilityStaticTests(unittest.TestCase):
    def test_windows_cycle_writes_structured_progress(self) -> None:
        source = Path("automation/windows_cycle.ps1").read_text(
            encoding="utf-8"
        )
        self.assertIn("last_cycle_status.json", source)
        self.assertIn("Write-CycleStatus", source)
        self.assertIn('"due_first"', source)
        self.assertIn('"network_wait"', source)
        self.assertIn('"complete"', source)

    def test_install_script_verifies_registered_wake_settings(self) -> None:
        source = Path(
            "automation/install_windows_task.ps1"
        ).read_text(encoding="utf-8")
        register_index = source.index("Register-ScheduledTask")
        verify_index = source.index(
            "Get-ScheduledTask -TaskName",
            register_index,
        )
        self.assertLess(register_index, verify_index)
        self.assertIn(
            "Registered task verification failed: WakeToRun=false",
            source,
        )
        self.assertIn(
            "Registered task verification failed: StartWhenAvailable=false",
            source,
        )

    def test_web_dashboard_contains_auto_post_sleep_diagnostics(self) -> None:
        source = Path("webapp.py").read_text(encoding="utf-8")
        self.assertIn("自動投稿・スリープ診断", source)
        self.assertIn("auto_post_health", source)
        self.assertIn("automationLogs", source)
        self.assertIn("wake_task_check", source)


if __name__ == "__main__":
    unittest.main()
