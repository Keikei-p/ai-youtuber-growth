from __future__ import annotations

import argparse
import json
import mimetypes
import os
import shutil
import threading
import traceback
from datetime import datetime
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

from config import settings
from paths import DATA_DIR, OUTPUT_DIR, ensure_runtime_dirs
from production_pipeline import produce_media
from storage import get_channel_state, init_db, set_channel_state


JOB_ROOT = DATA_DIR / "cloud_execution_jobs"
ARTIFACT_ROOT = OUTPUT_DIR / "cloud_worker_artifacts"
_STORE_LOCK = threading.Lock()
_AUTONOMY_STOP_EVENT = threading.Event()
_AUTONOMY_WAKE_EVENT = threading.Event()
_AUTONOMY_LOCK = threading.Lock()
_AUTONOMY_THREAD: threading.Thread | None = None


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


def _recover_stale_jobs() -> int:
    JOB_ROOT.mkdir(parents=True, exist_ok=True)
    recovered = 0
    for path in JOB_ROOT.glob("*.json"):
        try:
            payload = json.loads(
                path.read_text(
                    encoding="utf-8",
                    errors="replace",
                )
            )
        except Exception:
            continue
        if not isinstance(payload, dict):
            continue
        if str(payload.get("status") or "") not in {
            "pending",
            "running",
        }:
            continue
        job_id = str(payload.get("job_id") or path.stem)
        _write_job(
            job_id,
            {
                "job_id": job_id,
                "status": "failed",
                "error": (
                    "Cloud Worker再起動を検出したため、"
                    "未完了ジョブを安全に失敗扱いへ変更しました。"
                ),
            },
        )
        recovered += 1
    return recovered




def _autonomy_last_event() -> dict:
    raw = get_channel_state(
        "cloud_autonomy_last_event",
        "",
    ).strip()
    if not raw:
        return {}
    try:
        value = json.loads(raw)
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def _save_autonomy_event(
    status: str,
    *,
    detail: str = "",
    extra: dict | None = None,
) -> dict:
    payload = {
        "status": str(status),
        "checked_at": datetime.now().astimezone().isoformat(
            timespec="seconds"
        ),
        "detail": str(detail or "")[:2000],
    }
    if extra:
        payload.update(extra)
    set_channel_state(
        "cloud_autonomy_last_event",
        json.dumps(payload, ensure_ascii=False),
    )
    return payload


def autonomy_status() -> dict:
    init_db()
    from runtime_control import (
        automation_enabled,
        auto_upload_enabled,
        runtime_cancel_requested,
        web_interval_seconds,
    )

    thread = _AUTONOMY_THREAD
    return {
        "runner_enabled": bool(
            getattr(settings, "cloud_autonomy_runner", False)
        ),
        "thread_alive": bool(thread and thread.is_alive()),
        "automation_enabled": automation_enabled(),
        "auto_upload_enabled": auto_upload_enabled(),
        "runtime_cancel_requested": runtime_cancel_requested(),
        "production_armed": (
            get_channel_state(
                "production_autonomy_armed",
                "false",
            ).strip().lower()
            == "true"
        ),
        "interval_seconds": web_interval_seconds(),
        "last_event": _autonomy_last_event(),
    }


def _run_autonomy_cycle() -> dict:
    from delivery_supervisor import self_heal_delivery_controls
    from runtime_control import (
        automation_enabled,
        runtime_cancel_requested,
    )

    init_db()
    delivery = self_heal_delivery_controls()
    repairs = list(delivery.get("repairs") or [])

    if runtime_cancel_requested():
        return _save_autonomy_event(
            "blocked",
            detail="安全停止が有効なためCloud自動サイクルを実行しません。",
            extra={"repairs": repairs},
        )

    if not automation_enabled():
        return _save_autonomy_event(
            "idle",
            detail="自動運転がOFFのためCloudランナーは待機中です。",
            extra={"repairs": repairs},
        )

    from scheduler import tick

    tick()
    return _save_autonomy_event(
        "succeeded",
        detail="Cloud側で自動サイクルを完了しました。",
        extra={"repairs": repairs},
    )


def _autonomy_loop() -> None:
    _save_autonomy_event(
        "started",
        detail="Cloud常駐ランナーを開始しました。",
    )
    while not _AUTONOMY_STOP_EVENT.is_set():
        _AUTONOMY_WAKE_EVENT.clear()
        try:
            _run_autonomy_cycle()
        except Exception as exc:
            _save_autonomy_event(
                "failed",
                detail=str(exc),
            )

        if _AUTONOMY_STOP_EVENT.is_set():
            break

        try:
            from runtime_control import web_interval_seconds

            wait_for = web_interval_seconds()
        except Exception:
            wait_for = 600
        _AUTONOMY_WAKE_EVENT.wait(max(60, int(wait_for)))

    _save_autonomy_event(
        "stopped",
        detail="Cloud常駐ランナーを停止しました。",
    )


