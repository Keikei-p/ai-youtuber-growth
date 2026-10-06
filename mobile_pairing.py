from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from datetime import datetime
from typing import Any

from storage import get_channel_state, init_db, set_channel_state


TOKEN_HASH_KEY = "mobile_pairing_token_hash"
TOKEN_META_KEY = "mobile_pairing_meta"


def _hash_token(token: str) -> str:
    return hashlib.sha256(
        str(token).encode("utf-8")
    ).hexdigest()


def pairing_status() -> dict[str, Any]:
    init_db()
    token_hash = get_channel_state(
        TOKEN_HASH_KEY,
        "",
    ).strip()
    raw = get_channel_state(
        TOKEN_META_KEY,
        "",
    ).strip()
    try:
        meta = json.loads(raw) if raw else {}
        if not isinstance(meta, dict):
            meta = {}
    except Exception:
        meta = {}

    return {
        "paired": bool(token_hash),
        "created_at": str(
            meta.get("created_at") or ""
        ),
        "revoked_at": str(
            meta.get("revoked_at") or ""
        ),
    }


def create_pairing_token() -> dict[str, Any]:
    """
    端末へ一度だけ渡す長いtokenを発行する。
    DBにはSHA-256だけ保存し、平文tokenは再取得できない。
    """
    init_db()
    token = secrets.token_urlsafe(32)
    now = datetime.now().astimezone().isoformat(
        timespec="seconds"
    )
    set_channel_state(
        TOKEN_HASH_KEY,
        _hash_token(token),
    )
    set_channel_state(
        TOKEN_META_KEY,
        json.dumps(
            {
                "created_at": now,
                "revoked_at": "",
            },
            ensure_ascii=False,
        ),
    )
    return {
        "token": token,
        "created_at": now,
        "paired": True,
    }


def revoke_pairing_token() -> dict[str, Any]:
    init_db()
    now = datetime.now().astimezone().isoformat(
        timespec="seconds"
    )
    set_channel_state(
        TOKEN_HASH_KEY,
        "",
    )
    set_channel_state(
        TOKEN_META_KEY,
        json.dumps(
            {
                "created_at": "",
                "revoked_at": now,
            },
            ensure_ascii=False,
        ),
    )
    return {
        "paired": False,
        "revoked_at": now,
    }


def verify_pairing_token(token: str) -> bool:
    init_db()
    expected = get_channel_state(
        TOKEN_HASH_KEY,
        "",
    ).strip()
    if not expected:
        return False
    actual = _hash_token(str(token or ""))
    return hmac.compare_digest(
        expected,
        actual,
    )


def bearer_token(header_value: str) -> str:
    raw = str(header_value or "").strip()
    if not raw.lower().startswith("bearer "):
        return ""
    return raw[7:].strip()
