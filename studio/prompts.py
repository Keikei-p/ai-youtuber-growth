from __future__ import annotations

import json


MIRAI_BASE_PROMPT = (
    "same original Mirai AI girl as the reference image, "
    "white-silver layered long hair, vivid cyan blue eyes, "
    "distinct futuristic cat-ear triangular headset with cyan glowing rings, "
    "white black and electric-blue futuristic outfit, "
    "soft friendly anime face, luminous blue accents, "
    "preserve the same identity, face, hairstyle, headset and color palette, "
    "polished high quality anime illustration, safe for work"
)

EXPRESSION_PROMPTS = {
    "normal": "calm friendly expression",
    "smile": "bright natural smile",
    "wink": "playful wink",
    "surprised": "surprised expression",
    "thinking": "thoughtful expression",
    "troubled": "slightly troubled expression",
    "serious": "focused serious expression",
    "embarrassed": "slightly embarrassed expression",
}


def build_mirai_prompt(expression: str = "normal") -> str:
    expression_prompt = EXPRESSION_PROMPTS.get(expression, expression)
    return (
        f"{MIRAI_BASE_PROMPT}, {expression_prompt}, full body, front view, "
        "simple clean studio background, no text, no logo"
    )


def _profile_from_row(row: dict) -> dict:
    raw = row.get("profile_json") or {}
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            raw = {}
    return raw if isinstance(raw, dict) else {}


def build_guest_prompt(row: dict) -> str:
    profile = _profile_from_row(row)
    visual = profile.get("visual") or {}
    personality = ", ".join(profile.get("personality") or [])
    base_prompt = row.get("visual_prompt") or profile.get("visual_prompt") or ""
    return (
        f"{base_prompt}, original anime VTuber guest character, "
        f"hair: {visual.get('hair', '')}, eyes: {visual.get('eyes', '')}, "
        f"outfit: {visual.get('outfit', '')}, accessory: {visual.get('accent', '')}, "
        f"role: {profile.get('role', '')}, personality: {personality}, "
        "full body, front view, clean lineart, high detail, safe for work, "
        "simple clean background, no text, no logo"
    )


def build_background_prompt(theme: str) -> str:
    return (
        "original anime-style environment background, no people, no character, "
        f"theme: {theme}, clean composition, detailed lighting, suitable for "
        "vertical YouTube Shorts and VTuber content, no text, no logo"
    )
