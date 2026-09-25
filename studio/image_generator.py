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
from runtime_control import (
    visual_background_candidates,
    visual_candidate_count,
    visual_highres_enabled,
    visual_min_score,
    visual_retry_rounds,
    visual_runtime_settings,
)
from mirai_engines.visual_learning import VisualLearningMemory
from mirai_engines.visual_quality_engine import MiraiVisualQualityEngine
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
    *,
    seed: int | None = None,
) -> Image.Image:
    import torch

    pipe = _load_diffusers_pipeline(device)
    generator = None
    seed_raw = os.getenv("STUDIO_SEED", "").strip()
    seed_value = seed
    if seed_value is None and seed_raw:
        seed_value = int(seed_raw)
    if seed_value is not None:
        generator = torch.Generator(device=device).manual_seed(
            int(seed_value)
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
    *,
    seed: int | None = None,
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
                        seed=seed,
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
                seed=seed,
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
    *,
    seed: int | None = None,
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
            "seed": int(seed) if seed is not None else -1,
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
    *,
    seed: int | None = None,
) -> tuple[Image.Image, str]:
    backend = _resolve_backend()
    if backend == "diffusers":
        try:
            return _generate_diffusers(prompt, preset, seed=seed)
        finally:
            # Ollama/VOICEVOXへGPUを返すため、画像生成後は必ず退避。
            _park_pipeline()
    return _generate_webui(prompt, preset, seed=seed), backend


def _rounded_size(image: Image.Image, scale: float) -> tuple[int, int]:
    # Stable Diffusion系で扱いやすいよう8の倍数へ丸める。
    width = max(64, int(round(image.width * scale / 8.0)) * 8)
    height = max(64, int(round(image.height * scale / 8.0)) * 8)
    return width, height


def _refine_diffusers(
    image: Image.Image,
    prompt: str,
    *,
    seed: int | None = None,
) -> Image.Image:
    import torch
    from diffusers import StableDiffusionImg2ImgPipeline

    if not torch.cuda.is_available():
        raise RuntimeError("High-Res RefineはCUDA利用時のみ実行します。")

    settings_now = visual_runtime_settings()
    width, height = _rounded_size(
        image,
        float(settings_now["highres_scale"]),
    )
    source = image.convert("RGB").resize(
        (width, height),
        Image.Resampling.LANCZOS,
    )

    with exclusive_gpu_task("AI Studio High-Res Refine"):
        unload_ollama_model(wait_seconds=2)
        pipe = _load_diffusers_pipeline("cuda")
        img2img = StableDiffusionImg2ImgPipeline(**pipe.components)
        img2img.enable_attention_slicing()
        if hasattr(img2img, "enable_vae_slicing"):
            img2img.enable_vae_slicing()
        generator = None
        if seed is not None:
            generator = torch.Generator(device="cuda").manual_seed(
                int(seed)
            )
        try:
            result = img2img(
                prompt=prompt,
                negative_prompt=NEGATIVE_PROMPT,
                image=source,
                strength=float(settings_now["highres_strength"]),
                num_inference_steps=int(settings_now["highres_steps"]),
                guidance_scale=6.0,
                generator=generator,
            )
            if not result.images:
                raise RuntimeError(
                    "High-Res Refineから画像が返りませんでした。"
                )
            return result.images[0].convert("RGB")
        finally:
            del img2img
            _park_pipeline()
            release_torch_cuda_cache()


def _refine_webui(
    image: Image.Image,
    prompt: str,
    *,
    seed: int | None = None,
) -> Image.Image:
    settings_now = visual_runtime_settings()
    width, height = _rounded_size(
        image,
        float(settings_now["highres_scale"]),
    )
    buffer = io.BytesIO()
    image.convert("RGB").save(buffer, format="PNG")
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    response = requests.post(
        settings.sd_webui_url.rstrip("/") + "/sdapi/v1/img2img",
        json={
            "init_images": [encoded],
            "prompt": prompt,
            "negative_prompt": NEGATIVE_PROMPT,
            "steps": int(settings_now["highres_steps"]),
            "width": width,
            "height": height,
            "denoising_strength": float(
                settings_now["highres_strength"]
            ),
            "cfg_scale": 6.0,
            "batch_size": 1,
            "seed": int(seed) if seed is not None else -1,
        },
        timeout=300,
    )
    response.raise_for_status()
    images = response.json().get("images") or []
    if not images:
        raise RuntimeError(
            "High-Res WebUI Refineから画像が返りませんでした。"
        )
    raw = images[0].split(",", 1)[-1]
    return Image.open(
        io.BytesIO(base64.b64decode(raw))
    ).convert("RGB")


