from __future__ import annotations

import re
from pathlib import Path

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont, ImageOps

from config import settings
from mirai_engines.visual_quality_engine import MiraiVisualQualityEngine
from studio.asset_store import GENERATED_ROOT, record_asset


WIDTH = 1280
HEIGHT = 720


def _font(size: int, *, bold: bool = False):
    candidates = [
        Path(settings.font_path),
        Path(r"C:\Windows\Fonts\meiryob.ttc" if bold else r"C:\Windows\Fonts\meiryo.ttc"),
        Path(r"C:\Windows\Fonts\YuGothB.ttc" if bold else r"C:\Windows\Fonts\YuGothM.ttc"),
        Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc" if bold else "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    ]
    for path in candidates:
        if path.exists():
            return ImageFont.truetype(str(path), size=size)
    return ImageFont.load_default()


def _short_text(title: str) -> str:
    value = re.sub(r"#[^\s]+", "", str(title or ""))
    value = re.sub(r"【[^】]*】", "", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value[:32] or "AIが自分で成長する"


def _wrap(text: str, max_chars: int = 12) -> list[str]:
    text = str(text or "").strip()
    return [
        text[i:i + max_chars]
        for i in range(0, len(text), max_chars)
    ][:3] or ["AI YouTuber"]


def _load_cover(path: str | None) -> Image.Image | None:
    if not path:
        return None
    candidate = Path(path)
    if not candidate.is_file():
        return None
    try:
        with Image.open(candidate) as raw:
            return raw.convert("RGB")
    except Exception:
        return None


def _cover(image: Image.Image, size: tuple[int, int]) -> Image.Image:
    return ImageOps.fit(
        image,
        size,
        method=Image.Resampling.LANCZOS,
        centering=(0.5, 0.5),
    )


def _background(item: dict) -> Image.Image:
    sources = [
        *(item.get("background_image_paths") or []),
        item.get("background_image_path"),
    ]
    for source in sources:
        image = _load_cover(source)
        if image:
            image = _cover(image, (WIDTH, HEIGHT))
            image = image.filter(ImageFilter.GaussianBlur(radius=1.1))
            return ImageEnhance.Brightness(image).enhance(0.68)

    top = Image.new("RGB", (WIDTH, HEIGHT), (18, 26, 45))
    draw = ImageDraw.Draw(top)
    for y in range(HEIGHT):
        t = y / max(HEIGHT - 1, 1)
        value = (
            int(18 + 25 * t),
            int(26 + 34 * t),
            int(45 + 50 * t),
        )
        draw.line((0, y, WIDTH, y), fill=value)
    return top


def _paste_character(canvas: Image.Image, item: dict, layout: int) -> None:
    source = _load_cover(item.get("character_image_path"))
    if source is None:
        return
    char = source.convert("RGBA")
    max_w = 640 if layout != 1 else 540
    max_h = 690
    ratio = min(max_w / char.width, max_h / char.height, 1.0)
    char = char.resize(
        (
            max(1, int(char.width * ratio)),
            max(1, int(char.height * ratio)),
        ),
        Image.Resampling.LANCZOS,
    )
    x = WIDTH - char.width - (10 if layout == 0 else 55)
    if layout == 2:
        x = (WIDTH - char.width) // 2
    y = HEIGHT - char.height
    canvas.alpha_composite(char, (x, y))


def _draw_candidate(item: dict, path: Path, layout: int) -> None:
    canvas = _background(item).convert("RGBA")
    shade = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
    sd = ImageDraw.Draw(shade)

    if layout == 0:
        sd.rectangle((0, 0, 760, HEIGHT), fill=(3, 7, 16, 178))
    elif layout == 1:
        sd.rectangle((0, 0, WIDTH, HEIGHT), fill=(3, 7, 16, 105))
        sd.rounded_rectangle((45, 75, 890, 645), radius=38, fill=(3, 7, 16, 185))
    else:
        sd.rectangle((0, 0, WIDTH, 280), fill=(3, 7, 16, 190))
        sd.rectangle((0, 520, WIDTH, HEIGHT), fill=(3, 7, 16, 150))

    canvas = Image.alpha_composite(canvas, shade)
    _paste_character(canvas, item, layout)

    draw = ImageDraw.Draw(canvas)
    title = _short_text(item.get("title") or "")
    lines = _wrap(title, 11 if layout != 2 else 13)
    title_font = _font(78 if layout != 2 else 72, bold=True)
    small = _font(34, bold=True)

    if layout == 2:
        y = 52
        for line in lines[:2]:
            box = draw.textbbox((0, 0), line, font=title_font)
            x = max(35, (WIDTH - (box[2] - box[0])) // 2)
            draw.text((x + 4, y + 4), line, font=title_font, fill=(0, 0, 0, 210))
            draw.text((x, y), line, font=title_font, fill=(255, 255, 255, 255))
            y += 92
    else:
        x = 65
        y = 130
        for line in lines:
            draw.text((x + 4, y + 4), line, font=title_font, fill=(0, 0, 0, 220))
            draw.text((x, y), line, font=title_font, fill=(255, 255, 255, 255))
            y += 98

    hook = ""
    idea = item.get("idea") or {}
    if isinstance(idea, dict):
        hook = str(idea.get("hook") or "")
    hook = re.sub(r"\s+", " ", hook).strip()[:34]
    if hook:
        draw.rounded_rectangle(
            (65, HEIGHT - 120, 840, HEIGHT - 52),
            radius=22,
            fill=(245, 205, 70, 230),
        )
        draw.text(
            (92, HEIGHT - 106),
            hook,
            font=small,
            fill=(15, 18, 26, 255),
        )

    path.parent.mkdir(parents=True, exist_ok=True)
    canvas.convert("RGB").save(path, format="JPEG", quality=94, optimize=True)


def build_thumbnail_candidates(item: dict) -> dict:
    folder = GENERATED_ROOT / "thumbnails"
    folder.mkdir(parents=True, exist_ok=True)
    video_id = int(item["id"])
    candidates: list[dict] = []
    engine = MiraiVisualQualityEngine()

    for layout in range(3):
        path = folder / f"video_{video_id}_thumb_{layout + 1}.jpg"
        _draw_candidate(item, path, layout)
        quality = engine.inspect_image(path, asset_type="thumbnail")
        title_len = len(_short_text(item.get("title") or ""))
        text_penalty = max(0, title_len - 26)
        score = max(
            0,
            min(
                100,
                int(quality.get("score") or 0)
                - text_penalty
                + (4 if layout == 0 else 0),
            ),
        )
        row = {
            "path": str(path),
            "layout": layout + 1,
            "score": score,
            "quality": quality,
        }
        candidates.append(row)
        record_asset(
            "thumbnail",
            path,
            f"video {video_id} thumbnail layout {layout + 1}",
            backend="mirai-thumbnail-composer-v1",
            meta={
                "video_id": video_id,
                "layout": layout + 1,
                "score": score,
            },
        )

    candidates.sort(key=lambda row: int(row["score"]), reverse=True)
    best = candidates[0]
    return {
        "path": best["path"],
        "score": best["score"],
        "candidates": candidates,
    }
