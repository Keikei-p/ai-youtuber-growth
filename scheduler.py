from __future__ import annotations
import argparse
from datetime import datetime
from zoneinfo import ZoneInfo

from config import settings
from main import run_generation
from storage import (
    due_queue,
    init_db,
    mark_queue_error,
    mark_queue_uploaded,
    mark_uploaded,
    queue_for_day,
    queue_video,
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

def prepare_today() -> None:
    init_db()
    now = _now()
    day_prefix = now.date().isoformat()
    existing = queue_for_day(day_prefix)
    slots = _slot_datetimes(now.date())

    if not slots:
        print("[SCHEDULE] POST_TIMES が空です。")
        return

    target = min(settings.posts_per_day, len(slots))
    missing = target - len(existing)
    if missing <= 0:
        print(f"[SCHEDULE] 今日の投稿キューは準備済み: {len(existing)}本")
        return

    used = {item["scheduled_for"] for item in existing}
    free_slots = [
        slot for slot in slots
        if slot.isoformat(timespec="minutes") not in used
    ]

    print(f"[SCHEDULE] 今日の不足 {missing}本を生成してキューへ追加します。")
    results = run_generation(render=True, upload=False, target_override=missing)

    queued = 0
    for item, slot in zip(results, free_slots):
        if not item.get("output_path"):
            print(f"[SCHEDULE] #{item['id']} は動画未生成のためキューへ入れません。")
            continue

        scheduled_for = slot.isoformat(timespec="minutes")
        queue_video(item["id"], scheduled_for)
        queued += 1
        print(
            f"[SCHEDULE] #{item['id']} {item['title']} -> "
            f"{slot.strftime('%Y-%m-%d %H:%M')}"
        )

    print(f"[SCHEDULE] {queued}本を追加しました。")

def run_due() -> None:
    init_db()
    now = _now()
    rows = due_queue(now.isoformat(timespec="minutes"))

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
                video_path=__import__("pathlib").Path(output_path),
                title=row["title"],
                description="AIが自分で企画・制作・分析しながら成長するチャンネルです。",
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
        except Exception as exc:
            mark_queue_error(row["queue_id"], str(exc))
            print(
                f"[AUTO-UPLOAD] #{row['video_id']} 失敗 "
                f"(試行 {row.get('attempts', 0) + 1}/5): {exc}"
            )

def show_today() -> None:
    init_db()
    now = _now()
    rows = queue_for_day(now.date().isoformat())
    print(f"=== {now.date().isoformat()} 投稿キュー ===")
    if not rows:
        print("まだありません。")
        return

    for row in rows:
        print(
            f"{row['scheduled_for']} | #{row['video_id']} | "
            f"{row['status']} | {row['title']}"
        )

def tick() -> None:
    prepare_today()
    run_due()

def main() -> None:
    parser = argparse.ArgumentParser(description="AI YouTuber 自動投稿スケジューラ")
    parser.add_argument("--prepare", action="store_true", help="今日の動画を生成して投稿キューを作る")
    parser.add_argument("--run-due", action="store_true", help="投稿時刻を迎えた動画を投稿する")
    parser.add_argument("--show", action="store_true", help="今日の投稿キューを表示する")
    parser.add_argument("--tick", action="store_true", help="準備と期限到来投稿を1回実行する")
    args = parser.parse_args()

    if args.prepare:
        prepare_today()
    elif args.run_due:
        run_due()
    elif args.show:
        show_today()
    else:
        tick()

if __name__ == "__main__":
    main()
