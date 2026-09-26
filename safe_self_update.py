from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from safe_code_repair import inspect_code_health
from storage import get_channel_state, set_channel_state


ROOT = Path(__file__).resolve().parent


def _run(args: list[str], *, cwd: Path = ROOT, timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(
        args,
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )


def _save(result: dict[str, Any]) -> dict[str, Any]:
    result["checked_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        set_channel_state(
            "safe_self_update_last",
            json.dumps(result, ensure_ascii=False),
        )
    except Exception:
        pass
    return result


def _porcelain_files(output: str) -> list[str]:
    files: list[str] = []
    for raw in str(output or "").splitlines():
        if len(raw) < 4:
            continue
        path = raw[3:].strip()
        if " -> " in path:
            path = path.split(" -> ", 1)[1].strip()
        if path:
            files.append(path.replace("\\", "/"))
    return files


def _known_repair_dirty_files(output: str) -> list[str]:
    files = _porcelain_files(output)
    if not files:
        return []

    allowed = {"automation/windows_cycle.ps1"}
    if not set(files).issubset(allowed):
        return []

    try:
        payload = json.loads(
            get_channel_state("safe_code_repair_last", "") or "{}"
        )
    except Exception:
        payload = {}

    if str(payload.get("status") or "") != "applied":
        return []
    changed = [str(x) for x in (payload.get("changed") or [])]
    if not any(
        item.startswith("automation/windows_cycle.ps1:")
        for item in changed
    ):
        return []
    return files


def safe_self_update(root: Path = ROOT) -> dict[str, Any]:
    """
    origin/main の更新を別worktreeで検証してからfast-forwardする。
    ユーザーのローカル変更は触らない。
    Mirai自身の既知safe repairだけは一時stashして更新と両立させる。
    """
    git = shutil.which("git")
    if not git:
        return _save({"status": "skipped", "reason": "git_not_found"})

    if not (root / ".git").exists():
        return _save({"status": "skipped", "reason": "not_git_repo"})

    branch = _run([git, "rev-parse", "--abbrev-ref", "HEAD"], cwd=root)
    current_branch = branch.stdout.strip()
    if branch.returncode != 0 or current_branch != "main":
        return _save({
            "status": "skipped",
            "reason": "not_main_branch",
            "branch": current_branch,
        })

    repair_stash_ref = ""

    def restore_repair_stash(result: dict[str, Any]) -> dict[str, Any]:
        nonlocal repair_stash_ref
        if repair_stash_ref:
            restored = _run(
                [git, "stash", "pop", repair_stash_ref],
                cwd=root,
                timeout=120,
            )
            result["repair_stash_restored"] = restored.returncode == 0
            if restored.returncode != 0:
                result["repair_stash_restore_error"] = (
                    (restored.stderr or restored.stdout)[-2000:]
                )
            repair_stash_ref = ""
        return _save(result)

    status = _run([git, "status", "--porcelain"], cwd=root)
    if status.returncode != 0:
        return _save({"status": "skipped", "reason": "git_status_failed"})

    if status.stdout.strip():
        repair_files = _known_repair_dirty_files(status.stdout)
        if not repair_files:
            return _save({
                "status": "skipped",
                "reason": "local_changes_present",
                "detail": status.stdout[-2000:],
            })

        stash = _run(
            [
                git,
                "stash",
                "push",
                "-m",
                "mirai-safe-repair-autostash",
                "--",
                *repair_files,
            ],
            cwd=root,
            timeout=120,
        )
        if stash.returncode != 0:
            return _save({
                "status": "blocked",
                "reason": "safe_repair_stash_failed",
                "detail": (stash.stderr or stash.stdout)[-2000:],
            })
        stash_ref = _run(
            [git, "stash", "list", "-1", "--format=%gd"],
            cwd=root,
        )
        repair_stash_ref = stash_ref.stdout.strip()
        if not repair_stash_ref:
            return _save({
                "status": "blocked",
                "reason": "safe_repair_stash_ref_missing",
            })

    before = _run([git, "rev-parse", "HEAD"], cwd=root)
    if before.returncode != 0:
        return restore_repair_stash({
            "status": "error",
            "reason": "head_read_failed",
        })
    before_sha = before.stdout.strip()

    fetch = _run(
        [git, "fetch", "--quiet", "origin", "main"],
        cwd=root,
        timeout=180,
    )
    if fetch.returncode != 0:
        return restore_repair_stash({
            "status": "error",
            "reason": "fetch_failed",
            "detail": (fetch.stderr or fetch.stdout)[-2000:],
        })

    remote = _run([git, "rev-parse", "origin/main"], cwd=root)
    if remote.returncode != 0:
        return restore_repair_stash({
            "status": "error",
            "reason": "remote_head_failed",
        })
    target_sha = remote.stdout.strip()

    if target_sha == before_sha:
        return restore_repair_stash({
            "status": (
                "up_to_date_with_safe_repair"
                if repair_stash_ref
                else "up_to_date"
            ),
            "before": before_sha,
            "after": before_sha,
        })

    ancestor = _run(
        [git, "merge-base", "--is-ancestor", before_sha, target_sha],
        cwd=root,
    )
    if ancestor.returncode != 0:
        return restore_repair_stash({
            "status": "blocked",
            "reason": "non_fast_forward",
            "before": before_sha,
            "target": target_sha,
        })

    changed = _run(
        [git, "diff", "--name-only", before_sha, target_sha],
        cwd=root,
    )
    changed_files = [
        line.strip()
        for line in changed.stdout.splitlines()
        if line.strip()
    ]
    blocked_names = {
        ".env",
        "token.json",
        "client_secret.json",
    }
    if any(
        Path(name).name.lower() in blocked_names
        for name in changed_files
    ):
        return restore_repair_stash({
            "status": "blocked",
            "reason": "sensitive_file_change",
            "files": changed_files,
        })

    worktree_root = root.parent / (root.name + "_mirai_update_check")
    if worktree_root.exists():
        cleanup = _run(
            [git, "worktree", "remove", "--force", str(worktree_root)],
            cwd=root,
        )
        if cleanup.returncode != 0:
            shutil.rmtree(worktree_root, ignore_errors=True)

    add = _run(
        [git, "worktree", "add", "--detach", str(worktree_root), target_sha],
        cwd=root,
        timeout=120,
    )
    if add.returncode != 0:
        return restore_repair_stash({
            "status": "error",
            "reason": "worktree_add_failed",
            "detail": (add.stderr or add.stdout)[-2000:],
        })

    validation_output: list[str] = []
    validation_failure: dict[str, Any] | None = None
    try:
        health = inspect_code_health(worktree_root)
        if not health.get("healthy"):
            validation_failure = {
                "status": "rejected",
                "reason": "autonomy_invariant_failed",
                "target": target_sha,
                "issues": health.get("issues") or [],
            }
        else:
            checks = [
                [sys.executable, "-m", "compileall", "-q", "."],
                [
                    sys.executable,
                    "-m",
                    "unittest",
                    "discover",
                    "-s",
                    "tests",
                    "-v",
                ],
            ]
            for command in checks:
                completed = _run(
                    command,
                    cwd=worktree_root,
                    timeout=240,
                )
                output = (
                    (completed.stdout or "")
                    + "\n"
                    + (completed.stderr or "")
                )[-5000:]
                validation_output.append(output)
                if completed.returncode != 0:
                    validation_failure = {
                        "status": "rejected",
                        "reason": "validation_failed",
                        "target": target_sha,
                        "validation": "\n".join(
                            validation_output
                        )[-8000:],
                    }
                    break
    finally:
        _run(
            [git, "worktree", "remove", "--force", str(worktree_root)],
            cwd=root,
            timeout=60,
        )

    if validation_failure is not None:
        return restore_repair_stash(validation_failure)

    merge = _run(
        [git, "merge", "--ff-only", target_sha],
        cwd=root,
        timeout=120,
    )
    if merge.returncode != 0:
        return restore_repair_stash({
            "status": "error",
            "reason": "fast_forward_failed",
            "detail": (merge.stderr or merge.stdout)[-3000:],
        })

    repair_stash_dropped = None
    if repair_stash_ref:
        dropped = _run(
            [git, "stash", "drop", repair_stash_ref],
            cwd=root,
            timeout=60,
        )
        repair_stash_dropped = dropped.returncode == 0
        repair_stash_ref = ""

    return _save({
        "status": "updated",
        "before": before_sha,
        "after": target_sha,
        "files": changed_files[:100],
        "validation": "\n".join(validation_output)[-5000:],
        "safe_repair_reconciled": repair_stash_dropped,
    })

def main() -> None:
    result = safe_self_update()
    print(json.dumps(result, ensure_ascii=False))
    # 自動運転を止めるほどではない。更新失敗はログ化して現コードで続行する。
    raise SystemExit(0)


if __name__ == "__main__":
    main()
