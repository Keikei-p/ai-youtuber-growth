from __future__ import annotations

from pathlib import Path

from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

from youtube.auth import get_credentials


UPLOAD_RETRIES = 5


def upload_video(
    video_path: Path,
    title: str,
    description: str,
    tags: list[str] | None = None,
    privacy_status: str = "private",
    category_id: str = "22",
    default_language: str = "ja",
    contains_synthetic_media: bool = True,
) -> str:
    if not video_path.exists():
        raise FileNotFoundError(video_path)

    # 自動投稿ではブラウザ認証を絶対に待たない。
    # tokenが失効していれば明示エラーにして、次回の再認証へ回す。
    credentials = get_credentials(interactive=False)
    youtube = build("youtube", "v3", credentials=credentials)

    snippet = {
        "title": title[:100],
        "description": description,
        "tags": tags or [],
        "categoryId": category_id,
    }
    if default_language:
        snippet["defaultLanguage"] = default_language

    body = {
        "snippet": snippet,
        "status": {
            "privacyStatus": privacy_status,
            "selfDeclaredMadeForKids": False,
            "containsSyntheticMedia": bool(contains_synthetic_media),
        },
    }

    # YouTube/Google公式の再開可能アップロード経路。
    # chunksize=-1でも resumable session を使うため、通信断時は
    # next_chunk(num_retries=...) 側で指数バックオフ付き再試行が可能。
    media = MediaFileUpload(
        str(video_path),
        mimetype="video/mp4",
        chunksize=-1,
        resumable=True,
    )
    request = youtube.videos().insert(
        part="snippet,status",
        body=body,
        media_body=media,
    )

    response = None
    while response is None:
        status, response = request.next_chunk(
            num_retries=UPLOAD_RETRIES,
        )
        if status is not None:
            try:
                progress = int(status.progress() * 100)
                print(f"[YOUTUBE] upload progress {progress}%")
            except Exception:
                pass

    youtube_id = str((response or {}).get("id") or "").strip()
    if not youtube_id:
        raise RuntimeError(
            "YouTube APIは応答しましたが動画IDが返りませんでした。"
        )
    return youtube_id
