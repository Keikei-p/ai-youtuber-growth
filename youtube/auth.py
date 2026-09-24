from __future__ import annotations
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from paths import CLIENT_SECRET_FILE, TOKEN_FILE

SCOPES=[
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
    "https://www.googleapis.com/auth/yt-analytics.readonly",
]

def get_credentials() -> Credentials:
    creds=None
    if TOKEN_FILE.exists():
        creds=Credentials.from_authorized_user_file(str(TOKEN_FILE),SCOPES)

    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        TOKEN_FILE.parent.mkdir(parents=True,exist_ok=True)
        TOKEN_FILE.write_text(creds.to_json(),encoding="utf-8")

    if not creds or not creds.valid:
        if not CLIENT_SECRET_FILE.exists():
            raise FileNotFoundError(
                f"{CLIENT_SECRET_FILE} がありません。Google CloudでYouTube API用OAuthクライアントを作成してください。"
            )
        flow=InstalledAppFlow.from_client_secrets_file(str(CLIENT_SECRET_FILE),SCOPES)
        creds=flow.run_local_server(port=0)
        TOKEN_FILE.parent.mkdir(parents=True,exist_ok=True)
        TOKEN_FILE.write_text(creds.to_json(),encoding="utf-8")

    return creds
