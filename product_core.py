from __future__ import annotations

import importlib.util
import os
import platform
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from runtime_control import execution_mode
from storage import get_channel_state, init_db


PRODUCT_NAME = "Mirai Production OS"
PRODUCT_VERSION = "0.11.0-beta"
PRODUCT_CHANNEL = "beta"
PRODUCT_EDITION = "creator"


def _check(
    key: str,
    label: str,
    ok: bool,
    detail: str = "",
    *,
    required_for_sale: bool = True,
) -> dict[str, Any]:
    return {
        "key": key,
        "label": label,
        "ok": bool(ok),
        "detail": str(detail),
        "required_for_sale": bool(required_for_sale),
    }


def product_readiness(root: Path) -> dict[str, Any]:
    root = Path(root)
    checks = [
        _check(
            "base_runtime",
            "基本Pythonランタイム",
            all(
                importlib.util.find_spec(name) is not None
                for name in ("requests", "dotenv", "PIL")
            ),
            "requests / python-dotenv / Pillow",
        ),
        _check(
            "ffmpeg",
            "FFmpeg",
            bool(shutil.which("ffmpeg")),
            shutil.which("ffmpeg") or "未検出",
        ),
        _check(
            "third_party_notices",
            "第三者ライセンス表記",
            (root / "THIRD_PARTY_NOTICES.md").is_file(),
            "販売物へ同梱必須",
        ),
        _check(
            "privacy_policy",
            "プライバシーポリシー",
            (root / "docs" / "PRIVACY_POLICY_DRAFT.md").is_file(),
            "現段階はドラフト。公開前に事業者情報を確定する。",
        ),
        _check(
            "terms",
            "利用規約",
            (root / "docs" / "TERMS_DRAFT.md").is_file(),
            "現段階はドラフト。公開前に法務確認を推奨。",
        ),
        _check(
            "release_checklist",
            "販売前チェックリスト",
            (root / "docs" / "PRODUCT_RELEASE_CHECKLIST.md").is_file(),
        ),
        _check(
            "windows_packaging",
            "Windows配布ビルド",
            (root / "packaging" / "build_windows.ps1").is_file(),
        ),
        _check(
            "support_diagnostics",
            "安全な診断情報",
            True,
            "APIキー・OAuth token・secret本文を返さない設計",
        ),
        _check(
            "mobile_source",
            "iOS / Androidアプリソース",
            (
                (root / "mobile" / "package.json").is_file()
                and (root / "mobile" / "www" / "app.js").is_file()
                and (root / "mobile" / "capacitor.config.json").is_file()
            ),
            "Capacitor 8の軽量コントローラー。ストア署名済みバイナリは未作成。",
            required_for_sale=False,
        ),
        _check(
            "billing_license",
            "課金・ライセンス管理",
            False,
            "販売プラン確定後にStripe/StoreKit/Play Billing等を接続。",
            required_for_sale=False,
        ),
    ]

    required = [
        item for item in checks
        if item["required_for_sale"]
    ]
    passed = sum(1 for item in required if item["ok"])
    score = int(round((passed / max(len(required), 1)) * 100))

    blockers = [
        item for item in required
        if not item["ok"]
    ]
    return {
        "product": PRODUCT_NAME,
        "version": PRODUCT_VERSION,
        "channel": PRODUCT_CHANNEL,
        "edition": PRODUCT_EDITION,
        "score": score,
        "required_passed": passed,
        "required_total": len(required),
        "blockers": blockers,
        "checks": checks,
        "store_submission_ready": False,
        "commercial_note": (
            "技術販売基盤の準備度。法務・税務・ストア審査・"
            "決済契約の完了を保証する指標ではありません。"
        ),
    }


def product_status(root: Path) -> dict[str, Any]:
    init_db()
    return {
        "name": PRODUCT_NAME,
        "version": PRODUCT_VERSION,
        "channel": PRODUCT_CHANNEL,
        "edition": PRODUCT_EDITION,
        "execution_mode": execution_mode(),
        "readiness": product_readiness(root),
        "autopilot_armed": (
            get_channel_state(
                "production_autonomy_armed",
                "false",
            ).strip().lower()
            == "true"
        ),
    }


def safe_support_snapshot(root: Path) -> dict[str, Any]:
    """
    ユーザーがサポートへ共有できる診断情報。
    .env / OAuth token / API key / secret値は一切読み込まない。
    """
    init_db()
    keys = (
        "scheduler_last_run_due_at",
        "scheduler_last_tick_at",
        "autonomous_recovery_last",
        "runtime_bootstrap_last",
        "autopost_catchup_last",
        "full_autopilot_last_event",
        "youtube_auth_attention",
        "production_autonomy_armed",
        "automation_enabled",
        "auto_upload_enabled",
    )
    state: dict[str, str] = {}
    for key in keys:
        value = get_channel_state(key, "")
        # 状態JSONにも外部secretを書かない設計だが、長大ログ化を防ぐ。
        state[key] = str(value)[:4000]

    return {
        "generated_at": datetime.now().astimezone().isoformat(
            timespec="seconds"
        ),
        "product": {
            "name": PRODUCT_NAME,
            "version": PRODUCT_VERSION,
            "channel": PRODUCT_CHANNEL,
            "edition": PRODUCT_EDITION,
        },
        "system": {
            "platform": platform.system(),
            "platform_release": platform.release(),
            "python": sys.version.split()[0],
            "machine": platform.machine(),
            "execution_mode": execution_mode(),
        },
        "state": state,
        "secrets_included": False,
        "privacy": (
            "APIキー、OAuth token、client_secret、.env本文は含みません。"
        ),
    }
