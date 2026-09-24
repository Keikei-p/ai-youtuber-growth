from __future__ import annotations

import base64
import gc
import importlib.util
import io
import os
import threading
import time
from datetime import datetime
from pathlib import Path

import requests
from PIL import Image

from config import settings
from gpu_manager import (
    exclusive_gpu_task,
    release_torch_cuda_cache,
    unload_ollama_model,
)
from storage import set_guest_image
from studio.asset_store import GENERATED_ROOT, ensure_dirs, record_asset
from studio.models import (
    BACKGROUND_PRESET,
    GUEST_PRESET,
    MIRAI_PRESET,
    NEGATIVE_PROMPT,
    ImagePreset,
)
from studio.prompts import (
    build_background_prompt,
    build_guest_prompt,
    build_mirai_prompt,
)

_PIPELINE = None
_PIPELINE_MODEL = None
_PIPELINE_DEVICE = None
_PIPELINE_DTYPE = None
_PIPELINE_LOCK = threading.RLock()


def _configured_backend() -> str:
    value = getattr(settings, "studio_image_backend", "auto")
    value = str(value or "auto").strip().lower()
    return value if value in {"auto", "diffusers", "webui"} else "auto"


def _diffusers_installed() -> bool:
    return bool(
        importlib.util.find_spec("torch")
        and importlib.util.find_spec("diffusers")
    )


def _webui_available(timeout: float = 1.5) -> bool:
    try:
        response = requests.get(
            settings.sd_webui_url.rstrip("/") + "/sdapi/v1/options",
            timeout=timeout,
        )
        return response.ok
    except requests.RequestException:
        return False


def studio_status() -> dict:
    configured = _configured_backend()
    diffusers_ok = _diffusers_installed()
    torch_version = None
    cuda_available = False
    cuda_version = None
    gpu_name = None
    if diffusers_ok:
        try:
            import torch

            torch_version = getattr(torch, "__version__", None)
            cuda_available = bool(torch.cuda.is_available())
            cuda_version = getattr(
                getattr(torch, "version", None),
                "cuda",
                None,
            )
            if cuda_available:
                gpu_name = torch.cuda.get_device_name(0)
        except Exception:
            pass

    webui_ok = (
        _webui_available()
        if configured == "webui"
        or (configured == "auto" and not diffusers_ok)
        else False
    )
    selected = None
    if configured == "diffusers":
        selected = "diffusers" if diffusers_ok else None
    elif configured == "webui":
        selected = "webui" if webui_ok else None
    else:
        if diffusers_ok:
            selected = "diffusers"
        elif webui_ok:
            selected = "webui"

    return {
        "configured": configured,
        "selected": selected,
        "available": bool(selected),
        "diffusers_installed": diffusers_ok,
        "webui_available": webui_ok,
        "torch_version": torch_version,
        "cuda_available": cuda_available,
        "cuda_version": cuda_version,
        "gpu_name": gpu_name,
        "gpu_retries": max(1, int(settings.studio_gpu_retries)),
        "cpu_fallback": bool(settings.studio_cpu_fallback),
        "model": getattr(
            settings,
            "studio_diffusers_model",
            "stable-diffusion-v1-5/stable-diffusion-v1-5",
        ),
    }


def _resolve_backend() -> str:
    status = studio_status()
    if status["selected"]:
        return str(status["selected"])
    if status["configured"] == "diffusers":
        raise RuntimeError(
            "Diffusers画像生成が未導入です。"
            "requirements-studio.txt を導入してください。"
        )
    if status["configured"] == "webui":
        raise RuntimeError("WebUI互換画像生成APIへ接続できません。")
    raise RuntimeError(
        "画像生成エンジンが見つかりません。Diffusersを導入するか、"
        "WebUI互換APIを起動してください。"
    )


def _drop_pipeline() -> None:
    global _PIPELINE, _PIPELINE_MODEL, _PIPELINE_DEVICE, _PIPELINE_DTYPE

    with _PIPELINE_LOCK:
        pipe = _PIPELINE
        _PIPELINE = None
        _PIPELINE_MODEL = None
        _PIPELINE_DEVICE = None
        _PIPELINE_DTYPE = None

    if pipe is not None:
        try:
            pipe.to("cpu")
        except Exception:
            pass
        del pipe

    gc.collect()
    release_torch_cuda_cache()


