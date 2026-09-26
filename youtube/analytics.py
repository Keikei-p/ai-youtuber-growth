from __future__ import annotations
from datetime import date, timedelta

from googleapiclient.discovery import build

from youtube.auth import get_credentials

def _fetch_live_statistics(video_id: str) -> dict:
    youtube = build("youtube", "v3", credentials=get_credentials(interactive=False))
    response = youtube.videos().list(
        part="statistics",
        id=video_id,
    ).execute()

    items = response.get("items") or []
    if not items:
        return {
            "views": 0,
            "likes": 0,
            "comments": 0,
        }

    stats = items[0].get("statistics") or {}
    return {
        "views": int(stats.get("viewCount") or 0),
        "likes": int(stats.get("likeCount") or 0),
        "comments": int(stats.get("commentCount") or 0),
    }

def _fetch_retention(video_id: str, days: int = 28) -> dict:
    analytics = build(
        "youtubeAnalytics",
        "v2",
        credentials=get_credentials(interactive=False),
    )

    end = date.today() - timedelta(days=1)
    start = end - timedelta(days=max(days - 1, 0))

    response = analytics.reports().query(
        ids="channel==MINE",
        startDate=start.isoformat(),
        endDate=end.isoformat(),
        metrics="averageViewDuration,averageViewPercentage",
        filters=f"video=={video_id}",
    ).execute()

    rows = response.get("rows") or []
    if not rows:
        return {
            "averageViewDuration": 0.0,
            "averageViewPercentage": 0.0,
        }

    headers = [h["name"] for h in response["columnHeaders"]]
    values = dict(zip(headers, rows[0]))
    return {
        "averageViewDuration": float(values.get("averageViewDuration") or 0),
        "averageViewPercentage": float(values.get("averageViewPercentage") or 0),
    }

def fetch_video_metrics(video_id: str, days: int = 28) -> dict:
    metrics = _fetch_live_statistics(video_id)

    try:
        metrics.update(_fetch_retention(video_id, days=days))
    except Exception as exc:
        # Analytics側は反映が遅れることがあるため、
        # Data APIの再生数等は残し、維持率だけ0で継続する。
        metrics["averageViewDuration"] = 0.0
        metrics["averageViewPercentage"] = 0.0
        metrics["analytics_warning"] = str(exc)

    return metrics
