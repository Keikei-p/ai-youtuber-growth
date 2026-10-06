from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import product_core
import scheduler
import storage
import webapp


UTC = timezone.utc


class ProductRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db = storage.DB_PATH
        storage.DB_PATH = Path(self.tmp.name) / "product-runtime.db"
        storage.init_db()
        webapp._dashboard_cache.clear()

    def tearDown(self) -> None:
        storage.DB_PATH = self.old_db
        self.tmp.cleanup()
        webapp._dashboard_cache.clear()

    def _video(self, title: str = "ready") -> int:
        video_id = storage.save_video(
            idea=title,
            angle="test",
            title=title,
            script="safe",
            description="safe",
            tags=["mirai"],
            status="rendered",
        )
        return video_id

    def test_dashboard_heavy_block_is_cached(self) -> None:
        calls = []

        def builder():
            calls.append(1)
            return {"value": len(calls)}

        first = webapp._cached_dashboard_block(
            "test-heavy",
            60,
            builder,
        )
        second = webapp._cached_dashboard_block(
            "test-heavy",
            60,
            builder,
        )

        self.assertEqual(first, {"value": 1})
        self.assertEqual(second, {"value": 1})
        self.assertEqual(len(calls), 1)

    def test_queue_full_does_not_boot_ai_services(self) -> None:
        now = datetime(2026, 10, 6, 10, 0, tzinfo=UTC)
        video_id = self._video("future")
        storage.queue_video(
            video_id,
            "2026-10-06T12:00+00:00",
        )

        with (
            patch.object(scheduler, "_now", return_value=now),
            patch.object(
                scheduler,
                "reschedule_missed",
                return_value=0,
            ),
            patch.object(
                scheduler,
                "ensure_first_episode_delivery",
                return_value={"status": "uploaded"},
            ),
            patch.object(
                scheduler,
                "posts_per_day",
                return_value=1,
            ),
            patch.object(
                scheduler,
                "_catchup_slot",
                return_value=None,
            ),
            patch.object(
                scheduler,
                "bootstrap_runtime",
            ) as bootstrap,
            patch.object(
                scheduler,
                "run_generation",
            ) as generation,
        ):
            scheduler.prepare_upcoming()

        bootstrap.assert_not_called()
        generation.assert_not_called()

    def test_missing_generation_boots_runtime_only_at_generation_boundary(self) -> None:
        now = datetime(2026, 10, 6, 10, 0, tzinfo=UTC)
        slot = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)
        decision = SimpleNamespace(
            allowed=True,
            mode="autopilot",
            reason="safe",
            snapshot={},
        )

        with (
            patch.object(scheduler, "_now", return_value=now),
            patch.object(
                scheduler,
                "reschedule_missed",
                return_value=0,
            ),
            patch.object(
                scheduler,
                "ensure_first_episode_delivery",
                return_value={"status": "uploaded"},
            ),
            patch.object(
                scheduler,
                "posts_per_day",
                return_value=1,
            ),
            patch.object(
                scheduler,
                "_catchup_slot",
                return_value=None,
            ),
            patch.object(
                scheduler,
                "_next_free_slots",
                return_value=[slot],
            ),
            patch.object(
                scheduler,
                "background_production_decision",
                return_value=decision,
            ),
            patch.object(
                scheduler,
                "bootstrap_runtime",
                return_value={
                    "ready": False,
                    "blockers": ["voice"],
                },
            ) as bootstrap,
            patch.object(
                scheduler,
                "run_generation",
            ) as generation,
        ):
            scheduler.prepare_upcoming()

        bootstrap.assert_called_once()
        generation.assert_not_called()

    def test_safe_support_snapshot_never_reads_secret_files(self) -> None:
        snapshot = product_core.safe_support_snapshot(
            Path(".")
        )
        raw = str(snapshot).lower()

        self.assertFalse(snapshot["secrets_included"])
        self.assertNotIn("client_secret.json", raw)
        self.assertNotIn("api_key", raw)
        self.assertIn(
            "Mirai Production OS",
            snapshot["product"]["name"],
        )

    def test_productization_foundation_files_exist(self) -> None:
        root = Path(".")
        for relative in (
            "docs/PRIVACY_POLICY_DRAFT.md",
            "docs/TERMS_DRAFT.md",
            "docs/PRODUCT_RELEASE_CHECKLIST.md",
            "packaging/build_windows.ps1",
            "requirements-packaging.txt",
        ):
            self.assertTrue(
                (root / relative).is_file(),
                relative,
            )

        status = product_core.product_status(root)
        self.assertEqual(
            status["name"],
            "Mirai Production OS",
        )
        self.assertGreaterEqual(
            status["readiness"]["score"],
            70,
        )


if __name__ == "__main__":
    unittest.main()
