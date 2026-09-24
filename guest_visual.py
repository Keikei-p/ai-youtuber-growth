from __future__ import annotations
import base64
from pathlib import Path

import requests

from config import settings
from storage import set_guest_image

GUEST_ASSET_DIR = Path("assets/guests")

def generate_guest_image(
    guest_id: int,
    visual_prompt: str,
) -> str | None:
    """
    Stable Diffusion WebUI / Forge互換の /sdapi/v1/txt2img を利用。
    未導入・停止中ならNoneを返し、動画生成自体は止めない。
    """
    if not settings.guest_image_enabled:
        return None

    url = settings.sd_webui_url.rstrip("/") + "/sdapi/v1/txt2img"
    prompt = (
        visual_prompt
        + ", original character, anime VTuber, full body, front view, "
        "clean lineart, high detail, transparent-looking plain background, "
        "safe for work, no text, no logo"
    )
    negative = (
        "existing copyrighted character, celebrity, logo, watermark, text, "
        "nsfw, low quality, extra fingers, deformed hands, duplicate person"
    )

    try:
        response = requests.post(
            url,
            json={
                "prompt": prompt,
                "negative_prompt": negative,
                "steps": 24,
                "width": 768,
                "height": 1152,
                "cfg_scale": 6.5,
                "sampler_name": "DPM++ 2M",
                "batch_size": 1,
            },
            timeout=300,
        )
        response.raise_for_status()
        data = response.json()
        images = data.get("images") or []
        if not images:
            return None

        raw = images[0]
        if "," in raw:
            raw = raw.split(",", 1)[1]
        binary = base64.b64decode(raw)

        GUEST_ASSET_DIR.mkdir(parents=True, exist_ok=True)
        path = GUEST_ASSET_DIR / f"guest_{guest_id}.png"
        path.write_bytes(binary)
        set_guest_image(guest_id, str(path))
        print(f"[GUEST-IMAGE] 立ち絵生成: {path}")
        return str(path)
    except Exception as exc:
        print(f"[GUEST-IMAGE] 画像生成をスキップ: {exc}")
        return None
