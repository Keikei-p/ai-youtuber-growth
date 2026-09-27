from __future__ import annotations

import unittest
from unittest.mock import patch

import metadata


class MetadataTitleCandidateTests(unittest.TestCase):
    def test_title_selection_prefers_relevant_reasonable_length(self) -> None:
        idea = {
            "idea": "AIが自分の失敗から成長する",
            "angle": "前回との比較",
            "hook": "昨日の動画、AI目線でもダメでした。",
        }
        selected, scored = metadata._select_title(
            [
                "絶対100%ヤバすぎる衝撃動画",
                "AIが失敗から成長した結果",
                "関係ない旅行の話",
            ],
            idea=idea,
            script="AIが昨日の失敗を分析して改善した結果を見せます。",
            fallback="AIの成長記録",
        )
        self.assertEqual(selected, "AIが失敗から成長した結果")
        self.assertGreaterEqual(len(scored), 3)
        self.assertGreater(
            scored[0]["score"],
            next(
                row["score"]
                for row in scored
                if "絶対100%" in row["title"]
            ),
        )

    def test_build_metadata_keeps_candidate_scores(self) -> None:
        idea = {
            "idea": "AIが24時間で改善したこと",
            "angle": "成長比較",
            "hook": "昨日より少しだけ賢くなりました。",
        }
        written = {
            "title": "AIが24時間で改善したこと",
            "script": "昨日の失敗を分析したAIが、24時間で改善した結果を見せます。",
            "description": "成長の記録です。",
        }
        with (
            patch.object(metadata.OllamaClient, "available", return_value=False),
            patch.object(metadata, "voice_attribution", return_value=""),
        ):
            result = metadata.build_metadata(
                written=written,
                idea=idea,
                script=written["script"],
            )

        self.assertTrue(result["title"])
        self.assertGreaterEqual(
            len(result["title_candidates"]),
            3,
        )
        self.assertTrue(
            all("score" in row for row in result["title_candidates"])
        )


if __name__ == "__main__":
    unittest.main()
