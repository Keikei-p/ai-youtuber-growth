from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import storage
from autonomy_policy import resolve_approval
from legal_guard import assess_publish_risk, publish_gate
from voice.voicevox import VoicevoxClient


class LegalGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        storage.DB_PATH = Path(self.tmp.name) / "legal.db"
        storage.init_db()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_default_voicevox_speaker_has_required_credit(self) -> None:
        fake_settings = SimpleNamespace(
            voicevox_speaker=3,
            voicevox_url="http://127.0.0.1:50021",
        )
        with patch("voice.voicevox.settings", fake_settings):
            self.assertEqual(
                VoicevoxClient().attribution(),
                "VOICEVOX:ずんだもん",
            )

    def test_safe_mirai_story_passes(self) -> None:
        risks = assess_publish_risk(
            title="AIが昨日の動画を改善してみた",
            script="ミライが自分の動画を分析して、字幕と話す速度を変えました。",
        )
        self.assertEqual(risks, [])

    def test_high_risk_allegation_is_flagged(self) -> None:
        risks = assess_publish_risk(
            title="ある会社について",
            script="この株式会社の社長は詐欺師です。",
        )
        codes = {risk.code for risk in risks}
        self.assertIn("defamation_or_allegation", codes)
        self.assertIn("entity_reputation", codes)

    def test_missing_required_credit_is_hard_blocked(self) -> None:
        result = publish_gate(
            {
                "id": 42,
                "title": "安全な動画",
                "script": "ミライの成長記録です。",
                "description": "概要です。",
            },
            required_credit="VOICEVOX:ずんだもん",
        )
        self.assertFalse(result["allowed"])
        self.assertTrue(result["hard_blocked"])
        self.assertIsNone(result["approval_id"])
        codes = {
            item["code"]
            for item in result["risks"]
        }
        self.assertIn("missing_voice_credit", codes)

    def test_zundamon_political_topic_is_hard_blocked(self) -> None:
        result = publish_gate(
            {
                "id": 55,
                "title": "選挙の話",
                "script": "今日は選挙と候補者について話します。",
                "description": "VOICEVOX:ずんだもん",
            },
            required_credit="VOICEVOX:ずんだもん",
        )
        self.assertFalse(result["allowed"])
        self.assertTrue(result["hard_blocked"])
        self.assertIsNone(result["approval_id"])
        codes = {
            item["code"]
            for item in result["risks"]
        }
        self.assertIn("voice_license_politics_religion", codes)

    def test_unresolved_required_voice_credit_is_hard_blocked(self) -> None:
        result = publish_gate(
            {
                "id": 56,
                "title": "安全な動画",
                "script": "ミライの成長記録です。",
                "description": "概要です。",
            },
            required_credit="__UNRESOLVED_REQUIRED_VOICE_CREDIT__",
        )
        self.assertFalse(result["allowed"])
        self.assertTrue(result["hard_blocked"])
        self.assertIn(
            "unresolved_voice_credit",
            {item["code"] for item in result["risks"]},
        )

    def test_targeted_company_attack_cannot_be_approved_around_voice_license(self) -> None:
        item = {
            "id": 77,
            "title": "確認が必要",
            "script": "この株式会社の社長は詐欺師です。",
            "description": "VOICEVOX:ずんだもん",
        }
        result = publish_gate(
            item,
            required_credit="VOICEVOX:ずんだもん",
        )
        self.assertFalse(result["allowed"])
        self.assertTrue(result["hard_blocked"])
        self.assertIsNone(result["approval_id"])
        self.assertIn(
            "voice_license_targeted_support_or_criticism",
            {risk["code"] for risk in result["risks"]},
        )

    def test_non_license_review_risk_can_require_explicit_approval(self) -> None:
        item = {
            "id": 78,
            "title": "法律の話",
            "script": "この条件なら訴えれば勝てると考えます。",
            "description": "VOICEVOX:ずんだもん",
        }
        first = publish_gate(
            item,
            required_credit="VOICEVOX:ずんだもん",
        )
        self.assertFalse(first["allowed"])
        self.assertFalse(first.get("hard_blocked", False))
        self.assertTrue(first["approval_id"])
        resolve_approval(int(first["approval_id"]), True)

        second = publish_gate(
            item,
            required_credit="VOICEVOX:ずんだもん",
        )
        self.assertTrue(second["allowed"])
        self.assertTrue(second.get("approved_override"))


if __name__ == "__main__":
    unittest.main()
