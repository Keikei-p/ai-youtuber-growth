from __future__ import annotations
import argparse
import json
import os
import shutil
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from ai_client import OllamaClient
from autonomous_recovery import run_autonomous_recovery
from config import settings
from growth_engine import run_growth_cycle
from delivery_supervisor import self_heal_delivery_controls
from legal_guard import publish_gate
from main import load_character, render_results, run_generation
from paths import VIDEO_DIR
from native_models.auto_train import maybe_run_native_retraining
from resource_governor import background_production_decision
from runtime_bootstrap import bootstrap_runtime
from resilience_learning import record_stage_success
from self_improvement import (
    maybe_run_improvement_review,
    record_failure,
)
from runtime_control import (
    automation_enabled,
    auto_upload_enabled,
    execution_mode,
    post_times,
    posts_per_day,
    set_auto_upload_enabled,
    set_automation_enabled,
    set_upload_privacy,
    upload_privacy,
    runtime_cancel_requested,
)
from media_cleanup import cleanup_uploaded_media
from storage import (
    acquire_runtime_lock,
    acquire_upload_lock,
    active_guests,
    cancel_queued_videos,
    due_queue,
    init_db,
    mark_queue_error,
    mark_queue_uploaded,
    mark_uploaded,
    mark_video_queue_uploaded,
    occupied_schedule_times,
    pending_upload_receipt,
    record_upload_receipt,
    queue_item_for_video,
    queue_video,
    queued_items,
    release_runtime_lock,
    release_upload_lock,
    reset_queue_for_video,
    get_channel_state,
    set_channel_state,
    set_upload_receipt_thumbnail,
    update_queue_schedule,
    update_upload_receipt_verification,
    update_video_output,
    uploaded_videos,
    video_by_id,
)
from voice.provider import voice_attribution_status, voice_provider_status
from upload_recovery import recover_upload_failure
from youtube.auth import get_credentials
from youtube.post_verifier import (
    UploadVerificationError,
    verify_uploaded_video,
)
from youtube.thumbnail import set_custom_thumbnail
from youtube.market_research import maybe_refresh_market_research
from youtube.uploader import (
    find_recent_matching_upload,
    upload_video,
)

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

def _production_armed() -> bool:
    return (
        get_channel_state(
            "production_autonomy_armed",
            "false",
        ).strip().lower()
        == "true"
    )


def _uploads_today(now: datetime) -> int:
    count = 0
    for row in uploaded_videos(limit=100):
        raw = str(row.get("uploaded_at") or "").strip()
        if not raw:
            continue
        try:
            uploaded = datetime.fromisoformat(raw)
            if uploaded.tzinfo is None:
                uploaded = uploaded.replace(tzinfo=_tz())
            uploaded = uploaded.astimezone(_tz())
        except Exception:
            continue
        if uploaded.date() == now.date():
            count += 1
    return count


def _catchup_slot(now: datetime, queued: list[dict]) -> datetime | None:
    """
    本番arm中に今日の投稿時刻を過ぎたのに、投稿対象のqueue自体が
    存在しない場合の救済枠。

    既存queueがある場合はrun_due()へ任せ、余計な動画を増やさない。
    今日すでに必要本数を投稿済みならcatch-upしない。
    """
    if not _production_armed():
        return None
    if runtime_cancel_requested():
        return None
    due_queued = [
        item
        for item in queued
        if _parse_iso(str(item.get("scheduled_for") or "")) <= now
    ]
    if due_queued:
        # 既存の期限到来queueを新規生成で追い越さない。
        return None

    passed_slots = [
        slot
        for slot in _slot_datetimes(now.date())
        if slot <= now
    ]
    if not passed_slots:
        return None

    expected_by_now = min(
        len(passed_slots),
        max(1, posts_per_day()),
    )
    uploaded_today = _uploads_today(now)
    if uploaded_today >= expected_by_now:
        return None

    latest = passed_slots[-1]
    max_age = timedelta(
        hours=max(
            int(settings.post_sleep_catchup_hours),
            1,
        )
    )
    if now - latest > max_age:
        return None

    return now.replace(second=0, microsecond=0)


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
    if (
        not results
        or not results[0].get("output_path")
        or results[0].get("quality_passed") is False
    ):
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
    grace_cutoff = now - timedelta(
        minutes=max(settings.post_grace_minutes, 0)
    )
    retry_cutoff = now - timedelta(
        hours=max(int(settings.post_sleep_catchup_hours), 1)
    )
    queued = queued_items()

    full_test = _full_test_state()
    test_ids = {
        int(value)
        for value in full_test.get("video_ids") or []
        if str(value).isdigit()
    } if full_test.get("active") else set()

    overdue: list[dict] = []
    for item in queued:
        if int(item["video_id"]) in test_ids:
            continue
        scheduled = _parse_iso(item["scheduled_for"])
        attempts = int(item.get("attempts") or 0)
        # 本番完全自動運用中は、未試行でもcatch-up期間内なら
        # 次枠へ逃がさない。起動後run_due()がその場で回収する。
        if _production_armed():
            if scheduled < retry_cutoff:
                overdue.append(item)
        elif attempts <= 0 and scheduled < grace_cutoff:
            overdue.append(item)
        elif attempts > 0 and scheduled < retry_cutoff:
            overdue.append(item)

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

