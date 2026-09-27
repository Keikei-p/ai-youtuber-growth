from __future__ import annotations

import base64
import mimetypes
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import requests

from config import settings
from studio.asset_store import GENERATED_ROOT, ensure_dirs, record_asset


BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
SUPPORTED_MODELS = {
    "veo-3.1-generate-preview",
    "veo-3.1-fast-generate-preview",
    "veo-3.1-lite-generate-preview",
}


class GoogleVideoGenerationError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        code: str = "google_video_error",
        retryable: bool = True,
        detail: dict[str, Any] | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.detail = detail or {}


def _api_key() -> str:
    return str(
        getattr(settings, "google_ai_api_key", "")
        or os.getenv("GEMINI_API_KEY", "")
        or os.getenv("GOOGLE_API_KEY", "")
    ).strip()


def _model() -> str:
    model = str(
        getattr(
            settings,
            "google_video_model",
            "veo-3.1-fast-generate-preview",
        )
        or ""
    ).strip()
    if model not in SUPPORTED_MODELS:
        raise GoogleVideoGenerationError(
            f"未対応のGoogle動画モデルです: {model}",
            code="google_video_model_invalid",
            retryable=False,
        )
    return model


def google_video_status() -> dict[str, Any]:
    key = _api_key()
    model = str(
        getattr(
            settings,
            "google_video_model",
            "veo-3.1-fast-generate-preview",
        )
        or ""
    ).strip()
    duration = int(
        getattr(settings, "google_video_duration_seconds", 4)
    )
    resolution = str(
        getattr(settings, "google_video_resolution", "720p")
    )
    aspect = str(
        getattr(settings, "google_video_aspect_ratio", "9:16")
    )
    enabled = bool(
        getattr(settings, "google_video_enabled", False)
    )
    return {
        "available": enabled and bool(key) and model in SUPPORTED_MODELS,
        "api_key_configured": bool(key),
        "google_video_enabled": enabled,
        "backend": "google",
        "provider": "Gemini API / Veo",
        "model": model,
        "duration_seconds": duration,
        "resolution": resolution,
        "aspect_ratio": aspect,
        "uses_paid_api": True,
        "reason": (
            "初期運用ではGoogle有料動画生成を停止中です"
            if not enabled
            else (
                ""
                if key
                else "GEMINI_API_KEY または GOOGLE_API_KEY が未設定です"
            )
        ),
    }


def _output_path() -> Path:
    ensure_dirs()
    folder = GENERATED_ROOT / "videos"
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    return folder / f"google_veo_{stamp}.mp4"


def _inline_image(path: str | Path) -> dict[str, Any]:
    source = Path(path)
    if not source.is_file():
        raise FileNotFoundError(str(source))
    mime = mimetypes.guess_type(source.name)[0] or "image/png"
    if mime not in {
        "image/png",
        "image/jpeg",
        "image/webp",
    }:
        mime = "image/png"
    encoded = base64.b64encode(source.read_bytes()).decode("ascii")
    return {
        "inlineData": {
            "mimeType": mime,
            "data": encoded,
        }
    }


def _normalize_duration(
    duration_seconds: int,
    *,
    resolution: str,
    reference_count: int,
) -> int:
    value = int(duration_seconds)
    if value not in {4, 6, 8}:
        value = 4
    if resolution in {"1080p", "4k"} or reference_count > 0:
        value = 8
    return value


def _raise_http_error(
    response: requests.Response,
    *,
    code: str,
) -> None:
    try:
        detail = response.json()
    except Exception:
        detail = {"text": response.text[-2000:]}

    status = int(response.status_code)
    retryable = status in {408, 409, 429, 500, 502, 503, 504}
    message = ""
    if isinstance(detail, dict):
        error = detail.get("error")
        if isinstance(error, dict):
            message = str(error.get("message") or "")
    if not message:
        message = f"Google Veo API HTTP {status}"

    raise GoogleVideoGenerationError(
        message,
        code=code,
        retryable=retryable,
        detail=detail if isinstance(detail, dict) else {},
    )


