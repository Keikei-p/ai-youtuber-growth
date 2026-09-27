from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urljoin, urlparse
from urllib.request import Request, urlopen

from config import settings
from paths import VIDEO_DIR
from runtime_control import (
    cloud_execution_fallback_local,
    execution_mode,
)
from storage import (
    get_channel_state,
    set_channel_state,
    update_video_output,
    update_video_thumbnail,
)
from studio.asset_store import GENERATED_ROOT


class CloudExecutionError(RuntimeError):
    pass


def _safe_state(key: str, default: str = "") -> str:
    try:
        return get_channel_state(key, default)
    except Exception:
        return default


def _write_state(
    *,
    status: str,
    location: str,
    job_id: str = "",
    detail: str = "",
    fallback: bool = False,
) -> None:
    payload = {
        "status": str(status),
        "location": str(location),
        "job_id": str(job_id or ""),
        "detail": str(detail or "")[:2000],
        "fallback": bool(fallback),
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    set_channel_state(
        "execution_last_event",
        json.dumps(payload, ensure_ascii=False),
    )


def _last_event() -> dict[str, Any]:
    raw = _safe_state("execution_last_event", "").strip()
    if not raw:
        return {}
    try:
        value = json.loads(raw)
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def execution_status() -> dict[str, Any]:
    mode = execution_mode()
    endpoint = str(
        getattr(settings, "cloud_execution_url", "") or ""
    ).strip()
    parsed = urlparse(endpoint) if endpoint else None
    return {
        "mode": mode,
        "provider": (
            "Local PC"
            if mode == "local"
            else "Cloud Execution Worker"
        ),
        "cloud_configured": bool(
            endpoint
            and parsed
            and parsed.scheme in {"http", "https"}
            and parsed.netloc
        ),
        "cloud_host": (
            parsed.hostname
            if parsed and parsed.hostname
            else ""
        ),
        "fallback_local": cloud_execution_fallback_local(),
        "last_event": _last_event(),
        "token_configured": bool(
            str(
                getattr(
                    settings,
                    "cloud_execution_token",
                    "",
                )
                or ""
            ).strip()
        ),
    }


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(
        value,
        (str, int, float, bool),
    ):
        return value
    if isinstance(value, dict):
        return {
            str(key): _json_safe(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return str(value)


def _job_payload(
    results: list[dict],
    character: dict,
) -> dict[str, Any]:
    allowed_keys = {
        "id",
        "idea",
        "title",
        "title_candidates",
        "script",
        "description",
        "tags",
        "guest",
        "status",
        "first_episode",
    }
    items = []
    for row in results:
        items.append(
            {
                key: _json_safe(row.get(key))
                for key in allowed_keys
                if key in row
            }
        )
    return {
        "schema_version": 1,
        "kind": "produce_media",
        "character": _json_safe(character),
        "items": items,
    }


class CloudExecutionClient:
    def __init__(self) -> None:
        self.base_url = str(
            getattr(settings, "cloud_execution_url", "") or ""
        ).strip().rstrip("/") + "/"
        self.token = str(
            getattr(settings, "cloud_execution_token", "") or ""
        ).strip()
        self.timeout = max(
            30,
            int(
                getattr(
                    settings,
                    "cloud_execution_timeout_seconds",
                    1800,
                )
            ),
        )
        self.poll_seconds = max(
            1,
            min(
                int(
                    getattr(
                        settings,
                        "cloud_execution_poll_seconds",
                        5,
                    )
                ),
                60,
            ),
        )

    def configured(self) -> bool:
        parsed = urlparse(self.base_url)
        return (
            parsed.scheme in {"http", "https"}
            and bool(parsed.netloc)
        )

    def _request_json(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not self.configured():
            raise CloudExecutionError(
                "CLOUD_EXECUTION_URL が未設定です。"
            )
        raw = (
            json.dumps(
                payload,
                ensure_ascii=False,
            ).encode("utf-8")
            if payload is not None
            else None
        )
        headers = {
            "Accept": "application/json",
        }
        if raw is not None:
            headers["Content-Type"] = "application/json; charset=utf-8"
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        req = Request(
            urljoin(self.base_url, path.lstrip("/")),
            data=raw,
            headers=headers,
            method=method.upper(),
        )
        try:
            with urlopen(
                req,
                timeout=min(self.timeout, 60),
            ) as response:
                body = response.read().decode(
                    "utf-8",
                    errors="replace",
                )
        except HTTPError as exc:
            detail = exc.read().decode(
                "utf-8",
                errors="replace",
            )
            raise CloudExecutionError(
                f"Cloud Worker HTTP {exc.code}: {detail[:1000]}"
            ) from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise CloudExecutionError(
                f"Cloud Workerへ接続できません: {exc}"
            ) from exc

        try:
            value = json.loads(body or "{}")
        except Exception as exc:
            raise CloudExecutionError(
                "Cloud Worker応答がJSONではありません。"
            ) from exc
        if not isinstance(value, dict):
            raise CloudExecutionError(
                "Cloud Worker応答形式が不正です。"
            )
        return value

    def submit(
        self,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        return self._request_json(
            "POST",
            "/v1/jobs",
            payload,
        )

    def job(self, job_id: str) -> dict[str, Any]:
        return self._request_json(
            "GET",
            f"/v1/jobs/{quote(str(job_id), safe='')}",
        )

    def download(
        self,
        artifact_url: str,
        destination: Path,
    ) -> Path:
        target_url = urljoin(
            self.base_url,
            str(artifact_url or ""),
        )
        parsed = urlparse(target_url)
        base = urlparse(self.base_url)
        if (
            parsed.scheme not in {"http", "https"}
            or parsed.netloc != base.netloc
        ):
            raise CloudExecutionError(
                "Cloud artifact URLが許可されたWorker外を指しています。"
            )
        headers = {}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        request = Request(
            target_url,
            headers=headers,
            method="GET",
        )
        destination.parent.mkdir(
            parents=True,
            exist_ok=True,
        )
        try:
            with urlopen(
                request,
                timeout=min(self.timeout, 120),
            ) as response:
                data = response.read()
        except (HTTPError, URLError, TimeoutError, OSError) as exc:
            raise CloudExecutionError(
                f"Cloud artifact取得失敗: {exc}"
            ) from exc
        if not data:
            raise CloudExecutionError(
                "Cloud artifactが空です。"
            )
        destination.write_bytes(data)
        return destination


def _apply_manifest(
    results: list[dict],
    manifest: dict[str, Any],
    *,
    job_id: str,
    client: CloudExecutionClient,
) -> None:
    remote_items = manifest.get("items") or []
    if not isinstance(remote_items, list):
        raise CloudExecutionError(
            "Cloud manifest.items が不正です。"
        )
    local_by_id = {
        int(row["id"]): row
        for row in results
        if str(row.get("id") or "").isdigit()
    }
    copied = 0
    for remote in remote_items:
        if not isinstance(remote, dict):
            continue
        try:
            video_id = int(remote.get("id") or 0)
        except (TypeError, ValueError):
            continue
        local = local_by_id.get(video_id)
        if local is None:
            continue

        for key in (
            "status",
            "quality_passed",
            "quality",
            "editorial_quality",
            "media_error",
            "visual_warning",
            "composition_plan",
        ):
            if key in remote:
                local[key] = remote[key]

        output_url = str(
            remote.get("output_url") or ""
        ).strip()
        if output_url:
            destination = (
                VIDEO_DIR
                / f"cloud_{job_id}_{video_id}.mp4"
            )
            client.download(
                output_url,
                destination,
            )
            local["output_path"] = str(destination)
            local["status"] = str(
                remote.get("status") or "rendered"
            )
            update_video_output(
                video_id,
                str(destination),
                status=local["status"],
            )
            copied += 1

        thumbnail_url = str(
            remote.get("thumbnail_url") or ""
        ).strip()
        if thumbnail_url:
            suffix = Path(
                urlparse(thumbnail_url).path
            ).suffix.lower()
            if suffix not in {
                ".png",
                ".jpg",
                ".jpeg",
                ".webp",
            }:
                suffix = ".jpg"
            destination = (
                GENERATED_ROOT
                / "thumbnails"
                / f"cloud_{job_id}_{video_id}{suffix}"
            )
            client.download(
                thumbnail_url,
                destination,
            )
            local["thumbnail_path"] = str(destination)
            update_video_thumbnail(
                video_id,
                str(destination),
            )

    if results and copied == 0:
        raise CloudExecutionError(
            "Cloud jobは完了しましたが完成動画を受け取れませんでした。"
        )


def _run_local(
    results: list[dict],
    character: dict,
    *,
    fallback_reason: str = "",
) -> None:
    from production_pipeline import produce_media

    _write_state(
        status="running",
        location="local",
        detail=(
            "CloudからLocalへフォールバックして制作中"
            if fallback_reason
            else "PCで制作中"
        ),
        fallback=bool(fallback_reason),
    )
    produce_media(results, character)
    _write_state(
        status="succeeded",
        location="local",
        detail=(
            f"Local fallback成功: {fallback_reason}"
            if fallback_reason
            else "PC制作が正常終了"
        ),
        fallback=bool(fallback_reason),
    )


def execute_media(
    results: list[dict],
    character: dict,
) -> None:
    if execution_mode() == "local":
        _run_local(results, character)
        return

    client = CloudExecutionClient()
    job_id = ""
    try:
        if not client.configured():
            raise CloudExecutionError(
                "Cloud Execution Workerが未設定です。"
            )

        _write_state(
            status="submitting",
            location="cloud",
            detail="Cloud Workerへ制作ジョブを送信中",
        )
        submitted = client.submit(
            _job_payload(results, character)
        )
        job_id = str(
            submitted.get("job_id") or ""
        ).strip()
        if not job_id:
            raise CloudExecutionError(
                "Cloud Workerからjob_idを取得できません。"
            )

        _write_state(
            status="running",
            location="cloud",
            job_id=job_id,
            detail="Cloud Workerで制作中",
        )
        deadline = time.monotonic() + client.timeout
        response = submitted
        while True:
            status = str(
                response.get("status") or ""
            ).strip().lower()
            if status in {"succeeded", "failed"}:
                break
            if time.monotonic() >= deadline:
                raise CloudExecutionError(
                    f"Cloud job {job_id} がタイムアウトしました。"
                )
            time.sleep(client.poll_seconds)
            response = client.job(job_id)

        if status != "succeeded":
            raise CloudExecutionError(
                str(
                    response.get("error")
                    or f"Cloud job {job_id} failed"
                )
            )

        manifest = response.get("manifest") or {}
        if not isinstance(manifest, dict):
            raise CloudExecutionError(
                "Cloud job manifestが不正です。"
            )
        _apply_manifest(
            results,
            manifest,
            job_id=job_id,
            client=client,
        )
        _write_state(
            status="succeeded",
            location="cloud",
            job_id=job_id,
            detail="Cloud制作と成果物取得が正常終了",
        )
    except Exception as exc:
        try:
            from self_improvement import record_failure

            record_failure(
                "execution.cloud",
                exc,
                {
                    "job_id": job_id,
                    "fallback_local": cloud_execution_fallback_local(),
                },
            )
        except Exception:
            pass

        _write_state(
            status="failed",
            location="cloud",
            job_id=job_id,
            detail=str(exc),
        )
        if not cloud_execution_fallback_local():
            raise
        print(
            "[EXECUTION] Cloud制作失敗。"
            f"Localへ安全にフォールバックします: {exc}"
        )
        _run_local(
            results,
            character,
            fallback_reason=str(exc),
        )
