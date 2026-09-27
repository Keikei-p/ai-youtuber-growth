from __future__ import annotations

import unittest
from unittest.mock import patch

import reviewer


class ScriptReviewQualityTests(unittest.TestCase):
    def test_long_opening_and_missing_next_hook_are_rejected(self) -> None:
        script = (
            "これは最初の一文がとても長くて視聴者が結論にたどり着くまで"
            "かなり長い時間待つ必要がある説明になっているので冒頭として弱いです。"
            "その後に説明を続けて終わります。"
        )
        with (
            patch.object(reviewer, "assess_publish_risk", return_value=[]),
            patch.object(reviewer, "assess_voice_license_risk", return_value=[]),
            patch.object(reviewer, "voice_attribution", return_value=""),
        ):
            ok, issues = reviewer.review_script(
                "普通のタイトル",
                script,
                [],
            )
        self.assertFalse(ok)
        self.assertIn("weak_hook_too_long", issues)
        self.assertIn(
            "missing_viewer_or_next_episode_hook",
            issues,
        )

    def test_compact_hook_and_next_episode_signal_pass_structure(self) -> None:
        script = (
            "昨日の動画、AI目線でもダメでした。"
            "そこでデータを見て冒頭とテンポを変えました。"
            "今日の結果は前より少し見やすい。"
            "次は何を直すべきか、コメントで教えてください。"
        )
        with (
            patch.object(reviewer, "assess_publish_risk", return_value=[]),
            patch.object(reviewer, "assess_voice_license_risk", return_value=[]),
            patch.object(reviewer, "voice_attribution", return_value=""),
        ):
            ok, issues = reviewer.review_script(
                "AIが昨日の失敗を改善した結果",
                script,
                [],
            )
        self.assertTrue(ok, msg=str(issues))


if __name__ == "__main__":
    unittest.main()
