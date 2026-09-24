from __future__ import annotations
import json
import math

from ai_client import OllamaClient

def build_learning_note(
    metrics: dict,
    checkpoint_hours: int = 24,
) -> tuple[str, float]:
    views = float(metrics.get("views") or 0)
    likes = float(metrics.get("likes") or 0)
    comments = float(metrics.get("comments") or 0)
    retention = float(metrics.get("averageViewPercentage") or 0)

    like_rate = (likes / views * 100) if views else 0.0
    comment_rate = (comments / views * 100) if views else 0.0

    notes: list[str] = [
        f"{checkpoint_hours}時間時点の分析。"
    ]

    if retention > 0:
        if retention >= 80:
            notes.append(
                "視聴維持率が強い。冒頭の入り方とテンポを別テーマでも再利用する。"
            )
        elif retention >= 55:
            notes.append(
                "視聴維持率は中程度。冒頭3秒をさらに短くし、中盤の説明を圧縮する。"
            )
        else:
            notes.append(
                "視聴維持率が弱い。次回は結論を先に出し、説明量を減らす。"
            )
    else:
        notes.append(
            "視聴維持率はまだ取得できていないため、維持率だけで良し悪しを決めない。"
        )

    if views < 20:
        notes.append(
            "まだ視聴母数が少ない。テーマを即座に捨てず、追加データを待つ。"
        )
    else:
        if like_rate >= 5:
            notes.append(
                "高評価率が強い。テーマまたはキャラの切り口を別企画へ展開する。"
            )
        elif like_rate < 2:
            notes.append(
                "高評価率が弱い。視聴後に驚き・発見・共感が残る構成を増やす。"
            )

        if comment_rate >= 1:
            notes.append(
                "コメント反応が良い。具体的な一問で視聴者参加型の続きを作る。"
            )
        else:
            notes.append(
                "コメント誘導は短くし、答えやすい具体的な質問にする。"
            )

    if checkpoint_hours >= 168:
        notes.append(
            "7日データなので、一時的な伸びより再現できる型を優先して判断する。"
        )
    elif checkpoint_hours >= 72:
        notes.append(
            "72時間データなので、24時間時点との差も見て伸び続ける企画か判断する。"
        )

    retention_score = retention * 0.65 if retention > 0 else 32.5
    engagement_score = min(like_rate, 10) * 2.0 + min(comment_rate, 5) * 1.5
    volume_score = min(math.log10(max(views, 1) + 1) * 4.0, 12.0)
    score = min(100.0, retention_score + engagement_score + volume_score)

    return " ".join(notes), round(score, 2)

def _fallback_strategy(history: list[dict]) -> str:
    if not history:
        return (
            "まだ学習データが少ない。まず異なるテーマとフックを試し、"
            "24時間・72時間・7日の反応を蓄積する。"
        )

    latest_by_video: dict[int, dict] = {}
    for row in history:
        video_id = int(row["video_id"])
        current = latest_by_video.get(video_id)
        if current is None or int(row["checkpoint_hours"]) > int(current["checkpoint_hours"]):
            latest_by_video[video_id] = row

    samples = list(latest_by_video.values())
    ranked = sorted(samples, key=lambda x: float(x.get("score") or 0), reverse=True)

    strong = ranked[:3]
    weak = ranked[-2:] if len(ranked) >= 3 else []

    strong_titles = " / ".join(x.get("title", "") for x in strong if x.get("title"))
    weak_titles = " / ".join(x.get("title", "") for x in weak if x.get("title"))

    parts = []
    if strong_titles:
        parts.append(
            f"伸ばす候補: {strong_titles}。同じ内容のコピーではなく、"
            "フックや構成の型だけを別テーマへ転用する。"
        )
    if weak_titles:
        parts.append(
            f"改善候補: {weak_titles}。冒頭の結論を早め、説明量を減らして再検証する。"
        )

    parts.append(
        "毎回3本はテーマを分散し、1本は実績の良い型、1本は改善型、"
        "1本は新しい実験にする。"
    )
    return " ".join(parts)

def build_channel_strategy(history: list[dict]) -> str:
    fallback = _fallback_strategy(history)
    if not history:
        return fallback

    client = OllamaClient()
    if not client.available():
        return fallback

    prompt = f"""
あなたは成長型AI YouTuber「ミライ」のチャンネル戦略AIです。
以下は動画ごとの24時間・72時間・7日分析履歴です。

分析履歴:
{json.dumps(history[:60], ensure_ascii=False)}

次回以降の企画AIと脚本AIがそのまま使える「成長戦略」を作ってください。

条件:
- データが少ない場合は断定しない
- 再生数だけで判断せず、視聴維持率・高評価・コメントも見る
- 成功動画の内容をコピーせず、成功した構成・フック・話し方の型を抽出する
- 失敗動画は捨てるだけでなく、改善して再検証できる形にする
- 3本/日のうち、実績型・改善型・新規実験を混ぜる
- 300文字以内
- 日本語の文章だけを返す
"""
    try:
        strategy = client.generate(prompt).strip()
        return strategy or fallback
    except Exception:
        return fallback
