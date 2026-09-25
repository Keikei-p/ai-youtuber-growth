from __future__ import annotations
from pathlib import Path

from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

from youtube.auth import get_credentials

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

    youtube = build("youtube", "v3", credentials=get_credentials())

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

    media = MediaFileUpload(
        str(video_path),
        mimetype="video/mp4",
        resumable=True,
    )
    request = youtube.videos().insert(
        part="snippet,status",
        body=body,
        media_body=media,
    )
    response = request.execute()
    return response["id"]
