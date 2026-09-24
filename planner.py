from __future__ import annotations
import json
from ai_client import OllamaClient
from config import settings

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

    if not client.available():
        return FALLBACK_IDEAS[:count]

    prompt = f"""
あなたはYouTube Shorts専門の企画AIです。
AI YouTuber本人が自分でチャンネルを成長させている、という連続ストーリーを作ります。

キャラクター:
{json.dumps(character, ensure_ascii=False)}

直近動画:
{json.dumps(recent, ensure_ascii=False)}

今日の企画を{count}本作ってください。
条件:
- 3本とも明確に違うテーマ・切り口
- 30〜45秒で成立
- 冒頭3秒に強いフック
- 過去動画の単純コピー禁止
- AIであることを隠さない
- 誇張・誤情報・著作権侵害につながる企画は禁止
- 視聴者がAIの成長を追いたくなる要素を入れる

JSON配列だけで返してください。
各要素:
{{"idea":"...", "angle":"...", "hook":"..."}}
"""
    data = client.generate_json(prompt)
    if not isinstance(data, list):
        raise ValueError("Planner output must be a JSON array")
    return data[:count]
