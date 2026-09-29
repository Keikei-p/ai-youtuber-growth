from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import scheduler
import storage


class StaleVideoOutputRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.video_root = self.root / "videos"
        self.video_root.mkdir(parents=True, exist_ok=True)

        self.old_db = storage.DB_PATH
        storage.DB_PATH = self.root / "stale-path.db"
        storage.init_db()

    def tearDown(self) -> None:
        storage.DB_PATH = self.old_db
        self.tmp.cleanup()

    def _video(self) -> int:
        return storage.save_video(
            idea="stale output",
            angle="test",
            title="stale output",
            script="safe",
            description="safe",
            tags=["mirai"],
            status="rendered",
        )

    def test_legacy_date_folder_path_is_repaired_to_current_filename(self) -> None:
        video_id = self._video()
        legacy = (
            self.video_root
            / "20260928"
            / f"_{video_id}.mp4"
        )
        current = (
            self.video_root
            / f"20260928_{video_id}.mp4"
        )
        current.write_bytes(b"mp4")

        storage.update_video_output(
            video_id,
            str(legacy),
            status="rendered",
        )
        row = storage.video_by_id(video_id)

        with patch.object(
            scheduler,
            "VIDEO_DIR",
            self.video_root,
        ):
            repaired = (
                scheduler._repair_legacy_video_output_path(
                    row
                )
            )

        self.assertEqual(
            Path(repaired["output_path"]),
            current,
        )
        db_row = storage.video_by_id(video_id)
        self.assertEqual(
            Path(db_row["output_path"]),
            current,
        )
        state = storage.get_channel_state(
            f"video_output_path_repaired_{video_id}",
            "",
        )
        self.assertIn(str(current), state)

    def test_missing_output_is_found_by_video_id_scan(self) -> None:
        video_id = self._video()
        stale = self.video_root / "missing.mp4"
        actual = (
            self.video_root
            / f"20260929_{video_id}.mp4"
        )
        actual.write_bytes(b"mp4")
        storage.update_video_output(
            video_id,
            str(stale),
            status="rendered",
        )

        with patch.object(
            scheduler,
            "VIDEO_DIR",
            self.video_root,
        ):
            repaired = (
                scheduler._repair_legacy_video_output_path(
                    storage.video_by_id(video_id)
                )
            )

        self.assertEqual(
            Path(repaired["output_path"]),
            actual,
        )

    def test_upload_preflight_regenerates_if_file_disappears_late(self) -> None:
        video_id = self._video()
        stale = self.video_root / f"20260928_{video_id}.mp4"
        recovered = (
            self.video_root
            / f"20260929_{video_id}.mp4"
        )
        recovered.write_bytes(b"new-mp4")
        row = {
            "video_id": video_id,
            "id": video_id,
            "status": "rendered",
            "output_path": str(stale),
        }
        refreshed = dict(row)
        refreshed["output_path"] = str(recovered)

        with (
            patch.object(
                scheduler,
                "_ensure_video_output",
                return_value=row,
            ),
            patch.object(
                scheduler,
                "regenerate_saved_video",
                return_value=refreshed,
            ) as regenerate,
        ):
            result = scheduler._ensure_upload_file_now(row)

        regenerate.assert_called_once_with(video_id)
        self.assertEqual(
            Path(result["output_path"]),
            recovered,
        )


if __name__ == "__main__":
    unittest.main()
