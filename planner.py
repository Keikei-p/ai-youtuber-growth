from __future__ import annotations
import json

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
        return FALLBACK_IDEAS[:count]

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
- 視聴者がミライの成長を追いたくなる要素を入れる
- 分析データが少ない時は断定せず実験を続ける

JSON配列だけで返してください。
各要素:
{{"idea":"...", "angle":"...", "hook":"...", "experiment_type":"proven|improve|new"}}
"""
    data = client.generate_json(prompt)
    if not isinstance(data, list):
        raise ValueError("Planner output must be a JSON array")
    return data[:count]
