from __future__ import annotations

from runtime_control import guest_image_auto_enabled


def generate_guest_image(
    guest_id: int,
    visual_prompt: str,
) -> str | None:
    """
    AI Studioの画像生成エンジンを利用。
    自動生成がOFF、または生成環境が未導入なら動画生成自体は止めない。
    """
    if not guest_image_auto_enabled():
        return None

    try:
        from studio.image_generator import generate_guest_image_from_prompt

        path = generate_guest_image_from_prompt(
            guest_id=guest_id,
            visual_prompt=visual_prompt,
        )
        print(f"[GUEST-IMAGE] AI Studioで立ち絵生成: {path}")
        return path
    except Exception as exc:
        print(f"[GUEST-IMAGE] 画像生成をスキップ: {exc}")
        return None
