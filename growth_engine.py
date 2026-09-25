from __future__ import annotations
import json
from datetime import datetime, timezone

from learner import build_channel_strategy, build_learning_note
from learning_cleanup import cleanup_after_final_learning
from mirai_engines.visual_learning import VisualLearningMemory
from storage import (
    analytics_history,
    due_snapshot_candidates,
    get_channel_state,
    init_db,
    save_analytics_snapshot,
    save_learning_note,
    set_channel_state,
    update_metrics,
)
from youtube.analytics import fetch_video_metrics

CHECKPOINTS = (24, 72, 168)

def run_growth_cycle() -> int:
    """
    24h / 72h / 7d の時刻を迎えた投稿だけ分析する。
    新しい分析が1件でも増えた時だけチャンネル戦略を再構築する。
    """
    init_db()
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat(timespec="seconds")
    captured = 0
    finalized_video_ids: list[int] = []

    for checkpoint_hours in CHECKPOINTS:
        candidates = due_snapshot_candidates(
            checkpoint_hours=checkpoint_hours,
            now_iso=now_iso,
            limit=20,
        )

        for video in candidates:
            try:
                days = max(2, checkpoint_hours // 24 + 2)
                metrics = fetch_video_metrics(
                    video["youtube_video_id"],
                    days=days,
                )
                note, score = build_learning_note(
                    metrics,
                    checkpoint_hours=checkpoint_hours,
                )

                update_metrics(video["id"], metrics)
                save_learning_note(video["id"], note, score)
                save_analytics_snapshot(
                    video_id=video["id"],
                    checkpoint_hours=checkpoint_hours,
                    captured_at=now_iso,
                    metrics=metrics,
                    note=note,
                    score=score,
                )
                VisualLearningMemory().record_performance(
                    video_id=int(video["id"]),
                    checkpoint_hours=checkpoint_hours,
                    avg_view_percentage=float(metrics.get("averageViewPercentage") or 0),
                    views=int(metrics.get("views") or 0),
                    analytics_score=float(score or 0),
                )

                captured += 1
                if checkpoint_hours == 168:
                    finalized_video_ids.append(int(video["id"]))
                print(
                    f"[GROWTH] #{video['id']} {checkpoint_hours}h "
                    f"views={metrics.get('views', 0)} "
                    f"retention={metrics.get('averageViewPercentage', 0):.1f} "
                    f"score={score}"
                )
            except Exception as exc:
                print(
                    f"[GROWTH] #{video['id']} {checkpoint_hours}h "
                    f"分析失敗: {exc}"
                )

    if captured:
        history = analytics_history(limit=60)
        strategy = build_channel_strategy(history)
        set_channel_state("growth_strategy", strategy)
        visual_strategy = VisualLearningMemory().build_strategy()
        print("[GROWTH] チャンネル成長戦略を更新しました。")
        print(f"         {strategy}")
        print(
            "[GROWTH] Visual Strategy更新: "
            f"{json.dumps(visual_strategy, ensure_ascii=False)}"
        )
        for video_id in finalized_video_ids:
            try:
                cleanup = cleanup_after_final_learning(video_id)
                print(
                    f"[GROWTH][CLEANUP] #{video_id} "
                    f"removed={cleanup.get('removed_files', 0)} "
                    f"freed={cleanup.get('freed_mb', 0)}MB "
                    f"status={cleanup.get('status')}"
                )
            except Exception as exc:
                print(
                    f"[GROWTH][CLEANUP] #{video_id} 自動削除失敗: {exc}"
                )
    else:
        print("[GROWTH] 新しく分析するチェックポイントはありません。")

    return captured

def show_growth_state() -> None:
    init_db()
    strategy = get_channel_state(
        "growth_strategy",
        "まだ十分な学習データがありません。",
    )
    history = analytics_history(limit=12)

    print("=== ミライ 成長戦略 ===")
    print(strategy)
    print()
    print("=== 最近の分析 ===")
    if not history:
        print("まだありません。")
        return

    for row in history:
        print(
            f"#{row['video_id']} | {row['checkpoint_hours']}h | "
            f"score={row['score']:.2f} | views={row['views']} | "
            f"retention={row['avg_view_percentage']:.1f}% | "
            f"{row['title']}"
        )

if __name__ == "__main__":
    run_growth_cycle()
