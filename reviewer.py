from __future__ import annotations
import re

from legal_guard import assess_publish_risk, assess_voice_license_risk
from voice.provider import voice_attribution

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

    for risk in assess_publish_risk(
        title=title,
        script=script,
    ):
        issues.append(f"legal_risk:{risk.code}")

    current_credit = voice_attribution()
    for risk in assess_voice_license_risk(
        text=f"{title} {script}",
        required_credit=current_credit,
    ):
        issues.append(f"voice_license_risk:{risk.code}")

    return (len(issues) == 0, issues)
