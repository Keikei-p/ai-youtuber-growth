"""Regression checks for Windows PowerShell 5.1 scheduled-cycle bootstrap.

These checks never run the scheduler or trigger YouTube posting.
"""
from __future__ import annotations

import unittest
from pathlib import Path


CYCLE = Path(__file__).resolve().parents[1] / "automation" / "windows_cycle.ps1"


class WindowsCycleStartupTests(unittest.TestCase):
    def test_entrypoint_keeps_utf8_bom_for_windows_powershell_51(self) -> None:
        raw = CYCLE.read_bytes()
        self.assertTrue(
            raw.startswith(b"\xef\xbb\xbf"),
            "PowerShell 5.1 can misparse non-ASCII UTF-8 text without a BOM",
        )
        self.assertIn("Write-CycleStatus", raw.decode("utf-8-sig"))

    def test_named_mutex_uses_a_valid_constructor_without_weakening_lock(self) -> None:
        source = CYCLE.read_text(encoding="utf-8-sig")
        self.assertIn(
            '$mutex = [System.Threading.Mutex]::new($false, "AIYoutuberGrowthCycle")',
            source,
        )
        self.assertNotIn("New-Object System.Threading.Mutex(", source)
        self.assertIn("if (-not $mutex.WaitOne(0))", source)
        self.assertIn("$mutex.ReleaseMutex()", source)
        self.assertIn("$mutex.Dispose()", source)


if __name__ == "__main__":
    unittest.main()
