from __future__ import annotations

import gc
import shutil
import subprocess
import tempfile
import threading
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageOps

from native_models.video_v0 import (
    generate_frames as generate_native_video_frames,
    video_status as native_video_status,
)

from config import settings
from gpu_manager import (
    exclusive_gpu_task,
    release_torch_cuda_cache,
)
from self_improvement import record_failure
from runtime_control import ai_video_backend_name
from studio.asset_store import GENERATED_ROOT, ensure_dirs, record_asset
from studio.google_video_generator import (
    generate_google_veo_clip,
    google_video_status,
)
from studio.models import NEGATIVE_PROMPT
from video.ffmpeg_encoder import run_video_encode


_ANIMATEDIFF_PIPELINE = None
_ANIMATEDIFF_MODEL = None
_ANIMATEDIFF_LOCK = threading.RLock()


def _load_animatediff_pipeline(torch):
    global _ANIMATEDIFF_PIPELINE, _ANIMATEDIFF_MODEL

    from diffusers import (
        AnimateDiffPipeline,
        DDIMScheduler,
        MotionAdapter,
    )

    model_id = str(settings.studio_diffusers_model)
    with _ANIMATEDIFF_LOCK:
        if (
            _ANIMATEDIFF_PIPELINE is not None
            and _ANIMATEDIFF_MODEL == model_id
        ):
            return _ANIMATEDIFF_PIPELINE

        dtype = torch.float16
        adapter = MotionAdapter.from_pretrained(
            "guoyww/animatediff-motion-adapter-v1-5-2",
            torch_dtype=dtype,
        )
        pipe = AnimateDiffPipeline.from_pretrained(
            model_id,
            motion_adapter=adapter,
            torch_dtype=dtype,
        )
        pipe.scheduler = DDIMScheduler.from_config(
            pipe.scheduler.config,
            clip_sample=False,
            timestep_spacing="linspace",
            beta_schedule="linear",
            steps_offset=1,
        )
        if hasattr(pipe, "vae"):
            pipe.vae.enable_slicing()
            if hasattr(pipe.vae, "enable_tiling"):
                pipe.vae.enable_tiling()
        pipe.enable_model_cpu_offload()

        _ANIMATEDIFF_PIPELINE = pipe
        _ANIMATEDIFF_MODEL = model_id
        return pipe


def _drop_animatediff_pipeline() -> None:
    global _ANIMATEDIFF_PIPELINE, _ANIMATEDIFF_MODEL
    with _ANIMATEDIFF_LOCK:
        pipe = _ANIMATEDIFF_PIPELINE
        _ANIMATEDIFF_PIPELINE = None
        _ANIMATEDIFF_MODEL = None
    if pipe is not None:
        try:
            if hasattr(pipe, "maybe_free_model_hooks"):
                pipe.maybe_free_model_hooks()
        except Exception:
            pass
        del pipe
    gc.collect()
    release_torch_cuda_cache()


def ai_video_status() -> dict:
    backend = ai_video_backend_name()
    native = native_video_status()

    if backend == "google":
        google = google_video_status()
        return {
            **google,
            "enabled": bool(settings.ai_video_enabled),
            "native_status": native,
            "cuda": False,
            "fallback": "ffmpeg-identity-motion",
        }

    available = False
    cuda = False
    reason = ""
    if backend == "native":
        available = bool(native.get("ready"))
        cuda = bool(native.get("cuda_available"))
        if not available:
            reason = "Mirai Native Video v0の学習済み重みがありません"
    elif backend == "animatediff":
        try:
            import torch
            from diffusers import AnimateDiffPipeline, MotionAdapter  # noqa: F401

            available = True
            cuda = bool(torch.cuda.is_available())
            if not cuda:
                reason = "CUDA未検出"
        except Exception as exc:
            reason = str(exc)
    else:
        reason = f"未対応backend: {backend}"

    return {
        "available": available,
        "cuda": cuda,
        "enabled": bool(settings.ai_video_enabled),
        "backend": backend,
        "native_status": native,
        "frames": max(4, min(int(settings.ai_video_frames), 16)),
        "steps": max(4, min(int(settings.ai_video_steps), 25)),
        "size": [
            max(256, min(int(settings.ai_video_width), 512)),
            max(256, min(int(settings.ai_video_height), 768)),
        ],
        "reason": reason,
    }

def _output_path(prefix: str = "ai_video") -> Path:
    ensure_dirs()
    folder = GENERATED_ROOT / "videos"
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    return folder / f"{prefix}_{stamp}.mp4"


