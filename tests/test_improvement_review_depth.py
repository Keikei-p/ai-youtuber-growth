"""Review serialization regression for deeply nested stored recovery contexts.

The tests use a temporary SQLite database and never contact YouTube.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import self_improvement
import storage


class _UnavailableTextAI:
    def available(self) -> bool:
        return False


class ImprovementReviewDepthTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self._old_db = storage.DB_PATH
        storage.DB_PATH = Path(self._temp.name) / "improvement-depth.db"
        storage.init_db()

    def tearDown(self) -> None:
        storage.DB_PATH = self._old_db
        self._temp.cleanup()

    def test_cyclic_history_context_is_not_serialized_into_review(self) -> None:
        # Corrupt or recursively nested diagnostics must not prevent a review
        # from persisting its bounded report. Historical storage is untouched.
        deep_context: dict = {}
        deep_context["self"] = deep_context
        baseline = {
            "summary": "通常の改善レビュー",
            "recommendations": [],
            "actions": [],
            "requires_code_change": False,
            "source": "mirai-deterministic-engine",
            "resilience": {
                "total_patterns": 1,
                "total_failures": 3,
                "learned_successes": 0,
                "learned_patterns": 0,
                "unresolved_patterns": 1,
                "recurring_patterns": 1,
                "playbooks": [
                    {
                        "fingerprint": "deep-test",
                        "stage": "scheduler.test",
                        "category": "other",
                        "status": "learning",
                        "occurrences": 3,
                        "preferred_next_action": "ログを確認",
                        "latest_context": deep_context,
                    }
                ],
            },
        }
        with (
            patch.object(self_improvement, "MiraiImprovementEngine") as engine,
            patch.object(
                self_improvement, "OllamaClient",
                return_value=_UnavailableTextAI(),
            ),
        ):
            engine.return_value.review.return_value = baseline
            result = self_improvement.run_improvement_review()

        saved = json.loads(
            storage.get_channel_state("ai_improvement_report", "{}")
        )
        self.assertEqual(result["summary"], baseline["summary"])
        self.assertEqual(saved["resilience"]["total_failures"], 3)
        playbook = saved["resilience"]["playbooks"][0]
        self.assertEqual(playbook["preferred_next_action"], "ログを確認")
        self.assertEqual(playbook["fingerprint"], "deep-test")
        self.assertNotIn("latest_context", playbook)
        self.assertIs(baseline["resilience"]["playbooks"][0]["latest_context"], deep_context)
        self.assertIs(deep_context["self"], deep_context)

    def test_review_keeps_existing_actions_and_recommendations(self) -> None:
        baseline = {
            "summary": "改善候補あり",
            "recommendations": [{"area": "video", "action": "改善"}],
            "actions": [
                {
                    "action_type": "script_guidance",
                    "title": "字幕",
                    "value": "短く",
                }
            ],
            "requires_code_change": False,
            "resilience": {"total_patterns": 0, "playbooks": []},
        }
        with (
            patch.object(self_improvement, "MiraiImprovementEngine") as engine,
            patch.object(
                self_improvement, "OllamaClient",
                return_value=_UnavailableTextAI(),
            ),
        ):
            engine.return_value.review.return_value = baseline
            result = self_improvement.run_improvement_review()

        self.assertEqual(result["recommendations"], baseline["recommendations"])
        self.assertEqual(result["actions"], baseline["actions"])
        self.assertEqual(
            storage.get_channel_state("autonomous_script_guidance", ""),
            "短く",
        )


if __name__ == "__main__":
    unittest.main()
