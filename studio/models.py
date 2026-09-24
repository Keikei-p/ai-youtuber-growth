from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ImagePreset:
    width: int
    height: int
    steps: int = 24
    guidance_scale: float = 6.5
    sampler_name: str = "DPM++ 2M"


NEGATIVE_PROMPT = (
    "existing copyrighted character, celebrity, logo, watermark, text, "
    "nsfw, low quality, blurry, extra fingers, deformed hands, "
    "duplicate person, duplicate face"
)

# GTX 1070 8GBでも扱いやすい解像度を初期値にする。
MIRAI_PRESET = ImagePreset(width=512, height=768, steps=24, guidance_scale=6.5)
GUEST_PRESET = ImagePreset(width=512, height=768, steps=24, guidance_scale=6.5)
BACKGROUND_PRESET = ImagePreset(width=512, height=768, steps=22, guidance_scale=6.0)
