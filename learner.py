from __future__ import annotations

def build_learning_note(metrics:dict)->tuple[str,float]:
    views=float(metrics.get("views") or 0)
    likes=float(metrics.get("likes") or 0)
    comments=float(metrics.get("comments") or 0)
    retention=float(metrics.get("averageViewPercentage") or 0)

    like_rate=(likes/views*100) if views else 0.0
    comment_rate=(comments/views*100) if views else 0.0

    notes=[]
    if retention>=80:
        notes.append("視聴維持率が強い。冒頭とテンポの型を次回にも残す。")
    elif retention>=55:
        notes.append("視聴維持率は中程度。冒頭3秒と中盤の間延びを改善候補にする。")
    else:
        notes.append("視聴維持率が弱い。次回は冒頭を短くし、説明を削って結論を早める。")

    if like_rate>=5:
        notes.append("高評価反応が強い。テーマやキャラの切り口を別企画へ展開する。")
    elif views>=50 and like_rate<2:
        notes.append("高評価反応が弱い。視聴後に感情や発見が残る構成を増やす。")

    if comment_rate>=1:
        notes.append("コメント反応が良い。視聴者参加型の続きを優先する。")
    else:
        notes.append("コメント誘導は一言だけにして、質問をより具体的にする。")

    score=min(100.0,retention*0.75+min(like_rate,10)*2.0+min(comment_rate,5)*1.0)
    return " ".join(notes),round(score,2)
