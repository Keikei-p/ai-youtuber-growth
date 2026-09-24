from __future__ import annotations
import json
import re

from ai_client import OllamaClient
from storage import get_channel_state

BASE_TAGS = ["AIYouTuber", "ミライ", "AI", "YouTubeShorts", "Shorts"]

def _truncate_utf8(text: str, max_bytes: int) -> str:
    raw = text.encode("utf-8")
    if len(raw) <= max_bytes:
        return text
    return raw[:max_bytes].decode("utf-8", errors="ignore").rstrip()

def _clean_title(title: str) -> str:
    title = re.sub(r"\s+", " ", title).strip()
    title = title.replace("<", "").replace(">", "")
    return title[:100].rstrip()

def _clean_tags(tags: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in tags:
        tag = re.sub(r"[#,]+", "", str(raw)).strip()
        if not tag or tag.lower() in seen:
            continue
        seen.add(tag.lower())
        out.append(tag)

    # APIの500文字制限に余裕を持たせる。
    final: list[str] = []
    used = 0
    for tag in out:
        extra = len(tag) + (1 if final else 0)
        if used + extra > 470:
            break
        final.append(tag)
        used += extra
    return final[:25]

def _fallback_metadata(written: dict, idea: dict, guest: dict | None) -> dict:
    title = _clean_title(written.get("title") or idea.get("idea") or "ミライのAI実験")
    guest_name = guest.get("name") if guest else None

    description = written.get("description") or (
        "AI YouTuberミライが、自分で企画・投稿・分析・改善しながら"
        "成長していくチャンネルです。"
    )
    if guest_name:
        description += f" 今回はゲストAI「{guest_name}」も登場します。"

    hashtags = ["#AIYouTuber", "#ミライ", "#Shorts"]
    if guest_name:
        hashtags.append(f"#{guest_name}")
    description = _truncate_utf8(
        f"{description}\n\n{' '.join(hashtags)}",
        4800,
    )

    tags = BASE_TAGS + [
        idea.get("idea", ""),
        idea.get("angle", ""),
    ]
    if guest_name:
        tags += [guest_name, "AIゲスト"]

    return {
        "title": title,
        "description": description,
        "tags": _clean_tags(tags),
    }

def build_metadata(
    written: dict,
    idea: dict,
    script: str,
    guest: dict | None = None,
) -> dict:
    fallback = _fallback_metadata(written, idea, guest)
    client = OllamaClient()
    if not client.available():
        return fallback

    strategy = get_channel_state("growth_strategy", "")
    prompt = f"""
あなたはYouTube Shortsのメタデータ最適化AIです。
クリックだけを煽るのではなく、動画内容と一致するタイトル・説明・タグを作ります。

企画:
{json.dumps(idea, ensure_ascii=False)}

台本:
{script}

現在のチャンネル成長戦略:
{strategy}

ゲスト:
{json.dumps(guest, ensure_ascii=False) if guest else "なし"}

条件:
- titleは内容を正確に表し、100文字以内。できれば40文字前後で強く分かりやすく
- 誇大表現、動画と違う釣りタイトルは禁止
- descriptionは簡潔な概要 + 自然な視聴者参加の一言
- description末尾に関連ハッシュタグを3〜5個
- tagsは検索補助用。動画内容、AI YouTuber、ミライ、テーマ、ゲストに関連するもの
- tagsは多すぎない
- 同じタグだけを毎回機械的に量産しない
- 日本語中心
- JSONだけ

{{
  "title":"...",
  "description":"...",
  "tags":["...","..."]
}}
"""
    try:
        data = client.generate_json(prompt)
        if not isinstance(data, dict):
            return fallback
        title = _clean_title(str(data.get("title") or fallback["title"]))
        description = _truncate_utf8(
            str(data.get("description") or fallback["description"]),
            4800,
        )
        tags = _clean_tags(
            list(data.get("tags") or []) + BASE_TAGS
            + ([guest["name"]] if guest else [])
        )
        return {
            "title": title,
            "description": description,
            "tags": tags,
        }
    except Exception:
        return fallback
