from __future__ import annotations
from pathlib import Path

from config import settings
from paths import AUDIO_DIR
from storage import clear_video_output

def cleanup_uploaded_media(video_id: int, output_path: str | None) -> list[Path]:
    """
    YouTubeアップロード成功後だけ呼び出す。
    MP4と同じstemのVOICEVOX WAVを削除し、DBのoutput_pathも空にする。
    """
    if not settings.cleanup_after_upload or not output_path:
        return []

    removed: list[Path] = []
    video_path = Path(output_path)

    candidates = [
        video_path,
        AUDIO_DIR / f"{video_path.stem}.wav",
    ]

    for path in candidates:
        try:
            if path.exists() and path.is_file():
                path.unlink()
                removed.append(path)
        except OSError as exc:
            print(f"[CLEANUP] 削除失敗: {path} / {exc}")

    if not video_path.exists():
        clear_video_output(video_id)

    if removed:
        print(
            "[CLEANUP] YouTube投稿済みローカル素材を削除: "
            + ", ".join(str(p) for p in removed)
        )

    return removed
