from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import storage
from mirai_engines.editorial_quality_engine import (
    MiraiEditorialQualityEngine,
    evaluate_editorial_quality,
)


class EditorialQualityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db = storage.DB_PATH
        storage.DB_PATH = Path(self.tmp.name) / "editorial.db"
        storage.init_db()

    def tearDown(self) -> None:
        storage.DB_PATH = self.old_db
        self.tmp.cleanup()

    def test_strong_original_video_passes_multidimensional_gate(self) -> None:
        item = {
            "id": 1,
            "title": "AIが失敗から24時間で改善した結果",
            "script": (
                "昨日の動画、AI目線でもダメでした。"
                "そこで再生データを見て、冒頭とテンポを変えました。"
                "24時間後、前より見やすい動画に改善。"
                "次は何を直すべきか、コメントで教えてください。"
            ),
            "idea": {
                "idea": "AIが失敗から24時間で改善する",
                "angle": "昨日との比較",
                "hook": "昨日の動画、AI目線でもダメでした。",
            },
            "audio_path": "audio.wav",
            "thumbnail_path": "thumb.jpg",
            "thumbnail": {"score": 82},
            "composition_plan": {
                "scenes": [
                    {"text": "昨日の動画、AI目線でもダメでした。"},
                    {"text": "再生データを見て改善しました。"},
                    {"text": "次は何を直す？"},
                ]
            },
        }
        report = evaluate_editorial_quality(
            item,
            technical_quality={"score": 90},
        )
        self.assertTrue(report["passed"])
        self.assertGreaterEqual(report["score"], 65)
        self.assertEqual(len(report["dimensions"]), 12)

    def test_near_duplicate_is_blocked(self) -> None:
        script = (
            "昨日の失敗を分析して冒頭を変えました。"
            "数字を見ながら少しずつ改善します。"
            "次の動画も見守ってください。"
        )
        old_id = storage.save_video(
            idea="old",
            angle="test",
            title="old",
            script=script,
            description="",
            tags=[],
        )
        item = {
            "id": old_id + 1,
            "title": "昨日の失敗を改善",
            "script": script,
            "idea": {
                "idea": "昨日の失敗を改善",
                "hook": "昨日の失敗を分析しました。",
            },
            "audio_path": "audio.wav",
            "thumbnail_path": "thumb.jpg",
            "thumbnail": {"score": 80},
        }
        report = evaluate_editorial_quality(
            item,
            technical_quality={"score": 90},
        )
        self.assertFalse(report["passed"])
        self.assertLess(report["dimensions"]["originality"], 35)

    def test_engine_persists_report(self) -> None:
        item = {
            "id": 77,
            "title": "AIの改善実験",
            "script": (
                "AIが自分の動画を分析します。"
                "悪かった所を一つ直して再検証します。"
                "結果を次の動画へ反映します。"
                "次に直す所をコメントで教えてください。"
            ),
            "idea": {
                "idea": "AIの改善実験",
                "hook": "自分の動画を自分で直します。",
            },
            "audio_path": "audio.wav",
            "thumbnail_path": "thumb.jpg",
            "thumbnail": {"score": 75},
        }
        engine = MiraiEditorialQualityEngine()
        engine.inspect(item, technical_quality={"score": 85})
        rows = engine.recent(1)
        self.assertEqual(rows[0]["video_id"], 77)


if __name__ == "__main__":
    unittest.main()
