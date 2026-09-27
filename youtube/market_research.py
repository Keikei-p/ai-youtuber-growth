from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from statistics import median
from typing import Any

from googleapiclient.discovery import build

from ai_client import OllamaClient
from storage import get_channel_state, set_channel_state
from youtube.auth import get_credentials


STATE_KEY = "market_research_snapshot"
GUIDANCE_KEY = "market_research_guidance"
LAST_AT_KEY = "market_research_last_at"


def _duration_seconds(value: str) -> int:
    text = str(value or "").upper()
    match = re.fullmatch(
        r"P(?:(?P<days>\d+)D)?T"
        r"(?:(?P<hours>\d+)H)?"
        r"(?:(?P<minutes>\d+)M)?"
        r"(?:(?P<seconds>\d+)S)?",
        text,
    )
    if not match:
        return 0
    parts = {
        key: int(raw or 0)
        for key, raw in match.groupdict().items()
    }
    return (
        parts["days"] * 86400
        + parts["hours"] * 3600
        + parts["minutes"] * 60
        + parts["seconds"]
    )


def _video_rows(items: list[dict]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in items:
        snippet = item.get("snippet") or {}
        statistics = item.get("statistics") or {}
        content = item.get("contentDetails") or {}
        title = str(snippet.get("title") or "").strip()
        if not title:
            continue
        rows.append({
            "video_id": str(item.get("id") or ""),
            "title": title,
            "published_at": str(snippet.get("publishedAt") or ""),
            "channel_title": str(snippet.get("channelTitle") or ""),
            "views": int(statistics.get("viewCount") or 0),
            "likes": int(statistics.get("likeCount") or 0),
            "comments": int(statistics.get("commentCount") or 0),
            "duration_seconds": _duration_seconds(
                str(content.get("duration") or "")
            ),
        })
    return rows


def _details(youtube, ids: list[str]) -> list[dict[str, Any]]:
    ids = [value for value in ids if value]
    if not ids:
        return []
    response = youtube.videos().list(
        part="snippet,statistics,contentDetails",
        id=",".join(ids[:50]),
    ).execute()
    return _video_rows(response.get("items") or [])


def _fallback_guidance(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return (
            "市場データ未取得。過去動画の学習を優先し、"
            "冒頭3秒・短い導入・明確な1テーマを継続検証する。"
        )

    titles = [str(row["title"]) for row in rows]
    lengths = [len(title) for title in titles]
    durations = [
        int(row["duration_seconds"])
        for row in rows
        if int(row.get("duration_seconds") or 0) > 0
    ]
    question_rate = sum(
        1 for title in titles
        if "?" in title or "？" in title or "なぜ" in title
    ) / max(len(titles), 1)
    number_rate = sum(
        1 for title in titles
        if re.search(r"\d", title)
    ) / max(len(titles), 1)
    bracket_rate = sum(
        1 for title in titles
        if any(mark in title for mark in ("【", "「", "(", "（"))
    ) / max(len(titles), 1)

    by_views = sorted(
        rows,
        key=lambda row: int(row.get("views") or 0),
        reverse=True,
    )
    top = by_views[: max(3, min(8, len(by_views)))]
    common_tokens: dict[str, int] = {}
    for row in top:
        for token in re.findall(
            r"[A-Za-z]{3,}|[一-龯ぁ-んァ-ヶー]{2,8}",
            str(row["title"]),
        ):
            key = token.lower()
            if key in {"youtube", "shorts", "short", "動画"}:
                continue
            common_tokens[key] = common_tokens.get(key, 0) + 1
    themes = [
        token
        for token, count in sorted(
            common_tokens.items(),
            key=lambda pair: (-pair[1], pair[0]),
        )
        if count >= 2
    ][:6]

    duration_note = (
        f"中央値{int(median(durations))}秒"
        if durations
        else "尺データ不足"
    )
    return (
        f"市場比較{len(rows)}本。タイトル中央値{int(median(lengths))}文字、"
        f"疑問形{question_rate:.0%}、数字入り{number_rate:.0%}、"
        f"括弧入り{bracket_rate:.0%}、動画尺{duration_note}。"
        f"反復テーマ候補: {', '.join(themes) if themes else '分散'}。"
        "タイトルや台本をコピーせず、冒頭の見せ方・短さ・問いの型だけを"
        "自分の企画へ変換する。"
    )


def _ai_guidance(rows: list[dict[str, Any]], fallback: str) -> str:
    client = OllamaClient()
    if not client.available() or not rows:
        return fallback

    compact = [
        {
            "title": row["title"],
            "views": row["views"],
            "duration_seconds": row["duration_seconds"],
            "likes": row["likes"],
            "comments": row["comments"],
        }
        for row in sorted(
            rows,
            key=lambda value: int(value.get("views") or 0),
            reverse=True,
        )[:16]
    ]
    prompt = f"""
あなたはAI YouTuberの市場分析担当です。
以下はYouTube上の複数動画の公開情報です。

{json.dumps(compact, ensure_ascii=False)}

目的は特定動画をコピーすることではありません。
次回企画へ使える共通パターンだけを250文字以内で日本語要約してください。

必須:
- 複数動画に共通するテーマ/タイトル構造/尺/冒頭フックの仮説
- そのままのタイトル・固有フレーズ・台本を再利用しない
- 再生数だけで断定しない
- AI YouTuber「ミライ」独自の成長物語へ変換する
- 煽りや無関係な人気ワードは禁止
"""
    try:
        result = client.generate(prompt).strip()
        return result[:500] or fallback
    except Exception:
        return fallback


def refresh_market_research() -> dict[str, Any]:
    youtube = build(
        "youtube",
        "v3",
        credentials=get_credentials(interactive=False),
    )

    rows: list[dict[str, Any]] = []
    try:
        popular = youtube.videos().list(
            part="snippet,statistics,contentDetails",
            chart="mostPopular",
            regionCode="JP",
            videoCategoryId="22",
            maxResults=20,
        ).execute()
        rows.extend(_video_rows(popular.get("items") or []))
    except Exception:
        pass

    published_after = (
        datetime.now(timezone.utc) - timedelta(days=30)
    ).isoformat(timespec="seconds").replace("+00:00", "Z")
    for query in ("AI YouTuber", "生成AI Shorts"):
        try:
            search = youtube.search().list(
                part="snippet",
                q=query,
                type="video",
                order="viewCount",
                publishedAfter=published_after,
                videoDuration="short",
                maxResults=12,
                regionCode="JP",
                relevanceLanguage="ja",
            ).execute()
            ids = [
                str((item.get("id") or {}).get("videoId") or "")
                for item in (search.get("items") or [])
            ]
            rows.extend(_details(youtube, ids))
        except Exception:
            continue

    deduped: dict[str, dict[str, Any]] = {}
    for row in rows:
        key = row.get("video_id") or row.get("title")
        if key:
            deduped[str(key)] = row
    rows = list(deduped.values())
    rows.sort(
        key=lambda row: int(row.get("views") or 0),
        reverse=True,
    )
    rows = rows[:30]

    fallback = _fallback_guidance(rows)
    guidance = _ai_guidance(rows, fallback)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    snapshot = {
        "captured_at": now,
        "sample_size": len(rows),
        "videos": rows[:20],
        "guidance": guidance,
    }
    set_channel_state(
        STATE_KEY,
        json.dumps(snapshot, ensure_ascii=False),
    )
    set_channel_state(GUIDANCE_KEY, guidance)
    set_channel_state(LAST_AT_KEY, now)
    return snapshot


def maybe_refresh_market_research(
    *,
    min_hours: int = 24,
) -> dict[str, Any]:
    raw = get_channel_state(LAST_AT_KEY, "").strip()
    if raw:
        try:
            last = datetime.fromisoformat(raw)
            if last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
            age = datetime.now(timezone.utc) - last.astimezone(timezone.utc)
            if age < timedelta(hours=max(1, int(min_hours))):
                try:
                    cached = json.loads(
                        get_channel_state(STATE_KEY, "") or "{}"
                    )
                except Exception:
                    cached = {}
                return {
                    "status": "cached",
                    **(cached if isinstance(cached, dict) else {}),
                }
        except Exception:
            pass

    snapshot = refresh_market_research()
    return {"status": "refreshed", **snapshot}
