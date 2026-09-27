from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import cloud_worker
import execution_provider
import runtime_control
import storage


class LocalCloudExecutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.old_db = storage.DB_PATH
        storage.DB_PATH = self.root / "execution.db"
        storage.init_db()

    def tearDown(self) -> None:
        storage.DB_PATH = self.old_db
        self.tmp.cleanup()

    def _video(self) -> tuple[int, list[dict]]:
        video_id = storage.save_video(
            idea="cloud test",
            angle="provider",
            title="Cloud execution test",
            script="server execution test",
            description="test",
            tags=["test"],
            status="planned",
        )
        return video_id, [{
            "id": video_id,
            "idea": {
                "idea": "cloud test",
                "angle": "provider",
            },
            "title": "Cloud execution test",
            "script": "server execution test",
            "description": "test",
            "tags": ["test"],
            "guest": None,
            "status": "planned",
        }]

    def test_runtime_execution_mode_switches_between_local_and_cloud(self) -> None:
        self.assertEqual(runtime_control.execution_mode(), "local")
        runtime_control.set_execution_mode("cloud")
        self.assertEqual(runtime_control.execution_mode(), "cloud")
        runtime_control.set_execution_mode("local")
        self.assertEqual(runtime_control.execution_mode(), "local")
        with self.assertRaises(ValueError):
            runtime_control.set_execution_mode("invalid")

    def test_local_mode_uses_existing_production_pipeline(self) -> None:
        _, items = self._video()
        runtime_control.set_execution_mode("local")

        with patch(
            "production_pipeline.produce_media"
        ) as produce:
            execution_provider.execute_media(
                items,
                {"name": "Mirai"},
            )

        produce.assert_called_once_with(
            items,
            {"name": "Mirai"},
        )
        event = json.loads(
            storage.get_channel_state(
                "execution_last_event",
                "{}",
            )
        )
        self.assertEqual(event["status"], "succeeded")
        self.assertEqual(event["location"], "local")
        self.assertFalse(event["fallback"])

    def test_unconfigured_cloud_falls_back_to_local(self) -> None:
        _, items = self._video()
        runtime_control.set_execution_mode("cloud")
        runtime_control.set_cloud_execution_fallback_local(True)
        client = MagicMock()
        client.configured.return_value = False

        with (
            patch.object(
                execution_provider,
                "CloudExecutionClient",
                return_value=client,
            ),
            patch(
                "production_pipeline.produce_media"
            ) as produce,
        ):
            execution_provider.execute_media(
                items,
                {"name": "Mirai"},
            )

        produce.assert_called_once()
        event = json.loads(
            storage.get_channel_state(
                "execution_last_event",
                "{}",
            )
        )
        self.assertEqual(event["location"], "local")
        self.assertEqual(event["status"], "succeeded")
        self.assertTrue(event["fallback"])
        self.assertIn(
            "Cloud Execution Worker",
            event["detail"],
        )

    def test_unconfigured_cloud_can_stop_instead_of_fallback(self) -> None:
        _, items = self._video()
        runtime_control.set_execution_mode("cloud")
        runtime_control.set_cloud_execution_fallback_local(False)
        client = MagicMock()
        client.configured.return_value = False

        with (
            patch.object(
                execution_provider,
                "CloudExecutionClient",
                return_value=client,
            ),
            self.assertRaises(
                execution_provider.CloudExecutionError
            ),
        ):
            execution_provider.execute_media(
                items,
                {"name": "Mirai"},
            )

        event = json.loads(
            storage.get_channel_state(
                "execution_last_event",
                "{}",
            )
        )
        self.assertEqual(event["location"], "cloud")
        self.assertEqual(event["status"], "failed")

    def test_cloud_success_downloads_manifest_and_updates_local_db(self) -> None:
        video_id, items = self._video()
        runtime_control.set_execution_mode("cloud")
        runtime_control.set_cloud_execution_fallback_local(True)

        client = MagicMock()
        client.configured.return_value = True
        client.timeout = 30
        client.poll_seconds = 1
        client.submit.return_value = {
            "job_id": "abc123",
            "status": "succeeded",
            "manifest": {
                "items": [{
                    "id": video_id,
                    "status": "rendered",
                    "quality_passed": True,
                    "quality": {"score": 91},
                    "output_url": "/artifacts/abc123/video.mp4",
                    "thumbnail_url": "/artifacts/abc123/thumb.jpg",
                }],
            },
        }

        def fake_download(url: str, destination: Path) -> Path:
            destination.parent.mkdir(
                parents=True,
                exist_ok=True,
            )
            destination.write_bytes(
                b"video-bytes"
                if destination.suffix == ".mp4"
                else b"image-bytes"
            )
            return destination

        client.download.side_effect = fake_download
        video_dir = self.root / "videos"
        generated = self.root / "generated"

        with (
            patch.object(
                execution_provider,
                "CloudExecutionClient",
                return_value=client,
            ),
            patch.object(
                execution_provider,
                "VIDEO_DIR",
                video_dir,
            ),
            patch.object(
                execution_provider,
                "GENERATED_ROOT",
                generated,
            ),
        ):
            execution_provider.execute_media(
                items,
                {"name": "Mirai"},
            )

        self.assertTrue(items[0]["quality_passed"])
        self.assertEqual(
            items[0]["quality"]["score"],
            91,
        )
        output = Path(items[0]["output_path"])
        thumbnail = Path(items[0]["thumbnail_path"])
        self.assertTrue(output.is_file())
        self.assertTrue(thumbnail.is_file())
        row = storage.video_by_id(video_id)
        self.assertEqual(row["status"], "rendered")
        self.assertEqual(
            Path(row["output_path"]),
            output,
        )
        event = json.loads(
            storage.get_channel_state(
                "execution_last_event",
                "{}",
            )
        )
        self.assertEqual(event["location"], "cloud")
        self.assertEqual(event["status"], "succeeded")
        self.assertFalse(event["fallback"])

    def test_real_http_cloud_worker_roundtrip_returns_artifacts(self) -> None:
        video_id, items = self._video()
        runtime_control.set_execution_mode("cloud")
        runtime_control.set_cloud_execution_fallback_local(True)

        worker_jobs = self.root / "worker-jobs"
        worker_artifacts = self.root / "worker-artifacts"
        client_videos = self.root / "client-videos"
        client_generated = self.root / "client-generated"

        def fake_produce(remote_items, character):
            self.assertEqual(character["name"], "Mirai")
            for item in remote_items:
                output = self.root / f"remote-{item['id']}.mp4"
                output.write_bytes(b"remote-video")
                thumb = self.root / f"remote-{item['id']}.jpg"
                thumb.write_bytes(b"remote-thumbnail")
                item["output_path"] = str(output)
                item["thumbnail_path"] = str(thumb)
                item["status"] = "rendered"
                item["quality_passed"] = True
                item["quality"] = {"score": 93}

        worker_settings = SimpleNamespace(
            cloud_execution_token="test-token",
        )

        with (
            patch.object(
                cloud_worker,
                "JOB_ROOT",
                worker_jobs,
            ),
            patch.object(
                cloud_worker,
                "ARTIFACT_ROOT",
                worker_artifacts,
            ),
            patch.object(
                cloud_worker,
                "produce_media",
                side_effect=fake_produce,
            ),
            patch.object(
                cloud_worker,
                "settings",
                worker_settings,
            ),
        ):
            server = cloud_worker.ThreadingHTTPServer(
                ("127.0.0.1", 0),
                cloud_worker.CloudWorkerHandler,
            )
            thread = threading.Thread(
                target=server.serve_forever,
                daemon=True,
            )
            thread.start()
            port = int(server.server_address[1])
            client_settings = SimpleNamespace(
                cloud_execution_url=f"http://127.0.0.1:{port}",
                cloud_execution_token="test-token",
                cloud_execution_timeout_seconds=15,
                cloud_execution_poll_seconds=1,
            )
            try:
                with (
                    patch.object(
                        execution_provider,
                        "settings",
                        client_settings,
                    ),
                    patch.object(
                        execution_provider,
                        "VIDEO_DIR",
                        client_videos,
                    ),
                    patch.object(
                        execution_provider,
                        "GENERATED_ROOT",
                        client_generated,
                    ),
                ):
                    execution_provider.execute_media(
                        items,
                        {"name": "Mirai"},
                    )
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

        self.assertTrue(items[0]["quality_passed"])
        self.assertEqual(items[0]["quality"]["score"], 93)
        self.assertTrue(Path(items[0]["output_path"]).is_file())
        self.assertTrue(Path(items[0]["thumbnail_path"]).is_file())
        row = storage.video_by_id(video_id)
        self.assertEqual(row["status"], "rendered")
        event = json.loads(
            storage.get_channel_state(
                "execution_last_event",
                "{}",
            )
        )
        self.assertEqual(event["location"], "cloud")
        self.assertEqual(event["status"], "succeeded")
        self.assertFalse(event["fallback"])

    def test_execution_status_never_exposes_cloud_token(self) -> None:
        fake_settings = SimpleNamespace(
            cloud_execution_url="https://worker.example.com",
            cloud_execution_token="super-secret-token",
        )
        with patch.object(
            execution_provider,
            "settings",
            fake_settings,
        ):
            status = execution_provider.execution_status()

        serialized = json.dumps(status)
        self.assertNotIn("super-secret-token", serialized)
        self.assertTrue(status["token_configured"])
        self.assertEqual(
            status["cloud_host"],
            "worker.example.com",
        )