def _start_autonomy_runner() -> threading.Thread | None:
    global _AUTONOMY_THREAD

    if not bool(
        getattr(settings, "cloud_autonomy_runner", False)
    ):
        return None

    with _AUTONOMY_LOCK:
        if (
            _AUTONOMY_THREAD is not None
            and _AUTONOMY_THREAD.is_alive()
        ):
            return _AUTONOMY_THREAD

        _AUTONOMY_STOP_EVENT.clear()
        _AUTONOMY_WAKE_EVENT.clear()
        _AUTONOMY_THREAD = threading.Thread(
            target=_autonomy_loop,
            name="mirai-cloud-autonomy",
            daemon=True,
        )
        _AUTONOMY_THREAD.start()
        return _AUTONOMY_THREAD


def _stop_autonomy_runner() -> None:
    global _AUTONOMY_THREAD

    _AUTONOMY_STOP_EVENT.set()
    _AUTONOMY_WAKE_EVENT.set()
    thread = _AUTONOMY_THREAD
    if thread is not None and thread.is_alive():
        thread.join(timeout=10)
    _AUTONOMY_THREAD = None

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
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > 5 * 1024 * 1024:
            raise ValueError("request body size is invalid")
        raw = self.rfile.read(length)
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
        if path == "/v1/runtime/control":
            try:
                body = self._body()
                action = str(
                    body.get("action") or ""
                ).strip().lower()
                if action == "arm":
                    from delivery_supervisor import (
                        arm_production_autonomy,
                    )

                    delivery = arm_production_autonomy()
                    _AUTONOMY_WAKE_EVENT.set()
                    self._json(
                        {
                            "ok": True,
                            "action": "arm",
                            "runtime": autonomy_status(),
                            "delivery": delivery,
                        }
                    )
                    return
                if action == "disarm":
                    from delivery_supervisor import (
                        disarm_production_autonomy,
                    )
                    from runtime_control import (
                        set_auto_upload_enabled,
                        set_automation_enabled,
                    )

                    disarm_production_autonomy()
                    set_auto_upload_enabled(False)
                    set_automation_enabled(False)
                    _AUTONOMY_WAKE_EVENT.set()
                    self._json(
                        {
                            "ok": True,
                            "action": "disarm",
                            "runtime": autonomy_status(),
                        }
                    )
                    return
                self._json(
                    {
                        "message": (
                            "action must be arm or disarm"
                        )
                    },
                    HTTPStatus.BAD_REQUEST,
                )
                return
            except Exception as exc:
                self._json(
                    {"message": str(exc)},
                    HTTPStatus.BAD_REQUEST,
                )
                return

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
                    "autonomy": autonomy_status(),
                }
            )
            return

        if path == "/v1/runtime/status":
            self._json(
                {
                    "ok": True,
                    "runtime": autonomy_status(),
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
            if (
                len(job_id) != 32
                or any(
                    ch not in "0123456789abcdef"
                    for ch in job_id.lower()
                )
            ):
                self._json(
                    {"message": "invalid job id"},
                    HTTPStatus.BAD_REQUEST,
                )
                return
            if Path(filename).name != filename:
                self._json(
                    {"message": "invalid artifact name"},
                    HTTPStatus.BAD_REQUEST,
                )
                return
            root = (ARTIFACT_ROOT / job_id).resolve()
            try:
                root.relative_to(ARTIFACT_ROOT.resolve())
            except ValueError:
                self._json(
                    {"message": "invalid artifact path"},
                    HTTPStatus.BAD_REQUEST,
                )
                return
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
    init_db()
    JOB_ROOT.mkdir(parents=True, exist_ok=True)
    ARTIFACT_ROOT.mkdir(parents=True, exist_ok=True)
    recovered = _recover_stale_jobs()
    if recovered:
        print(
            f"[CLOUD-WORKER] stale jobs recovered: {recovered}"
        )
    server = ThreadingHTTPServer(
        (host, int(port)),
        CloudWorkerHandler,
    )
    runner = _start_autonomy_runner()
    print(
        f"[CLOUD-WORKER] http://{host}:{int(port)} / "
        f"token={'configured' if token else 'local-only'} / "
        f"autonomy={'enabled' if runner else 'disabled'}"
    )
    try:
        server.serve_forever()
    finally:
        _stop_autonomy_runner()
        server.server_close()


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
