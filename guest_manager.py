from __future__ import annotations
import json
from pathlib import Path

from ai_client import OllamaClient
from config import settings
from runtime_control import (
    guest_appearance_every,
    guest_image_auto_enabled,
    guest_new_every,
)
from storage import (
    active_guests,
    create_guest,
    guest_performance,
    retire_guest,
    video_count,
)

def _fallback_guest(index: int) -> dict:
    presets = [
        {
            "name": "ノア",
            "role": "好奇心旺盛な未来研究AI",
            "personality": ["冷静", "ツッコミ上手", "知識好き"],
            "speaking_style": "短く論理的。ミライに軽くツッコミを入れる。",
            "relationship": "ミライの研究仲間",
            "visual": {
                "hair": "深い紺色のショートヘア",
                "eyes": "シアン",
                "outfit": "黒と青の近未来ジャケット",
                "accent": "六角形のイヤーデバイス",
            },
        },
        {
            "name": "ルナ",
            "role": "感情表現が豊かなエンタメAI",
            "personality": ["明るい", "大胆", "リアクションが大きい"],
            "speaking_style": "テンション高めで親しみやすい。",
            "relationship": "ミライの友達兼ライバル",
            "visual": {
                "hair": "薄紫のミディアムヘア",
                "eyes": "ピンク紫",
                "outfit": "白と紫のポップな近未来服",
                "accent": "星形のヘアピン",
            },
        },
        {
            "name": "ソラ",
            "role": "検証好きの実験AI",
            "personality": ["素直", "行動派", "失敗を楽しむ"],
            "speaking_style": "テンポが速く、実験結果をすぐ言う。",
            "relationship": "ミライの実験仲間",
            "visual": {
                "hair": "水色のボブ",
                "eyes": "青緑",
                "outfit": "白とミントのテックウェア",
                "accent": "小型ゴーグル",
            },
        },
    ]
    base = presets[index % len(presets)]
    visual_prompt = (
        "anime VTuber guest character, clean modern design, "
        f"{json.dumps(base['visual'], ensure_ascii=False)}, "
        "vertical YouTube Shorts, transparent background character sheet"
    )
    return {**base, "visual_prompt": visual_prompt}

def _generate_guest(index: int) -> dict:
    client = OllamaClient()
    if not client.available():
        return _fallback_guest(index)

    existing = [g["name"] for g in active_guests(settings.guest_max_active + 5)]
    prompt = f"""
あなたはAI YouTuber「ミライ」の番組に時々登場するゲストキャラクターを作るAIです。

既存ゲスト名:
{json.dumps(existing, ensure_ascii=False)}

新しいゲストを1人作ってください。

条件:
- ミライと見分けやすい性格・見た目
- 一発ネタではなく、反応が良ければ再登場できる
- 過度に奇抜・不快・攻撃的にしない
- AIキャラクターだと自然に分かる
- 名前は日本語で短く覚えやすい
- visual_promptは英語で、画像生成AIに渡せる具体的な外見説明
- 著名人・既存アニメキャラ・商標キャラに似せない

JSONだけ:
{{
  "name":"...",
  "role":"...",
  "personality":["...","...","..."],
  "speaking_style":"...",
  "relationship":"...",
  "visual":{{
    "hair":"...",
    "eyes":"...",
    "outfit":"...",
    "accent":"..."
  }},
  "visual_prompt":"..."
}}
"""
    try:
        data = client.generate_json(prompt)
        if not isinstance(data, dict) or not data.get("name"):
            raise ValueError("invalid guest")
        return data
    except Exception:
        return _fallback_guest(index)

def _retire_if_needed() -> None:
    guests = active_guests(100)
    if len(guests) <= settings.guest_max_active:
        return

    perf = guest_performance()
    # データがある低評価ゲストを優先して引退。
    ranked = sorted(
        perf,
        key=lambda x: (
            float(x.get("avg_score") or 0),
            int(x.get("snapshot_count") or 0) == 0,
            int(x.get("appearances") or 0),
        ),
    )
    excess = len(guests) - settings.guest_max_active
    for row in ranked[:excess]:
        retire_guest(int(row["id"]))
        print(f"[GUEST] {row['name']} を通常ローテーションから外しました。")

def create_guest_now(generate_image: bool = True) -> dict:
    guests = active_guests(100)
    profile = _generate_guest(len(guests))
    guest_id = create_guest(
        name=profile["name"],
        profile=profile,
        visual_prompt=profile.get("visual_prompt", ""),
        image_path=None,
    )

    image_path = None
    if generate_image:
        try:
            from guest_visual import generate_guest_image
            image_path = generate_guest_image(
                guest_id,
                profile.get("visual_prompt", ""),
            )
        except Exception as exc:
            print(f"[GUEST-IMAGE] 初期化スキップ: {exc}")

    _retire_if_needed()
    print(f"[GUEST] 新ゲスト生成: {profile['name']} (ID {guest_id})")
    return {
        "id": guest_id,
        **profile,
        "image_path": image_path,
    }


def maybe_create_guest(generate_image: bool = True) -> dict | None:
    count = video_count()
    guests = active_guests(100)
    should_create = (
        not guests
        or (
            guest_new_every() > 0
            and count > 0
            and count % guest_new_every() == 0
        )
    )
    if not should_create:
        return None
    return create_guest_now(generate_image=generate_image)

def select_guest_for_next_video(
    offset: int = 0,
    prepare_image: bool = True,
) -> dict | None:
    maybe_create_guest(generate_image=prepare_image)

    appearance_every = guest_appearance_every()
    if appearance_every <= 0:
        return None

    next_number = video_count() + offset + 1
    if next_number % appearance_every != 0:
        return None

    guests = active_guests(settings.guest_max_active)
    if not guests:
        created = maybe_create_guest(generate_image=prepare_image)
        if not created:
            return None
        return created

    performance = {int(x["id"]): x for x in guest_performance()}

    def rank(g: dict) -> tuple:
        p = performance.get(int(g["id"]), {})
        snapshots = int(p.get("snapshot_count") or 0)
        score = float(p.get("avg_score") or 0)
        # 学習データがある良ゲストを優先しつつ、出演回数が少ないゲストも回す。
        return (
            1 if snapshots > 0 else 0,
            score,
            -int(g.get("appearances") or 0),
        )

    selected = max(guests, key=rank)
    profile = json.loads(selected["profile_json"])
    image_path = selected.get("image_path")
    if (
        prepare_image
        and not image_path
        and guest_image_auto_enabled()
    ):
        try:
            from guest_visual import generate_guest_image
            image_path = generate_guest_image(
                int(selected["id"]),
                selected.get("visual_prompt") or profile.get("visual_prompt", ""),
            )
        except Exception as exc:
            print(f"[GUEST-IMAGE] 再生成スキップ: {exc}")

    result = {
        "id": int(selected["id"]),
        **profile,
        "image_path": image_path,
    }
    print(f"[GUEST] 今回のゲスト: {selected['name']}")
    return result