class CloudWorkerStaticTests(unittest.TestCase):
    def test_cloud_worker_has_job_health_and_artifact_endpoints(self) -> None:
        source = Path("cloud_worker.py").read_text(
            encoding="utf-8",
        )
        self.assertIn('"/health"', source)
        self.assertIn('"/v1/jobs"', source)
        self.assertIn('"/artifacts/"', source)
        self.assertIn("CLOUD_EXECUTION_TOKEN", source)
        self.assertIn(
            "外部公開するCloud Worker",
            source,
        )

    def test_main_routes_rendering_through_execution_provider(self) -> None:
        source = Path("main.py").read_text(
            encoding="utf-8",
        )
        self.assertIn(
            "from execution_provider import execute_media",
            source,
        )
        self.assertIn(
            "execute_media(results, character)",
            source,
        )


class ExecutionDashboardStaticTests(unittest.TestCase):
    def test_dashboard_exposes_execution_mode_and_fallback(self) -> None:
        source = Path("webapp.py").read_text(
            encoding="utf-8",
        )
        self.assertIn('id="executionMode"', source)
        self.assertIn('id="cloudFallback"', source)
        self.assertIn('id="executionStatus"', source)
        self.assertIn('"execution": execution,', source)
        self.assertIn(
            "set_execution_mode",
            source,
        )


if __name__ == "__main__":
    unittest.main()
