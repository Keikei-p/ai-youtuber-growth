from __future__ import annotations
import argparse
import json
import shutil
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from ai_client import OllamaClient
from config import settings
from growth_engine import run_growth_cycle
from main import run_generation
from resource_governor import background_production_decision
from self_improvement import (
    maybe_run_improvement_review,
    record_failure,
)
from runtime_control import (
    automation_enabled,
    auto_upload_enabled,
    post_times,
    posts_per_day,
    set_auto_upload_enabled,
    set_automation_enabled,
    set_upload_privacy,
    upload_privacy,
)
from media_cleanup import cleanup_uploaded_media
from storage import (
    cancel_queued_videos,
    due_queue,
    init_db,
    mark_queue_error,
    mark_queue_uploaded,
    mark_uploaded,
    occupied_schedule_times,
    queue_video,
    queued_items,
    get_channel_state,
    set_channel_state,
    update_queue_schedule,
    video_by_id,
)
from voice.voicevox import VoicevoxClient
from youtube.uploader import upload_video

def _tz() -> ZoneInfo:
    return ZoneInfo(settings.app_timezone)

def _now() -> datetime:
    return datetime.now(_tz())

def _slot_datetimes(day) -> list[datetime]:
    slots: list[datetime] = []
    for raw in post_times().split(","):
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


FULL_TEST_STATE_KEY = "today_full_test_state"


def _full_test_state() -> dict:
    raw = get_channel_state(FULL_TEST_STATE_KEY, "").strip()
    if not raw:
        return {}
    try:
        value = json.loads(raw)
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def _save_full_test_state(state: dict) -> None:
    set_channel_state(
        FULL_TEST_STATE_KEY,
        json.dumps(state, ensure_ascii=False),
    )


def full_test_status() -> dict:
    state = _full_test_state()
    if not state:
        return {
            "active": False,
            "status": "未実行",
            "target": 0,
            "uploaded": 0,
            "video_ids": [],
            "slots": [],
            "end_at": None,
        }

    video_ids = [
        int(value)
        for value in state.get("video_ids") or []
        if str(value).isdigit()
    ]
    uploaded = 0
    rows: list[dict] = []
    for video_id in video_ids:
        row = video_by_id(video_id)
        if row:
            rows.append(row)
            if row.get("youtube_video_id"):
                uploaded += 1

    return {
        "active": bool(state.get("active")),
        "status": str(state.get("status") or "準備中"),
        "target": int(state.get("target") or 0),
        "uploaded": uploaded,
        "video_ids": video_ids,
        "slots": list(state.get("slots") or []),
        "end_at": state.get("end_at"),
        "started_at": state.get("started_at"),
        "videos": [
            {
                "id": row.get("id"),
                "title": row.get("title"),
                "youtube_video_id": row.get("youtube_video_id"),
                "status": row.get("status"),
            }
            for row in rows
        ],
    }


def _ceil_to_quarter(value: datetime) -> datetime:
    value = value.replace(second=0, microsecond=0)
    remainder = value.minute % 15
    if remainder:
        value += timedelta(minutes=15 - remainder)
    return value


def _test_slots(
    now: datetime,
    end_at: datetime,
    count: int,
) -> list[datetime]:
    if count <= 0:
        return []

    # 1本ずつ完結するため、最初の投稿判定は約45分後から。
    # 生成が長引いて時刻を過ぎた場合は、完成直後のrun_dueで投稿する。
    earliest = _ceil_to_quarter(now + timedelta(minutes=45))
    latest = end_at - timedelta(minutes=15)
    if latest <= now:
        return []

    if earliest > latest:
        earliest = _ceil_to_quarter(now + timedelta(minutes=30))

    if count == 1:
        return [min(earliest, latest)]

    total_seconds = max(
        (latest - earliest).total_seconds(),
        (count - 1) * 15 * 60,
    )
    step = total_seconds / (count - 1)

    slots: list[datetime] = []
    for index in range(count):
        slot = earliest + timedelta(seconds=step * index)
        slot = _ceil_to_quarter(slot)
        if slot > latest:
            slot = latest
        if slots and slot <= slots[-1]:
            slot = slots[-1] + timedelta(minutes=15)
        slots.append(slot)

    if slots[-1] > latest:
        shift = slots[-1] - latest
        slots = [slot - shift for slot in slots]

    return slots