def _park_pipeline() -> None:
    global _PIPELINE_DEVICE

    with _PIPELINE_LOCK:
        pipe = _PIPELINE
        device = _PIPELINE_DEVICE
        if pipe is None or device != "cuda":
            return
        try:
            pipe.to("cpu")
            _PIPELINE_DEVICE = "cpu-parked"
        except Exception:
            # 退避に失敗した場合は参照ごと捨ててVRAM解放を優先。
            pass

    if _PIPELINE_DEVICE == "cuda":
        _drop_pipeline()
    else:
        gc.collect()
        release_torch_cuda_cache()


def _load_diffusers_pipeline(device: str):
    global _PIPELINE, _PIPELINE_MODEL, _PIPELINE_DEVICE, _PIPELINE_DTYPE

    model_id = getattr(
        settings,
        "studio_diffusers_model",
        "stable-diffusion-v1-5/stable-diffusion-v1-5",
    )

    import torch
    from diffusers import StableDiffusionPipeline

    dtype_name = "float16" if device == "cuda" else "float32"
    dtype = torch.float16 if device == "cuda" else torch.float32

    with _PIPELINE_LOCK:
        can_reuse = (
            _PIPELINE is not None
            and _PIPELINE_MODEL == model_id
            and _PIPELINE_DTYPE == dtype_name
        )

        if can_reuse:
            if _PIPELINE_DEVICE != device:
                _PIPELINE.to(device)
                _PIPELINE_DEVICE = device
            return _PIPELINE

    _drop_pipeline()

    pipe = StableDiffusionPipeline.from_pretrained(
        model_id,
        torch_dtype=dtype,
    )
    pipe.enable_attention_slicing()
    if hasattr(pipe, "enable_vae_slicing"):
        pipe.enable_vae_slicing()
    pipe = pipe.to(device)

    with _PIPELINE_LOCK:
        _PIPELINE = pipe
        _PIPELINE_MODEL = model_id
        _PIPELINE_DEVICE = device
        _PIPELINE_DTYPE = dtype_name
    return pipe


def _run_diffusers_once(
    prompt: str,
    preset: ImagePreset,
    device: str,
) -> Image.Image:
    import torch

    pipe = _load_diffusers_pipeline(device)
    generator = None
    seed_raw = os.getenv("STUDIO_SEED", "").strip()
    if seed_raw:
        generator = torch.Generator(device=device).manual_seed(
            int(seed_raw)
        )

    result = pipe(
        prompt=prompt,
        negative_prompt=NEGATIVE_PROMPT,
        width=preset.width,
        height=preset.height,
        num_inference_steps=preset.steps,
        guidance_scale=preset.guidance_scale,
        generator=generator,
    )
    if not result.images:
        raise RuntimeError("Diffusersから画像が返りませんでした。")
    flagged = getattr(result, "nsfw_content_detected", None)
    if flagged and bool(flagged[0]):
        raise RuntimeError("安全フィルターにより画像生成を中止しました。")
    return result.images[0]


def _cpu_fallback_preset(preset: ImagePreset) -> ImagePreset:
    # CPUは最終手段。待ち時間を抑えるため解像度とstepを下げる。
    if preset.height >= preset.width:
        width, height = 384, 576
    else:
        width, height = 576, 384
    return ImagePreset(
        width=width,
        height=height,
        steps=min(preset.steps, 12),
        guidance_scale=preset.guidance_scale,
        sampler_name=preset.sampler_name,
    )


def _looks_like_cuda_problem(exc: Exception) -> bool:
    text = str(exc).lower()
    markers = (
        "cuda",
        "cudart",
        "device(s) is/are busy",
        "device unavailable",
        "out of memory",
        "cublas",
        "cudnn",
    )
    return any(marker in text for marker in markers)


