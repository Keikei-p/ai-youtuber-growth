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

def _load_background(
    index: int,
    background_image_path: str | None = None,
    background_image_paths: list[str] | None = None,
) -> Image.Image:
    candidates = [
        Path(path)
        for path in (background_image_paths or [])
        if path and Path(path).exists()
    ]
    if candidates:
        explicit = candidates[index % len(candidates)]
        with Image.open(explicit) as raw:
            bg = _cover(raw.convert("RGB"), (WIDTH, HEIGHT))
        bg = bg.filter(ImageFilter.GaussianBlur(radius=1.0))
        return ImageEnhance.Brightness(bg).enhance(0.64)

    if background_image_path:
        explicit = Path(background_image_path)
        if explicit.exists():
            with Image.open(explicit) as raw:
                bg = _cover(raw.convert("RGB"), (WIDTH, HEIGHT))
            bg = bg.filter(ImageFilter.GaussianBlur(radius=1.0))
            return ImageEnhance.Brightness(bg).enhance(0.64)

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


def _paste_character(
    canvas: Image.Image,
    character_image_path: str | None = None,
) -> None:
    path = (
        Path(character_image_path)
        if character_image_path and Path(character_image_path).exists()
        else _character_image_path()
    )
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
    character_image_path: str | None,
    background_image_path: str | None,
    background_image_paths: list[str] | None,
    path: Path,
    index: int,
    total: int,
) -> None:
    base = _load_background(
        index,
        background_image_path=background_image_path,
        background_image_paths=background_image_paths,
    ).convert("RGBA")

    # 中央の視認性を確保
    overlay = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)
    od.rectangle((0, 0, WIDTH, HEIGHT), fill=(0, 0, 0, 28))
    base = Image.alpha_composite(base, overlay)

    _paste_character(
        base,
        character_image_path=character_image_path,
    )
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

def _render_motion_segment(
    frame_path: Path,
    output_path: Path,
    duration: float,
    index: int,
) -> Path:
    """
    1枚の完成フレームに緩やかなカメラ移動を付ける。
    AI動画より圧倒的に軽く、Shortsの静止画感を減らす。
    """
    duration = max(float(duration), 0.35)
    frames = max(1, int(duration * 30))

    # シーンごとに少しだけ動きを変える。
    if index % 3 == 0:
        zoom = "min(zoom+0.00055,1.045)"
        x = "iw/2-(iw/zoom/2)"
        y = "ih/2-(ih/zoom/2)"
    elif index % 3 == 1:
        zoom = "min(zoom+0.00040,1.035)"
        x = "min(iw-iw/zoom,max(0,(iw-iw/zoom)*on/{frames}))".format(
            frames=max(frames - 1, 1)
        )
        y = "ih/2-(ih/zoom/2)"
    else:
        zoom = "min(zoom+0.00045,1.04)"
        x = "max(0,(iw-iw/zoom)*(1-on/{frames}))".format(
            frames=max(frames - 1, 1)
        )
        y = "ih/2-(ih/zoom/2)"

    vf = (
        f"zoompan=z='{zoom}':x='{x}':y='{y}':"
        f"d=1:s={WIDTH}x{HEIGHT}:fps=30,"
        "format=yuv420p"
    )

    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-loop",
            "1",
            "-framerate",
            "30",
            "-i",
            str(frame_path),
            "-vf",
            vf,
            "-t",
            f"{duration:.4f}",
            "-an",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "18",
            "-pix_fmt",
            "yuv420p",
            str(output_path),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return output_path


def render_short(
    title: str,
    script: str,
    audio_path: Path,
    output_path: Path,
    character_name: str,
    guest_name: str | None = None,
    guest_image_path: str | None = None,
    character_image_path: str | None = None,
    background_image_path: str | None = None,
    background_image_paths: list[str] | None = None,
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
            character_image_path,
            background_image_path,
            background_image_paths,
            frame,
            i,
            len(chunks),
        )
        frames.append(frame)

    # 静止画をそのまま並べず、各シーンに軽いカメラモーションを付ける。
    segments: list[Path] = []
    for i, frame in enumerate(frames):
        segment = work / f"segment_{i:03d}.mp4"
        _render_motion_segment(
            frame_path=frame,
            output_path=segment,
            duration=per,
            index=i,
        )
        segments.append(segment)

    concat = work / "concat.txt"
    concat.write_text(
        "\n".join(
            f"file '{segment.resolve().as_posix()}'"
            for segment in segments
        ),
        encoding="utf-8",
    )

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
            "[1:a]highpass=f=60,lowpass=f=15000,"
            "loudnorm=I=-14:TP=-1.5:LRA=7[voice];"
            "[2:a]volume=0.055[bgm];"
            "[voice][bgm]amix=inputs=2:duration=first:"
            "dropout_transition=2[aout]",
            "-map",
            "0:v:0",
            "-map",
            "[aout]",
        ]

    if not use_bgm:
        cmd += [
            "-filter:a",
            "highpass=f=60,lowpass=f=15000,"
            "loudnorm=I=-14:TP=-1.5:LRA=7",
        ]

    cmd += [
        "-c:v",
        "copy",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-movflags",
        "+faststart",
        "-shortest",
        str(output_path),
    ]

    subprocess.run(cmd, check=True)
    shutil.rmtree(work, ignore_errors=True)
    return output_path
