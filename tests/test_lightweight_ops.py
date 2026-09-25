from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import maintenance
import runtime_control
import storage
from quick_test import run_quick_diagnostics


class LightweightOpsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.old_db = storage.DB_PATH
        storage.DB_PATH = self.root / "ops.db"
        storage.init_db()

    def tearDown(self) -> None:
        storage.DB_PATH = self.old_db
        self.tmp.cleanup()

    def test_ai_video_license_confirmation_controls_runtime_toggle(self) -> None:
        runtime_control.set_ai_video_license_confirmed(False)
        self.assertFalse(runtime_control.ai_video_license_confirmed())
        self.assertFalse(runtime_control.ai_video_enabled())

        runtime_control.set_ai_video_license_confirmed(True)
        self.assertTrue(runtime_control.ai_video_license_confirmed())
        runtime_control.set_ai_video_enabled(True)
        self.assertTrue(runtime_control.ai_video_enabled())

        runtime_control.set_ai_video_license_confirmed(False)
        self.assertFalse(runtime_control.ai_video_license_confirmed())
        self.assertFalse(runtime_control.ai_video_enabled())

    def test_visual_settings_change_without_process_restart(self) -> None:
        runtime_control.set_visual_candidate_count(3)
        runtime_control.set_visual_background_candidates(2)
        runtime_control.set_visual_retry_rounds(0)
        runtime_control.set_visual_min_score(74)
        runtime_control.set_visual_video_min_score(71)

        state = runtime_control.visual_runtime_settings()
        self.assertEqual(state["candidate_count"], 3)
        self.assertEqual(state["background_candidates"], 2)
        self.assertEqual(state["retry_rounds"], 0)
        self.assertEqual(state["min_score"], 74)
        self.assertEqual(state["video_min_score"], 71)

    def test_prune_folder_keeps_recent_and_protected_files(self) -> None:
        folder = self.root / "cache"
        folder.mkdir()
        files = []
        for index in range(6):
            path = folder / f"{index}.bin"
            path.write_bytes(b"x" * (index + 1))
            timestamp = time.time() - (100 - index)
            os.utime(path, (timestamp, timestamp))
            files.append(path)

        protected = {files[0]}
        removed, freed = maintenance.prune_folder(
            folder,
            keep=2,
            protected=protected,
        )
        self.assertEqual(removed, 3)
        self.assertGreater(freed, 0)
        self.assertTrue(files[0].exists())
        self.assertTrue(files[-1].exists())
        self.assertTrue(files[-2].exists())

    def test_log_rotation_caps_large_log(self) -> None:
        log = self.root / "webapp.log"
        log.write_bytes(b"x" * 200_000)
        freed = maintenance.rotate_log(
            log,
            max_bytes=100_000,
            backups=2,
        )
        self.assertGreater(freed, 0)
        self.assertTrue(log.exists())
        self.assertTrue((self.root / "webapp.log.1").exists())

    def test_quick_diagnostics_survives_missing_ffmpeg(self) -> None:
        with patch("quick_test.shutil.which", return_value=None):
            result = run_quick_diagnostics()
        self.assertFalse(result["ok"])
        self.assertTrue(result["checks"]["database"]["ok"])
        self.assertTrue(result["checks"]["visual_quality"]["ok"])
        self.assertFalse(result["checks"]["ffmpeg"]["ok"])
        self.assertFalse(result["persistent_test_media"])


if __name__ == "__main__":
    unittest.main()
