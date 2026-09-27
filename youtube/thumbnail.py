from __future__ import annotations

from pathlib import Path

from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

from youtube.auth import get_credentials


def set_custom_thumbnail(
    video_id: str,
    thumbnail_path: str | Path,
) -> dict:
    path = Path(thumbnail_path)
    if not path.is_file():
        raise FileNotFoundError(path)

    suffix = path.suffix.lower()
    mime = "image/png" if suffix == ".png" else "image/jpeg"
    youtube = build(
        "youtube",
        "v3",
        credentials=get_credentials(interactive=False),
    )
    response = youtube.thumbnails().set(
        videoId=str(video_id),
        media_body=MediaFileUpload(
            str(path),
            mimetype=mime,
            resumable=False,
        ),
    ).execute()

    items = (response or {}).get("items") or []
    return {
        "ok": bool(items),
        "items": items,
        "path": str(path),
    }