def generate_google_veo_clip(
    prompt: str,
    *,
    image_path: str | Path | None = None,
    reference_images: list[str | Path] | None = None,
    model: str | None = None,
    duration_seconds: int | None = None,
    resolution: str | None = None,
    aspect_ratio: str | None = None,
    timeout_seconds: int | None = None,
    poll_seconds: int | None = None,
) -> str:
    """
    Gemini Developer API / Veo 3.1 で縦動画素材を生成する。

    - text-to-video / image-to-video
    - 最大3枚のreferenceImages
    - long-running operationをpoll
    - 完成した動画は2日以内に必ずローカル保存
    """
    if not bool(getattr(settings, "google_video_enabled", False)):
        raise GoogleVideoGenerationError(
            "Google有料動画生成は初期運用で無効です。"
            " GOOGLE_VIDEO_ENABLED=true を明示設定した時だけ利用できます。",
            code="google_video_disabled",
            retryable=False,
        )

    api_key = _api_key()
    if not api_key:
        raise GoogleVideoGenerationError(
            "Google動画生成APIキーが未設定です。"
            " GEMINI_API_KEY または GOOGLE_API_KEY を.envに設定してください。",
            code="google_video_api_key_missing",
            retryable=False,
        )

    selected_model = str(model or _model()).strip()
    if selected_model not in SUPPORTED_MODELS:
        raise GoogleVideoGenerationError(
            f"未対応のGoogle動画モデルです: {selected_model}",
            code="google_video_model_invalid",
            retryable=False,
        )

    selected_resolution = str(
        resolution
        or getattr(settings, "google_video_resolution", "720p")
    ).strip().lower()
    if selected_resolution not in {"720p", "1080p", "4k"}:
        selected_resolution = "720p"
    if (
        selected_model == "veo-3.1-lite-generate-preview"
        and selected_resolution == "4k"
    ):
        selected_resolution = "1080p"

    selected_aspect = str(
        aspect_ratio
        or getattr(settings, "google_video_aspect_ratio", "9:16")
    ).strip()
    if selected_aspect not in {"9:16", "16:9"}:
        selected_aspect = "9:16"

    references = [
        Path(value)
        for value in (reference_images or [])
        if value and Path(value).is_file()
    ][:3]
    duration = _normalize_duration(
        int(
            duration_seconds
            or getattr(settings, "google_video_duration_seconds", 4)
        ),
        resolution=selected_resolution,
        reference_count=len(references),
    )

    instance: dict[str, Any] = {
        "prompt": str(prompt or "").strip()[:8000],
    }
    if not instance["prompt"]:
        raise ValueError("Google動画生成promptが空です")

    if image_path and Path(image_path).is_file():
        instance["image"] = _inline_image(image_path)

    if references:
        if selected_model == "veo-3.1-lite-generate-preview":
            # LiteはreferenceImages非対応。最初の画像を開始フレームとして利用する。
            if "image" not in instance:
                instance["image"] = _inline_image(references[0])
        else:
            instance["referenceImages"] = [
                {
                    "image": _inline_image(path),
                    "referenceType": "asset",
                }
                for path in references
            ]

    params: dict[str, Any] = {
        "aspectRatio": selected_aspect,
        "durationSeconds": str(duration),
        "resolution": selected_resolution,
    }

    # 画像入力では公式仕様上 personGeneration=allow_adult。
    # テキストのみではallow_all。成人/実在人物を作る意図ではなくAPI制約対応。
    params["personGeneration"] = (
        "allow_adult"
        if ("image" in instance or "referenceImages" in instance)
        else "allow_all"
    )

    endpoint = (
        f"{BASE_URL}/models/"
        f"{selected_model}:predictLongRunning"
    )
    headers = {
        "x-goog-api-key": api_key,
        "Content-Type": "application/json",
    }
    try:
        response = requests.post(
            endpoint,
            headers=headers,
            json={
                "instances": [instance],
                "parameters": params,
            },
            timeout=60,
        )
    except requests.RequestException as exc:
        raise GoogleVideoGenerationError(
            f"Google Veo生成開始に接続できません: {exc}",
            code="google_video_network",
            retryable=True,
        ) from exc

    if response.status_code >= 400:
        _raise_http_error(
            response,
            code="google_video_start_failed",
        )

    payload = response.json()
    operation_name = str(payload.get("name") or "").strip()
    if not operation_name:
        raise GoogleVideoGenerationError(
            "Google Veo APIがoperation nameを返しませんでした。",
            code="google_video_operation_missing",
            retryable=True,
            detail=payload,
        )

    timeout = max(
        60,
        int(
            timeout_seconds
            or getattr(settings, "google_video_timeout_seconds", 480)
        ),
    )
    poll = max(
        3,
        int(
            poll_seconds
            or getattr(settings, "google_video_poll_seconds", 10)
        ),
    )
    deadline = time.monotonic() + timeout
    operation_url = f"{BASE_URL}/{operation_name.lstrip('/')}"

    completed: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        try:
            status_response = requests.get(
                operation_url,
                headers={"x-goog-api-key": api_key},
                timeout=30,
            )
        except requests.RequestException as exc:
            time.sleep(poll)
            continue

        if status_response.status_code >= 400:
            _raise_http_error(
                status_response,
                code="google_video_poll_failed",
            )

        status = status_response.json()
        if status.get("done"):
            completed = status
            break
        time.sleep(poll)

    if completed is None:
        raise GoogleVideoGenerationError(
            "Google Veo動画生成が制限時間内に完了しませんでした。",
            code="google_video_timeout",
            retryable=True,
            detail={"operation": operation_name},
        )

    if completed.get("error"):
        error = completed.get("error") or {}
        message = (
            str(error.get("message") or "")
            if isinstance(error, dict)
            else str(error)
        )
        raise GoogleVideoGenerationError(
            message or "Google Veo動画生成が失敗しました。",
            code="google_video_generation_failed",
            retryable=False,
            detail=completed,
        )

    response_block = (
        completed.get("response") or {}
    )
    generate_block = (
        response_block.get("generateVideoResponse") or {}
        if isinstance(response_block, dict)
        else {}
    )
    samples = generate_block.get("generatedSamples") or []
    if not samples:
        raise GoogleVideoGenerationError(
            "Google Veo APIの完了応答に動画がありません。",
            code="google_video_sample_missing",
            retryable=True,
            detail=completed,
        )

    video = (samples[0] or {}).get("video") or {}
    uri = str(video.get("uri") or "").strip()
    if not uri:
        raise GoogleVideoGenerationError(
            "Google Veo APIの動画URIがありません。",
            code="google_video_uri_missing",
            retryable=True,
            detail=completed,
        )

    output = _output_path()
    try:
        download = requests.get(
            uri,
            headers={"x-goog-api-key": api_key},
            timeout=120,
            allow_redirects=True,
        )
    except requests.RequestException as exc:
        raise GoogleVideoGenerationError(
            f"Google Veo動画の保存に失敗しました: {exc}",
            code="google_video_download_failed",
            retryable=True,
        ) from exc

    if download.status_code >= 400:
        _raise_http_error(
            download,
            code="google_video_download_failed",
        )
    output.write_bytes(download.content)

    if not output.is_file() or output.stat().st_size < 20_000:
        raise GoogleVideoGenerationError(
            "Google Veo動画ファイルが空または小さすぎます。",
            code="google_video_file_invalid",
            retryable=True,
        )

    record_asset(
        "ai_video",
        output,
        prompt=instance["prompt"],
        backend=selected_model,
        meta={
            "provider": "google-veo",
            "operation": operation_name,
            "model": selected_model,
            "duration_seconds": duration,
            "resolution": selected_resolution,
            "aspect_ratio": selected_aspect,
            "image_to_video": "image" in instance,
            "reference_count": len(references),
        },
    )
    return str(output)
