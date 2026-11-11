from __future__ import annotations

"""Safely replace video #32's near-duplicate script without creating a new video ID.

Default mode is read-only. --apply requires the Windows scheduler to be paused
by the caller. No upload or failed-queue revival is performed here.
"""

import argparse
from datetime import datetime
from difflib import SequenceMatcher
import json
from pathlib import Path
import re
import shutil
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import storage
from mirai_engines.editorial_quality_engine import evaluate_editorial_quality
from runtime_control import (
    ai_video_backend_name,
    execution_mode,
    runtime_cancel_requested,
)

VIDEO_ID = 32
SOURCE_VIDEO_ID = 26
TITLE = "AIミライの2分集中チャレンジ！やる気ゼロの日の始め方"
IDEA = TITLE + "を紹介する"
ANGLE = "長いタスクリストではなく、通知を切り二分間の最初の一手に取り組む方法"
SCRIPT = (
    "やる気ゼロの日、AIミライの二分集中チャレンジ！"
    "まずスマホの通知を切って、今日の作業を一つだけ決める。"
    "タイマーを二分にして、完成は目指さず最初の一手だけ始めよう。"
    "二分経ったら、続けるか休むか自分で選んでいい。"
    "長いタスクリストより、最初の一歩を小さくするのがポイント。"
    "あなたなら何を二分だけ試す？コメントで教えて！"
)
DESCRIPTION_PREFIX = (
    "【改訂版】AIミライの2分集中チャレンジ。\n"
    "通知をオフにして、最初の2分だけ作業を始めるための手順を紹介します。"
    "\n\n"
)


def similarity(left: str, right: str) -> float:
    normalize = lambda value: re.sub(r"\s+", "", value or "")
    return SequenceMatcher(
        None, normalize(left), normalize(right)
    ).ratio()


def _safe_read() -> tuple[dict, dict, float, float]:
    row = storage.video_by_id(VIDEO_ID)
    if not row:
        raise RuntimeError("VIDEO32_MISSING")
    if row["script"] == SCRIPT:
        raise RuntimeError("VIDEO32_ALREADY_REVISED")

    with storage.connect() as conn:
        queue = conn.execute(
            "SELECT id,status,scheduled_for FROM posting_queue WHERE video_id=?",
            (VIDEO_ID,),
        ).fetchone()
        origin = conn.execute(
            "SELECT script FROM videos WHERE id=?", (SOURCE_VIDEO_ID,)
        ).fetchone()
        others = conn.execute(
            "SELECT id,script FROM videos WHERE id!=?", (VIDEO_ID,)
        ).fetchall()
        receipt = conn.execute(
            "SELECT 1 FROM youtube_upload_receipts WHERE video_id=?", (VIDEO_ID,)
        ).fetchone()
        history = conn.execute(
            "SELECT 1 FROM youtube_upload_history WHERE video_id=?", (VIDEO_ID,)
        ).fetchone()
    if not queue or queue["status"] != "failed":
        raise RuntimeError("VIDEO32_QUEUE_CHANGED")
    if row["status"] != "editorial_failed":
        raise RuntimeError("VIDEO32_STATUS_CHANGED")
    if row["youtube_video_id"] or receipt or history:
        raise RuntimeError("VIDEO32_HAS_REMOTE_RECORD")
    if not origin:
        raise RuntimeError("ORIGINAL_VIDEO26_MISSING")
    old_similarity = similarity(row["script"], origin["script"])
    if old_similarity < 0.90:
        raise RuntimeError("VIDEO32_ORIGINAL_SCRIPT_CHANGED")
    if not Path(str(row["output_path"] or "")).is_file():
        raise RuntimeError("VIDEO32_ORIGINAL_MP4_MISSING")

    new_similarity = max(
        (similarity(SCRIPT, candidate["script"]) for candidate in others),
        default=0.0,
    )
    if new_similarity >= 0.70:
        raise RuntimeError("REVISED_SCRIPT_TOO_SIMILAR")
    return row, dict(queue), old_similarity, new_similarity


def _draft(row: dict) -> dict:
    description = DESCRIPTION_PREFIX + str(row["description"] or "")
    return {
        "id": VIDEO_ID,
        "idea": {"idea": IDEA, "angle": ANGLE, "hook": "やる気ゼロの日"},
        "angle": ANGLE,
        "title": TITLE,
        "script": SCRIPT,
        "description": description,
        "tags": json.loads(row["tags_json"] or "[]"),
        "status": "rendering",
    }


def inspect() -> dict:
    row, queue, old_similarity, new_similarity = _safe_read()
    item = _draft(row)
    report = evaluate_editorial_quality(
        item, technical_quality={"score": 90}
    )
    if (
        not report["passed"]
        or report["dimensions"]["originality"] < 55
        or report["dimensions"]["title_match"] < 55
    ):
        raise RuntimeError(
            "DRAFT_QUALITY_PRECHECK_FAILED "
            + json.dumps(report, ensure_ascii=False)
        )
    result = {
        "video_id": VIDEO_ID,
        "queue_status": queue["status"],
        "old_similarity_with_26": round(old_similarity, 3),
        "new_max_similarity": round(new_similarity, 3),
        "draft_score": report["score"],
        "draft_originality": report["dimensions"]["originality"],
        "draft_title_match": report["dimensions"]["title_match"],
        "title": TITLE,
        "script": SCRIPT,
    }
    print("REPAIR32_PREVIEW=" + json.dumps(result, ensure_ascii=False))
    return result


