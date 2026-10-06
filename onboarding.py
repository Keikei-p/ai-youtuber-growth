from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from paths import CLIENT_SECRET_FILE, TOKEN_FILE
from storage import get_channel_state, init_db, set_channel_state
from youtube.auth import get_credentials


SETUP_COMPLETE_KEY = "first_run_setup_complete"


def _client_secret_info() -> dict[str, Any]:
    if not CLIENT_SECRET_FILE.is_file():
        return {
            "configured": False,
            "valid": False,
            "detail": "YouTube OAuth設定ファイルが未登録です。",
        }

    try:
        raw = json.loads(
            CLIENT_SECRET_FILE.read_text(
                encoding="utf-8",
            )
        )
    except Exception:
        return {
            "configured": True,
            "valid": False,
            "detail": "YouTube OAuth設定ファイルを読み込めません。",
        }

    installed = raw.get("installed")
    if not isinstance(installed, dict):
        return {
            "configured": True,
            "valid": False,
            "detail": (
                "Google CloudのDesktop app用OAuth JSONを"
                "登録してください。"
            ),
        }

    required = (
        "client_id",
        "client_secret",
        "auth_uri",
        "token_uri",
    )
    missing = [
        key for key in required
        if not str(installed.get(key) or "").strip()
    ]
    if missing:
        return {
            "configured": True,
            "valid": False,
            "detail": (
                "OAuth JSONに必要な項目が不足しています: "
                + ", ".join(missing)
            ),
        }

    return {
        "configured": True,
        "valid": True,
        "detail": "YouTube OAuth設定ファイルを確認しました。",
    }


def youtube_auth_status() -> dict[str, Any]:
    client = _client_secret_info()
    token_present = TOKEN_FILE.is_file()
    ready = False
    detail = ""

    if token_present:
        try:
            get_credentials(interactive=False)
            ready = True
            detail = "YouTube認証済みです。"
        except Exception as exc:
            detail = (
                "保存済みYouTube認証を再確認してください。 "
                + str(exc)
            )
    elif client["valid"]:
        detail = "OAuth設定済み。YouTube接続を実行してください。"
    else:
        detail = str(client["detail"])

    return {
        "client_secret": client,
        "token_present": token_present,
        "ready": ready,
        "detail": detail,
    }


def onboarding_status() -> dict[str, Any]:
    init_db()
    youtube = youtube_auth_status()
    complete = (
        get_channel_state(
            SETUP_COMPLETE_KEY,
            "false",
        ).strip().lower()
        == "true"
    )
    return {
        "complete": complete,
        "youtube": youtube,
        "client_secret_path": str(
            CLIENT_SECRET_FILE.parent
        ),
        "token_path": str(
            TOKEN_FILE.parent
        ),
    }


def save_youtube_client_secret(
    payload: dict[str, Any],
) -> dict[str, Any]:
    installed = payload.get("installed")
    if not isinstance(installed, dict):
        raise ValueError(
            "Google CloudのDesktop app用OAuth JSONを選択してください。"
        )

    required = (
        "client_id",
        "client_secret",
        "auth_uri",
        "token_uri",
    )
    missing = [
        key for key in required
        if not str(installed.get(key) or "").strip()
    ]
    if missing:
        raise ValueError(
            "OAuth JSONに必要な項目が不足しています: "
            + ", ".join(missing)
        )

    # Google OAuth JSON以外の任意ファイル保存に使われないよう、
    # 必要な構造を確認してから原文JSONを保存する。
    CLIENT_SECRET_FILE.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    serialized = json.dumps(
        payload,
        ensure_ascii=False,
        indent=2,
    )
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=str(CLIENT_SECRET_FILE.parent),
            prefix="client_secret_",
            suffix=".tmp",
            delete=False,
        ) as handle:
            handle.write(serialized)
            temp_path = Path(handle.name)

        temp_path.replace(CLIENT_SECRET_FILE)
        try:
            os.chmod(CLIENT_SECRET_FILE, 0o600)
        except OSError:
            pass
    finally:
        if temp_path and temp_path.exists():
            temp_path.unlink(missing_ok=True)

    # OAuth設定を差し替えた時は古いtokenをそのまま使わない。
    if TOKEN_FILE.exists():
        TOKEN_FILE.unlink()

    set_channel_state(
        SETUP_COMPLETE_KEY,
        "false",
    )
    return onboarding_status()


def connect_youtube_interactive() -> dict[str, Any]:
    client = _client_secret_info()
    if not client["valid"]:
        raise RuntimeError(
            str(client["detail"])
        )

    get_credentials(interactive=True)
    status = youtube_auth_status()
    if not status["ready"]:
        raise RuntimeError(
            "YouTube認証完了を確認できませんでした。"
        )

    set_channel_state(
        SETUP_COMPLETE_KEY,
        "true",
    )
    return onboarding_status()


def disconnect_youtube(
    *,
    remove_client_secret: bool = False,
) -> dict[str, Any]:
    if TOKEN_FILE.exists():
        TOKEN_FILE.unlink()
    if (
        remove_client_secret
        and CLIENT_SECRET_FILE.exists()
    ):
        CLIENT_SECRET_FILE.unlink()

    set_channel_state(
        SETUP_COMPLETE_KEY,
        "false",
    )
    return onboarding_status()
