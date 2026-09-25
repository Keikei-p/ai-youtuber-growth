from __future__ import annotations
from pathlib import Path

from config import settings
from paths import AUDIO_DIR
from storage import clear_video_output, snapshot_exists, uploaded_videos

FINAL_LEARNING_HOURS = 168

def cleanup_uploaded_media(video_id: int, output_path: str | None) -> list[Path]:
    """
    YouTubeアップロード後または手動掃除から呼び出す。
    ただし7日(168h)学習が完了するまでは削除しない。
    学習完了後だけMP4と同じstemのWAVを削除し、DBのoutput_pathも空にする。
    """
    if not output_path:
        return []

    # 投稿直後には消さない。24h/72h/7d学習の最終168hが完了してから削除する。
    if not snapshot_exists(int(video_id), FINAL_LEARNING_HOURS):
        print(
            f"[CLEANUP] #{video_id} は7日学習前のため"
            "MP4/WAVを保持します。"
        )
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

def cleanup_all_uploaded_media(limit: int = 500) -> tuple[int, int]:
    """
    すでにYouTubeへ投稿済みで、ローカルに残っている素材を一括削除する。
    戻り値は (対象動画数, 削除ファイル数)。
    """
    rows = uploaded_videos(limit)
    target_count = 0
    removed_count = 0

    for row in rows:
        output_path = row.get("output_path")
        if not output_path:
            continue
        target_count += 1
        removed_count += len(
            cleanup_uploaded_media(
                video_id=int(row["id"]),
                output_path=str(output_path),
            )
        )

    return target_count, removed_count