def _generate_diffusers(
    prompt: str,
    preset: ImagePreset,
) -> tuple[Image.Image, str]:
    import torch

    attempts = max(1, int(settings.studio_gpu_retries))
    last_cuda_error: Exception | None = None

    with exclusive_gpu_task("AI Studio画像生成"):
        cuda_available = False
        try:
            cuda_available = bool(torch.cuda.is_available())
        except Exception:
            cuda_available = False

        if cuda_available:
            for attempt in range(1, attempts + 1):
                try:
                    print(
                        f"[STUDIO] CUDA画像生成 "
                        f"{attempt}/{attempts}"
                    )
                    image = _run_diffusers_once(
                        prompt,
                        preset,
                        "cuda",
                    )
                    return image, "diffusers-cuda"
                except Exception as exc:
                    if not _looks_like_cuda_problem(exc):
                        raise
                    last_cuda_error = exc
                    print(
                        "[STUDIO] CUDA生成に失敗。"
                        f"GPUを整理して再試行します: {exc}"
                    )
                    _drop_pipeline()
                    unload_ollama_model(wait_seconds=5)
                    release_torch_cuda_cache()
                    time.sleep(min(1.5 * attempt, 3.0))

        if settings.studio_cpu_fallback:
            if last_cuda_error:
                print(
                    "[STUDIO] CUDA再試行で復旧しないため、"
                    "低負荷CPU生成へ切り替えます。"
                )
            else:
                print(
                    "[STUDIO] CUDAが利用できないため、"
                    "低負荷CPU生成へ切り替えます。"
                )
            _drop_pipeline()
            image = _run_diffusers_once(
                prompt,
                _cpu_fallback_preset(preset),
                "cpu",
            )
            return image, "diffusers-cpu"

        if last_cuda_error is not None:
            raise RuntimeError(
                "GPU自動整理と再試行でもCUDAを利用できませんでした: "
                f"{last_cuda_error}"
            ) from last_cuda_error

        raise RuntimeError(
            "CUDAを利用できず、CPUフォールバックも無効です。"
        )


def _generate_webui(
    prompt: str,
    preset: ImagePreset,
) -> Image.Image:
    response = requests.post(
        settings.sd_webui_url.rstrip("/") + "/sdapi/v1/txt2img",
        json={
            "prompt": prompt,
            "negative_prompt": NEGATIVE_PROMPT,
            "steps": preset.steps,
            "width": preset.width,
            "height": preset.height,
            "cfg_scale": preset.guidance_scale,
            "sampler_name": preset.sampler_name,
            "batch_size": 1,
        },
        timeout=300,
    )
    response.raise_for_status()
    images = response.json().get("images") or []
    if not images:
        raise RuntimeError("画像生成APIから画像が返りませんでした。")
    raw = images[0].split(",", 1)[-1]
    return Image.open(
        io.BytesIO(base64.b64decode(raw))
    ).convert("RGB")


def _generate(
    prompt: str,
    preset: ImagePreset,
) -> tuple[Image.Image, str]:
    backend = _resolve_backend()
    if backend == "diffusers":
        try:
            return _generate_diffusers(prompt, preset)
        finally:
            # Ollama/VOICEVOXへGPUを返すため、画像生成後は必ず退避。
            _park_pipeline()
    return _generate_webui(prompt, preset), backend


def _save_image(
    image: Image.Image,
    folder: str,
    prefix: str,
) -> Path:
    ensure_dirs()
    target = GENERATED_ROOT / folder
    target.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    path = target / f"{prefix}_{stamp}.png"
    image.save(path, "PNG")
    return path


def generate_mirai_image(expression: str = "normal") -> str:
    prompt = build_mirai_prompt(expression)
    image, backend = _generate(prompt, MIRAI_PRESET)
    path = _save_image(
        image,
        "mirai",
        f"mirai_{expression}",
    )
    record_asset(
        "mirai",
        path,
        prompt,
        backend=backend,
        meta={"expression": expression},
    )
    return str(path)


def generate_background_image(theme: str) -> str:
    theme = theme.strip()
    if not theme:
        raise ValueError("背景テーマを入力してください。")
    prompt = build_background_prompt(theme)
    image, backend = _generate(prompt, BACKGROUND_PRESET)
    path = _save_image(
        image,
        "backgrounds",
        "background",
    )
    record_asset(
        "background",
        path,
        prompt,
        backend=backend,
        meta={"theme": theme},
    )
    return str(path)


def generate_guest_image(row: dict) -> str:
    guest_id = int(row["id"])
    guest_name = str(
        row.get("name") or f"guest_{guest_id}"
    )
    prompt = build_guest_prompt(row)
    image, backend = _generate(prompt, GUEST_PRESET)
    path = _save_image(
        image,
        "guests",
        f"guest_{guest_id}",
    )
    set_guest_image(guest_id, str(path))
    record_asset(
        "guest",
        path,
        prompt,
        backend=backend,
        meta={
            "guest_id": guest_id,
            "guest_name": guest_name,
        },
    )
    return str(path)


def generate_guest_image_from_prompt(
    guest_id: int,
    visual_prompt: str,
) -> str:
    row = {
        "id": guest_id,
        "name": f"guest_{guest_id}",
        "profile_json": "{}",
        "visual_prompt": visual_prompt,
    }
    return generate_guest_image(row)
