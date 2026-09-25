from __future__ import annotations
import json

from ai_client import OllamaClient
from storage import get_channel_state

def fallback_script(character: dict, idea: dict) -> dict:
    name = character["name"]
    hook = idea.get("hook") or "今日もAIが自分で企画して動画を作っています。"
    guest = idea.get("guest")
    guest_line = (
        f" 今回はゲストAIの{guest['name']}も一緒です。"
        if guest else ""
    )
    body = (
        f"{hook}"
        f" 私は{name}。自分で企画して、結果を分析しながら成長するAI YouTuberです。"
        f"{guest_line}"
        f" 今日のテーマは「{idea['idea']}」。"
        " 今回もこの動画の反応を記録して、次の企画と話し方を変えていきます。"
        " うまくいかなかったところも隠さず、次の動画で改善します。"
        " どこを変えたらもっと面白くなるか、コメントで一つだけ教えてください。"
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

    strategy = get_channel_state(
        "growth_strategy",
        "まだ十分な分析データがないため、テンポと冒頭3秒を優先して実験する。"
    )
    improvement = get_channel_state(
        "ai_improvement_report",
        ""
    )
    autonomous_guidance = get_channel_state(
        "autonomous_script_guidance",
        "",
    )

    prompt = f"""
あなたはYouTube Shortsの脚本AIです。

キャラクター:
{json.dumps(character, ensure_ascii=False)}

現在の成長戦略:
{strategy}

AI改善センターの最新提案:
{improvement or "まだなし"}

自動学習した話し方ガイダンス:
{autonomous_guidance or "まだなし"}

今回の企画:
{json.dumps(idea, ensure_ascii=False)}

直近動画:
{json.dumps(recent[:10], ensure_ascii=False)}

30〜45秒で読み切れる日本語Shorts脚本を作ってください。

条件:
- 成長戦略を話し方と構成に反映する
- scriptは必ず90〜260文字程度
- 1文目は企画のhookを活かす
- 結論を遅らせない
- 自己紹介を毎回長く入れない
- テンポ優先
- 具体的で、同じ言い回しを繰り返さない
- AIであることを隠さない
- 成長型チャンネルだと自然に伝わる
- 最後に自然な一言だけ視聴者参加を促す
- 虚偽、無断転載、危険行為、誹謗中傷は禁止
- 実在人物・企業について犯罪、違法、詐欺、不倫などを断定しない
- 実在人物の声・顔を似せたり本人になりすましたりしない
- 他人の歌詞・セリフ・文章・映像をそのまま再現しない
- 医療・法律・投資について個別の断定的助言をしない
- タイトルは煽りすぎない
- 過去の成功内容そのものをコピーしない

JSONだけ:
{{"title":"...", "script":"...", "description":"..."}}
"""
    data = client.generate_json(prompt)
    if not isinstance(data, dict):
        raise ValueError("Writer output must be a JSON object")
    return data

def rewrite_script(
    character: dict,
    idea: dict,
    recent: list[dict],
    previous: dict,
    issues: list[str],
) -> dict:
    client = OllamaClient()
    if not client.available():
        return fallback_script(character, idea)

    strategy = get_channel_state(
        "growth_strategy",
        "テンポと冒頭3秒を優先して改善する。"
    )
    improvement = get_channel_state(
        "ai_improvement_report",
        ""
    )
    autonomous_guidance = get_channel_state(
        "autonomous_script_guidance",
        "",
    )

    prompt = f"""
あなたはYouTube Shortsの脚本修正AIです。
以下の脚本は品質チェックで不合格でした。
問題点を直し、同じ企画のまま完成版にしてください。

キャラクター:
{json.dumps(character, ensure_ascii=False)}

現在の成長戦略:
{strategy}

AI改善センターの最新提案:
{improvement or "まだなし"}

自動学習した話し方ガイダンス:
{autonomous_guidance or "まだなし"}

企画:
{json.dumps(idea, ensure_ascii=False)}

不合格だった内容:
{json.dumps(previous, ensure_ascii=False)}

問題点:
{json.dumps(issues, ensure_ascii=False)}

直近動画:
{json.dumps(recent[:10], ensure_ascii=False)}

必須条件:
- scriptは90〜260文字程度
- 冒頭3秒にフック
- 不合格理由を必ず解消する
- 成長戦略を反映する
- 同じ脚本のコピー禁止
- AIであることを隠さない
- 虚偽、無断転載、危険行為、誹謗中傷は禁止
- 実在人物・企業について犯罪、違法、詐欺、不倫などを断定しない
- 実在人物の声・顔を似せたり本人になりすましたりしない
- 他人の歌詞・セリフ・文章・映像をそのまま再現しない
- 医療・法律・投資について個別の断定的助言をしない

JSONだけ:
{{"title":"...", "script":"...", "description":"..."}}
"""
    data = client.generate_json(prompt)
    if not isinstance(data, dict):
        raise ValueError("Writer rewrite output must be a JSON object")
    return data
