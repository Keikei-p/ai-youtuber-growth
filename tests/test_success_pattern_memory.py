from __future__ import annotations

import unittest

from learner import build_success_pattern_memory


class SuccessPatternMemoryTests(unittest.TestCase):
    def test_latest_checkpoint_per_video_is_used(self) -> None:
        history = [
            {
                "video_id": 1,
                "checkpoint_hours": 24,
                "score": 50,
                "views": 50,
                "avg_view_percentage": 50,
                "ctr": 2,
                "comments": 0,
                "shares": 0,
                "subscribers_gained": 0,
                "title": "old",
                "idea": "idea1",
                "angle": "a",
                "note": "24h",
            },
            {
                "video_id": 1,
                "checkpoint_hours": 168,
                "score": 90,
                "views": 500,
                "avg_view_percentage": 82,
                "ctr": 8,
                "comments": 5,
                "shares": 3,
                "subscribers_gained": 2,
                "title": "winner",
                "idea": "idea1",
                "angle": "a",
                "note": "7d",
            },
            {
                "video_id": 2,
                "checkpoint_hours": 72,
                "score": 60,
                "views": 100,
                "avg_view_percentage": 55,
                "ctr": 4,
                "comments": 1,
                "shares": 0,
                "subscribers_gained": 0,
                "title": "other",
                "idea": "idea2",
                "angle": "b",
                "note": "72h",
            },
        ]
        result = build_success_pattern_memory(history)
        self.assertEqual(result["sample_size"], 2)
        self.assertEqual(
            result["top_patterns"][0]["title"],
            "winner",
        )
        self.assertTrue(result["top_patterns"][0]["signals"])
        self.assertIn("コピーせず", result["instruction"])


if __name__ == "__main__":
    unittest.main()
