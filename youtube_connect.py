from __future__ import annotations

from googleapiclient.discovery import build

from paths import CLIENT_SECRET_FILE, TOKEN_FILE
from youtube.auth import get_credentials

def main() -> None:
    print("=== YouTube OAuth 接続 ===")
    print(f"client secret: {CLIENT_SECRET_FILE}")
    print(f"token: {TOKEN_FILE}")

    creds = get_credentials()
    youtube = build("youtube", "v3", credentials=creds)

    response = youtube.channels().list(
        part="snippet",
        mine=True,
    ).execute()

    items = response.get("items") or []
    if not items:
        print("[NG] 認証できましたが、YouTubeチャンネルが見つかりません。")
        return

    channel = items[0]
    title = channel.get("snippet", {}).get("title", "")
    channel_id = channel.get("id", "")

    print()
    print("[OK] YouTube接続成功")
    print(f"チャンネル名: {title}")
    print(f"チャンネルID: {channel_id}")
    print(f"token保存先: {TOKEN_FILE}")
    print()
    print("このtoken.jsonはGitHubへアップロードしないでください。")
    print("将来サーバーへ移すときは、安全な秘密ファイルとして移行します。")

if __name__ == "__main__":
    main()