def _frames_to_mp4(
    frames: list[Image.Image],
    output_path: Path,
    *,
    fps: int = 8,
) -> Path:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg が見つかりません")

    with tempfile.TemporaryDirectory(prefix="mirai_ai_video_") as tmp:
        tmp_dir = Path(tmp)
        for index, frame in enumerate(frames):
            frame.convert("RGB").save(
                tmp_dir / f"frame_{index:03d}.png",
                "PNG",
                compress_level=1,
                optimize=False,
            )

        run_video_encode(
            [
                ffmpeg,
                "-y",
                "-framerate",
                str(max(1, fps)),
                "-i",
                str(tmp_dir / "frame_%03d.png"),
            ],
            [
                "-pix_fmt",
                "yuv420p",
                "-movflags",
                "+faststart",
                str(output_path),
            ],
            crf=20,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    return output_path


def generate_motion_clip(
    image_path: str | Path,
    *,
    duration: float = 2.5,
    label: str = "motion",
) -> str:
    """
    低負荷の画像→動画。AIモデルを使わず、FFmpegで緩やかなズームを付ける。
    本番投稿の標準フォールバックとして使える。
    """
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg が見つかりません")

    source = Path(image_path)
    if not source.exists():
        raise FileNotFoundError(str(source))

    output = _output_path("motion")
    fps = 24
    frames = max(1, int(float(duration) * fps))
    zoompan = (
        "scale=720:1280:force_original_aspect_ratio=increase,"
        "crop=720:1280,"
        f"zoompan=z='min(zoom+0.0008,1.06)':"
        f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':"
        f"d={frames}:s=720x1280:fps={fps}"
    )
    run_video_encode(
        [
            ffmpeg,
            "-y",
            "-loop",
            "1",
            "-i",
            str(source),
            "-vf",
            zoompan,
            "-t",
            f"{float(duration):.2f}",
            "-an",
        ],
        [
            "-pix_fmt",
            "yuv420p",
            str(output),
        ],
        crf=20,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    record_asset(
        "motion_video",
        output,
        prompt=label,
        backend="ffmpeg-motion",
        meta={"source": str(source)},
    )
    return str(output)



def _generate_native_clip(prompt: str) -> str:
    source = Path(settings.mirai_reference_image)
    if not source.is_file():
        source = Path(settings.character_image)
    if not source.is_file():
        raise FileNotFoundError(
            "Native Video v0の開始画像がありません。"
        )
    with Image.open(source) as raw:
        start = raw.convert("RGB")
    frames = generate_native_video_frames(start, prompt)
    if not frames:
        raise RuntimeError("Mirai Native Video v0がframeを返しませんでした。")

    width = max(256, min(int(settings.ai_video_width), 512))
    height = max(256, min(int(settings.ai_video_height), 768))
    fitted = [
        ImageOps.fit(
            frame.convert("RGB"),
            (width, height),
            method=Image.Resampling.LANCZOS,
        )
        for frame in frames
    ]
    output = _output_path("mirai_native_video")
    _frames_to_mp4(fitted, output, fps=8)
    record_asset(
        "ai_video",
        output,
        prompt=prompt,
        backend="mirai-native-video-v0",
        meta={
            "frames": len(fitted),
            "width": width,
            "height": height,
            "pretrained_dependency": False,
        },
    )
    return str(output)


def generate_animatediff_clip(
    prompt: str,
    *,
    negative_prompt: str | None = None,
    image_path: str | Path | None = None,
    reference_images: list[str | Path] | None = None,
) -> str:
    """
    短いAI動画素材を1本だけ生成。
    GTX 1070向けに低解像度・少フレーム・CPUオフロードを前提にする。
    """
    backend = ai_video_backend_name()
    if backend == "google":
        return generate_google_veo_clip(
            prompt,
            image_path=image_path,
            reference_images=reference_images,
        )
    if backend == "native":
        return _generate_native_clip(prompt)
    if backend != "animatediff":
        raise RuntimeError(
            f"未対応のAI動画backend: {settings.ai_video_backend}"
        )

    try:
        import torch
        from diffusers import AnimateDiffPipeline, MotionAdapter  # noqa: F401
    except Exception as exc:
        raise RuntimeError(
            "AnimateDiffを利用できません。AI Studio依存関係を確認してください。"
        ) from exc

    if not torch.cuda.is_available():
        raise RuntimeError(
            "AI動画はCUDAが利用できないためスキップします。"
        )

    frames_count = max(4, min(int(settings.ai_video_frames), 16))
    steps = max(4, min(int(settings.ai_video_steps), 25))
    width = max(256, min(int(settings.ai_video_width), 512))
    height = max(256, min(int(settings.ai_video_height), 768))

    output = _output_path("animatediff")
    pipe = None

    with exclusive_gpu_task("AI動画生成"):
        try:
            # Fast modeではAnimateDiff本体を毎回from_pretrainedし直さず、
            # CPU側に保持して次回生成で再利用する。
            pipe = _load_animatediff_pipeline(torch)

            result = pipe(
                prompt=prompt,
                negative_prompt=(
                    negative_prompt
                    or NEGATIVE_PROMPT
                    or "low quality, worst quality"
                ),
                num_frames=frames_count,
                width=width,
                height=height,
                guidance_scale=6.5,
                num_inference_steps=steps,
                generator=torch.Generator("cpu").manual_seed(42),
            )
            frames = list(result.frames[0])
            if not frames:
                raise RuntimeError("AI動画フレームが生成されませんでした")

            _frames_to_mp4(frames, output, fps=8)
            record_asset(
                "ai_video",
                output,
                prompt=prompt,
                backend="animatediff",
                meta={
                    "frames": frames_count,
                    "steps": steps,
                    "width": width,
                    "height": height,
                },
            )
            return str(output)
        except Exception as exc:
            record_failure(
                "ai_video.generate",
                exc,
                {
                    "backend": "animatediff",
                    "frames": frames_count,
                    "steps": steps,
                    "width": width,
                    "height": height,
                },
            )
            raise
        finally:
            if pipe is not None:
                try:
                    if hasattr(pipe, "maybe_free_model_hooks"):
                        pipe.maybe_free_model_hooks()
                except Exception:
                    pass
            if not bool(getattr(settings, "media_fast_mode", True)):
                _drop_animatediff_pipeline()
            else:
                # パイプライン本体はCPU側に温存し、VRAMだけ次工程へ返す。
                gc.collect()
                release_torch_cuda_cache()
