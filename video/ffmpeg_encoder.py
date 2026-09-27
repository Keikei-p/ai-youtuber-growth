from __future__ import annotations

import shutil
import subprocess
import threading

from config import settings

_STATE_LOCK = threading.Lock()
_NVENC_AVAILABLE: bool | None = None
_NVENC_DISABLED = False


def _preference() -> str:
    value = str(
        getattr(settings, "ffmpeg_video_encoder", "auto")
        or "auto"
    ).strip().lower()
    return value if value in {"auto", "nvenc", "x264"} else "auto"


def _ffmpeg_has_nvenc() -> bool:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return False
    try:
        completed = subprocess.run(
            [ffmpeg, "-hide_banner", "-encoders"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=8,
        )
        return "h264_nvenc" in (
            (completed.stdout or "") + (completed.stderr or "")
        )
    except Exception:
        return False


def nvenc_available() -> bool:
    global _NVENC_AVAILABLE

    preference = _preference()
    if preference == "x264" or _NVENC_DISABLED:
        return False

    with _STATE_LOCK:
        if _NVENC_AVAILABLE is not None:
            return bool(_NVENC_AVAILABLE)

        has_encoder = _ffmpeg_has_nvenc()
        if not has_encoder:
            _NVENC_AVAILABLE = False
            return False

        # autoではNVIDIAドライバが見える環境だけNVENCを選ぶ。
        # nvenc明示指定時はffmpeg側の実行可否に任せ、失敗時にx264へ退避。
        if (
            preference == "auto"
            and not shutil.which("nvidia-smi")
        ):
            _NVENC_AVAILABLE = False
            return False

        _NVENC_AVAILABLE = True
        return True


def encoder_args(*, crf: int = 18) -> list[str]:
    if nvenc_available():
        # GTX 10xx世代でも通りやすい互換寄り設定。
        return [
            "-c:v",
            "h264_nvenc",
            "-preset",
            "fast",
            "-rc",
            "vbr",
            "-cq",
            str(max(1, min(int(crf), 51))),
            "-b:v",
            "0",
        ]
    return [
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        str(max(1, min(int(crf), 51))),
    ]


def _disable_nvenc_after_failure() -> None:
    global _NVENC_AVAILABLE, _NVENC_DISABLED
    with _STATE_LOCK:
        _NVENC_AVAILABLE = False
        _NVENC_DISABLED = True


def run_video_encode(
    command_prefix: list[str],
    command_suffix: list[str],
    *,
    crf: int = 18,
    stdout=None,
    stderr=None,
) -> None:
    """
    ffmpeg動画エンコードをNVENC優先で実行し、GPU/driver/codec不整合なら
    そのプロセス中はlibx264へ自動退避する。
    """
    selected_nvenc = nvenc_available()
    command = [
        *command_prefix,
        *encoder_args(crf=crf),
        *command_suffix,
    ]
    try:
        subprocess.run(
            command,
            check=True,
            stdout=stdout,
            stderr=stderr,
        )
        return
    except subprocess.CalledProcessError:
        if not selected_nvenc:
            raise

    print(
        "[FFMPEG] NVENCを利用できなかったため"
        "libx264へ自動フォールバックします。"
    )
    _disable_nvenc_after_failure()
    fallback = [
        *command_prefix,
        *encoder_args(crf=crf),
        *command_suffix,
    ]
    subprocess.run(
        fallback,
        check=True,
        stdout=stdout,
        stderr=stderr,
    )
