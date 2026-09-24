from __future__ import annotations
import shutil
import subprocess
from pathlib import Path

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont

from config import settings

WIDTH = 1080
HEIGHT = 1920

def _font(size: int):
    candidates = [
        Path(settings.font_path),
        Path(r"C:\Windows\Fonts\meiryo.ttc"),
        Path(r"C:\Windows\Fonts\YuGothM.ttc"),
        Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    ]
    for path in candidates:
        if path.exists():
            return ImageFont.truetype(str(path), size=size)
    return ImageFont.load_default()

def _wrap(text: str, max_chars: int = 17) -> list[str]:
    text = text.strip()
    return [text[i:i + max_chars] for i in range(0, len(text), max_chars)] or [""]

def _chunks(script: str, max_chars: int = 34) -> list[str]:
    script = script.replace("。", "。|").replace("！", "！|").replace("？", "？|")
    pieces = [p.strip() for p in script.split("|") if p.strip()]
    out: list[str] = []
    buf = ""
    for piece in pieces:
        if len(buf) + len(piece) <= max_chars:
            buf += piece
        else:
            if buf:
                out.append(buf)
            buf = piece
    if buf:
        out.append(buf)
    return out or [script[:max_chars]]

def _gradient_background(seed: int) -> Image.Image:
    palettes = [
        ((16, 24, 45), (48, 38, 88)),
        ((17, 35, 46), (29, 76, 82)),
        ((32, 25, 44), (83, 41, 69)),
        ((22, 30, 42), (57, 67, 92)),
    ]
    top, bottom = palettes[seed % len(palettes)]
    img = Image.new("RGB", (WIDTH, HEIGHT))
    px = img.load()
    for y in range(HEIGHT):
        t = y / max(HEIGHT - 1, 1)
        c = tuple(int(top[i] * (1 - t) + bottom[i] * t) for i in range(3))
        for x in range(WIDTH):
            px[x, y] = c
    return img

def _cover(image: Image.Image, size: tuple[int, int]) -> Image.Image:
    target_w, target_h = size
    ratio = max(target_w / image.width, target_h / image.height)
    resized = image.resize(
        (int(image.width * ratio), int(image.height * ratio)),
        Image.Resampling.LANCZOS,
    )
    left = (resized.width - target_w) // 2
    top = (resized.height - target_h) // 2
    return resized.crop((left, top, left + target_w, top + target_h))

def _backgrounds() -> list[Path]:
    folders = [
        Path(settings.background_dir),
        Path("assets/generated/backgrounds"),
    ]
    exts = {".png", ".jpg", ".jpeg", ".webp"}
    files: list[Path] = []
    seen: set[str] = set()
    for folder in folders:
        if not folder.exists():
            continue
        for path in folder.iterdir():
            if path.suffix.lower() not in exts:
                continue
            key = str(path.resolve())
            if key not in seen:
                seen.add(key)
                files.append(path)
    return sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)

def _load_background(index: int) -> Image.Image:
    files = _backgrounds()
    if not files:
        return _gradient_background(index)

    path = files[index % len(files)]
    with Image.open(path) as raw:
        bg = _cover(raw.convert("RGB"), (WIDTH, HEIGHT))

    # 字幕が読みやすいように少し暗く・ぼかす
    bg = bg.filter(ImageFilter.GaussianBlur(radius=1.2))
    bg = ImageEnhance.Brightness(bg).enhance(0.58)
    return bg

def _character_image_path() -> Path | None:
    configured = Path(settings.character_image)
    if configured.exists():
        return configured

    generated = Path("assets/generated/mirai")
    if generated.exists():
        candidates = [
            path
            for path in generated.iterdir()
            if path.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}
        ]
        if candidates:
            return max(candidates, key=lambda path: path.stat().st_mtime)
    return None


def _paste_character(canvas: Image.Image) -> None:
    path = _character_image_path()
    if path is None:
        return

    with Image.open(path) as raw:
        char = raw.convert("RGBA")

    max_w, max_h = 720, 1050
    ratio = min(max_w / char.width, max_h / char.height, 1.0)
    char = char.resize(
        (max(1, int(char.width * ratio)), max(1, int(char.height * ratio))),
        Image.Resampling.LANCZOS,
    )

    x = WIDTH - char.width - 25
    y = 500
    canvas.alpha_composite(char, (x, y))

def _paste_guest(canvas: Image.Image, guest_image_path: str | None) -> None:
    if not guest_image_path:
        return

    path = Path(guest_image_path)
    if not path.exists():
        return

    with Image.open(path) as raw:
        guest = raw.convert("RGBA")

    max_w, max_h = 520, 900
    ratio = min(max_w / guest.width, max_h / guest.height, 1.0)
    guest = guest.resize(
        (max(1, int(guest.width * ratio)), max(1, int(guest.height * ratio))),
        Image.Resampling.LANCZOS,
    )

    x = 20
    y = 560
    canvas.alpha_composite(guest, (x, y))