def _create_backup(row: dict, when: str) -> Path:
    folder = storage.DB_PATH.parent / ("video32-repair-backup-" + when)
    folder.mkdir(parents=True, exist_ok=False)
    source = sqlite3.connect(storage.DB_PATH, timeout=15)
    target = sqlite3.connect(folder / "memory.db")
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()

    (folder / "original-video32.json").write_text(
        json.dumps(row, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    shutil.copy2(row["output_path"], folder / "original-32.mp4")
    thumb = str(row.get("thumbnail_path") or "").strip()
    if thumb and Path(thumb).is_file():
        shutil.copy2(thumb, folder / ("original-thumbnail" + Path(thumb).suffix))

    # Thumbnail candidates reuse filenames across renders; archive them too.
    from studio.asset_store import GENERATED_ROOT

    for candidate in (GENERATED_ROOT / "thumbnails").glob("video_32_thumb_*"):
        if candidate.is_file():
            dest = folder / ("old-" + candidate.name)
            shutil.copy2(candidate, dest)
    return folder


def _restore_video_metadata(row: dict) -> None:
    # Preserve original script/description: these are updated only after PASS.
    with storage.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        changed = conn.execute(
            """
            UPDATE videos SET status=?,output_path=?,thumbnail_path=?
            WHERE id=? AND youtube_video_id IS NULL
            """,
            (
                row["status"], row["output_path"],
                row.get("thumbnail_path"), VIDEO_ID,
            ),
        ).rowcount
        if changed != 1:
            raise RuntimeError("ROLLBACK_BLOCKED_REMOTE_STATE_CHANGED")


def apply() -> dict:
    if execution_mode() != "local" or ai_video_backend_name() == "google":
        raise RuntimeError("EXTERNAL_RENDER_BACKEND_ABORT")
    if runtime_cancel_requested():
        raise RuntimeError("SAFETY_STOP_ACTIVE")
    if not storage.acquire_runtime_lock(
        "scheduler_tick", owner="video32-originality-repair"
    ):
        raise RuntimeError("ANOTHER_SCHEDULER_CYCLE_RUNNING")

    try:
        row, queue, _, _ = _safe_read()
        inspect()
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        folder = _create_backup(row, stamp)
        print("DB_AND_MP4_BACKUP=" + str(folder), flush=True)

        # Use unique output directories so a failed render cannot overwrite
        # the old MP4 or the old WAV.
        import production_pipeline
        from main import load_character, render_results

        video_dir = production_pipeline.VIDEO_DIR / ("repair32-" + stamp)
        audio_dir = production_pipeline.AUDIO_DIR / ("repair32-" + stamp)
        video_dir.mkdir(parents=True, exist_ok=False)
        audio_dir.mkdir(parents=True, exist_ok=False)

        old_video_dir = production_pipeline.VIDEO_DIR
        old_audio_dir = production_pipeline.AUDIO_DIR
        committed = False
        try:
            production_pipeline.VIDEO_DIR = video_dir
            production_pipeline.AUDIO_DIR = audio_dir
            item = _draft(row)
            guest_id = row.get("guest_id")
            if guest_id:
                guest = next(
                    (g for g in storage.active_guests(100)
                     if int(g["id"]) == int(guest_id)),
                    None,
                )
                if guest:
                    item["guest"] = guest

            render_results([item], load_character())
            updated = storage.video_by_id(VIDEO_ID)
            output = Path(str((updated or {}).get("output_path") or ""))
            if (
                not item.get("quality_passed")
                or not (item.get("editorial_quality") or {}).get("passed")
                or not updated
                or updated["status"] != "rendered"
                or not output.is_file()
                or video_dir not in output.parents
            ):
                raise RuntimeError("VIDEO32_REBUILD_QUALITY_FAILED")

            with storage.connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                q = conn.execute(
                    "SELECT status FROM posting_queue WHERE video_id=?",
                    (VIDEO_ID,),
                ).fetchone()
                if not q or q["status"] != queue["status"]:
                    raise RuntimeError("QUEUE_CHANGED_DURING_RENDER")
                if conn.execute(
                    "SELECT 1 FROM youtube_upload_history WHERE video_id=?",
                    (VIDEO_ID,),
                ).fetchone() or conn.execute(
                    "SELECT 1 FROM youtube_upload_receipts WHERE video_id=?",
                    (VIDEO_ID,),
                ).fetchone():
                    raise RuntimeError("REMOTE_RECORD_APPEARED")
                changed = conn.execute(
                    """
                    UPDATE videos SET title=?,idea=?,angle=?,script=?,description=?
                    WHERE id=? AND status='rendered'
                    AND output_path=? AND youtube_video_id IS NULL
                    """,
                    (
                        TITLE, IDEA, ANGLE, SCRIPT, item["description"],
                        VIDEO_ID, str(output),
                    ),
                ).rowcount
                if changed != 1:
                    raise RuntimeError("VIDEO_STATE_CHANGED_DURING_RENDER")
            committed = True
            result = {
                "video_id": VIDEO_ID,
                "quality": item["editorial_quality"]["score"],
                "mp4": str(output),
                "queue_unchanged": True,
                "backup": str(folder),
            }
            print("REPAIR32_PASS=" + json.dumps(result, ensure_ascii=False))
            return result
        finally:
            production_pipeline.VIDEO_DIR = old_video_dir
            production_pipeline.AUDIO_DIR = old_audio_dir
            if not committed:
                _restore_video_metadata(row)
                print("REPAIR32_ROLLBACK=ORIGINAL_VIDEO_METADATA_RESTORED")
    finally:
        storage.release_runtime_lock("scheduler_tick")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--apply", action="store_true",
        help="Back up and render the new script. No YouTube upload.",
    )
    args = parser.parse_args()
    if args.apply:
        apply()
    else:
        inspect()


if __name__ == "__main__":
    main()