def _highres_refine_selection(
    selection: dict,
    *,
    asset_type: str,
    seed: int | None = None,
) -> dict:
    if not visual_highres_enabled():
        return selection
    # 背景は本数が多く負荷増が大きいため、v2初期は人物系に集中。
    if asset_type not in {"mirai", "guest"}:
        return selection

    quality_engine = MiraiVisualQualityEngine()
    original = selection["image"]
    original_score = int(
        selection["quality"].get("score") or 0
    )
    try:
        backend = str(selection.get("backend") or "")
        if backend.startswith("diffusers"):
            refined = _refine_diffusers(
                original,
                selection["prompt"],
                seed=seed,
            )
            refined_backend = backend + "-highres"
        else:
            refined = _refine_webui(
                original,
                selection["prompt"],
                seed=seed,
            )
            refined_backend = backend + "-highres"

        report = quality_engine.inspect_image(
            refined,
            asset_type=asset_type,
        )
        refined_score = int(report.get("score") or 0)
        if refined_score < original_score:
            print(
                "[VISUAL] High-Res Refineは品質点が下がったため"
                f"元画像を採用: {original_score}>{refined_score}"
            )
            return selection

        upgraded = dict(selection)
        upgraded["image"] = refined
        upgraded["backend"] = refined_backend
        upgraded["quality"] = report
        upgraded["highres_refined"] = True
        upgraded["base_quality_score"] = original_score
        return upgraded
    except Exception as exc:
        # 高画質化の失敗で本番生成全体を止めない。
        print(
            "[VISUAL] High-Res Refineを安全スキップ: "
            f"{exc}"
        )
        fallback = dict(selection)
        fallback["highres_refined"] = False
        fallback["highres_error"] = str(exc)[:300]
        return fallback


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


def _seed_base() -> int:
    raw = os.getenv("STUDIO_SEED", "").strip()
    if raw:
        return int(raw)
    return int(time.time_ns() % 2_000_000_000)


def _visual_min_score() -> int:
    return visual_min_score()


def _generate_best_image(
    prompt: str,
    preset: ImagePreset,
    *,
    asset_type: str,
    meta: dict | None = None,
) -> dict:
    memory = VisualLearningMemory()
    quality_engine = MiraiVisualQualityEngine()
    preferred = memory.recommended_profile(asset_type)
    candidate_count = (
        visual_candidate_count()
        if asset_type in {"mirai", "guest"}
        else visual_background_candidates()
    )
    retry_rounds = visual_retry_rounds()
    threshold = _visual_min_score()
    seed_base = _seed_base()
    candidates: list[dict] = []
    last_error: Exception | None = None

    def run_candidate(attempt_index: int) -> None:
        nonlocal last_error
        profile = memory.profile_for_attempt(preferred, attempt_index)
        evolved_prompt = memory.evolve_prompt(
            prompt,
            asset_type=asset_type,
            profile=profile,
        )
        try:
            generated, backend = _generate(
                evolved_prompt,
                preset,
                seed=seed_base + attempt_index * 9973,
            )
            report = quality_engine.inspect_image(
                generated,
                asset_type=asset_type,
            )
            candidates.append({
                "image": generated,
                "backend": backend,
                "quality": report,
                "prompt": evolved_prompt,
                "profile": profile,
                "candidate_index": attempt_index,
            })
        except Exception as exc:
            last_error = exc

    for candidate_index in range(candidate_count):
        run_candidate(candidate_index)

    if not candidates and last_error is not None:
        raise last_error
    if not candidates:
        raise RuntimeError("画像候補を生成できませんでした。")

    best = max(candidates, key=lambda row: int(row["quality"].get("score") or 0))
    for retry_index in range(retry_rounds):
        if int(best["quality"].get("score") or 0) >= threshold:
            break
        run_candidate(candidate_count + retry_index)
        best = max(candidates, key=lambda row: int(row["quality"].get("score") or 0))

    for candidate in candidates:
        if candidate is best:
            continue
        report = candidate["quality"]
        memory.record_result(
            asset_type=asset_type,
            path="",
            prompt=candidate["prompt"],
            backend=candidate["backend"],
            profile=candidate["profile"],
            score=int(report.get("score") or 0),
            passed=bool(report.get("passed")),
            accepted=False,
            metrics=report.get("metrics") or {},
            meta={**(meta or {}), "candidate_index": candidate["candidate_index"]},
        )

    best_score = int(best["quality"].get("score") or 0)
    if best_score < threshold:
        report = best["quality"]
        memory.record_result(
            asset_type=asset_type,
            path="",
            prompt=best["prompt"],
            backend=best["backend"],
            profile=best["profile"],
            score=best_score,
            passed=bool(report.get("passed")),
            accepted=False,
            metrics=report.get("metrics") or {},
            meta={**(meta or {}), "rejected_by_quality_floor": True},
        )
        raise RuntimeError(
            f"Visual Quality {best_score}/100で基準{threshold}点未満のため採用しません。"
        )

    best = _highres_refine_selection(
        best,
        asset_type=asset_type,
        seed=seed_base + 500_003,
    )
    return best