def _make_frame(
    title: str,
    text: str,
    character_name: str,
    guest_name: str | None,
    guest_image_path: str | None,
    path: Path,
    index: int,
    total: int,
) -> None:
    base = _load_background(index).convert("RGBA")

    # 中央の視認性を確保
    overlay = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)
    od.rectangle((0, 0, WIDTH, HEIGHT), fill=(0, 0, 0, 28))
    base = Image.alpha_composite(base, overlay)

    _paste_character(base)
    _paste_guest(base, guest_image_path)

    draw = ImageDraw.Draw(base)
    title_font = _font(50)
    body_font = _font(72)
    small_font = _font(34)

    # 上部タイトル
    draw.rounded_rectangle(
        (55, 105, 1025, 300),
        radius=34,
        fill=(8, 12, 20, 186),
        outline=(255, 255, 255, 30),
        width=2,
    )
    for i, line in enumerate(_wrap(title, 21)[:2]):
        draw.text((95, 145 + i * 64), line, font=title_font, fill=(250, 250, 250, 255))

    # 字幕は下側に固定して、キャラや背景が見える余白を残す
    subtitle_lines = _wrap(text, 14)
    box_h = max(260, len(subtitle_lines) * 96 + 90)
    box_top = HEIGHT - box_h - 150
    draw.rounded_rectangle(
        (55, box_top, 1025, HEIGHT - 95),
        radius=38,
        fill=(5, 8, 14, 210),
        outline=(255, 255, 255, 34),
        width=2,
    )

    y = box_top + 45
    for line in subtitle_lines:
        bbox = draw.textbbox((0, 0), line, font=body_font)
        x = (WIDTH - (bbox[2] - bbox[0])) // 2
        draw.text(
            (x + 3, y + 4),
            line,
            font=body_font,
            fill=(0, 0, 0, 180),
        )
        draw.text((x, y), line, font=body_font, fill=(255, 255, 255, 255))
        y += 96

    draw.text(
        (70, 345),
        f"{character_name} / AI YouTuber",
        font=small_font,
        fill=(225, 230, 240, 235),
    )
    if guest_name:
        draw.rounded_rectangle(
            (65, 400, 590, 470),
            radius=22,
            fill=(8, 12, 20, 190),
        )
        draw.text(
            (90, 415),
            f"Guest: {guest_name}",
            font=small_font,
            fill=(255, 235, 170, 255),
        )
    draw.text(
        (900, 345),
        f"{index + 1}/{total}",
        font=small_font,
        fill=(190, 198, 214, 220),
    )

    path.parent.mkdir(parents=True, exist_ok=True)
    base.convert("RGB").save(path, quality=94)

def _audio_duration(audio_path: Path) -> float:
    if not shutil.which("ffprobe"):
        raise RuntimeError("ffprobe が見つかりません。FFmpegをインストールしてください。")
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(audio_path),
    ]
    return float(subprocess.check_output(cmd, text=True).strip())

def render_short(
    title: str,
    script: str,
    audio_path: Path,
    output_path: Path,
    character_name: str,
    guest_name: str | None = None,
    guest_image_path: str | None = None,
) -> Path:
    if not shutil.which("ffmpeg"):
        raise RuntimeError("ffmpeg が見つかりません。FFmpegをインストールしてください。")

    duration = max(_audio_duration(audio_path), 1.0)
    chunks = _chunks(script)
    per = duration / len(chunks)
    work = output_path.parent / f".frames_{output_path.stem}"
    work.mkdir(parents=True, exist_ok=True)

    frames: list[Path] = []
    for i, chunk in enumerate(chunks):
        frame = work / f"frame_{i:03d}.png"
        _make_frame(
            title,
            chunk,
            character_name,
            guest_name,
            guest_image_path,
            frame,
            i,
            len(chunks),
        )
        frames.append(frame)

    concat = work / "concat.txt"
    lines: list[str] = []
    for frame in frames:
        lines.append(f"file '{frame.resolve().as_posix()}'")
        lines.append(f"duration {per:.4f}")
    lines.append(f"file '{frames[-1].resolve().as_posix()}'")
    concat.write_text("\n".join(lines), encoding="utf-8")

    output_path.parent.mkdir(parents=True, exist_ok=True)

    bgm_path = Path(settings.bgm_file) if settings.bgm_file else None
    use_bgm = bool(bgm_path and bgm_path.exists())

    cmd = [
        "ffmpeg",
        "-y",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(concat),
        "-i",
        str(audio_path),
    ]

    if use_bgm:
        cmd += ["-stream_loop", "-1", "-i", str(bgm_path)]
        cmd += [
            "-filter_complex",
            "[1:a]volume=1.0[voice];[2:a]volume=0.09[bgm];"
            "[voice][bgm]amix=inputs=2:duration=first:dropout_transition=2[aout]",
            "-map",
            "0:v:0",
            "-map",
            "[aout]",
        ]

    cmd += [
        "-c:v",
        "libx264",
        "-preset",
        "medium",
        "-r",
        "30",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "160k",
        "-shortest",
        str(output_path),
    ]

    subprocess.run(cmd, check=True)
    shutil.rmtree(work, ignore_errors=True)
    return output_path
