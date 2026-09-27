from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from config import settings
from runtime_control import (
    automation_enabled,
    auto_upload_enabled,
    post_times,
    upload_privacy,
)
from storage import (
    get_channel_state,
    init_db,
    queued_items,
)
from upload_recovery import recent_recovery_events


ROOT = Path(__file__).resolve().parent


def _load_json_file(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        value = json.loads(
            path.read_text(
                encoding="utf-8-sig",
                errors="replace",
            )
        )
        return value if isinstance(value, dict) else {}
    except Exception as exc:
        return {
            "status": "invalid",
            "stage": "status_file",
            "detail": f"状態ファイルを読めません: {exc}",
        }


def read_cycle_status(root: Path = ROOT) -> dict[str, Any]:
    return _load_json_file(
        root / "automation" / "last_cycle_status.json"
    )


def read_auto_post_event() -> dict[str, Any]:
    raw = get_channel_state(
        "auto_post_last_event",
        "",
    ).strip()
    if not raw:
        return {}
    try:
        value = json.loads(raw)
        return value if isinstance(value, dict) else {}
    except Exception:
        return {
            "status": "invalid",
            "detail": "auto_post_last_event JSONを読めません。",
        }


def read_automation_log_tail(
    root: Path = ROOT,
    *,
    max_chars: int = 12000,
) -> str:
    log_dir = root / "logs"
    if not log_dir.is_dir():
        return ""
    files = sorted(
        log_dir.glob("automation-*.log"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    if not files:
        return ""
    try:
        text = files[0].read_text(
            encoding="utf-8",
            errors="replace",
        )
        return text[-max(1000, int(max_chars)):]
    except Exception as exc:
        return f"automation log read failed: {exc}"


def windows_task_status(
    root: Path = ROOT,
    *,
    platform_name: str | None = None,
) -> dict[str, Any]:
    platform_name = platform_name or os.name
    if platform_name != "nt":
        return {
            "supported": False,
            "registered": False,
            "state": "not_windows",
        }

    powershell = (
        shutil.which("powershell.exe")
        or shutil.which("powershell")
    )
    script = root / "automation" / "windows_task_status.ps1"
    if not powershell:
        return {
            "supported": True,
            "registered": False,
            "state": "powershell_missing",
            "error": "PowerShellが見つかりません。",
        }
    if not script.is_file():
        return {
            "supported": True,
            "registered": False,
            "state": "diagnostic_script_missing",
            "error": str(script),
        }

    try:
        completed = subprocess.run(
            [
                powershell,
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(script),
                "-Json",
            ],
            cwd=root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=25,
            creationflags=(
                subprocess.CREATE_NO_WINDOW
                if os.name == "nt"
                else 0
            ),
        )
    except Exception as exc:
        return {
            "supported": True,
            "registered": False,
            "state": "diagnostic_failed",
            "error": str(exc),
        }

    output = (completed.stdout or "").strip()
    if completed.returncode != 0:
        return {
            "supported": True,
            "registered": False,
            "state": "diagnostic_failed",
            "error": (
                (completed.stderr or output)[-3000:]
                or f"exit={completed.returncode}"
            ),
        }

    # PowerShellが警告を混ぜても最後のJSON行を優先する。
    candidates = [
        line.strip()
        for line in output.splitlines()
        if line.strip().startswith("{")
        and line.strip().endswith("}")
    ]
    raw = candidates[-1] if candidates else output
    try:
        value = json.loads(raw)
        if isinstance(value, dict):
            value["supported"] = True
            return value
    except Exception:
        pass

    return {
        "supported": True,
        "registered": False,
        "state": "invalid_diagnostic_output",
        "error": output[-3000:],
    }


def _next_queue_item() -> dict[str, Any] | None:
    rows = queued_items()
    if not rows:
        return None
    rows = sorted(
        rows,
        key=lambda row: str(row.get("scheduled_for") or ""),
    )
    row = rows[0]
    return {
        "video_id": int(row.get("video_id") or 0),
        "title": str(row.get("title") or ""),
        "scheduled_for": row.get("scheduled_for"),
        "attempts": int(row.get("attempts") or 0),
        "error": row.get("error"),
        "status": row.get("status"),
    }


def collect_auto_post_health(
    root: Path = ROOT,
    *,
    platform_name: str | None = None,
) -> dict[str, Any]:
    init_db()
    platform_name = platform_name or os.name
    cycle = read_cycle_status(root)
    event = read_auto_post_event()
    task = windows_task_status(
        root,
        platform_name=platform_name,
    )
    production_armed = (
        get_channel_state(
            "production_autonomy_armed",
            "false",
        ).strip().lower()
        == "true"
    )

    problems: list[dict[str, str]] = []
    warnings: list[dict[str, str]] = []

    if not automation_enabled():
        problems.append({
            "code": "automation_off",
            "detail": "Web自動運転がOFFです。",
        })
    if not auto_upload_enabled():
        problems.append({
            "code": "auto_upload_off",
            "detail": "YouTube自動投稿がOFFです。",
        })
    if not production_armed:
        problems.append({
            "code": "production_not_armed",
            "detail": "本番自動投稿が明示ON状態ではありません。",
        })

    if platform_name == "nt":
        if not task.get("registered"):
            problems.append({
                "code": "wake_task_missing",
                "detail": "Windowsスリープ復帰タスクが登録されていません。",
            })
        else:
            if not task.get("wake_to_run"):
                problems.append({
                    "code": "wake_to_run_off",
                    "detail": "WindowsタスクのWakeToRunがOFFです。",
                })
            if not task.get("start_when_available"):
                warnings.append({
                    "code": "start_when_available_off",
                    "detail": "取りこぼし後のStartWhenAvailableがOFFです。",
                })
            if not task.get("enabled", True):
                problems.append({
                    "code": "wake_task_disabled",
                    "detail": "Windowsタスク自体が無効です。",
                })
            if not task.get("wake_timer_present"):
                warnings.append({
                    "code": "wake_timer_not_visible",
                    "detail": (
                        "powercfgで有効なスリープ解除タイマーを"
                        "確認できません。PC/BIOS側設定の影響もあります。"
                    ),
                })

    cycle_status = str(cycle.get("status") or "")
    if cycle_status == "failed":
        problems.append({
            "code": "last_wake_cycle_failed",
            "detail": str(
                cycle.get("detail")
                or "直近のスリープ復帰サイクルが失敗しました。"
            ),
        })
    elif cycle_status == "retry_wait":
        warnings.append({
            "code": "last_wake_cycle_retry_wait",
            "detail": str(
                cycle.get("detail")
                or "直近は復旧待ちで終了しました。"
            ),
        })

    event_status = str(event.get("status") or "")
    if event_status in {"attention", "blocked"}:
        problems.append({
            "code": str(event.get("code") or "auto_post_attention"),
            "detail": str(
                event.get("detail")
                or "直近の自動投稿に確認が必要です。"
            ),
        })
    elif event_status == "recovery_wait":
        warnings.append({
            "code": str(event.get("code") or "auto_post_recovery"),
            "detail": str(
                event.get("detail")
                or "自動復旧の再試行待ちです。"
            ),
        })

    next_item = _next_queue_item()
    recoveries = recent_recovery_events(12)

    if problems:
        overall = "attention"
    elif cycle_status == "running" or event_status == "attempting":
        overall = "running"
    elif warnings:
        overall = "warning"
    else:
        overall = "ready"

    return {
        "overall": overall,
        "ready": overall in {"ready", "warning"},
        "automation_enabled": automation_enabled(),
        "auto_upload_enabled": auto_upload_enabled(),
        "production_armed": production_armed,
        "dry_run": bool(getattr(settings, "dry_run", False)),
        "privacy": upload_privacy(),
        "post_times": post_times(),
        "next_queue": next_item,
        "cycle": cycle,
        "last_post_event": event,
        "windows_task": task,
        "problems": problems,
        "warnings": warnings,
        "recent_recoveries": recoveries,
        "automation_log_tail": read_automation_log_tail(root),
    }