def _restore_after_full_test(
    state: dict,
    final_status: str,
) -> None:
    previous = state.get("previous") or {}
    set_automation_enabled(
        bool(previous.get("automation_enabled", False))
    )
    set_auto_upload_enabled(
        bool(previous.get("auto_upload_enabled", False))
    )
    try:
        set_upload_privacy(
            str(previous.get("privacy") or "private")
        )
    except Exception:
        set_upload_privacy("private")

    state["active"] = False
    state["status"] = final_status
    state["finished_at"] = _now().isoformat(timespec="seconds")
    _save_full_test_state(state)


def abort_full_test(reason: str = "安全停止") -> int:
    state = _full_test_state()
    if not state.get("active"):
        return 0

    video_ids = [
        int(value)
        for value in state.get("video_ids") or []
        if str(value).isdigit()
    ]
    cancelled = cancel_queued_videos(video_ids)
    state["active"] = False
    state["status"] = reason
    state["finished_at"] = _now().isoformat(timespec="seconds")
    _save_full_test_state(state)
    return cancelled


def _maybe_finish_full_test() -> None:
    state = _full_test_state()
    if not state.get("active"):
        return

    target = int(state.get("target") or 0)
    status = full_test_status()
    if target > 0 and status["uploaded"] >= target:
        print(
            f"[FULL-TEST] {status['uploaded']}/{target}本の"
            "非公開自動投稿が完了しました。"
        )
        _restore_after_full_test(state, "成功")
        return

    end_raw = str(state.get("end_at") or "")
    try:
        end_at = datetime.fromisoformat(end_raw)
    except Exception:
        return

    if _now() >= end_at:
        print(
            f"[FULL-TEST] 18時の終了時刻に到達。"
            f"{status['uploaded']}/{target}本完了。"
        )
        if status["uploaded"] < target:
            cancelled = cancel_queued_videos(
                [
                    int(value)
                    for value in state.get("video_ids") or []
                    if str(value).isdigit()
                ]
            )
            print(
                f"[FULL-TEST] 時間切れの未投稿キューを"
                f"{cancelled}件キャンセルしました。"
            )
        _restore_after_full_test(
            state,
            (
                "成功"
                if status["uploaded"] >= target
                else f"未完了 {status['uploaded']}/{target}"
            ),
        )


def start_today_full_test(
    *,
    target: int = 3,
    end_hour: int = 18,
) -> dict:
    """
    今日だけの実運転テストを開始する。
    実際の制作はtickごとに1本ずつ完結させ、
    完成した動画から即キューへ入れる。
    """
    init_db()
    existing = _full_test_state()
    if existing.get("active"):
        return full_test_status()

    now = _now()
    end_at = now.replace(
        hour=end_hour,
        minute=0,
        second=0,
        microsecond=0,
    )
    if now >= end_at:
        raise RuntimeError(
            f"本日{end_hour}:00を過ぎているため開始できません。"
        )

    if not _generation_runtime_ready():
        raise RuntimeError(
            "完全テストを開始できません。Ollama/VOICEVOX/FFmpegを確認してください。"
        )
    if not Path(settings.youtube_token_file).exists():
        raise RuntimeError(
            "YouTube認証が未完了です。完全テストを開始できません。"
        )

    target = max(1, min(int(target), 3))
    slots = _test_slots(now, end_at, target)
    if len(slots) != target:
        raise RuntimeError(
            "18時までに3本分の投稿枠を確保できません。"
        )

    previous = {
        "automation_enabled": automation_enabled(),
        "auto_upload_enabled": auto_upload_enabled(),
        "privacy": upload_privacy(),
    }
    state = {
        "active": True,
        "status": "1本ずつ生成待ち",
        "target": target,
        "started_at": now.isoformat(timespec="seconds"),
        "end_at": end_at.isoformat(timespec="minutes"),
        "slots": [
            slot.isoformat(timespec="minutes")
            for slot in slots
        ],
        "video_ids": [],
        "generation_failures": 0,
        "previous": previous,
    }
    _save_full_test_state(state)

    # 実アップロード経路を試すが、テスト中は必ずprivate。
    set_automation_enabled(True)
    set_auto_upload_enabled(True)
    set_upload_privacy("private")

    print(
        "[FULL-TEST] 今日18時までの3本完全テストを開始。"
    )
    print(
        "[FULL-TEST] 1本完成→キュー→投稿判定→次の1本、"
        "の順で低負荷に進めます。"
    )
    print(
        "[FULL-TEST] 投稿予定: "
        + ", ".join(slot.strftime("%H:%M") for slot in slots)
    )
    return full_test_status()


