from __future__ import annotations
import re

BANNED_PHRASES = [
    "絶対に稼げる",
    "100%儲かる",
    "必ず成功",
]

def review_script(title: str, script: str, recent: list[dict]) -> tuple[bool, list[str]]:
    issues: list[str] = []

    if len(script) < 70:
        issues.append("script_too_short")
    if len(script) > 420:
        issues.append("script_too_long")
    if not title.strip():
        issues.append("missing_title")

    for phrase in BANNED_PHRASES:
        if phrase in title or phrase in script:
            issues.append(f"banned_phrase:{phrase}")

    normalized = re.sub(r"\s+", "", script)
    for old in recent[:20]:
        old_script = re.sub(r"\s+", "", old.get("script") or "")
        if old_script and normalized == old_script:
            issues.append("exact_duplicate_script")
            break

    return (len(issues) == 0, issues)
