from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import resilience_learning
import self_improvement
import storage


class ResilienceLearningTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db = storage.DB_PATH
        storage.DB_PATH = Path(self.tmp.name) / "resilience.db"
        storage.init_db()

    def tearDown(self) -> None:
        storage.DB_PATH = self.old_db
        self.tmp.cleanup()

    def test_failure_builds_playbook_and_success_teaches_recovery(self) -> None:
        playbook = resilience_learning.record_failure_pattern(
            fingerprint="abc123",
            stage="voice.synthesis",
            category="voice_service",
            context={"video_id": 10},
            attempted_actions=[
                "音声サービス状態確認",
                "次回サイクルで再試行",
            ],
            prevention_note="音声providerの起動待ち",
        )
        self.assertEqual(playbook["occurrences"], 1)
        self.assertEqual(playbook["status"], "learning")
        self.assertEqual(
            playbook["preferred_next_action"],
            "次回サイクルで再試行",
        )

        updated = resilience_learning.record_stage_success(
            "voice.synthesis",
            action="VOICEVOX起動後に再生成して成功",
            context={"video_id": 10},
        )
        self.assertEqual(len(updated), 1)

        learned = resilience_learning.playbook_for_fingerprint(
            "abc123"
        )
        self.assertEqual(learned["status"], "learned")
        self.assertEqual(learned["success_count"], 1)
        self.assertEqual(
            learned["preferred_next_action"],
            "VOICEVOX起動後に再生成して成功",
        )

    def test_recurrence_after_success_is_marked_regressed(self) -> None:
        resilience_learning.record_failure_pattern(
            fingerprint="repeat",
            stage="youtube.upload",
            category="youtube_network",
            attempted_actions=["10分後に再試行"],
        )
        resilience_learning.record_stage_success(
            "youtube.upload",
            action="ネット復旧後に同じvideoIdを確認して成功",
        )
        resilience_learning.record_failure_pattern(
            fingerprint="repeat",
            stage="youtube.upload",
            category="youtube_network",
            attempted_actions=["10分後に再試行"],
        )
        row = resilience_learning.playbook_for_fingerprint("repeat")
        self.assertEqual(row["status"], "regressed")
        self.assertEqual(row["occurrences"], 2)
        self.assertEqual(row["success_count"], 1)
        self.assertEqual(
            row["preferred_next_action"],
            "ネット復旧後に同じvideoIdを確認して成功",
        )

    def test_snapshot_counts_learned_and_unresolved_patterns(self) -> None:
        resilience_learning.record_failure_pattern(
            fingerprint="learned",
            stage="image.background",
            category="other",
            attempted_actions=["再生成"],
        )
        resilience_learning.record_stage_success(
            "image.background",
            action="再生成して成功",
        )
        resilience_learning.record_failure_pattern(
            fingerprint="open",
            stage="video.render",
            category="render",
            attempted_actions=["入力確認"],
        )
        snapshot = resilience_learning.resilience_snapshot()
        self.assertEqual(snapshot["total_patterns"], 2)
        self.assertEqual(snapshot["learned_patterns"], 1)
        self.assertEqual(snapshot["unresolved_patterns"], 1)
        self.assertEqual(snapshot["learned_successes"], 1)

    def test_record_failure_returns_preferred_playbook_action(self) -> None:
        with (
            patch.object(
                self_improvement,
                "repair_known_code_invariants",
                return_value={"status": "healthy"},
            ),
        ):
            first = self_improvement.record_failure(
                "voice.synthesis",
                "VOICEVOX connection refused",
                {"video_id": 3},
            )
        self.assertIn("playbook", first)
        self.assertTrue(first["preferred_next_action"])

        resilience_learning.record_stage_success(
            "voice.synthesis",
            action="音声サービス再起動で復旧",
            context={"video_id": 3},
        )
        with patch.object(
            self_improvement,
            "repair_known_code_invariants",
            return_value={"status": "healthy"},
        ):
            second = self_improvement.record_failure(
                "voice.synthesis",
                "VOICEVOX connection refused",
                {"video_id": 4},
            )
        self.assertEqual(
            second["preferred_next_action"],
            "音声サービス再起動で復旧",
        )


class AppShellStaticTests(unittest.TestCase):
    def test_dashboard_has_five_simple_app_tabs(self) -> None:
        source = Path("webapp.py").read_text(encoding="utf-8")
        for page in ("home", "posts", "create", "growth", "settings"):
            self.assertIn(
                f'data-app-tab="{page}"',
                source,
            )
        self.assertIn("function showAppPage(page)", source)
        self.assertIn("appPageMap", source)
        self.assertIn("homeOverview", source)

    def test_growth_page_exposes_recovery_playbooks(self) -> None:
        source = Path("webapp.py").read_text(encoding="utf-8")
        self.assertIn("復旧プレイブック", source)
        self.assertIn("resilienceSummary", source)
        self.assertIn("preferred_next_action", source)


if __name__ == "__main__":
    unittest.main()
