from __future__ import annotations

from datetime import date, timedelta
import re

from googleapiclient.discovery import build

from youtube.auth import get_credentials


def _analytics_service():
    return build(
        "youtubeAnalytics",
        "v2",
        credentials=get_credentials(interactive=False),
    )


def _date_range(days: int) -> tuple[str, str]:
    end = date.today() - timedelta(days=1)
    start = end - timedelta(days=max(days - 1, 0))
    return start.isoformat(), end.isoformat()


def _duration_seconds(value: str) -> float:
    text = str(value or "").upper()
    match = re.fullmatch(
        r"P(?:(?P<days>\d+)D)?T"
        r"(?:(?P<hours>\d+)H)?"
        r"(?:(?P<minutes>\d+)M)?"
        r"(?:(?P<seconds>[\d.]+)S)?",
        text,
    )
    if not match:
        return 0.0
    parts = match.groupdict()
    return (
        float(parts.get("days") or 0) * 86400
        + float(parts.get("hours") or 0) * 3600
        + float(parts.get("minutes") or 0) * 60
        + float(parts.get("seconds") or 0)
    )


def _fetch_live_statistics(video_id: str) -> dict:
    youtube = build(
        "youtube",
        "v3",
        credentials=get_credentials(interactive=False),
    )
    response = youtube.videos().list(
        part="statistics,contentDetails",
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
    content = items[0].get("contentDetails") or {}
    return {
        "views": int(stats.get("viewCount") or 0),
        "likes": int(stats.get("likeCount") or 0),
        "comments": int(stats.get("commentCount") or 0),
        "durationSeconds": _duration_seconds(
            str(content.get("duration") or "")
        ),
    }


def _query_single_row(
    *,
    video_id: str,
    days: int,
    metrics: str,
) -> dict:
    analytics = _analytics_service()
    start, end = _date_range(days)
    response = analytics.reports().query(
        ids="channel==MINE",
        startDate=start,
        endDate=end,
        metrics=metrics,
        filters=f"video=={video_id}",
    ).execute()

    rows = response.get("rows") or []
    if not rows:
        return {}
    headers = [h["name"] for h in response.get("columnHeaders") or []]
    return dict(zip(headers, rows[0]))


def _fetch_engagement(video_id: str, days: int = 28) -> dict:
    values = _query_single_row(
        video_id=video_id,
        days=days,
        metrics=(
            "estimatedMinutesWatched,averageViewDuration,"
            "averageViewPercentage,shares,subscribersGained,"
            "subscribersLost,engagedViews"
        ),
    )
    return {
        "estimatedMinutesWatched": float(
            values.get("estimatedMinutesWatched") or 0
        ),
        "averageViewDuration": float(
            values.get("averageViewDuration") or 0
        ),
        "averageViewPercentage": float(
            values.get("averageViewPercentage") or 0
        ),
        "shares": int(values.get("shares") or 0),
        "subscribersGained": int(
            values.get("subscribersGained") or 0
        ),
        "subscribersLost": int(
            values.get("subscribersLost") or 0
        ),
        "engagedViews": int(values.get("engagedViews") or 0),
    }


def _fetch_reach(video_id: str, days: int = 28) -> dict:
    values = _query_single_row(
        video_id=video_id,
        days=days,
        metrics=(
            "videoThumbnailImpressions,"
            "videoThumbnailImpressionsClickRate"
        ),
    )
    return {
        "impressions": int(
            values.get("videoThumbnailImpressions") or 0
        ),
        "ctr": float(
            values.get("videoThumbnailImpressionsClickRate") or 0
        ),
    }


def _fetch_traffic(video_id: str, days: int = 28) -> dict:
    analytics = _analytics_service()
    start, end = _date_range(days)
    response = analytics.reports().query(
        ids="channel==MINE",
        startDate=start,
        endDate=end,
        metrics="views,estimatedMinutesWatched",
        dimensions="insightTrafficSourceType",
        filters=f"video=={video_id}",
        sort="-views",
        maxResults=25,
    ).execute()
    headers = [h["name"] for h in response.get("columnHeaders") or []]
    rows = []
    for raw in response.get("rows") or []:
        item = dict(zip(headers, raw))
        rows.append({
            "source": str(
                item.get("insightTrafficSourceType") or "UNKNOWN"
            ),
            "views": int(item.get("views") or 0),
            "estimatedMinutesWatched": float(
                item.get("estimatedMinutesWatched") or 0
            ),
        })
    return {"trafficSources": rows}


def _fetch_retention_curve(
    video_id: str,
    *,
    days: int = 28,
    duration_seconds: float = 0.0,
) -> dict:
    analytics = _analytics_service()
    start, end = _date_range(days)
    response = analytics.reports().query(
        ids="channel==MINE",
        startDate=start,
        endDate=end,
        metrics=(
            "audienceWatchRatio,relativeRetentionPerformance,"
            "startedWatching,stoppedWatching,totalSegmentImpressions"
        ),
        dimensions="elapsedVideoTimeRatio",
        filters=f"video=={video_id}",
        sort="elapsedVideoTimeRatio",
        maxResults=200,
    ).execute()

    headers = [
        h["name"]
        for h in response.get("columnHeaders") or []
    ]
    curve: list[dict] = []
    for raw in response.get("rows") or []:
        row = dict(zip(headers, raw))
        curve.append({
            "ratio": float(
                row.get("elapsedVideoTimeRatio") or 0
            ),
            "audienceWatchRatio": float(
                row.get("audienceWatchRatio") or 0
            ),
            "relativeRetentionPerformance": float(
                row.get("relativeRetentionPerformance") or 0
            ),
            "startedWatching": float(
                row.get("startedWatching") or 0
            ),
            "stoppedWatching": float(
                row.get("stoppedWatching") or 0
            ),
            "totalSegmentImpressions": float(
                row.get("totalSegmentImpressions") or 0
            ),
        })

    def at_second(second: float) -> float:
        if not curve or duration_seconds <= 0:
            return 0.0
        target = max(
            0.01,
            min(1.0, float(second) / duration_seconds),
        )
        nearest = min(
            curve,
            key=lambda row: abs(
                float(row["ratio"]) - target
            ),
        )
        return float(
            nearest.get("audienceWatchRatio") or 0
        )

    return {
        "retentionCurve": curve,
        "retentionAt3Seconds": at_second(3),
        "retentionAt15Seconds": at_second(15),
    }


def fetch_video_metrics(video_id: str, days: int = 28) -> dict:
    metrics = _fetch_live_statistics(video_id)
    warnings: list[str] = []

    try:
        metrics.update(_fetch_engagement(video_id, days=days))
    except Exception as exc:
        metrics.update({
            "estimatedMinutesWatched": 0.0,
            "averageViewDuration": 0.0,
            "averageViewPercentage": 0.0,
            "shares": 0,
            "subscribersGained": 0,
            "subscribersLost": 0,
            "engagedViews": 0,
        })
        warnings.append("engagement: " + str(exc))

    try:
        metrics.update(_fetch_reach(video_id, days=days))
    except Exception as exc:
        metrics.update({
            "impressions": 0,
            "ctr": 0.0,
        })
        warnings.append("reach: " + str(exc))

    try:
        metrics.update(_fetch_traffic(video_id, days=days))
    except Exception as exc:
        metrics["trafficSources"] = []
        warnings.append("traffic: " + str(exc))

    try:
        metrics.update(
            _fetch_retention_curve(
                video_id,
                days=days,
                duration_seconds=float(
                    metrics.get("durationSeconds") or 0
                ),
            )
        )
    except Exception as exc:
        metrics.update({
            "retentionCurve": [],
            "retentionAt3Seconds": 0.0,
            "retentionAt15Seconds": 0.0,
        })
        warnings.append("retention_curve: " + str(exc))

    if warnings:
        metrics["analytics_warning"] = " | ".join(warnings)
    return metrics
