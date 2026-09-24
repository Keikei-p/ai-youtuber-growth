from __future__ import annotations
import argparse
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from config import settings
from growth_engine import run_growth_cycle
from main import run_generation
from media_cleanup import cleanup_uploaded_media
from storage import (
    due_queue,
    init_db,
    mark_queue_error,
    mark_queue_uploaded,
    mark_uploaded,
    occupied_schedule_times,
    queue_video,
    queued_items,
    update_queue_schedule,
)
from youtube.uploader import upload_video

def _tz() -> ZoneInfo:
    return ZoneInfo(settings.app_timezone)

def _now() -> datetime:
    return datetime.now(_tz())

def _slot_datetimes(day) -> list[datetime]:
    slots: list[datetime] = []
    for raw in settings.post_times.split(","):
        raw = raw.strip()
        if not raw:
            continue
        hour, minute = [int(x) for x in raw.split(":", 1)]
        slots.append(
            datetime(
                day.year,
                day.month,
                day.day,
                hour,
                minute,
                tzinfo=_tz(),
            )
        )
    return sorted(slots)

def _parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value)

def _next_free_slots(start: datetime, count: int, days: int = 14) -> list[datetime]:
    occupied = occupied_schedule_times()
    slots: list[datetime] = []

    for offset in range(days + 1):
        day = (start + timedelta(days=offset)).date()
        for slot in _slot_datetimes(day):
            key = slot.isoformat(timespec="minutes")
            if slot <= start:
                continue
            if key in occupied:
                continue
            slots.append(slot)
            if len(slots) >= count:
                return slots

    return slots

def reschedule_missed() -> int:
    """
    投稿時刻を大きく過ぎた queued 動画は、その場でまとめて投稿せず、
    次の空き投稿枠へ順番に繰り越す。
    """
    init_db()
    now = _now()
    cutoff = now - timedelta(minutes=max(settings.post_grace_minutes, 0))
    queued = queued_items()

    overdue = [
        item
        for item in queued
        if _parse_iso(item["scheduled_for"]) < cutoff
    ]

    if not overdue:
        return 0

    free_slots = _next_free_slots(now, len(overdue))
    moved = 0

    for item, slot in zip(overdue, free_slots):
        new_time = slot.isoformat(timespec="minutes")
        update_queue_schedule(item["queue_id"], new_time)
        moved += 1
        print(
            f"[RESCHEDULE] #{item['video_id']} "
            f"{item['scheduled_for']} -> {new_time}"
        )

    if moved < len(overdue):
        print(
            f"[WARN] 繰り越し対象{len(overdue)}本のうち"
            f"{moved}本しか空き枠を確保できませんでした。"
        )

    return moved

def prepare_upcoming() -> None:
    """
    常に「次の投稿枠」を POSTS_PER_DAY 本ぶん先回りして準備する。
    夜に実行した場合は自動的に翌日の枠へ回る。
    """
    init_db()
    reschedule_missed()

    now = _now()
    queued = queued_items()
    future_queued = [
        item
        for item in queued
        if _parse_iso(item["scheduled_for"]) > now
    ]

    target = settings.posts_per_day
    missing = max(target - len(future_queued), 0)

    if missing <= 0:
        print(
            f"[SCHEDULE] 次の投稿キューは準備済み: "
            f"{len(future_queued)}本"
        )
        return

    free_slots = _next_free_slots(now, missing)
    if not free_slots:
        print("[SCHEDULE] 空いている投稿時刻を確保できませんでした。")
        return

    print(
        f"[SCHEDULE] 次の投稿枠に不足している"
        f"{min(missing, len(free_slots))}本を生成します。"
    )

    results = run_generation(
        render=True,
        upload=False,
        target_override=min(missing, len(free_slots)),
    )

    queued_count = 0
    for item, slot in zip(results, free_slots):
        if not item.get("output_path"):
            print(
                f"[SCHEDULE] #{item['id']} は動画未生成のため"
                "キューへ入れません。"
            )
            continue

        scheduled_for = slot.isoformat(timespec="minutes")
        queue_video(item["id"], scheduled_for)
        queued_count += 1
        print(
            f"[SCHEDULE] #{item['id']} {item['title']} -> "
            f"{slot.strftime('%Y-%m-%d %H:%M')}"
        )

    print(f"[SCHEDULE] {queued_count}本を投稿キューへ追加しました。")

