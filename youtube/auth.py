from __future__ import annotations
from pathlib import Path
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

TOKEN_PATH=Path("token.json")
CLIENT_SECRET_PATH=Path("client_secret.json")

SCOPES=[
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
    "https://www.googleapis.com/auth/yt-analytics.readonly",
]

def get_credentials() -> Credentials:
    creds=None
    if TOKEN_PATH.exists():
        creds=Credentials.from_authorized_user_file(str(TOKEN_PATH),SCOPES)

    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())

    if not creds or not creds.valid:
        if not CLIENT_SECRET_PATH.exists():
            raise FileNotFoundError(
                "client_secret.json がありません。Google CloudでYouTube API用OAuthクライアントを作成してください。"
            )
        flow=InstalledAppFlow.from_client_secrets_file(str(CLIENT_SECRET_PATH),SCOPES)
        creds=flow.run_local_server(port=0)
        TOKEN_PATH.write_text(creds.to_json(),encoding="utf-8")

    return creds
