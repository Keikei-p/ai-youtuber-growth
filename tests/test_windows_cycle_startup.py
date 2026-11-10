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

    def test_named_mutex_and_locking_behavior_are_unchanged(self) -> None:
        source = CYCLE.read_text(encoding="utf-8-sig")
        self.assertIn(
            '$mutex = New-Object System.Threading.Mutex($false, "AIYoutuberGrowthCycle")',
            source,
        )
        self.assertIn("if (-not $mutex.WaitOne(0))", source)
        self.assertIn("$mutex.ReleaseMutex()", source)
        self.assertIn("$mutex.Dispose()", source)

    def test_unsigned_sleep_flag_avoids_signed_hex_conversion(self) -> None:
        source = CYCLE.read_text(encoding="utf-8-sig")
        # Windows PowerShell 5.1 parses 0x80000000 as a negative Int32;
        # casting that value to UInt32 raises before cycle status/logging.
        self.assertIn("$ES_CONTINUOUS = [uint32]2147483648", source)
        self.assertNotIn("[uint32]0x80000000", source)




    def test_cycle_uses_utf8_only_log_and_preserves_legacy_files(self) -> None:
        source = CYCLE.read_text(encoding="utf-8-sig")
        self.assertIn('"-utf8.log"', source)
        self.assertIn('[System.IO.File]::WriteAllBytes($LogFile, [byte[]]@(239, 187, 191))', source)
        self.assertIn('$PSDefaultParameterValues["Out-File:Encoding"] = "utf8"', source)
        self.assertIn('$env:PYTHONIOENCODING = "utf-8"', source)
        self.assertIn('[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)', source)
        self.assertIn("Add-Content -Path $LogFile -Encoding UTF8 -Value", source)
        self.assertNotIn("Add-Content -Path $LogFile -Value", source)



if __name__ == "__main__":
    unittest.main()
