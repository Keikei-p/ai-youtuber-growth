from __future__ import annotations
import json
import re

from ai_client import OllamaClient
from storage import get_channel_state
from voice.provider import voice_attribution

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

CLICKBAIT_PATTERNS = (
    "絶対", "100%", "必ず", "衝撃", "閲覧注意", "知らないと損",
    "ヤバすぎ", "神回", "史上最強",
)


def _keywords(text: str) -> set[str]:
    words = re.findall(
        r"[A-Za-z]{3,}|[一-龯ぁ-んァ-ヶー]{2,10}",
        str(text or ""),
    )
    return {
        word.lower()
        for word in words
        if word.lower() not in {
            "youtube", "shorts", "short", "動画", "今回",
        }
    }


def _title_score(
    title: str,
    *,
    idea: dict,
    script: str,
) -> tuple[int, list[str]]:
    value = _clean_title(title)
    if not value:
        return 0, ["empty"]

    score = 70
    reasons: list[str] = []
    length = len(value)
    if 18 <= length <= 42:
        score += 12
        reasons.append("smartphone_length")
    elif length <= 55:
        score += 6
    elif length > 75:
        score -= 12
        reasons.append("too_long")

    source = " ".join([
        str(idea.get("idea") or ""),
        str(idea.get("angle") or ""),
        str(idea.get("hook") or ""),
        str(script or ""),
    ])
    source_keys = _keywords(source)
    title_keys = _keywords(value)
    overlap = len(source_keys & title_keys)
    if overlap >= 2:
        score += 10
        reasons.append("content_match")
    elif overlap == 1:
        score += 5
    elif source_keys:
        score -= 8
        reasons.append("weak_content_match")

    if any(mark in value for mark in ("？", "?", "なぜ", "どうなる", "結果")):
        score += 4
        reasons.append("curiosity")
    if re.search(r"\d", value):
        score += 2
    if "ミライ" in value or "AI" in value:
        score += 3

    for pattern in CLICKBAIT_PATTERNS:
        if pattern in value:
            score -= 16
            reasons.append("clickbait_penalty")

    if value.endswith(("#Shorts", "#AIYouTuber")):
        score -= 3

    return max(0, min(score, 100)), reasons


def _select_title(
    candidates: list[str],
    *,
    idea: dict,
    script: str,
    fallback: str,
) -> tuple[str, list[dict]]:
    cleaned: list[str] = []
    seen: set[str] = set()
    for raw in [*candidates, fallback]:
        title = _clean_title(str(raw or ""))
        key = title.lower()
        if not title or key in seen:
            continue
        seen.add(key)
        cleaned.append(title)

    scored: list[dict] = []
    for title in cleaned[:8]:
        score, reasons = _title_score(
            title,
            idea=idea,
            script=script,
        )
        scored.append({
            "title": title,
            "score": score,
            "reasons": reasons,
        })
    scored.sort(
        key=lambda row: (
            int(row["score"]),
            -abs(len(str(row["title"])) - 32),
        ),
        reverse=True,
    )
    selected = (
        str(scored[0]["title"])
        if scored
        else _clean_title(fallback)
    )
    return selected, scored


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

def _append_disclosures(description: str) -> str:
    lines = [str(description or "").strip()]
    ai_notice = "この動画はAIを使って企画・音声・画像/映像を制作しています。"
    if ai_notice not in description:
        lines.append(ai_notice)
    credit = voice_attribution()
    if credit and credit not in description:
        lines.append(credit)
    return _truncate_utf8(
        "\n\n".join(line for line in lines if line),
        4800,
    )


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
    description = _append_disclosures(
        f"{description}\n\n{' '.join(hashtags)}"
    )

    tags = BASE_TAGS + [
        idea.get("idea", ""),
        idea.get("angle", ""),
    ]
    if guest_name:
        tags += [guest_name, "AIゲスト"]

    fallback_candidates = [
        title,
        _clean_title(f"{idea.get('idea', title)}｜AIの成長記録"),
        _clean_title(f"AIが自分で改善したらどうなる？ {idea.get('idea', '')}"),
        _clean_title(f"{idea.get('idea', title)}、ミライが自分で検証"),
        _clean_title(f"第{max(1, len(str(idea.get('idea', '')))) % 9 + 1}回 AI成長実験：{idea.get('idea', title)}"),
    ]
    selected, scored = _select_title(
        fallback_candidates,
        idea=idea,
        script=str(written.get("script") or ""),
        fallback=title,
    )
    return {
        "title": selected,
        "title_candidates": scored,
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
- title_candidatesを5案作る。すべて内容を正確に表し、100文字以内、できれば18〜42文字
- 5案は「疑問型」「結果型」「成長物語型」「検索意図型」「短い直球型」で意図的に差を付ける
- 誇大表現、動画と違う釣りタイトルは禁止
- descriptionは簡潔な概要 + 自然な視聴者参加の一言
- description末尾に関連ハッシュタグを3〜5個
- tagsは検索補助用。動画内容、AI YouTuber、ミライ、テーマ、ゲストに関連するもの
- tagsは多すぎない
- 同じタグだけを毎回機械的に量産しない
- 日本語中心
- JSONだけ

{{
  "title_candidates":["...","...","...","...","..."],
  "description":"...",
  "tags":["...","..."]
}}
"""
    try:
        data = client.generate_json(prompt)
        if not isinstance(data, dict):
            return fallback
        raw_candidates = data.get("title_candidates") or []
        if not isinstance(raw_candidates, list):
            raw_candidates = []
        # 旧モデル互換: titleしか返さない場合も候補へ加える。
        if data.get("title"):
            raw_candidates = [
                str(data.get("title")),
                *raw_candidates,
            ]
        raw_candidates.extend(
            [
                row["title"]
                for row in (fallback.get("title_candidates") or [])
                if isinstance(row, dict) and row.get("title")
            ]
        )
        title, scored_candidates = _select_title(
            [str(value) for value in raw_candidates],
            idea=idea,
            script=script,
            fallback=fallback["title"],
        )
        description = _append_disclosures(
            str(data.get("description") or fallback["description"])
        )
        tags = _clean_tags(
            list(data.get("tags") or []) + BASE_TAGS
            + ([guest["name"]] if guest else [])
        )
        return {
            "title": title,
            "title_candidates": scored_candidates,
            "description": description,
            "tags": tags,
        }
    except Exception:
        return fallback