def run_due() -> None:
    init_db()
    reschedule_missed()

    now = _now()
    oldest_allowed = now - timedelta(
        minutes=max(settings.post_grace_minutes, 0)
    )

    rows = due_queue(
        now.isoformat(timespec="minutes"),
        oldest_allowed.isoformat(timespec="minutes"),
    )

    if not rows:
        print("[SCHEDULE] 現在、投稿時刻を迎えた動画はありません。")
        return

    if not settings.auto_upload_enabled:
        print(
            f"[SCHEDULE] {len(rows)}本が投稿時刻を迎えていますが、"
            "AUTO_UPLOAD_ENABLED=false のため投稿しません。"
        )
        return

    for row in rows:
        try:
            output_path = row.get("output_path")
            if not output_path:
                raise FileNotFoundError("動画ファイルのパスがありません")

            youtube_id = upload_video(
                video_path=Path(output_path),
                title=row["title"],
                description=(
                    "AIが自分で企画・制作・分析しながら"
                    "成長するチャンネルです。"
                ),
                privacy_status=settings.auto_upload_privacy,
            )
            mark_uploaded(row["video_id"], youtube_id)
            mark_queue_uploaded(
                row["queue_id"],
                _now().isoformat(timespec="seconds"),
            )
            print(
                f"[AUTO-UPLOAD] #{row['video_id']} -> {youtube_id} "
                f"[{settings.auto_upload_privacy}]"
            )
            try:
                cleanup_uploaded_media(
                    row["video_id"],
                    output_path,
                )
            except Exception as cleanup_exc:
                print(
                    "[CLEANUP] 投稿は成功済みですが削除処理でエラー: "
                    f"{cleanup_exc}"
                )
        except Exception as exc:
            mark_queue_error(row["queue_id"], str(exc))
            print(
                f"[AUTO-UPLOAD] #{row['video_id']} 失敗 "
                f"(試行 {row.get('attempts', 0) + 1}/5): {exc}"
            )

def show_queue() -> None:
    init_db()
    reschedule_missed()

    now = _now()
    rows = queued_items()

    print(f"=== 投稿キュー / 現在 {now.strftime('%Y-%m-%d %H:%M')} ===")
    if not rows:
        print("まだありません。")
        return

    for row in rows:
        scheduled = _parse_iso(row["scheduled_for"])
        relation = "次回以降" if scheduled > now else "投稿時刻内"
        print(
            f"{row['scheduled_for']} | #{row['video_id']} | "
            f"{relation} | {row['title']}"
        )

def tick() -> None:
    # 先に過去動画を学習し、その最新戦略で次の動画を作る。
    run_growth_cycle()
    prepare_upcoming()
    run_due()

def main() -> None:
    parser = argparse.ArgumentParser(
        description="AI YouTuber 自動投稿スケジューラ"
    )
    parser.add_argument(
        "--prepare",
        action="store_true",
        help="次の投稿枠ぶん動画を生成してキューを準備",
    )
    parser.add_argument(
        "--run-due",
        action="store_true",
        help="投稿時刻を迎えた動画を投稿",
    )
    parser.add_argument(
        "--show",
        action="store_true",
        help="現在の投稿キューを表示",
    )
    parser.add_argument(
        "--repair",
        action="store_true",
        help="投稿時刻を大きく過ぎた動画を次の空き枠へ移動",
    )
    parser.add_argument(
        "--tick",
        action="store_true",
        help="準備・繰り越し・期限到来投稿を1回実行",
    )
    args = parser.parse_args()

    if args.prepare:
        prepare_upcoming()
    elif args.run_due:
        run_due()
    elif args.show:
        show_queue()
    elif args.repair:
        moved = reschedule_missed()
        print(f"[RESCHEDULE] 合計 {moved}本を繰り越しました。")
    else:
        tick()

if __name__ == "__main__":
    main()