def _advance_full_test_generation() -> None:
    state = _full_test_state()
    if not state.get("active"):
        return

    target = int(state.get("target") or 0)
    video_ids = [
        int(value)
        for value in state.get("video_ids") or []
        if str(value).isdigit()
    ]
    if len(video_ids) >= target:
        state["status"] = "投稿待ち"
        _save_full_test_state(state)
        return

    slots_raw = list(state.get("slots") or [])
    index = len(video_ids)
    if index >= len(slots_raw):
        _restore_after_full_test(
            state,
            f"投稿枠不足 {len(video_ids)}/{target}",
        )
        return

    try:
        end_at = datetime.fromisoformat(
            str(state.get("end_at"))
        )
    except Exception:
        end_at = _now().replace(hour=18, minute=0, second=0, microsecond=0)

    if _now() >= end_at:
        _maybe_finish_full_test()
        return

    state["status"] = f"{index + 1}/{target}本目を生成中"
    _save_full_test_state(state)
    print(
        f"[FULL-TEST] {index + 1}/{target}本目を"
        "通常制作フローで生成します。"
    )

    results = run_generation(
        render=True,
        upload=False,
        target_override=1,
    )
    if not results or not results[0].get("output_path"):
        failures = int(state.get("generation_failures") or 0) + 1
        state["generation_failures"] = failures
        state["status"] = f"生成失敗 {index}/{target} / 再試行 {failures}/3"
        _save_full_test_state(state)
        record_failure(
            "full_test.generate",
            "1本の完成動画を生成できませんでした",
            {
                "index": index + 1,
                "target": target,
                "attempt": failures,
            },
        )
        if failures >= 3:
            cancel_queued_videos(video_ids)
            _restore_after_full_test(
                state,
                f"生成失敗で停止 {index}/{target}",
            )
            print(
                "[FULL-TEST] 同じ生成失敗が3回続いたため、"
                "PC負荷を避けてテストを停止しました。"
            )
        return

    state["generation_failures"] = 0
    item = results[0]
    video_id = int(item["id"])
    slot = datetime.fromisoformat(str(slots_raw[index]))
    queue_video(
        video_id,
        slot.isoformat(timespec="minutes"),
    )
    video_ids.append(video_id)
    state["video_ids"] = video_ids
    state["status"] = (
        "投稿待ち"
        if len(video_ids) >= target
        else f"{len(video_ids)}/{target}本完成"
    )
    _save_full_test_state(state)

    print(
        f"[FULL-TEST] #{video_id} 完成 → "
        f"{slot.strftime('%H:%M')} private投稿キュー"
    )


