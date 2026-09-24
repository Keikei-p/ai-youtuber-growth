from __future__ import annotations
import json
from ai_client import OllamaClient

def fallback_script(character: dict, idea: dict) -> dict:
    name = character["name"]
    hook = idea["hook"]
    body = (
        f"{hook}"
        f" 私は{name}。人間が毎回操作しなくても、自分で企画して成長するAI YouTuberです。"
        f" 今日のテーマは「{idea['idea']}」。"
        " まだ始まったばかりだから、失敗も全部データにします。"
        " この動画の結果も次の企画に反映します。"
        " 次に試してほしいことがあればコメントで教えてください。"
    )
    return {
        "title": f"{idea['idea']} #AIYouTuber #Shorts",
        "script": body,
        "description": "AIが自分で企画・分析・改善しながら成長するチャンネルの実験記録です。"
    }

def write_script(character: dict, idea: dict, recent: list[dict]) -> dict:
    client = OllamaClient()
    if not client.available():
        return fallback_script(character, idea)

    prompt = f"""
あなたはYouTube Shortsの脚本AIです。
キャラクター:
{json.dumps(character, ensure_ascii=False)}

今回の企画:
{json.dumps(idea, ensure_ascii=False)}

直近動画:
{json.dumps(recent[:10], ensure_ascii=False)}

30〜45秒で読み切れる日本語Shorts脚本を作ってください。
条件:
- 1文目は企画のhookを活かす
- 自己紹介を毎回長く入れない
- テンポ優先
- 具体的で、同じ言い回しを繰り返さない
- AIであることを隠さない
- 最後に自然な一言だけ視聴者参加を促す
- 虚偽、無断転載、危険行為、誹謗中傷は禁止
- タイトルは煽りすぎない

JSONだけ:
{{"title":"...", "script":"...", "description":"..."}}
"""
    data = client.generate_json(prompt)
    if not isinstance(data, dict):
        raise ValueError("Writer output must be a JSON object")
    return data
