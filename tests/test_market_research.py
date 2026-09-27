from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from youtube import market_research


class MarketResearchTests(unittest.TestCase):
    def test_duration_parser(self) -> None:
        self.assertEqual(
            market_research._duration_seconds("PT1M30S"),
            90,
        )
        self.assertEqual(
            market_research._duration_seconds("PT45S"),
            45,
        )

    def test_guidance_uses_multiple_videos_without_copy_instruction(self) -> None:
        rows = [
            {
                "title": "AIが24時間で変わった理由？",
                "views": 1000,
                "duration_seconds": 35,
                "likes": 50,
                "comments": 12,
            },
            {
                "title": "3日でAIはどこまで成長する？",
                "views": 900,
                "duration_seconds": 42,
                "likes": 42,
                "comments": 9,
            },
            {
                "title": "AIの失敗を全部見せます",
                "views": 800,
                "duration_seconds": 39,
                "likes": 35,
                "comments": 7,
            },
        ]
        guidance = market_research._fallback_guidance(rows)
        self.assertIn("市場比較3本", guidance)
        self.assertIn("コピーせず", guidance)

    def test_cached_market_research_avoids_api_call(self) -> None:
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        cached = {
            "captured_at": now,
            "sample_size": 8,
            "guidance": "cached",
            "videos": [],
        }
        with (
            patch.object(
                market_research,
                "get_channel_state",
                side_effect=lambda key, default="": {
                    market_research.LAST_AT_KEY: now,
                    market_research.STATE_KEY: json.dumps(cached),
                }.get(key, default),
            ),
            patch.object(
                market_research,
                "refresh_market_research",
            ) as refresh,
        ):
            result = market_research.maybe_refresh_market_research(
                min_hours=24,
            )

        refresh.assert_not_called()
        self.assertEqual(result["status"], "cached")
        self.assertEqual(result["sample_size"], 8)


if __name__ == "__main__":
    unittest.main()
