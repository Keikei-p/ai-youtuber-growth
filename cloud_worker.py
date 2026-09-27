from __future__ import annotations

import argparse
import json
import mimetypes
import os
import shutil
import threading
import traceback
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

from config import settings
from paths import DATA_DIR, OUTPUT_DIR, ensure_runtime_dirs
from production_pipeline import produce_media
from storage import init_db


JOB_ROOT = DATA_DIR / "cloud_execution_jobs"
ARTIFACT_ROOT = OUTPUT_DIR / "cloud_worker_artifacts"
_STORE_LOCK = threading.Lock()


def _job_file(job_id: str) -> Path:
    return JOB_ROOT / f"{job_id}.json"


def _write_job(job_id: str, payload: dict) -> None:
    JOB_ROOT.mkdir(parents=True, exist_ok=True)
    target = _job_file(job_id)
    temp = target.with_suffix(".json.tmp")
    with _STORE_LOCK:
        temp.write_text(
            json.dumps(
                payload,
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        temp.replace(target)


def _read_job(job_id: str) -> dict | None:
    target = _job_file(job_id)
    if not target.is_file():
        return None
    with _STORE_LOCK:
        try:
            value = json.loads(
                target.read_text(
                    encoding="utf-8",
                    errors="replace",
                )
            )
            return value if isinstance(value, dict) else None
        except Exception:
            return None


def _artifact_copy(
    job_id: str,
    source_raw: str,
    *,
    label: str,
    video_id: int,
) -> str | None:
    source = Path(str(source_raw or ""))
    if not source.is_file():
        return None
    suffix = source.suffix.lower() or (
        ".mp4" if label == "video" else ".jpg"
    )
    target_dir = ARTIFACT_ROOT / job_id
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{video_id}_{label}{suffix}"
    shutil.copy2(source, target)
    return f"/artifacts/{job_id}/{target.name}"


def _manifest(
    job_id: str,
    items: list[dict],
) -> dict:
    rows: list[dict] = []
    for item in items:
        try:
            video_id = int(item.get("id") or 0)
        except (TypeError, ValueError):
            continue
        row = {
            "id": video_id,
            "status": item.get("status"),
            "quality_passed": item.get("quality_passed"),
            "quality": item.get("quality"),
            "editorial_quality": item.get("editorial_quality"),
            "media_error": item.get("media_error"),
            "visual_warning": item.get("visual_warning"),
            "composition_plan": item.get("composition_plan"),
            "output_url": _artifact_copy(
                job_id,
                str(item.get("output_path") or ""),
                label="video",
                video_id=video_id,
            ),
            "thumbnail_url": _artifact_copy(
                job_id,
                str(item.get("thumbnail_path") or ""),
                label="thumbnail",
                video_id=video_id,
            ),
        }
        rows.append(row)
    return {
        "schema_version": 1,
        "job_id": job_id,
        "items": rows,
    }


def _run_job(
    job_id: str,
    request_payload: dict,
) -> None:
    try:
        _write_job(
            job_id,
            {
                "job_id": job_id,
                "status": "running",
            },
        )
        if request_payload.get("kind") != "produce_media":
            raise ValueError(
                "unsupported job kind"
            )
        items = request_payload.get("items") or []
        character = request_payload.get("character") or {}
        if not isinstance(items, list):
            raise ValueError("items must be a list")
        if not isinstance(character, dict):
            raise ValueError("character must be an object")

        ensure_runtime_dirs()
        init_db()
        produce_media(items, character)
        manifest = _manifest(job_id, items)
        _write_job(
            job_id,
            {
                "job_id": job_id,
                "status": "succeeded",
                "manifest": manifest,
            },
        )
    except Exception as exc:
        _write_job(
            job_id,
            {
                "job_id": job_id,
                "status": "failed",
                "error": str(exc),
                "traceback": traceback.format_exc()[-6000:],
            },
        )


class CloudWorkerHandler(BaseHTTPRequestHandler):
    server_version = "MiraiCloudWorker/1.0"

    def _authorized(self) -> bool:
        expected = str(
            getattr(settings, "cloud_execution_token", "") or ""
        ).strip()
        if not expected:
            return True
        auth = str(
            self.headers.get("Authorization") or ""
        ).strip()
        return auth == f"Bearer {expected}"

    def _json(
        self,
        payload: dict,
        status: int = 200,
    ) -> None:
        raw = json.dumps(
            payload,
            ensure_ascii=False,
        ).encode("utf-8")
        self.send_response(status)
        self.send_header(
            "Content-Type",
            "application/json; charset=utf-8",
        )
        self.send_header(
            "Content-Length",
            str(len(raw)),
        )
        self.send_header(
            "Cache-Control",
            "no-store",
        )
        self.end_headers()
        self.wfile.write(raw)

    def _body(self) -> dict:
        raw = self.rfile.read(
            int(self.headers.get("Content-Length") or 0)
        )
        value = json.loads(
            raw.decode("utf-8") or "{}"
        )
        if not isinstance(value, dict):
            raise ValueError("body must be JSON object")
        return value

    def do_POST(self) -> None:
        if not self._authorized():
            self._json(
                {"message": "unauthorized"},
                HTTPStatus.UNAUTHORIZED,
            )
            return
        path = urlparse(self.path).path
        if path != "/v1/jobs":
            self._json(
                {"message": "not found"},
                HTTPStatus.NOT_FOUND,
            )
            return
        try:
            body = self._body()
            if int(body.get("schema_version") or 0) != 1:
                raise ValueError(
                    "unsupported schema_version"
                )
            if body.get("kind") != "produce_media":
                raise ValueError(
                    "unsupported job kind"
                )
            job_id = uuid.uuid4().hex
            _write_job(
                job_id,
                {
                    "job_id": job_id,
                    "status": "pending",
                },
            )
            threading.Thread(
                target=_run_job,
                args=(job_id, body),
                name=f"cloud-job-{job_id[:8]}",
                daemon=True,
            ).start()
            self._json(
                {
                    "job_id": job_id,
                    "status": "pending",
                },
                HTTPStatus.ACCEPTED,
            )
        except Exception as exc:
            self._json(
                {"message": str(exc)},
                HTTPStatus.BAD_REQUEST,
            )

    def do_GET(self) -> None:
        if not self._authorized():
            self._json(
                {"message": "unauthorized"},
                HTTPStatus.UNAUTHORIZED,
            )
            return

        path = urlparse(self.path).path
        if path == "/health":
            self._json(
                {
                    "ok": True,
                    "provider": "mirai-cloud-worker",
                    "schema_version": 1,
                }
            )
            return

        if path.startswith("/v1/jobs/"):
            job_id = path[len("/v1/jobs/"):].strip("/")
            if not job_id or any(
                ch not in "0123456789abcdef"
                for ch in job_id.lower()
            ):
                self._json(
                    {"message": "invalid job id"},
                    HTTPStatus.BAD_REQUEST,
                )
                return
            row = _read_job(job_id)
            if not row:
                self._json(
                    {"message": "job not found"},
                    HTTPStatus.NOT_FOUND,
                )
                return
            # tracebackはサーバー内部ログ用。外部応答へは出さない。
            row = {
                key: value
                for key, value in row.items()
                if key != "traceback"
            }
            self._json(row)
            return

        if path.startswith("/artifacts/"):
            parts = [
                unquote(value)
                for value in path.split("/")
                if value
            ]
            if len(parts) != 3:
                self._json(
                    {"message": "invalid artifact path"},
                    HTTPStatus.BAD_REQUEST,
                )
                return
            _, job_id, filename = parts
            root = (ARTIFACT_ROOT / job_id).resolve()
            candidate = (root / filename).resolve()
            try:
                candidate.relative_to(root)
            except ValueError:
                self._json(
                    {"message": "invalid artifact path"},
                    HTTPStatus.BAD_REQUEST,
                )
                return
            if not candidate.is_file():
                self._json(
                    {"message": "artifact not found"},
                    HTTPStatus.NOT_FOUND,
                )
                return
            raw = candidate.read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header(
                "Content-Type",
                mimetypes.guess_type(candidate.name)[0]
                or "application/octet-stream",
            )
            self.send_header(
                "Content-Length",
                str(len(raw)),
            )
            self.send_header(
                "Cache-Control",
                "private, no-store",
            )
            self.end_headers()
            self.wfile.write(raw)
            return

        self._json(
            {"message": "not found"},
            HTTPStatus.NOT_FOUND,
        )

    def log_message(self, format: str, *args) -> None:
        return


def run(
    host: str = "127.0.0.1",
    port: int = 8766,
) -> None:
    token = str(
        getattr(settings, "cloud_execution_token", "") or ""
    ).strip()
    if host not in {"127.0.0.1", "localhost", "::1"} and not token:
        raise RuntimeError(
            "外部公開するCloud Workerには"
            "CLOUD_EXECUTION_TOKENが必須です。"
        )
    ensure_runtime_dirs()
    JOB_ROOT.mkdir(parents=True, exist_ok=True)
    ARTIFACT_ROOT.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer(
        (host, int(port)),
        CloudWorkerHandler,
    )
    print(
        f"[CLOUD-WORKER] http://{host}:{int(port)} / "
        f"token={'configured' if token else 'local-only'}"
    )
    server.serve_forever()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--host",
        default=os.getenv(
            "CLOUD_WORKER_HOST",
            "127.0.0.1",
        ),
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(
            os.getenv(
                "CLOUD_WORKER_PORT",
                "8766",
            )
        ),
    )
    args = parser.parse_args()
    run(args.host, args.port)