def ensure_first_episode_delivery() -> dict:
    """
    第1話は「生成完了」ではなく「YouTube投稿成功」まで未完了扱い。
    既存の第1話IDがあれば、その動画を最優先で復旧・即時キューへ戻す。
    """
    raw_id = get_channel_state(
        "mirai_first_episode_video_id",
        "",
    ).strip()
    if not raw_id.isdigit():
        return {"status": "not_created"}

    video_id = int(raw_id)
    row = video_by_id(video_id)
    if not row:
        set_channel_state(
            "mirai_first_episode_video_id",
            "",
        )
        set_channel_state(
            "mirai_first_episode_completed",
            "false",
        )
        set_channel_state(
            "mirai_first_episode_uploaded",
            "false",
        )
        return {"status": "missing_record"}

    if str(row.get("youtube_video_id") or "").strip():
        set_channel_state(
            "mirai_first_episode_uploaded",
            "true",
        )
        return {
            "status": "uploaded",
            "video_id": video_id,
            "youtube_video_id": row.get("youtube_video_id"),
        }

    # レンダリング済みフラグは互換用として保持するが、
    # 投稿成功までは first_episode_uploaded=false のまま。
    set_channel_state(
        "mirai_first_episode_uploaded",
        "false",
    )

    output = str(row.get("output_path") or "").strip()
    if not output or not Path(output).is_file():
        try:
            row = regenerate_saved_video(video_id)
        except Exception as exc:
            record_failure(
                "episode1.regenerate",
                exc,
                {"video_id": video_id},
            )
            return {
                "status": "regeneration_failed",
                "video_id": video_id,
                "error": str(exc),
            }

    now = _now()
    queue = queue_item_for_video(video_id)
    due_now = now.isoformat(timespec="minutes")
    recovery_backoff = (
        str((queue or {}).get("error") or "").startswith(
            ("[AUTO-RECOVERY:", "[ACTION-REQUIRED:")
        )
    )
    if (
        not queue
        or queue.get("status") != "queued"
        or (
            _parse_iso(str(queue.get("scheduled_for"))) > now
            and not recovery_backoff
        )
    ):
        reset_queue_for_video(
            video_id,
            due_now,
            error="[EPISODE-1] delivery recovery",
        )

    set_channel_state(
        "first_episode_delivery_last",
        json.dumps(
            {
                "video_id": video_id,
                "scheduled_for": due_now,
                "status": "queued_priority",
            },
            ensure_ascii=False,
        ),
    )
    return {
        "status": "queued_priority",
        "video_id": video_id,
        "scheduled_for": due_now,
    }


def _generation_runtime_ready() -> bool:
    """
    動画生成が本当に必要になった時だけAIサービスを起動・確認する。
    待機中のtickでは呼ばない。

    Cloud制作ではメディア依存はWorker側が持つため、
    ローカルは企画用Ollamaだけ確認する。
    """
    if execution_mode() == "cloud":
        if OllamaClient().available():
            return True
        print(
            "[SCHEDULE] Cloud制作の企画用Ollamaへ接続できません。"
        )
        return False

    try:
        runtime = bootstrap_runtime()
    except Exception as exc:
        print(
            "[SCHEDULE] 制作runtimeの自動起動に失敗: "
            + str(exc)
        )
        return False

    if bool(runtime.get("ready")):
        return True

    print(
        "[SCHEDULE] 制作runtimeが未準備のため今回は延期: "
        + ", ".join(
            str(x)
            for x in (runtime.get("blockers") or [])
        )
    )
    return False

