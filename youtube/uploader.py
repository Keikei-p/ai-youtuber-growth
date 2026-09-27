from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

from config import settings
from youtube.auth import get_credentials


UPLOAD_RETRIES = 5
_CHUNK_UNIT = 256 * 1024


def _upload_chunk_size() -> int:
    megabytes = max(
        1,
        min(
            int(
                getattr(
                    settings,
                    "youtube_upload_chunk_mb",
                    8,
                )
            ),
            64,
        ),
    )
    raw = megabytes * 1024 * 1024
    return max(
        _CHUNK_UNIT,
        (raw // _CHUNK_UNIT) * _CHUNK_UNIT,
    )


def _published_at(value: str) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(
            raw.replace("Z", "+00:00")
        )
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except Exception:
        return None


def find_recent_matching_upload(
    *,
    title: str,
    description: str,
    started_at: str | None = None,
    max_results: int = 25,
) -> str | None:
    """
    ローカルreceipt保存前にプロセスが落ちたケースを回収する。

    自分のUploads playlistの直近動画だけを確認し、
    title + description が完全一致する動画だけを採用する。
    新規uploadより先に使うことで二重投稿を避ける。
    """
    credentials = get_credentials(interactive=False)
    youtube = build(
        "youtube",
        "v3",
        credentials=credentials,
        cache_discovery=False,
    )

    channel_response = youtube.channels().list(
        part="contentDetails",
        mine=True,
        maxResults=1,
    ).execute()
    channels = channel_response.get("items") or []
    if not channels:
        return None

    related = (
        channels[0]
        .get("contentDetails", {})
        .get("relatedPlaylists", {})
    )
    uploads = str(related.get("uploads") or "").strip()
    if not uploads:
        return None

    now = datetime.now(timezone.utc)
    lookback = max(
        5,
        min(
            int(
                getattr(
                    settings,
                    "youtube_reconcile_lookback_minutes",
                    30,
                )
            ),
            180,
        ),
    )
    cutoff = now - timedelta(minutes=lookback)
    if started_at:
        parsed_started = _published_at(started_at)
        if parsed_started is not None:
            cutoff = min(
                cutoff,
                parsed_started - timedelta(minutes=2),
            )

    target_title = str(title or "")
    target_description = str(description or "")
    limit = max(1, min(int(max_results), 50))
    response = youtube.playlistItems().list(
        part="snippet,contentDetails",
        playlistId=uploads,
        maxResults=limit,
    ).execute()

    for item in response.get("items") or []:
        snippet = item.get("snippet") or {}
        published = _published_at(
            str(snippet.get("publishedAt") or "")
        )
        if published is not None and published < cutoff:
            continue
        if str(snippet.get("title") or "") != target_title:
            continue
        if (
            str(snippet.get("description") or "")
            != target_description
        ):
            continue
        video_id = str(
            (item.get("contentDetails") or {}).get("videoId")
            or (snippet.get("resourceId") or {}).get("videoId")
            or ""
        ).strip()
        if video_id:
            return video_id
    return None


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
    youtube = build(
        "youtube",
        "v3",
        credentials=credentials,
    )

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
    # 小分けchunkで送るため、一時的な通信断から復旧した時に
    # 大きなファイル全体を送り直すリスクを抑える。
    media = MediaFileUpload(
        str(video_path),
        mimetype="video/mp4",
        chunksize=_upload_chunk_size(),
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
