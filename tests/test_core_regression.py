from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import autonomy_policy
import scheduler
import storage


class CoreRegressionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        storage.DB_PATH = Path(self.tmp.name) / "memory.db"
        storage.init_db()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_sqlite_channel_state_roundtrip(self) -> None:
        storage.set_channel_state("test_key", "ok")
        self.assertEqual(storage.get_channel_state("test_key"), "ok")

    def test_cancel_only_queued_videos(self) -> None:
        video_id = storage.save_video(
            idea="idea",
            angle="angle",
            title="title",
            script="script",
        )
        storage.queue_video(video_id, "2026-09-25T15:00+09:00")
        cancelled = storage.cancel_queued_videos([video_id])
        self.assertEqual(cancelled, 1)
        self.assertEqual(storage.queued_items(), [])

    def test_full_test_slots_are_ordered_and_before_end(self) -> None:
        tz = ZoneInfo("Asia/Tokyo")
        now = datetime(2026, 9, 25, 12, 13, tzinfo=tz)
        end = datetime(2026, 9, 25, 18, 0, tzinfo=tz)
        slots = scheduler._test_slots(now, end, 3)
        self.assertEqual(len(slots), 3)
        self.assertTrue(all(a < b for a, b in zip(slots, slots[1:])))
        self.assertTrue(all(now < slot < end for slot in slots))
        self.assertLessEqual(slots[-1], end.replace(minute=45))

    def test_autonomy_defaults_unknown_actions_to_approval(self) -> None:
        self.assertEqual(
            autonomy_policy.classify_action("unknown_new_action"),
            "approval",
        )
        self.assertEqual(
            autonomy_policy.classify_action("script_guidance"),
            "auto",
        )
        self.assertEqual(
            autonomy_policy.classify_action("minor_code_change"),
            "test_then_auto",
        )


if __name__ == "__main__":
    unittest.main()
