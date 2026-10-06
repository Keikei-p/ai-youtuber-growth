from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import autonomous_recovery
import scheduler
import self_improvement
import storage
import upload_recovery


JST = timezone(timedelta(hours=9))


class ClosedLoopRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.old_db = storage.DB_PATH
        storage.DB_PATH = self.root / "closed-loop.db"
        storage.init_db()
        storage.set_channel_state(
            "production_autonomy_armed",
            "true",
        )
        storage.set_channel_state(
            "automation_enabled",
            "true",
        )
        storage.set_channel_state(
            "auto_upload_enabled",
            "true",
        )
        storage.set_channel_state(
            "runtime_cancel_requested",
            "false",
        )

    def tearDown(self) -> None:
        storage.DB_PATH = self.old_db
        self.tmp.cleanup()

    def _failed_video(
        self,
        error: str,
    ) -> tuple[int, int]:
        video_id = storage.save_video(
            idea="recovery",
            angle="closed loop",
            title="Recovery",
            script="safe",
            description="safe",
            tags=["mirai"],
            status="rendered",
        )
        output = self.root / f"{video_id}.mp4"
        output.write_bytes(b"fake")
        storage.update_video_output(
            video_id,
            str(output),
            status="rendered",
        )
        storage.queue_video(
            video_id,
            "2026-10-06T12:00+09:00",
        )
        row = storage.queue_item_for_video(video_id)
        storage.set_queue_failed(
            int(row["queue_id"]),
            error,
        )
        return video_id, int(row["queue_id"])

    def test_revive_failed_queue_resets_attempts_so_due_queue_can_run(self) -> None:
        video_id, queue_id = self._failed_video(
            "[CIRCUIT-BREAKER:network] timeout"
        )
        storage.revive_failed_queue(
            queue_id,
            "2026-10-06T18:30+09:00",
            reason="repaired",
        )
        row = storage.queue_item_for_video(video_id)
        self.assertEqual(row["status"], "queued")
        self.assertEqual(int(row["attempts"]), 0)

        due = storage.due_queue(
            "2026-10-06T18:31+09:00",
            "2026-10-05T18:31+09:00",
        )
        self.assertEqual(
            [int(item["video_id"]) for item in due],
            [video_id],
        )

    def test_autonomous_recovery_revives_network_circuit_breaker_same_tick(self) -> None:
        video_id, _ = self._failed_video(
            "[CIRCUIT-BREAKER:network] network timeout"
        )
        now = datetime(2026, 10, 6, 18, 30, tzinfo=JST)

        with (
            patch.object(
                autonomous_recovery,
                "bootstrap_runtime",
                return_value={
                    "ready": True,
                    "blockers": [],
                },
            ),
            patch.object(
                autonomous_recovery,
                "get_credentials",
                return_value=object(),
            ),
            patch.object(
                autonomous_recovery,
                "self_heal_delivery_controls",
                return_value={"repairs": []},
            ),
        ):
            result = autonomous_recovery.run_autonomous_recovery(
                now=now
            )

        self.assertEqual(result["status"], "recovered")
        self.assertEqual(result["revived"][0]["video_id"], video_id)
        row = storage.queue_item_for_video(video_id)
        self.assertEqual(row["status"], "queued")
        self.assertEqual(int(row["attempts"]), 0)
        self.assertEqual(
            row["scheduled_for"],
            "2026-10-06T18:30+09:00",
        )

    def test_missing_file_recovery_requests_regeneration_before_revival(self) -> None:
        video_id, _ = self._failed_video(
            "[CIRCUIT-BREAKER:missing_file] missing"
        )
        now = datetime(2026, 10, 6, 18, 30, tzinfo=JST)
        with (
            patch.object(
                autonomous_recovery,
                "bootstrap_runtime",
                return_value={
                    "ready": True,
                    "blockers": [],
                },
            ),
            patch.object(
                autonomous_recovery,
                "get_credentials",
                return_value=object(),
            ),
            patch.object(
                autonomous_recovery,
                "self_heal_delivery_controls",
                return_value={"repairs": []},
            ),
        ):
            autonomous_recovery.run_autonomous_recovery(
                now=now
            )

        self.assertEqual(
            storage.get_channel_state(
                f"video_regeneration_requested_{video_id}",
                "",
            ),
            "true",
        )

    def test_oauth_failed_queue_stays_failed_until_auth_is_ready(self) -> None:
        video_id, _ = self._failed_video(
            "[CIRCUIT-BREAKER:oauth] invalid_grant"
        )
        now = datetime(2026, 10, 6, 18, 30, tzinfo=JST)

        with (
            patch.object(
                autonomous_recovery,
                "bootstrap_runtime",
                return_value={
                    "ready": True,
                    "blockers": [],
                },
            ),
            patch.object(
                autonomous_recovery,
                "get_credentials",
                side_effect=RuntimeError("token expired"),
            ),
            patch.object(
                autonomous_recovery,
                "self_heal_delivery_controls",
                return_value={"repairs": []},
            ),
        ):
            result = autonomous_recovery.run_autonomous_recovery(
                now=now
            )

        self.assertEqual(
            storage.queue_item_for_video(video_id)["status"],
            "failed",
        )
        self.assertTrue(
            any(
                item.get("code") == "oauth"
                for item in result["blocked"]
            )
        )

    def test_record_failure_requests_recovery_instead_of_only_logging(self) -> None:
        with patch.object(
            self_improvement,
            "repair_known_code_invariants",
            return_value={"status": "healthy"},
        ):
            result = self_improvement.record_failure(
                "youtube.upload",
                "network timeout",
                {"video_id": 1},
            )

        self.assertTrue(result["preferred_next_action"])
        self.assertEqual(
            storage.get_channel_state(
                "autonomous_recovery_requested",
                "",
            ),
            "true",
        )

    def test_retry_tuning_changes_real_upload_backoff(self) -> None:
        storage.set_channel_state(
            "autonomous_retry_profile",
            "adaptive",
        )
        self.assertEqual(
            upload_recovery._adaptive_retry_delay(
                "network",
                10,
                0,
            ),
            10,
        )
        self.assertEqual(
            upload_recovery._adaptive_retry_delay(
                "network",
                10,
                2,
            ),
            20,
        )

    def test_safe_test_queue_is_consumed_instead_of_staying_inert(self) -> None:
        storage.set_channel_state(
            "autonomous_test_queue",
            json.dumps(
                [
                    {
                        "action_type": "minor_code_change",
                        "status": "test_required",
                        "value": {
                            "repair_id": "known_invariants"
                        },
                    }
                ],
                ensure_ascii=False,
            ),
        )
        with patch.object(
            autonomous_recovery,
            "repair_known_code_invariants",
            return_value={"status": "healthy"},
        ):
            results = autonomous_recovery._run_safe_test_queue()

        self.assertEqual(results[0]["status"], "applied")
        queue = json.loads(
            storage.get_channel_state(
                "autonomous_test_queue",
                "[]",
            )
        )
        self.assertEqual(queue[0]["status"], "applied")

    def test_scheduler_runs_recovery_before_first_episode_early_return(self) -> None:
        calls: list[str] = []
        with (
            patch.object(
                scheduler,
                "self_heal_delivery_controls",
                return_value={"repairs": []},
            ),
            patch.object(
                scheduler,
                "run_autonomous_recovery",
                side_effect=lambda **kwargs: (
                    calls.append("recovery")
                    or {"status": "healthy"}
                ),
            ),
            patch.object(
                scheduler,
                "ensure_first_episode_delivery",
                return_value={"status": "not_created"},
            ),
            patch.object(
                scheduler,
                "prepare_upcoming",
                side_effect=lambda: calls.append("prepare"),
            ),
            patch.object(
                scheduler,
                "run_due",
                side_effect=lambda: calls.append("due"),
            ),
        ):
            scheduler._tick_unlocked()

        self.assertEqual(
            calls[:3],
            ["recovery", "prepare", "due"],
        )


if __name__ == "__main__":
    unittest.main()
