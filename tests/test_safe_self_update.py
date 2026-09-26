from __future__ import annotations

import json
import unittest
from unittest.mock import patch

import safe_self_update


class SafeSelfUpdateTests(unittest.TestCase):
    def test_porcelain_parser_normalizes_paths(self) -> None:
        files = safe_self_update._porcelain_files(
            " M automation\\windows_cycle.ps1\n"
            "?? notes.txt\n"
        )
        self.assertEqual(
            files,
            [
                "automation/windows_cycle.ps1",
                "notes.txt",
            ],
        )

    def test_known_safe_repair_can_be_autostashed(self) -> None:
        payload = {
            "status": "applied",
            "changed": [
                "automation/windows_cycle.ps1:due-first",
            ],
        }
        with patch.object(
            safe_self_update,
            "get_channel_state",
            return_value=json.dumps(payload),
        ):
            files = safe_self_update._known_repair_dirty_files(
                " M automation/windows_cycle.ps1\n"
            )
        self.assertEqual(
            files,
            ["automation/windows_cycle.ps1"],
        )

    def test_user_local_changes_are_never_treated_as_safe_repair(self) -> None:
        payload = {
            "status": "applied",
            "changed": [
                "automation/windows_cycle.ps1:due-first",
            ],
        }
        with patch.object(
            safe_self_update,
            "get_channel_state",
            return_value=json.dumps(payload),
        ):
            files = safe_self_update._known_repair_dirty_files(
                " M automation/windows_cycle.ps1\n"
                " M scheduler.py\n"
            )
        self.assertEqual(files, [])

    def test_production_update_defers_while_cycle_lock_is_held(self) -> None:
        with (
            patch.object(
                safe_self_update,
                "acquire_runtime_lock",
                return_value=False,
            ),
            patch.object(
                safe_self_update,
                "_save",
                side_effect=lambda value: value,
            ),
        ):
            result = safe_self_update.safe_self_update()

        self.assertEqual(result["status"], "deferred")
        self.assertEqual(
            result["reason"],
            "automation_cycle_active",
        )

    def test_update_validation_includes_autonomy_invariants(self) -> None:
        source = open(
            "safe_self_update.py",
            encoding="utf-8",
        ).read()
        self.assertIn(
            "inspect_code_health(worktree_root)",
            source,
        )
        self.assertIn(
            "mirai-safe-repair-autostash",
            source,
        )
        self.assertIn(
            "local_changes_present",
            source,
        )


if __name__ == "__main__":
    unittest.main()
