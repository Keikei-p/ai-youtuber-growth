from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import automation_health
import delivery_supervisor
import resource_governor
import runtime_bootstrap
import storage


class RuntimeReliabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.old_db = storage.DB_PATH
        storage.DB_PATH = self.root / "runtime-reliability.db"
        storage.init_db()

    def tearDown(self) -> None:
        storage.DB_PATH = self.old_db
        self.tmp.cleanup()

    def test_legacy_on_on_state_arms_even_before_episode_one_exists(self) -> None:
        storage.set_channel_state("automation_enabled", "true")
        storage.set_channel_state("auto_upload_enabled", "true")
        storage.set_channel_state("runtime_cancel_requested", "false")
        storage.set_channel_state("production_autonomy_armed", "false")

        with patch.object(
            delivery_supervisor,
            "get_credentials",
            return_value=object(),
        ):
            result = delivery_supervisor.self_heal_delivery_controls()

        self.assertEqual(
            storage.get_channel_state(
                "production_autonomy_armed",
                "",
            ),
            "true",
        )
        self.assertEqual(
            storage.get_channel_state(
                "full_autopilot_enabled",
                "",
            ),
            "true",
        )
        self.assertTrue(result["repairs"])

    def test_armed_autopilot_is_not_blocked_only_because_user_is_active(self) -> None:
        storage.set_channel_state(
            "production_autonomy_armed",
            "true",
        )
        snapshot = {
            "gpu": {
                "memory_free_mb": 4096,
                "loaded_ollama_models": [],
            },
            "memory": {
                "available_mb": 8192,
                "total_mb": 16384,
                "load_percent": 50,
            },
            "user_idle_seconds": 2.0,
        }
        fake_settings = SimpleNamespace(
            resource_min_memory_mb=2500,
            resource_min_idle_seconds=45,
            resource_min_gpu_free_mb=1800,
        )
        with (
            patch.object(
                resource_governor,
                "resource_snapshot",
                return_value=snapshot,
            ),
            patch.object(
                resource_governor,
                "settings",
                fake_settings,
            ),
        ):
            decision = (
                resource_governor.background_production_decision()
            )

        self.assertTrue(decision.allowed)
        self.assertEqual(decision.mode, "autopilot")

    def test_unarmed_background_work_still_defers_while_user_is_active(self) -> None:
        storage.set_channel_state(
            "production_autonomy_armed",
            "false",
        )
        snapshot = {
            "gpu": {
                "memory_free_mb": 4096,
                "loaded_ollama_models": [],
            },
            "memory": {
                "available_mb": 8192,
                "total_mb": 16384,
                "load_percent": 50,
            },
            "user_idle_seconds": 2.0,
        }
        fake_settings = SimpleNamespace(
            resource_min_memory_mb=2500,
            resource_min_idle_seconds=45,
            resource_min_gpu_free_mb=1800,
        )
        with (
            patch.object(
                resource_governor,
                "resource_snapshot",
                return_value=snapshot,
            ),
            patch.object(
                resource_governor,
                "settings",
                fake_settings,
            ),
        ):
            decision = (
                resource_governor.background_production_decision()
            )

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.mode, "defer")

    def test_runtime_bootstrap_persists_service_blockers_without_crashing(self) -> None:
        with (
            patch.object(
                runtime_bootstrap,
                "_start_ollama",
                return_value={
                    "ready": False,
                    "action": "not_found",
                },
            ),
            patch.object(
                runtime_bootstrap,
                "_start_voicevox",
                return_value={
                    "ready": True,
                    "action": "already_running",
                },
            ),
            patch.object(
                runtime_bootstrap,
                "execution_mode",
                return_value="local",
            ),
            patch.object(
                runtime_bootstrap.shutil,
                "which",
                return_value="ffmpeg",
            ),
        ):
            result = runtime_bootstrap.bootstrap_runtime()

        self.assertFalse(result["ready"])
        self.assertEqual(result["blockers"], ["ollama"])
        saved = json.loads(
            storage.get_channel_state(
                "runtime_bootstrap_last",
                "{}",
            )
        )
        self.assertEqual(saved["blockers"], ["ollama"])

    def test_health_flags_failed_windows_task_result(self) -> None:
        storage.set_channel_state("automation_enabled", "true")
        storage.set_channel_state("auto_upload_enabled", "true")
        storage.set_channel_state(
            "production_autonomy_armed",
            "true",
        )
        task = {
            "supported": True,
            "registered": True,
            "enabled": True,
            "wake_to_run": True,
            "start_when_available": True,
            "wake_timer_present": True,
            "last_task_result": 1,
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

        codes = {
            row["code"]
            for row in health["problems"]
        }
        self.assertIn(
            "wake_task_last_run_failed",
            codes,
        )

    def test_windows_cycle_can_rebuild_python_and_bootstrap_services(self) -> None:
        source = Path(
            "automation/windows_cycle.ps1"
        ).read_text(encoding="utf-8")
        self.assertIn("Resolve-MiraiPython", source)
        self.assertIn("python_repair", source)
        self.assertIn("dependency_repair", source)
        self.assertIn("runtime_bootstrap.py", source)
        self.assertIn("Ensure-MiraiDependencies", source)


if __name__ == "__main__":
    unittest.main()
