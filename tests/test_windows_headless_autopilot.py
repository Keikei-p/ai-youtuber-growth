from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import runtime_bootstrap
import storage


class WindowsHeadlessAutopilotTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.previous_db = storage.DB_PATH
        storage.DB_PATH = Path(self.temp.name) / "test.db"
        storage.init_db()

    def tearDown(self) -> None:
        storage.DB_PATH = self.previous_db
        self.temp.cleanup()

    def test_service_start_attempts_back_off_and_resume_after_wait(self) -> None:
        start = 1_700_000_000
        with patch.object(runtime_bootstrap.time, "time", return_value=start):
            allowed, _ = runtime_bootstrap._service_launch_guard("ollama")
            self.assertTrue(allowed)
            allowed, reason = runtime_bootstrap._service_launch_guard("ollama")
            self.assertFalse(allowed)
            self.assertIn("backoff", reason)

        with patch.object(runtime_bootstrap.time, "time", return_value=start + 901):
            allowed, _ = runtime_bootstrap._service_launch_guard("ollama")
            self.assertTrue(allowed)

        with patch.object(runtime_bootstrap.time, "time", return_value=start + 1802):
            allowed, _ = runtime_bootstrap._service_launch_guard("ollama")
            self.assertTrue(allowed)

        with patch.object(runtime_bootstrap.time, "time", return_value=start + 1803):
            allowed, _ = runtime_bootstrap._service_launch_guard("ollama")
            self.assertFalse(allowed)

        with patch.object(runtime_bootstrap.time, "time", return_value=start + 1802 + 6 * 3600):
            allowed, _ = runtime_bootstrap._service_launch_guard("ollama")
            self.assertTrue(allowed)

    def test_service_ready_clears_launch_backoff(self) -> None:
        with patch.object(runtime_bootstrap.time, "time", return_value=1_700_000_000):
            self.assertTrue(runtime_bootstrap._service_launch_guard("voicevox")[0])
            self.assertFalse(runtime_bootstrap._service_launch_guard("voicevox")[0])
            runtime_bootstrap._service_ready("voicevox")
            self.assertTrue(runtime_bootstrap._service_launch_guard("voicevox")[0])

    def test_service_subprocess_logs_errors_instead_of_discarding(self) -> None:
        with (
            patch.object(runtime_bootstrap, "ROOT", Path(self.temp.name)),
            patch.object(runtime_bootstrap.subprocess, "Popen") as popen,
        ):
            runtime_bootstrap._spawn_service(["local-service", "serve"], cwd=Path(self.temp.name), name="ollama")
            self.assertEqual(popen.call_count, 1)
            kwargs = popen.call_args.kwargs
            self.assertEqual(kwargs["stderr"], runtime_bootstrap.subprocess.STDOUT)
            self.assertNotEqual(kwargs["stdout"], runtime_bootstrap.subprocess.DEVNULL)
            self.assertTrue((Path(self.temp.name) / "logs" / "runtime-ollama.log").exists())

    def test_install_is_hidden_and_disables_immediate_restart_bursts(self) -> None:
        install = Path("automation/install_windows_task.ps1").read_text(encoding="utf-8-sig")
        self.assertIn("hidden_task_runner.vbs", install)
        self.assertIn('New-ScheduledTaskAction -Execute $WScript', install)
        self.assertIn("-RestartCount 0", install)
        self.assertIn("WakeToRun", install)
        self.assertIn("New-ScheduledTaskTrigger -AtLogOn", install)
        self.assertIn('$PrepareMinutes = 90', install)
        self.assertIn("$RecoveryMinutes = @(5, 15, 30, 60)", install)


if __name__ == "__main__":
    unittest.main()
