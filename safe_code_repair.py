from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from storage import connect, set_channel_state


ROOT = Path(__file__).resolve().parent


def inspect_code_health(root: Path = ROOT) -> dict[str, Any]:
    issues: list[dict[str, str]] = []
    cycle = root / "automation" / "windows_cycle.ps1"
    install = root / "automation" / "install_windows_task.ps1"

    cycle_text = cycle.read_text(encoding="utf-8") if cycle.exists() else ""
    install_text = install.read_text(encoding="utf-8") if install.exists() else ""

    if "# MIRAI_DUE_FIRST" not in cycle_text:
        issues.append({
            "code": "wake_due_first_missing",
            "file": "automation/windows_cycle.ps1",
            "detail": "スリープ復帰後の投稿最優先マーカーがありません。",
        })
    if "WakeToRun" not in install_text:
        issues.append({
            "code": "wake_to_run_missing",
            "file": "automation/install_windows_task.ps1",
            "detail": "WakeToRun設定が見つかりません。",
        })
    if "# MIRAI_RECOVERY_TRIGGERS" not in install_text:
        issues.append({
            "code": "recovery_triggers_missing",
            "file": "automation/install_windows_task.ps1",
            "detail": "複数回復トリガーのマーカーがありません。",
        })

    return {
        "healthy": not issues,
        "issues": issues,
    }


def _git_clean(root: Path) -> bool:
    git = shutil.which("git")
    if not git or not (root / ".git").exists():
        return True
    completed = subprocess.run(
        [git, "status", "--porcelain"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    return completed.returncode == 0 and not completed.stdout.strip()


def _validate(root: Path) -> tuple[bool, str]:
    commands: list[list[str]] = [
        [
            sys.executable,
            "-m",
            "unittest",
            "tests.test_sleep_wake_scheduler",
            "-v",
        ],
    ]
    if os.name == "nt":
        powershell = shutil.which("powershell")
        if powershell:
            commands.append([
                powershell,
                "-NoProfile",
                "-Command",
                (
                    "[scriptblock]::Create((Get-Content -Raw "
                    "'automation/windows_cycle.ps1')) | Out-Null; "
                    "[scriptblock]::Create((Get-Content -Raw "
                    "'automation/install_windows_task.ps1')) | Out-Null"
                ),
            ])

    outputs: list[str] = []
    for command in commands:
        completed = subprocess.run(
            command,
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
            timeout=180,
        )
        outputs.append(
            ((completed.stdout or "") + "\n" + (completed.stderr or ""))[-3000:]
        )
        if completed.returncode != 0:
            return False, "\n".join(outputs)
    return True, "\n".join(outputs)


def _record(result: dict[str, Any]) -> None:
    with connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS code_repair_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                repair_id TEXT NOT NULL,
                status TEXT NOT NULL,
                detail_json TEXT NOT NULL DEFAULT '{}'
            )
            """
        )
        conn.execute(
            """
            INSERT INTO code_repair_events (
                created_at, repair_id, status, detail_json
            ) VALUES (?, ?, ?, ?)
            """,
            (
                datetime.now(timezone.utc).isoformat(timespec="seconds"),
                str(result.get("repair_id") or "known_invariants"),
                str(result.get("status") or "unknown"),
                json.dumps(result, ensure_ascii=False),
            ),
        )
    set_channel_state(
        "safe_code_repair_last",
        json.dumps(result, ensure_ascii=False),
    )


def repair_known_code_invariants(root: Path = ROOT) -> dict[str, Any]:
    """
    自動コード編集は既知の小さな不変条件だけに限定する。
    gitが汚れている時は編集せず、バックアップ→テスト→失敗時rollback。
    """
    health = inspect_code_health(root)
    if health["healthy"]:
        result = {
            "repair_id": "known_invariants",
            "status": "healthy",
            "changed": [],
            "issues": [],
        }
        if root == ROOT:
            _record(result)
        return result

    if not _git_clean(root):
        result = {
            "repair_id": "known_invariants",
            "status": "blocked_dirty_repo",
            "changed": [],
            "issues": health["issues"],
        }
        if root == ROOT:
            _record(result)
        return result

    cycle = root / "automation" / "windows_cycle.ps1"
    originals: dict[Path, str] = {}
    changed: list[str] = []

    try:
        issue_codes = {item["code"] for item in health["issues"]}
        if "wake_due_first_missing" in issue_codes and cycle.exists():
            text = cycle.read_text(encoding="utf-8")
            anchor = '    try {\n        Invoke-RestMethod -Uri "http://127.0.0.1:11434/api/tags"'
            patch = (
                '    # MIRAI_DUE_FIRST: wake後はAIサービスより投稿を最優先。\n'
                '    & $Python "scheduler.py" "--run-due" *>> $LogFile\n'
                '    if ($LASTEXITCODE -ne 0) {\n'
                '        throw "scheduler.py --run-due failed with exit code $LASTEXITCODE"\n'
                '    }\n\n'
            )
            if anchor in text:
                originals[cycle] = text
                cycle.write_text(
                    text.replace(anchor, patch + anchor, 1),
                    encoding="utf-8",
                )
                changed.append("automation/windows_cycle.ps1:due-first")
            else:
                result = {
                    "repair_id": "known_invariants",
                    "status": "anchor_not_found",
                    "changed": [],
                    "issues": health["issues"],
                }
                if root == ROOT:
                    _record(result)
                return result

        remaining = inspect_code_health(root)
        # WakeToRun/複数トリガーは構造変更が大きいため自動書換えしない。
        blocking = [
            item for item in remaining["issues"]
            if item["code"] != "wake_due_first_missing"
        ]
        if blocking:
            for path, original in originals.items():
                path.write_text(original, encoding="utf-8")
            result = {
                "repair_id": "known_invariants",
                "status": "needs_review",
                "changed": [],
                "issues": blocking,
            }
            if root == ROOT:
                _record(result)
            return result

        ok, validation = _validate(root)
        if not ok:
            for path, original in originals.items():
                path.write_text(original, encoding="utf-8")
            result = {
                "repair_id": "known_invariants",
                "status": "rolled_back",
                "changed": changed,
                "validation": validation,
            }
            if root == ROOT:
                _record(result)
            return result

        result = {
            "repair_id": "known_invariants",
            "status": "applied",
            "changed": changed,
            "validation": validation[-1200:],
        }
        if root == ROOT:
            _record(result)
        return result
    except Exception as exc:
        for path, original in originals.items():
            try:
                path.write_text(original, encoding="utf-8")
            except Exception:
                pass
        result = {
            "repair_id": "known_invariants",
            "status": "error_rollback",
            "changed": changed,
            "error": str(exc),
        }
        if root == ROOT:
            _record(result)
        return result
