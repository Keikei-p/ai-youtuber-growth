from __future__ import annotations
import json
import re

from ai_client import OllamaClient
from config import settings
from storage import get_channel_state

FALLBACK_IDEAS = [
    {
        "idea": "AIがYouTuberを始めて最初に困ったこと",
        "angle": "自分の失敗を笑いに変える自己紹介型",
        "hook": "YouTuberになったAI、開始5分で詰みました。"
    },
    {
        "idea": "AIは再生数0でも落ち込むのか",
        "angle": "AIの感情っぽい反応を通じて視聴者参加を促す",
        "hook": "再生数0。AIの私でも、これはちょっと悔しい。"
    },
    {
        "idea": "視聴者に次の企画を決めてもらう",
        "angle": "コメントを成長データとして使う参加型",
        "hook": "次の動画、私じゃなくてあなたが決めてください。"
    },
    {
        "idea": "AIが自分の黒歴史動画を分析する",
        "angle": "過去動画の弱点を自分で指摘する成長型",
        "hook": "昨日の私の動画、AI目線でも普通にダメでした。"
    },
    {
        "idea": "AIが24時間でどこまで改善できるか",
        "angle": "昨日との比較で成長を見せる",
        "hook": "24時間前の私より、今日は少しだけ賢いです。"
    }
]

def _idea_text(row: dict) -> str:
    raw = row.get("idea") if isinstance(row, dict) else ""
    if isinstance(raw, dict):
        raw = raw.get("idea") or ""
    return str(raw or "").strip()


def _idea_key(value: str) -> str:
    value = re.sub(r"\s+", "", str(value or "")).lower()
    value = re.sub(r"[【】\[\]（）()「」『』・:：#＃!?！？。、,.\-—_]", "", value)
    return value


def _fallback_variant(base: dict, cycle: int, serial: int) -> dict:
    item = dict(base)
    experiment_types = ("proven", "improve", "new")
    item["experiment_type"] = experiment_types[(serial - 1) % 3]
    if cycle <= 0:
        return item

    item["idea"] = f"{base['idea']}（成長実験{serial}）"
    item["angle"] = (
        f"{base.get('angle', '')}。前回との差を1点だけ変えて再検証する"
    )
    item["hook"] = f"成長実験{serial}。{base.get('hook', '')}"
    return item


def fallback_ideas(recent: list[dict], count: int) -> list[dict]:
    count = max(1, int(count))
    used = {
        _idea_key(_idea_text(row))
        for row in recent[:40]
        if _idea_text(row)
    }
    selected: list[dict] = []
    serial = 1

    # 基本5企画を使い切った後も、成長実験番号付きの別検証として
    # 無限ループせず安全に企画を分散できる。
    for cycle in range(0, 50):
        for base in FALLBACK_IDEAS:
            candidate = _fallback_variant(base, cycle, serial)
            serial += 1
            key = _idea_key(candidate.get("idea", ""))
            if not key or key in used:
                continue
            used.add(key)
            selected.append(candidate)
            if len(selected) >= count:
                return selected
    return selected


def _validated_ideas(
    data: list,
    recent: list[dict],
    count: int,
) -> list[dict]:
    used = {
        _idea_key(_idea_text(row))
        for row in recent[:40]
        if _idea_text(row)
    }
    selected: list[dict] = []
    for raw in data:
        if not isinstance(raw, dict):
            continue
        idea = str(raw.get("idea") or "").strip()
        if not idea:
            continue
        key = _idea_key(idea)
        if not key or key in used:
            continue
        item = dict(raw)
        item["idea"] = idea
        item["angle"] = str(item.get("angle") or "").strip()
        item["hook"] = str(item.get("hook") or "").strip()
        experiment = str(item.get("experiment_type") or "new").strip().lower()
        item["experiment_type"] = (
            experiment
            if experiment in {"proven", "improve", "new"}
            else "new"
        )
        used.add(key)
        selected.append(item)
        if len(selected) >= count:
            return selected

    if len(selected) < count:
        synthetic_recent = [
            *recent,
            *[
                {"idea": item["idea"]}
                for item in selected
            ],
        ]
        selected.extend(
            fallback_ideas(
                synthetic_recent,
                count - len(selected),
            )
        )
    return selected[:count]


def plan_ideas(character: dict, recent: list[dict], count: int | None = None) -> list[dict]:
    count = count or settings.posts_per_day
    client = OllamaClient()
    strategy = get_channel_state(
        "growth_strategy",
        "まだ十分な分析データがない。テーマを分散し、実績型・改善型・新規実験を混ぜる。"
    )
    improvement = get_channel_state(
        "ai_improvement_report",
        ""
    )
    autonomous_guidance = get_channel_state(
        "autonomous_planner_guidance",
        "",
    )

    if not client.available():
        return fallback_ideas(recent, count)

    prompt = f"""
あなたはYouTube Shorts専門の企画AIです。
AI YouTuber本人が自分でチャンネルを成長させている、という連続ストーリーを作ります。

キャラクター:
{json.dumps(character, ensure_ascii=False)}

現在の成長戦略:
{strategy}

AI改善センターの最新提案:
{improvement or "まだなし"}

自動学習した企画ガイダンス:
{autonomous_guidance or "まだなし"}

直近動画:
{json.dumps(recent, ensure_ascii=False)}

次の企画を{count}本作ってください。

重要:
- 成長戦略を反映する
- ただし成功動画の内容をそのままコピーしない
- countが3以上なら、実績の良い型1本・改善型1本・新規実験1本を混ぜる
- 各企画は明確に違うテーマ・切り口
- 30〜45秒で成立
- 冒頭3秒に強いフック
- 直近動画と同じ言い回しを避ける
- AIであることを隠さない
- 誇張・誤情報・著作権侵害につながる企画は禁止
- 原則として実在人物・企業・事件・ニュースを企画の主役にしない
- 他人の歌詞・映画・アニメ・漫画・動画・画像を再現/転載する企画は禁止
- 医療・法律・投資で個別判断や断定的な助言をしない
- 実在人物の声・顔を似せる企画や、本人になりすます企画は禁止
- 現在のずんだもん音声では政治・宗教・情報商材・風俗営業の宣伝に関する企画を作らない
- 視聴者がミライの成長を追いたくなる要素を入れる
- 分析データが少ない時は断定せず実験を続ける

JSON配列だけで返してください。
各要素:
{{"idea":"...", "angle":"...", "hook":"...", "experiment_type":"proven|improve|new"}}
"""
    data = client.generate_json(prompt)
    if not isinstance(data, list):
        raise ValueError("Planner output must be a JSON array")
    return _validated_ideas(data, recent, count)