def reschedule_missed() -> int:
    """
    投稿時刻を大きく過ぎた queued 動画は、その場でまとめて投稿せず、
    次の空き投稿枠へ順番に繰り越す。
    """
    init_db()
    now = _now()
    cutoff = now - timedelta(minutes=max(settings.post_grace_minutes, 0))
    queued = queued_items()

    full_test = _full_test_state()
    test_ids = {
        int(value)
        for value in full_test.get("video_ids") or []
        if str(value).isdigit()
    } if full_test.get("active") else set()

    overdue = [
        item
        for item in queued
        if _parse_iso(item["scheduled_for"]) < cutoff
        and int(item["video_id"]) not in test_ids
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

def _generation_runtime_ready() -> bool:
    problems: list[str] = []
    if not OllamaClient().available():
        problems.append("Ollama")
    if not VoicevoxClient().available():
        problems.append("VOICEVOX")
    if not shutil.which("ffmpeg"):
        problems.append("FFmpeg")

    if problems:
        print(
            "[SCHEDULE] 動画生成を見送ります。未起動/未検出: "
            + ", ".join(problems)
        )
        return False
    return True

def prepare_upcoming() -> None:
    """
    常に「次の投稿枠」を POSTS_PER_DAY 本ぶん先回りして準備する。
    夜に実行した場合は自動的に翌日の枠へ回る。
    """
    init_db()
    reschedule_missed()

    if not _generation_runtime_ready():
        return

    now = _now()
    queued = queued_items()
    future_queued = [
        item
        for item in queued
        if _parse_iso(item["scheduled_for"]) > now
    ]

    target = posts_per_day()
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

    minutes_to_next = (
        free_slots[0] - now
    ).total_seconds() / 60 if free_slots else 9999
    urgent = minutes_to_next <= max(
        5,
        int(settings.resource_urgent_minutes),
    )
    decision = background_production_decision(
        urgent=urgent,
    )
    set_channel_state(
        "runtime_resource_snapshot",
        json.dumps(
            decision.snapshot,
            ensure_ascii=False,
        ),
    )

    if not decision.allowed:
        print(
            "[RESOURCE] 自動制作を今回は延期: "
            + decision.reason
        )
        return

    set_channel_state(
        "runtime_resource_mode",
        decision.mode,
    )
    print(
        f"[RESOURCE] 制作開始 mode={decision.mode}: "
        f"{decision.reason}"
    )

    try:
        results = run_generation(
            render=True,
            upload=False,
            target_override=min(missing, len(free_slots)),
        )
    finally:
        set_channel_state(
            "runtime_resource_mode",
            "",
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
    full_test = _full_test_state()
    if full_test.get("active"):
        try:
            oldest_allowed = datetime.fromisoformat(
                str(full_test.get("started_at"))
            )
        except Exception:
            oldest_allowed = now - timedelta(hours=12)
    else:
        oldest_allowed = now - timedelta(
            minutes=max(settings.post_grace_minutes, 0)
        )

    rows = due_queue(
        now.isoformat(timespec="minutes"),
        oldest_allowed.isoformat(timespec="minutes"),
    )

    if full_test.get("active"):
        test_ids = {
            int(value)
            for value in full_test.get("video_ids") or []
            if str(value).isdigit()
        }
        rows = [
            row
            for row in rows
            if int(row["video_id"]) in test_ids
        ]

    if not rows:
        print("[SCHEDULE] 現在、投稿時刻を迎えた動画はありません。")
        _maybe_finish_full_test()
        return

    if not auto_upload_enabled():
        print(
            f"[SCHEDULE] {len(rows)}本が投稿時刻を迎えていますが、"
            "Web/設定上の自動投稿がOFFのため投稿しません。"
        )
        _maybe_finish_full_test()
        return

    for row in rows:
        try:
            output_path = row.get("output_path")
            if not output_path:
                raise FileNotFoundError("動画ファイルのパスがありません")

            youtube_id = upload_video(
                video_path=Path(output_path),
                title=row["title"],
                description=row.get("description") or (
                    "AIが自分で企画・制作・分析しながら"
                    "成長するチャンネルです。"
                ),
                tags=json.loads(row.get("tags_json") or "[]"),
                privacy_status=upload_privacy(),
                category_id=settings.youtube_category_id,
                default_language=settings.youtube_default_language,
                contains_synthetic_media=settings.youtube_contains_synthetic_media,
            )
            mark_uploaded(row["video_id"], youtube_id)
            mark_queue_uploaded(
                row["queue_id"],
                _now().isoformat(timespec="seconds"),
            )
            print(
                f"[AUTO-UPLOAD] #{row['video_id']} -> {youtube_id} "
                f"[{upload_privacy()}]"
            )
            active_test = _full_test_state()
            test_ids = {
                int(value)
                for value in active_test.get("video_ids") or []
                if str(value).isdigit()
            } if active_test.get("active") else set()

            if int(row["video_id"]) in test_ids:
                print(
                    "[FULL-TEST] 確認用にローカル動画を残します。"
                )
            else:
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
            record_failure(
                "youtube.upload",
                exc,
                {
                    "video_id": row.get("video_id"),
                    "queue_id": row.get("queue_id"),
                },
            )
            print(
                f"[AUTO-UPLOAD] #{row['video_id']} 失敗 "
                f"(試行 {row.get('attempts', 0) + 1}/5): {exc}"
            )

    _maybe_finish_full_test()

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
    full_test = _full_test_state()
    if full_test.get("active"):
        # 完全テスト中は通常の補充を止める。
        # 1本を最後まで完成させてキューへ入れ、
        # その直後に投稿時刻判定をする。
        print("[FULL-TEST] テストセッション中: 1本ずつ直列運転")
        _advance_full_test_generation()
        run_due()
        _maybe_finish_full_test()
        return

    # 通常運転。
    run_growth_cycle()
    try:
        maybe_run_improvement_review(min_hours=12)
    except Exception as exc:
        record_failure("improvement.review", exc)
        print(f"[IMPROVEMENT] AI改善分析をスキップ: {exc}")
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
