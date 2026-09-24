from __future__ import annotations
from pathlib import Path
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from youtube.auth import get_credentials

def upload_video(
    video_path:Path,
    title:str,
    description:str,
    privacy_status:str="private",
)->str:
    if not video_path.exists():
        raise FileNotFoundError(video_path)

    youtube=build("youtube","v3",credentials=get_credentials())
    body={
        "snippet":{
            "title":title[:100],
            "description":description,
            "categoryId":"22",
        },
        "status":{
            "privacyStatus":privacy_status,
            "selfDeclaredMadeForKids":False,
        },
    }
    media=MediaFileUpload(str(video_path),mimetype="video/mp4",resumable=True)
    request=youtube.videos().insert(part="snippet,status",body=body,media_body=media)
    response=request.execute()
    return response["id"]