def prepare_upcoming() -> None:
    """
    常に「次の投稿枠」を POSTS_PER_DAY 本ぶん先回りして準備する。
    夜に実行した場合は自動的に翌日の枠へ回る。
    ただし第1話が生成済み未投稿なら、まず第1話の配送を完了させる。
    """
    init_db()
    reschedule_missed()

    first_episode = ensure_first_episode_delivery()
    first_episode_bootstrap = first_episode.get("status") in {
        "not_created",
        "missing_record",
    }
    if first_episode.get("status") in {
        "queued_priority",
        "regeneration_failed",
    }:
        print(
            "[EPISODE-1] 第1話がYouTubeへ届くまで"
            "2話以降の自動生成を保留します。"
        )
        return

    now = _now()
    queued = queued_items()
    future_queued = [
        item
        for item in queued
        if _parse_iso(item["scheduled_for"]) > now
    ]

    # 初回は第1話だけ。投稿成功後に通常の日次本数へ戻す。
    target = 1 if first_episode_bootstrap else posts_per_day()
    catchup = _catchup_slot(now, queued)
    missing = max(target - len(future_queued), 0)
    if catchup is not None:
        # 明日分など未来queueが目標数そろっていても、
        # 今日の未投稿枠は別件として1本だけ即時回収する。
        missing = max(missing, 1)

    if missing <= 0:
        print(
            f"[SCHEDULE] 次の投稿キューは準備済み: "
            f"{len(future_queued)}本"
        )
        return

    free_slots: list[datetime] = []
    if catchup is not None:
        free_slots.append(catchup)
        set_channel_state(
            "autopost_catchup_last",
            json.dumps(
                {
                    "status": "scheduled_immediate",
                    "scheduled_for": catchup.isoformat(
                        timespec="minutes"
                    ),
                    "reason": (
                        "今日の投稿時刻を過ぎた状態で"
                        "投稿queueが空だったため即時回収"
                    ),
                },
                ensure_ascii=False,
            ),
        )
        print(
            "[AUTO-CATCHUP] 今日の未投稿枠を検出。"
            "新規生成後すぐ投稿する救済枠を追加します。"
        )

    remaining = max(missing - len(free_slots), 0)
    if remaining:
        free_slots.extend(
            _next_free_slots(now, remaining)
        )
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

    # 本当に不足分を生成する時だけAIサービスを起動する。
    # 既存のruntime gateを維持し、内部でbootstrapする。
    if not _generation_runtime_ready():
        set_channel_state(
            "runtime_resource_mode",
            "",
        )
        return

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
        if (
            not item.get("output_path")
            or item.get("quality_passed") is False
        ):
            print(
                f"[SCHEDULE] #{item['id']} は未生成または品質不合格のため"
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

def _safe_upload_metadata(row: dict) -> tuple[str, str, list[str]]:
    video_id = int(
        row.get("video_id")
        or row.get("id")
        or 0
    )
    strict = (
        video_id > 0
        and get_channel_state(
            f"metadata_sanitize_requested_{video_id}",
            "false",
        ).strip().lower()
        == "true"
    )

    def clean_text(value: object, *, keep_lines: bool) -> str:
        out: list[str] = []
        for char in str(value or ""):
            code = ord(char)
            if code >= 32:
                out.append(char)
            elif keep_lines and char in {"\n", "\t"}:
                out.append(char)
        return "".join(out)

    title_limit = 90 if strict else 100
    title = " ".join(
        clean_text(row.get("title"), keep_lines=False).split()
    )[:title_limit]
    if not title:
        title = "ミライ AI YouTuber"

    description_limit = 4500 if strict else 5000
    description = clean_text(
        row.get("description")
        or "AIが自分で企画・制作・分析しながら成長するチャンネルです。",
        keep_lines=True,
    )[:description_limit]

    try:
        raw_tags = json.loads(row.get("tags_json") or "[]")
    except Exception:
        raw_tags = []
    if not isinstance(raw_tags, list):
        raw_tags = []

    tags: list[str] = []
    total = 0
    max_tags = 15 if strict else 30
    max_total = 300 if strict else 450
    max_tag_len = 40 if strict else 60
    for raw in raw_tags:
        tag = " ".join(
            clean_text(raw, keep_lines=False).split()
        ).strip("#, ")
        if not tag or tag in tags:
            continue
        tag = tag[:max_tag_len]
        projected = total + len(tag) + (1 if tags else 0)
        if projected > max_total or len(tags) >= max_tags:
            break
        tags.append(tag)
        total = projected

    return title, description, tags


def regenerate_saved_video(video_id: int) -> dict:
    """
    投稿予定のMP4が欠損している場合、保存済み台本とメタデータから
    同じ動画レコードを再レンダリングする。新しい企画IDは作らない。
    """
    row = video_by_id(int(video_id))
    if not row:
        raise ValueError(f"動画 #{video_id} が見つかりません")

    item = {
        "id": int(video_id),
        "idea": {
            "idea": str(row.get("idea") or row.get("title") or ""),
            "angle": str(row.get("angle") or ""),
        },
        "angle": str(row.get("angle") or ""),
        "title": str(row.get("title") or ""),
        "script": str(row.get("script") or ""),
        "description": str(row.get("description") or ""),
        "tags": json.loads(row.get("tags_json") or "[]"),
        "status": "rendering",
    }

    guest_id = row.get("guest_id")
    if guest_id:
        guest = next(
            (
                candidate
                for candidate in active_guests(100)
                if int(candidate["id"]) == int(guest_id)
            ),
            None,
        )
        if guest:
            item["guest"] = guest

    print(f"[AUTO-RECOVERY] #{video_id} 欠損動画を保存済み台本から再生成")
    render_results([item], load_character())

    refreshed = video_by_id(int(video_id))
    output = str((refreshed or {}).get("output_path") or "").strip()
    if not output or not Path(output).is_file():
        raise RuntimeError(
            f"動画 #{video_id} の自動再生成後もMP4を確認できません"
        )
    set_channel_state(
        f"video_regeneration_requested_{video_id}",
        "false",
    )
    return refreshed or row


def _repair_legacy_video_output_path(row: dict) -> dict:
    """
    旧バージョンで残った
      output/videos/YYYYMMDD/_35.mp4
    のようなパスを、現行の
      output/videos/YYYYMMDD_35.mp4
    へ自動修復する。

    それ以外でも同じvideo_idの完成MP4がVIDEO_DIR直下に1つ見つかれば
    DBのoutput_pathを現物へ合わせる。
    """
    video_id = int(row.get("video_id") or row.get("id") or 0)
    raw = str(row.get("output_path") or "").strip()
    if video_id <= 0 or not raw:
        return row

    original = Path(raw)
    if original.is_file():
        return row

    candidates: list[Path] = []

    # 旧形式: .../videos/20260928/_35.mp4
    parent_name = original.parent.name
    if (
        len(parent_name) == 8
        and parent_name.isdigit()
        and original.name.startswith("_")
    ):
        candidates.append(
            original.parent.parent
            / f"{parent_name}{original.name}"
        )

    # 現行形式の同一IDファイルを安全に探索。
    try:
        candidates.extend(
            sorted(
                VIDEO_DIR.glob(f"*_{video_id}.mp4"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
        )
    except OSError:
        pass

    seen: set[str] = set()
    for candidate in candidates:
        key = str(candidate.resolve())
        if key in seen:
            continue
        seen.add(key)
        if not candidate.is_file():
            continue

        status = str(row.get("status") or "rendered")
        update_video_output(
            video_id,
            str(candidate),
            status=status,
        )
        repaired = dict(row)
        repaired["output_path"] = str(candidate)
        set_channel_state(
            f"video_output_path_repaired_{video_id}",
            json.dumps(
                {
                    "from": raw,
                    "to": str(candidate),
                    "repaired_at": _now().isoformat(
                        timespec="seconds"
                    ),
                },
                ensure_ascii=False,
            ),
        )
        print(
            f"[AUTO-RECOVERY] #{video_id} 古い動画パスを修復: "
            f"{raw} -> {candidate}"
        )
        return repaired

    return row


def _ensure_video_output(row: dict) -> dict:
    video_id = int(row.get("video_id") or row.get("id") or 0)
    row = _repair_legacy_video_output_path(row)
    regeneration_requested = (
        get_channel_state(
            f"video_regeneration_requested_{video_id}",
            "false",
        ).strip().lower() == "true"
    )
    output = str(row.get("output_path") or "").strip()
    if (
        not regeneration_requested
        and output
        and Path(output).is_file()
    ):
        return row
    return regenerate_saved_video(video_id)


def _ensure_upload_file_now(row: dict) -> dict:
    """
    _ensure_video_output()後に別処理や旧データ不整合でファイルが消えても、
    YouTubeへ渡す直前にもう一度確認して自動再生成する。
    """
    row = _ensure_video_output(row)
    output = str(row.get("output_path") or "").strip()
    if output and Path(output).is_file():
        return row

    video_id = int(row.get("video_id") or row.get("id") or 0)
    refreshed = regenerate_saved_video(video_id)
    output = str(refreshed.get("output_path") or "").strip()
    if not output or not Path(output).is_file():
        raise RuntimeError(
            f"動画 #{video_id} の投稿直前MP4を復旧できませんでした"
        )
    return refreshed


def _try_set_thumbnail(
    video_id: int,
    youtube_id: str,
    row: dict,
    receipt: dict,
) -> None:
    thumbnail = str(
        receipt.get("thumbnail_path")
        or row.get("thumbnail_path")
        or ""
    ).strip()
    if not thumbnail or bool(receipt.get("thumbnail_set")):
        return

    try:
        result = set_custom_thumbnail(youtube_id, thumbnail)
        if result.get("ok"):
            set_upload_receipt_thumbnail(
                video_id,
                thumbnail_set=True,
            )
            print(
                f"[YOUTUBE] #{video_id} custom thumbnail set"
            )
    except Exception as exc:
        # チャンネル権限等でcustom thumbnail不可でも、
        # YouTube自動サムネイルが存在すれば投稿完了は継続できる。
        record_failure(
            "youtube.thumbnail",
            exc,
            {
                "video_id": video_id,
                "youtube_video_id": youtube_id,
                "thumbnail_path": thumbnail,
            },
        )
        print(
            f"[YOUTUBE] #{video_id} custom thumbnail skipped: {exc}"
        )


def _verify_receipt_and_finalize(
    *,
    video_id: int,
    row: dict,
    receipt: dict,
    source: str,
    previous_youtube_id: str | None = None,
) -> dict:
    youtube_id = str(receipt["youtube_video_id"])
    try:
        verification = verify_uploaded_video(
            youtube_id,
            expected_title=str(receipt.get("expected_title") or ""),
            expected_description=str(
                receipt.get("expected_description") or ""
            ),
            expected_privacy=str(
                receipt.get("expected_privacy") or ""
            ),
        )
    except UploadVerificationError as exc:
        update_upload_receipt_verification(
            video_id,
            status=(
                "failed"
                if exc.terminal
                else "processing"
            ),
            detail=exc.detail,
            error=str(exc),
        )
        raise

    # YouTube側の変換完了後にcustom thumbnailを設定する。
    _try_set_thumbnail(video_id, youtube_id, row, receipt)

    update_upload_receipt_verification(
        video_id,
        status="verified",
        detail=verification,
    )
    uploaded_at = _now().isoformat(timespec="seconds")
    privacy = str(
        receipt.get("expected_privacy") or upload_privacy()
    )
    mark_uploaded(
        video_id,
        youtube_id,
        uploaded_at,
        privacy_status=privacy,
        source=source,
        replaced_youtube_video_id=previous_youtube_id,
    )
    set_channel_state("youtube_auth_attention", "false")
    set_channel_state(
        f"metadata_sanitize_requested_{video_id}",
        "false",
    )
    set_channel_state(
        f"publish_guard_attention_{video_id}",
        "",
    )
    set_channel_state(
        f"upload_recovery_attention_{video_id}",
        "",
    )
    _clear_upload_intent(video_id)
    return {
        "youtube_video_id": youtube_id,
        "uploaded_at": uploaded_at,
        "privacy": privacy,
        "verification": verification,
    }


def upload_saved_video_now(
    video_id: int,
    privacy_status: str | None = None,
    *,
    force_reupload: bool = False,
) -> dict:
    """
    生成ライブラリから完成済み動画を即時投稿/再投稿する。
    API応答だけで完了にせず、YouTube側処理完了まで確認する。
    """
    init_db()
    video_id = int(video_id)
    if not acquire_upload_lock(
        video_id,
        owner="library_repost" if force_reupload else "library_manual",
    ):
        raise RuntimeError(
            f"動画 #{video_id} は別の投稿処理が実行中です。"
        )

    try:
        row = video_by_id(video_id)
        if not row:
            raise ValueError(f"動画 #{video_id} が見つかりません")

        existing = str(row.get("youtube_video_id") or "").strip()
        if existing and not force_reupload:
            return {
                "status": "already_uploaded",
                "video_id": video_id,
                "youtube_video_id": existing,
                "privacy": None,
            }

        source = "library_repost" if force_reupload else "library_manual"
        if not force_reupload:
            receipt = pending_upload_receipt(video_id)
            if receipt:
                finalized = _verify_receipt_and_finalize(
                    video_id=video_id,
                    row=row,
                    receipt=receipt,
                    source=source,
                    previous_youtube_id=existing or None,
                )
                mark_video_queue_uploaded(
                    video_id,
                    finalized["uploaded_at"],
                )
                return {
                    "status": "verified_existing_upload",
                    "video_id": video_id,
                    "previous_youtube_video_id": existing or None,
                    **finalized,
                }

        row = _ensure_video_output(row)
        candidate = Path(str(row.get("output_path") or ""))
        privacy = str(privacy_status or upload_privacy()).strip().lower()
        if privacy not in {"private", "unlisted", "public"}:
            raise ValueError("privacy must be private, unlisted, or public")

        voice_status = voice_attribution_status()
        required_credit = (
            voice_status["credit"]
            if voice_status["resolved"]
            else "__UNRESOLVED_REQUIRED_VOICE_CREDIT__"
        )
        gate = publish_gate(
            row,
            required_credit=required_credit,
        )
        if not gate["allowed"]:
            risks = ", ".join(
                str(item)
                for item in (gate.get("risks") or [])
            )
            raise RuntimeError(
                "公開前確認待ちのため投稿を停止しました。"
                + (f" {risks}" if risks else "")
            )

        title, description, tags = _safe_upload_metadata(row)
        try:
            youtube_id = upload_video(
                video_path=candidate,
                title=title,
                description=description,
                tags=tags,
                privacy_status=privacy,
                category_id=settings.youtube_category_id,
                default_language=settings.youtube_default_language,
                contains_synthetic_media=True,
            )
            record_upload_receipt(
                video_id,
                youtube_id,
                source=source,
                expected_title=title,
                expected_description=description,
                expected_privacy=privacy,
                thumbnail_path=str(
                    row.get("thumbnail_path") or ""
                ) or None,
            )
            receipt = pending_upload_receipt(video_id)
            if not receipt:
                raise RuntimeError(
                    "YouTube ID取得後の投稿レシート保存に失敗しました。"
                )
            finalized = _verify_receipt_and_finalize(
                video_id=video_id,
                row=row,
                receipt=receipt,
                source=source,
                previous_youtube_id=existing or None,
            )
        except Exception as exc:
            record_failure(
                "youtube.manual_repost"
                if force_reupload
                else "youtube.manual_upload",
                exc,
                {
                    "video_id": video_id,
                    "previous_youtube_id": existing,
                },
            )
            raise

        mark_video_queue_uploaded(
            video_id,
            finalized["uploaded_at"],
        )
        print(
            f"[MANUAL-UPLOAD] #{video_id} -> "
            f"{finalized['youtube_video_id']} "
            f"[{privacy}] verified"
        )
        return {
            "status": "reuploaded" if force_reupload else "uploaded",
            "video_id": video_id,
            "previous_youtube_video_id": existing or None,
            **finalized,
        }
    finally:
        release_upload_lock(video_id)

def _upload_intent_key(video_id: int) -> str:
    return f"youtube_upload_intent_{int(video_id)}"


def _load_upload_intent(video_id: int) -> dict:
    raw = get_channel_state(
        _upload_intent_key(video_id),
        "",
    ).strip()
    if not raw:
        return {}
    try:
        value = json.loads(raw)
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def _save_upload_intent(
    video_id: int,
    *,
    title: str,
    description: str,
    privacy: str,
    queue_id: int | None = None,
) -> dict:
    payload = {
        "video_id": int(video_id),
        "queue_id": int(queue_id or 0),
        "started_at": _now().isoformat(timespec="seconds"),
        "title": str(title),
        "description": str(description),
        "privacy": str(privacy),
        "status": "upload_started",
    }
    set_channel_state(
        _upload_intent_key(video_id),
        json.dumps(payload, ensure_ascii=False),
    )
    return payload


def _clear_upload_intent(video_id: int) -> None:
    set_channel_state(
        _upload_intent_key(video_id),
        "",
    )


def _reconcile_upload_intent(
    *,
    video_id: int,
    title: str,
    description: str,
    privacy: str,
    thumbnail_path: str | None,
    source: str,
) -> dict | None:
    """
    YouTubeが動画IDを返した直後、receipt保存前にプロセスが落ちても
    次回tickでremote側を照合し、同じ動画を再uploadしない。
    """
    intent = _load_upload_intent(video_id)
    if not intent:
        return None

    recovered_id = find_recent_matching_upload(
        title=title,
        description=description,
        started_at=str(intent.get("started_at") or ""),
    )
    if recovered_id:
        record_upload_receipt(
            video_id,
            recovered_id,
            source=source + "_reconciled",
            expected_title=title,
            expected_description=description,
            expected_privacy=privacy,
            thumbnail_path=thumbnail_path,
        )
        receipt = pending_upload_receipt(video_id)
        if receipt:
            _clear_upload_intent(video_id)
            print(
                f"[AUTO-UPLOAD] #{video_id} "
                f"中断前のYouTube投稿 {recovered_id} を回収しました。"
            )
            return receipt

    started_raw = str(intent.get("started_at") or "").strip()
    try:
        now_value = _now()
        started = datetime.fromisoformat(started_raw)
        if started.tzinfo is None:
            started = started.replace(tzinfo=now_value.tzinfo)
        elif now_value.tzinfo is not None:
            started = started.astimezone(now_value.tzinfo)
        age_minutes = (
            now_value - started
        ).total_seconds() / 60
    except Exception:
        age_minutes = 999.0

    grace = max(
        1,
        min(
            int(
                getattr(
                    settings,
                    "youtube_reconcile_grace_minutes",
                    3,
                )
            ),
            15,
        ),
    )
    if age_minutes < grace:
        raise RuntimeError(
            "youtube_upload_reconcile_pending: "
            "直前の投稿結果をYouTube側で確認中です。"
        )

    # remoteに見つからず猶予時間も超えた場合だけ再uploadを許可。
    return None


def _save_auto_post_event(
    status: str,
    *,
    video_id: int | None = None,
    queue_id: int | None = None,
    detail: str = "",
    extra: dict | None = None,
) -> None:
    payload = {
        "status": str(status),
        "checked_at": _now().isoformat(timespec="seconds"),
        "video_id": video_id,
        "queue_id": queue_id,
        "detail": str(detail or "")[:2000],
    }
    if extra:
        payload.update(extra)
    set_channel_state(
        "auto_post_last_event",
        json.dumps(payload, ensure_ascii=False),
    )


def _upload_runtime_block_reason() -> str:
    if bool(getattr(settings, "dry_run", False)):
        production_armed = get_channel_state(
            "production_autonomy_armed",
            "false",
        ).strip().lower() == "true"
        if not production_armed:
            return (
                "DRY_RUN=true かつ本番自動投稿が未承認です。"
                " 管理画面で自動投稿を明示ONにしてください。"
            )
    return ""


def run_due() -> None:
    init_db()
    set_channel_state(
        "scheduler_last_run_due_at",
        _now().isoformat(timespec="seconds"),
    )
    delivery_state = self_heal_delivery_controls()
    if delivery_state.get("repairs"):
        print(
            "[DELIVERY-SUPERVISOR] "
            + " / ".join(delivery_state["repairs"])
        )

    if runtime_cancel_requested():
        _save_auto_post_event(
            "blocked",
            detail="安全停止が有効です。",
            extra={"code": "safety_stop"},
        )
        print("[SCHEDULE] 安全停止中のためYouTube投稿を実行しません。")
        return

    first_episode = ensure_first_episode_delivery()
    if first_episode.get("status") == "queued_priority":
        print(
            f"[EPISODE-1] #{first_episode['video_id']} を"
            "最優先投稿キューへ復旧しました。"
        )

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
        # PCがスリープして投稿時刻を過ぎても、復帰後一定時間は
        # 「取りこぼし」ではなくcatch-up投稿として扱う。
        oldest_allowed = now - timedelta(
            hours=max(int(settings.post_sleep_catchup_hours), 1)
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
    elif len(rows) > 1:
        # 通常の数分差キューは従来どおり処理する。
        # 20分以上前の古い枠が混ざった「スリープ復帰catch-up」の時だけ
        # 一気に連投せず、現在時刻に最も近い1本を優先する。
        grace_cutoff = now - timedelta(
            minutes=max(settings.post_grace_minutes, 0)
        )
        has_stale_row = any(
            _parse_iso(row["scheduled_for"]) < grace_cutoff
            for row in rows
        )
        if has_stale_row:
            rows = sorted(
                rows,
                key=lambda row: _parse_iso(row["scheduled_for"]),
                reverse=True,
            )[:1]
            print(
                "[SCHEDULE] スリープ復帰catch-up: "
                "直近1本だけ投稿し、古い未投稿分は次枠へ繰り越します。"
            )

    if not rows:
        _save_auto_post_event(
            "idle",
            detail="現在、投稿時刻を迎えた動画はありません。",
            extra={"due_count": 0},
        )
        print("[SCHEDULE] 現在、投稿時刻を迎えた動画はありません。")
        _maybe_finish_full_test()
        return

    block_reason = _upload_runtime_block_reason()
    if block_reason:
        record_failure(
            "youtube.runtime_block",
            block_reason,
            {
                "due_count": len(rows),
                "dry_run": bool(getattr(settings, "dry_run", False)),
            },
        )
        _save_auto_post_event(
            "blocked",
            detail=block_reason,
            extra={
                "code": "runtime_block",
                "due_count": len(rows),
            },
        )
        print(f"[SCHEDULE] 投稿停止: {block_reason}")
        _maybe_finish_full_test()
        return

    if not auto_upload_enabled():
        first_id = get_channel_state(
            "mirai_first_episode_video_id",
            "",
        ).strip()
        first_pending = (
            get_channel_state(
                "mirai_first_episode_uploaded",
                "false",
            ).strip().lower() != "true"
        )
        production_armed = get_channel_state(
            "production_autonomy_armed",
            "false",
        ).strip().lower() == "true"
        only_first_episode = (
            production_armed
            and automation_enabled()
            and first_pending
            and first_id.isdigit()
            and rows
            and all(
                int(row["video_id"]) == int(first_id)
                for row in rows
            )
        )
        if not only_first_episode:
            _save_auto_post_event(
                "blocked",
                detail="YouTube自動投稿がOFFです。",
                extra={
                    "code": "auto_upload_off",
                    "due_count": len(rows),
                },
            )
            print(
                f"[SCHEDULE] {len(rows)}本が投稿時刻を迎えていますが、"
                "自動投稿がOFFのため投稿しません。"
            )
            _maybe_finish_full_test()
            return
        set_auto_upload_enabled(True)
        print(
            "[EPISODE-1] 自動運転ON / 自動投稿OFFの不整合を検出。"
            "第1話配送のため自動投稿をONへ自己修復しました。"
        )

    for row in rows:
        video_id = int(row["video_id"])
        if not acquire_upload_lock(
            video_id,
            owner=f"auto_queue:{row['queue_id']}",
        ):
            _save_auto_post_event(
                "busy",
                video_id=video_id,
                queue_id=int(row["queue_id"]),
                detail="別処理が同じ動画を投稿中です。",
            )
            print(
                f"[AUTO-UPLOAD] #{video_id} は別処理が投稿中のため"
                "このtickではスキップします。"
            )
            continue

        try:
            _save_auto_post_event(
                "attempting",
                video_id=video_id,
                queue_id=int(row["queue_id"]),
                detail="YouTube自動投稿を開始しました。",
                extra={
                    "scheduled_for": row.get("scheduled_for"),
                    "attempts": int(row.get("attempts") or 0),
                },
            )
            row = _ensure_video_output(row)
            output_path = str(row.get("output_path") or "")

            gate = publish_gate(
                row,
                required_credit=(
                    voice_attribution_status()["credit"]
                    if voice_attribution_status()["resolved"]
                    else "__UNRESOLVED_REQUIRED_VOICE_CREDIT__"
                ),
            )
            if not gate["allowed"]:
                risks = ", ".join(
                    str(value)
                    for value in (gate.get("risks") or [])
                )
                raise RuntimeError(
                    "公開前確認待ちのため投稿を停止しました。"
                    + (
                        f" approval_id={gate.get('approval_id')}"
                        if gate.get("approval_id")
                        else ""
                    )
                    + (f" / {risks}" if risks else "")
                )

            privacy = upload_privacy()
            title, description, tags = _safe_upload_metadata(row)
            receipt = pending_upload_receipt(video_id)
            thumbnail_path = str(
                row.get("thumbnail_path") or ""
            ) or None

            if receipt:
                print(
                    f"[AUTO-UPLOAD] #{video_id} 既存YouTubeレシート "
                    f"{receipt['youtube_video_id']} を再確認します。"
                )
            else:
                receipt = _reconcile_upload_intent(
                    video_id=video_id,
                    title=title,
                    description=description,
                    privacy=privacy,
                    thumbnail_path=thumbnail_path,
                    source="auto_schedule",
                )

            if not receipt:
                # metadata/gate/reconcile処理の間にファイルが消える競合も
                # 最後の瞬間に検知して復旧する。
                row = _ensure_upload_file_now(row)
                output_path = str(row.get("output_path") or "")
                _save_upload_intent(
                    video_id,
                    title=title,
                    description=description,
                    privacy=privacy,
                    queue_id=int(row["queue_id"]),
                )
                youtube_id = upload_video(
                    video_path=Path(output_path),
                    title=title,
                    description=description,
                    tags=tags,
                    privacy_status=privacy,
                    category_id=settings.youtube_category_id,
                    default_language=settings.youtube_default_language,
                    contains_synthetic_media=True,
                )
                record_upload_receipt(
                    video_id,
                    youtube_id,
                    source="auto_schedule",
                    expected_title=title,
                    expected_description=description,
                    expected_privacy=privacy,
                    thumbnail_path=thumbnail_path,
                )
                receipt = pending_upload_receipt(video_id)
                if not receipt:
                    raise RuntimeError(
                        "YouTube ID取得後の投稿レシート保存に失敗しました。"
                    )
                _clear_upload_intent(video_id)

            finalized = _verify_receipt_and_finalize(
                video_id=video_id,
                row=row,
                receipt=receipt,
                source="auto_schedule",
            )
            uploaded_at = finalized["uploaded_at"]
            youtube_id = finalized["youtube_video_id"]
            mark_queue_uploaded(
                row["queue_id"],
                uploaded_at,
            )
            _save_auto_post_event(
                "verified",
                video_id=video_id,
                queue_id=int(row["queue_id"]),
                detail="YouTube上の存在・処理完了・投稿情報を確認しました。",
                extra={
                    "youtube_video_id": youtube_id,
                    "privacy": finalized["privacy"],
                    "uploaded_at": uploaded_at,
                },
            )
            record_stage_success(
                "youtube.upload",
                action=(
                    "投稿レシートを保持して同じvideoIdを検証し、"
                    "YouTube上の処理完了確認まで待って成功"
                ),
                context={
                    "video_id": video_id,
                    "queue_id": int(row["queue_id"]),
                    "youtube_video_id": youtube_id,
                    "privacy": finalized["privacy"],
                },
            )
            print(
                f"[AUTO-UPLOAD] #{video_id} -> {youtube_id} "
                f"[{finalized['privacy']}] verified"
            )
            active_test = _full_test_state()
            test_ids = {
                int(value)
                for value in active_test.get("video_ids") or []
                if str(value).isdigit()
            } if active_test.get("active") else set()

            if video_id in test_ids:
                print(
                    "[FULL-TEST] 確認用にローカル動画を残します。"
                )
            else:
                try:
                    cleanup_uploaded_media(
                        video_id,
                        output_path,
                    )
                except Exception as cleanup_exc:
                    print(
                        "[CLEANUP] 投稿は成功済みですが削除処理でエラー: "
                        f"{cleanup_exc}"
                    )
        except Exception as exc:
            recovery = recover_upload_failure(
                row,
                exc,
                now=_now(),
            )
            if not recovery.get("handled"):
                mark_queue_error(row["queue_id"], str(exc))
            record_failure(
                "youtube.upload",
                exc,
                {
                    "video_id": video_id,
                    "queue_id": row.get("queue_id"),
                    "recovery": recovery,
                },
            )
            _save_auto_post_event(
                (
                    "attention"
                    if recovery.get("circuit_breaker")
                    or recovery.get("code") in {"oauth", "publish_guard"}
                    else "recovery_wait"
                ),
                video_id=video_id,
                queue_id=int(row["queue_id"]),
                detail=str(exc),
                extra={
                    "code": recovery.get("code"),
                    "safe_action": recovery.get("safe_action"),
                    "handled": bool(recovery.get("handled")),
                    "scheduled_for": recovery.get("scheduled_for"),
                    "same_failure_count": recovery.get("same_failure_count"),
                },
            )
            print(
                f"[AUTO-UPLOAD] #{video_id} 失敗 / "
                f"分類={recovery.get('code')} / "
                f"自動処置={recovery.get('safe_action')} / "
                f"{exc}"
            )
        finally:
            release_upload_lock(video_id)

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

def _tick_unlocked() -> None:
    delivery_state = self_heal_delivery_controls()
    if delivery_state.get("repairs"):
        print(
            "[DELIVERY-SUPERVISOR] "
            + " / ".join(delivery_state["repairs"])
        )

    # 失敗学習を「メモ」で終わらせず、次の実行前に安全修復と
    # failed queue復活まで実行する。第1話未投稿でも必ず通る。
    recovery_requested = (
        get_channel_state(
            "autonomous_recovery_requested",
            "false",
        ).strip().lower()
        == "true"
    )
    try:
        recovery = run_autonomous_recovery(now=_now())
        if recovery.get("status") not in {
            "healthy",
            "idle",
            "not_armed",
        }:
            print(
                "[AUTONOMOUS-RECOVERY] "
                + json.dumps(recovery, ensure_ascii=False)
            )

        if (
            recovery_requested
            or recovery.get("status")
            in {"recovered", "attention", "improved"}
        ):
            try:
                review = maybe_run_improvement_review(
                    min_hours=1
                )
                if review:
                    print(
                        "[IMPROVEMENT] 失敗直後レビューを反映: "
                        + str(review.get("summary") or "")
                    )
            except Exception as improvement_exc:
                print(
                    "[IMPROVEMENT] 失敗直後レビューは次回へ: "
                    f"{improvement_exc}"
                )

        if recovery.get("status") in {
            "recovered",
            "improved",
            "healthy",
        }:
            set_channel_state(
                "autonomous_recovery_requested",
                "false",
            )
    except Exception as recovery_exc:
        record_failure(
            "scheduler.autonomous_recovery",
            recovery_exc,
        )
        print(
            "[AUTONOMOUS-RECOVERY] 復旧処理は次回へ延期: "
            f"{recovery_exc}"
        )

    full_test = _full_test_state()
    if full_test.get("active"):
        print("[FULL-TEST] テストセッション中: 1本ずつ直列運転")
        _advance_full_test_generation()
        run_due()
        _maybe_finish_full_test()
        return

    # 第1話が未生成なら、通常の成長分析より先に1本を完成させる。
    first_before = ensure_first_episode_delivery()
    if first_before.get("status") in {
        "not_created",
        "missing_record",
    }:
        print("[EPISODE-1] 第1話を最優先で生成します。")
        prepare_upcoming()
        run_due()
        first_after_generation = ensure_first_episode_delivery()
        if first_after_generation.get("status") != "uploaded":
            print(
                "[EPISODE-1] 第1話の投稿成功待ち。"
                "2話以降は生成しません。"
            )
        return

    # 生成済み第1話はYouTube動画ID取得まで最優先で配送する。
    run_due()
    first_after = ensure_first_episode_delivery()
    if first_after.get("status") != "uploaded":
        print(
            "[EPISODE-1] 第1話のYouTube投稿成功を最優先。"
            "成長分析・2話以降の生成は次tickへ延期します。"
        )
        return

    # 第1話投稿後だけ通常の成長ループへ進む。
    try:
        market = maybe_refresh_market_research(min_hours=24)
        if market.get("status") == "refreshed":
            print(
                "[MARKET] YouTube市場パターンを更新: "
                f"sample={market.get('sample_size', 0)}"
            )
    except Exception as exc:
        record_failure("market.research", exc)
        print(f"[MARKET] 市場調査は次回へ延期: {exc}")

    run_growth_cycle()
    try:
        maybe_run_improvement_review(min_hours=12)
    except Exception as exc:
        record_failure("improvement.review", exc)
        print(f"[IMPROVEMENT] AI改善分析をスキップ: {exc}")
    prepare_upcoming()
    try:
        native_result = maybe_run_native_retraining()
        if native_result.get("status") not in {"not_due", "disabled"}:
            print(
                "[NATIVE-TRAIN] "
                + json.dumps(native_result, ensure_ascii=False)
            )
    except Exception as exc:
        record_failure(
            "native.auto_train",
            exc,
        )
        print(f"[NATIVE-TRAIN] 自動再学習を次回へ延期: {exc}")


def tick() -> None:
    init_db()
    set_channel_state(
        "scheduler_last_tick_at",
        _now().isoformat(timespec="seconds"),
    )
    owner = f"pid={os.getpid()} / {_now().isoformat(timespec='seconds')}"
    if not acquire_runtime_lock(
        "scheduler_tick",
        owner=owner,
        ttl_minutes=180,
    ):
        print(
            "[SCHEDULE] 別プロセスの自動サイクルが実行中のため"
            "このtickはスキップします。"
        )
        return

    try:
        _tick_unlocked()
    finally:
        release_runtime_lock("scheduler_tick")

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
    parser.add_argument(
        "--upload-preflight",
        action="store_true",
        help="実投稿をせず自動投稿の阻害要因を診断",
    )
    args = parser.parse_args()

    if args.upload_preflight:
        init_db()
        blockers: list[str] = []
        warnings: list[str] = []

        if runtime_cancel_requested():
            blockers.append("runtime_cancel_requested=true")

        try:
            get_credentials(interactive=False)
        except Exception as exc:
            blockers.append("YouTube OAuth: " + str(exc))

        voice = voice_attribution_status()
        if not voice["resolved"]:
            blockers.append("voice credit unresolved")

        if not automation_enabled():
            warnings.append(
                "automation_enabled=false: Web常駐サイクルは停止中"
            )
        if not auto_upload_enabled():
            warnings.append(
                "auto_upload_enabled=false: 第1話以外の通常自動投稿は停止中"
            )

        first = ensure_first_episode_delivery()
        payload = {
            "ok": not blockers,
            "blockers": blockers,
            "warnings": warnings,
            "first_episode": first,
            "privacy": upload_privacy(),
            "post_times": post_times(),
            "dry_run_note": (
                "DRY_RUNはmain.py直接投稿用。"
                "schedulerの自動投稿経路は別管理です。"
            ),
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        if blockers:
            raise SystemExit(3)
    elif args.prepare:
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
