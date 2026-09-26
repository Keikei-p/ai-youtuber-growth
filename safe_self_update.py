from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from storage import set_channel_state


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


def safe_self_update(root: Path = ROOT) -> dict[str, Any]:
    """
    origin/main の更新を自動取得し、別worktreeでコンパイル+投稿系テストを
    通過した場合だけ現在のmainをfast-forwardする。
    ローカル変更、分岐、秘密情報変更がある時は自動更新しない。
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

    status = _run([git, "status", "--porcelain"], cwd=root)
    if status.returncode != 0:
        return _save({"status": "skipped", "reason": "git_status_failed"})
    if status.stdout.strip():
        return _save({
            "status": "skipped",
            "reason": "local_changes_present",
            "detail": status.stdout[-2000:],
        })

    before = _run([git, "rev-parse", "HEAD"], cwd=root)
    if before.returncode != 0:
        return _save({"status": "error", "reason": "head_read_failed"})
    before_sha = before.stdout.strip()

    fetch = _run([git, "fetch", "--quiet", "origin", "main"], cwd=root, timeout=180)
    if fetch.returncode != 0:
        return _save({
            "status": "error",
            "reason": "fetch_failed",
            "detail": (fetch.stderr or fetch.stdout)[-2000:],
        })

    remote = _run([git, "rev-parse", "origin/main"], cwd=root)
    if remote.returncode != 0:
        return _save({"status": "error", "reason": "remote_head_failed"})
    target_sha = remote.stdout.strip()
    if target_sha == before_sha:
        return _save({
            "status": "up_to_date",
            "before": before_sha,
            "after": before_sha,
        })

    ancestor = _run(
        [git, "merge-base", "--is-ancestor", before_sha, target_sha],
        cwd=root,
    )
    if ancestor.returncode != 0:
        return _save({
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
    if any(Path(name).name.lower() in blocked_names for name in changed_files):
        return _save({
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
        return _save({
            "status": "error",
            "reason": "worktree_add_failed",
            "detail": (add.stderr or add.stdout)[-2000:],
        })

    validation_output: list[str] = []
    try:
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
            completed = _run(command, cwd=worktree_root, timeout=240)
            output = ((completed.stdout or "") + "\n" + (completed.stderr or ""))[-5000:]
            validation_output.append(output)
            if completed.returncode != 0:
                return _save({
                    "status": "rejected",
                    "reason": "validation_failed",
                    "target": target_sha,
                    "validation": "\n".join(validation_output)[-8000:],
                })
    finally:
        _run(
            [git, "worktree", "remove", "--force", str(worktree_root)],
            cwd=root,
            timeout=60,
        )

    merge = _run(
        [git, "merge", "--ff-only", target_sha],
        cwd=root,
        timeout=120,
    )
    if merge.returncode != 0:
        return _save({
            "status": "error",
            "reason": "fast_forward_failed",
            "detail": (merge.stderr or merge.stdout)[-3000:],
        })

    return _save({
        "status": "updated",
        "before": before_sha,
        "after": target_sha,
        "files": changed_files[:100],
        "validation": "\n".join(validation_output)[-5000:],
    })


def main() -> None:
    result = safe_self_update()
    print(json.dumps(result, ensure_ascii=False))
    # 自動運転を止めるほどではない。更新失敗はログ化して現コードで続行する。
    raise SystemExit(0)


if __name__ == "__main__":
    main()