def _save_selected_visual(
    selection: dict,
    *,
    folder: str,
    prefix: str,
    asset_type: str,
    meta: dict | None = None,
) -> Path:
    path = _save_image(selection["image"], folder, prefix)
    report = selection["quality"]
    VisualLearningMemory().record_result(
        asset_type=asset_type,
        path=path,
        prompt=selection["prompt"],
        backend=selection["backend"],
        profile=selection["profile"],
        score=int(report.get("score") or 0),
        passed=bool(report.get("passed")),
        accepted=True,
        metrics=report.get("metrics") or {},
        meta=meta or {},
    )
    return path


def _asset_visual_meta(selection: dict, meta: dict | None = None) -> dict:
    quality = selection["quality"]
    return {
        **(meta or {}),
        "visual_score": int(quality.get("score") or 0),
        "visual_profile": selection["profile"],
        "visual_metrics": quality.get("metrics") or {},
        "visual_issues": quality.get("issues") or [],
        "highres_refined": bool(
            selection.get("highres_refined")
        ),
        "base_quality_score": selection.get(
            "base_quality_score"
        ),
        "final_width": int(selection["image"].width),
        "final_height": int(selection["image"].height),
    }


def generate_mirai_image(expression: str = "normal") -> str:
    prompt = build_mirai_prompt(expression)
    selection = _generate_best_image(
        prompt, MIRAI_PRESET, asset_type="mirai", meta={"expression": expression}
    )
    path = _save_selected_visual(
        selection, folder="mirai", prefix=f"mirai_{expression}",
        asset_type="mirai", meta={"expression": expression}
    )
    record_asset(
        "mirai", path, selection["prompt"], backend=selection["backend"],
        meta=_asset_visual_meta(selection, {"expression": expression}),
    )
    return str(path)


def generate_background_image(theme: str) -> str:
    theme = theme.strip()
    if not theme:
        raise ValueError("背景テーマを入力してください。")
    prompt = build_background_prompt(theme)
    selection = _generate_best_image(
        prompt, BACKGROUND_PRESET, asset_type="background", meta={"theme": theme}
    )
    path = _save_selected_visual(
        selection, folder="backgrounds", prefix="background",
        asset_type="background", meta={"theme": theme}
    )
    record_asset(
        "background", path, selection["prompt"], backend=selection["backend"],
        meta=_asset_visual_meta(selection, {"theme": theme}),
    )
    return str(path)


def generate_guest_image(row: dict) -> str:
    guest_id = int(row["id"])
    guest_name = str(row.get("name") or f"guest_{guest_id}")
    prompt = build_guest_prompt(row)
    meta = {"guest_id": guest_id, "guest_name": guest_name}
    selection = _generate_best_image(
        prompt, GUEST_PRESET, asset_type="guest", meta=meta
    )
    path = _save_selected_visual(
        selection, folder="guests", prefix=f"guest_{guest_id}",
        asset_type="guest", meta=meta
    )
    set_guest_image(guest_id, str(path))
    record_asset(
        "guest", path, selection["prompt"], backend=selection["backend"],
        meta=_asset_visual_meta(selection, meta),
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
