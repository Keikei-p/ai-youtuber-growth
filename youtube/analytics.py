from __future__ import annotations
from datetime import date,timedelta
from googleapiclient.discovery import build
from youtube.auth import get_credentials

def fetch_video_metrics(video_id:str,days:int=28)->dict:
    analytics=build("youtubeAnalytics","v2",credentials=get_credentials())
    end=date.today()-timedelta(days=1)
    start=end-timedelta(days=max(days-1,0))

    response=analytics.reports().query(
        ids="channel==MINE",
        startDate=start.isoformat(),
        endDate=end.isoformat(),
        metrics="views,likes,comments,averageViewDuration,averageViewPercentage",
        filters=f"video=={video_id}",
    ).execute()

    rows=response.get("rows") or []
    if not rows:
        return {
            "views":0,
            "likes":0,
            "comments":0,
            "averageViewDuration":0.0,
            "averageViewPercentage":0.0,
        }

    headers=[h["name"] for h in response["columnHeaders"]]
    return dict(zip(headers,rows[0]))
