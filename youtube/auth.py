from __future__ import annotations

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

from paths import CLIENT_SECRET_FILE, TOKEN_FILE

SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
    "https://www.googleapis.com/auth/yt-analytics.readonly",
]


def _save_credentials(creds: Credentials) -> None:
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_FILE.write_text(creds.to_json(), encoding="utf-8")


def get_credentials(*, interactive: bool = True) -> Credentials:
    """
    YouTube OAuth credentialsを取得する。

    自動投稿/Windowsタスクからは interactive=False を使い、
    認証失効時にバックグラウンドでブラウザ待ちにならないようにする。
    明示的な youtube_connect.py 実行時だけ interactive=True で再認証する。
    """
    creds = None
    if TOKEN_FILE.exists():
        try:
            creds = Credentials.from_authorized_user_file(
                str(TOKEN_FILE),
                SCOPES,
            )
        except (OSError, ValueError) as exc:
            if not interactive:
                raise RuntimeError(
                    "保存済みYouTube認証(token.json)を読み込めません。"
                    " python youtube_connect.py を実行して再認証してください。"
                ) from exc
            creds = None

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            _save_credentials(creds)
        except Exception as exc:
            if not interactive:
                raise RuntimeError(
                    "YouTube認証の更新に失敗しました。"
                    " Google側で権限が失効した可能性があります。"
                    " python youtube_connect.py を実行して再認証してください。"
                ) from exc
            creds = None

    if creds and creds.valid:
        return creds

    if not interactive:
        if not TOKEN_FILE.exists():
            raise FileNotFoundError(
                f"{TOKEN_FILE} がありません。"
                " python youtube_connect.py を実行してYouTube認証を完了してください。"
            )
        raise RuntimeError(
            "保存済みYouTube認証が無効です。"
            " python youtube_connect.py を実行して再認証してください。"
        )

    if not CLIENT_SECRET_FILE.exists():
        raise FileNotFoundError(
            f"{CLIENT_SECRET_FILE} がありません。"
            " Google CloudでYouTube API用OAuthクライアントを作成してください。"
        )

    flow = InstalledAppFlow.from_client_secrets_file(
        str(CLIENT_SECRET_FILE),
        SCOPES,
    )
    creds = flow.run_local_server(port=0)
    _save_credentials(creds)
    return creds
