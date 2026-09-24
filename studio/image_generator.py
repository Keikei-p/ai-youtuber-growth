from __future__ import annotations

import base64
import importlib.util
import io
import os
import threading
from datetime import datetime
from pathlib import Path

import requests
from PIL import Image

from config import settings
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
_PIPELINE_LOCK = threading.Lock()


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
    webui_ok = (
        _webui_available()
        if configured == "webui" or (configured == "auto" and not diffusers_ok)
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
            "Diffusers画像生成が未導入です。requirements-studio.txt を導入してください。"
        )
    if status["configured"] == "webui":
        raise RuntimeError("WebUI互換画像生成APIへ接続できません。")
    raise RuntimeError(
        "画像生成エンジンが見つかりません。Diffusersを導入するか、"
        "WebUI互換APIを起動してください。"
    )


def _load_diffusers_pipeline():
    global _PIPELINE, _PIPELINE_MODEL

    model_id = getattr(
        settings,
        "studio_diffusers_model",
        "stable-diffusion-v1-5/stable-diffusion-v1-5",
    )
    with _PIPELINE_LOCK:
        if _PIPELINE is not None and _PIPELINE_MODEL == model_id:
            return _PIPELINE

        import torch
        from diffusers import StableDiffusionPipeline

        use_cuda = torch.cuda.is_available()
        dtype = torch.float16 if use_cuda else torch.float32
        pipe = StableDiffusionPipeline.from_pretrained(
            model_id,
            torch_dtype=dtype,
            safety_checker=None,
            requires_safety_checker=False,
        )
        pipe.enable_attention_slicing()
        if hasattr(pipe, "enable_vae_slicing"):
            pipe.enable_vae_slicing()
        pipe = pipe.to("cuda" if use_cuda else "cpu")

        _PIPELINE = pipe
        _PIPELINE_MODEL = model_id
        return pipe


def _generate_diffusers(prompt: str, preset: ImagePreset) -> Image.Image:
    import torch

    pipe = _load_diffusers_pipeline()
    generator = None
    seed_raw = os.getenv("STUDIO_SEED", "").strip()
    if seed_raw:
        device = "cuda" if torch.cuda.is_available() else "cpu"
        generator = torch.Generator(device=device).manual_seed(int(seed_raw))

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
    return result.images[0]


def _generate_webui(prompt: str, preset: ImagePreset) -> Image.Image:
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
    return Image.open(io.BytesIO(base64.b64decode(raw))).convert("RGB")


def _generate(prompt: str, preset: ImagePreset) -> tuple[Image.Image, str]:
    backend = _resolve_backend()
    if backend == "diffusers":
        return _generate_diffusers(prompt, preset), backend
    return _generate_webui(prompt, preset), backend


def _save_image(image: Image.Image, folder: str, prefix: str) -> Path:
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
    path = _save_image(image, "mirai", f"mirai_{expression}")
    record_asset(
        "mirai", path, prompt, backend=backend,
        meta={"expression": expression},
    )
    return str(path)


def generate_background_image(theme: str) -> str:
    theme = theme.strip()
    if not theme:
        raise ValueError("背景テーマを入力してください。")
    prompt = build_background_prompt(theme)
    image, backend = _generate(prompt, BACKGROUND_PRESET)
    path = _save_image(image, "backgrounds", "background")
    record_asset(
        "background", path, prompt, backend=backend,
        meta={"theme": theme},
    )
    return str(path)


def generate_guest_image(row: dict) -> str:
    guest_id = int(row["id"])
    guest_name = str(row.get("name") or f"guest_{guest_id}")
    prompt = build_guest_prompt(row)
    image, backend = _generate(prompt, GUEST_PRESET)
    path = _save_image(image, "guests", f"guest_{guest_id}")
    set_guest_image(guest_id, str(path))
    record_asset(
        "guest", path, prompt, backend=backend,
        meta={"guest_id": guest_id, "guest_name": guest_name},
    )
    return str(path)


def generate_guest_image_from_prompt(guest_id: int, visual_prompt: str) -> str:
    row = {
        "id": guest_id,
        "name": f"guest_{guest_id}",
        "profile_json": "{}",
        "visual_prompt": visual_prompt,
    }
    return generate_guest_image(row)
