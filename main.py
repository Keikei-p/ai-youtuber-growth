from __future__ import annotations
import argparse
import json
from datetime import datetime
from pathlib import Path

from config import settings
from learner import build_learning_note
from media_cleanup import cleanup_all_uploaded_media, cleanup_uploaded_media
from paths import AUDIO_DIR, CHARACTER_FILE, PLAN_DIR, VIDEO_DIR, ensure_runtime_dirs
from planner import plan_ideas
from reviewer import review_script
from storage import (
    export_json,
    init_db,
    mark_uploaded,
    recent_videos,
    save_learning_note,
    save_video,
    update_metrics,
    update_video_output,
    uploaded_videos,
)
from writer import fallback_script, rewrite_script, write_script

def load_character() -> dict:
    return json.loads(CHARACTER_FILE.read_text(encoding="utf-8"))

def render_results(results: list[dict], character: dict) -> None:
    from voice.voicevox import VoicevoxClient
    from video.renderer import render_short

    voice = VoicevoxClient()
    if not voice.available():
        print("[MEDIA] VOICEVOXが起動していないため動画生成をスキップしました。")
        return

    stamp = datetime.now().strftime("%Y%m%d")
    for item in results:
        audio_path = AUDIO_DIR / f"{stamp}_{item['id']}.wav"
        video_path = VIDEO_DIR / f"{stamp}_{item['id']}.mp4"
        try:
            voice.synthesize(item["script"], audio_path)
            render_short(
                title=item["title"],
                script=item["script"],
                audio_path=audio_path,
                output_path=video_path,
                character_name=character["name"],
            )
            item["output_path"] = str(video_path)
            item["status"] = "rendered"
            update_video_output(item["id"], str(video_path))
            print(f"[MEDIA] #{item['id']} -> {video_path}")
        except Exception as exc:
            item["media_error"] = str(exc)
            print(f"[MEDIA] #{item['id']} 失敗: {exc}")

def upload_results(
    results: list[dict],
    *,
    privacy_status: str | None = None,
    force: bool = False,
    max_items: int | None = None,
) -> None:
    if settings.dry_run and not force:
        print("[UPLOAD] DRY_RUN=true のためYouTube投稿は実行しません。")
        return

    from youtube.uploader import upload_video

    effective_privacy = privacy_status or settings.youtube_privacy_status
    candidates = [item for item in results if item.get("output_path")]
    if max_items is not None:
        candidates = candidates[:max_items]

    for item in candidates:
        try:
            youtube_id = upload_video(
                video_path=Path(item["output_path"]),
                title=item["title"],
                description=item.get("description", ""),
                privacy_status=effective_privacy,
            )
            item["youtube_video_id"] = youtube_id
            item["status"] = "uploaded"
            mark_uploaded(item["id"], youtube_id)
            print(
                f"[UPLOAD] #{item['id']} -> YouTube ID {youtube_id} "
                f"[{effective_privacy}]"
            )
            try:
                cleanup_uploaded_media(item["id"], item.get("output_path"))
                if settings.cleanup_after_upload:
                    item["output_path"] = None
            except Exception as cleanup_exc:
                print(f"[CLEANUP] 投稿は成功済みですが削除処理でエラー: {cleanup_exc}")
        except Exception as exc:
            item["upload_error"] = str(exc)
            print(f"[UPLOAD] #{item['id']} 失敗: {exc}")

def _make_valid_script(character: dict, idea: dict, recent: list[dict]) -> dict | None:
    try:
        written = write_script(character, idea, recent)
    except Exception as exc:
        print(f"[WRITE] 初回生成失敗: {exc}")
        written = fallback_script(character, idea)

    for retry in range(settings.max_script_retries + 1):
        ok, issues = review_script(written["title"], written["script"], recent)
        if ok:
            if retry:
                print(f"[REPAIR] {retry}回の修正で品質チェックOK")
            return written

        print(f"[REPAIR] 品質チェックNG: {issues} / 修正 {retry + 1}/{settings.max_script_retries}")

        if retry >= settings.max_script_retries:
            break

        try:
            written = rewrite_script(
                character=character,
                idea=idea,
                recent=recent,
                previous=written,
                issues=issues,
            )
        except Exception as exc:
            print(f"[REPAIR] AI修正失敗: {exc}")
            written = fallback_script(character, idea)

    fallback = fallback_script(character, idea)
    ok, issues = review_script(fallback["title"], fallback["script"], recent)
    if ok:
        print("[REPAIR] 安全テンプレートへ切り替えて品質チェックOK")
        return fallback

    print(f"[SKIP] 安全テンプレートも品質チェックNG: {issues}")
    return None

def run_generation(
    render: bool = False,
    upload: bool = False,
    target_override: int | None = None,
) -> list[dict]:
    ensure_runtime_dirs()
    init_db()
    character = load_character()
    recent = recent_videos(30)
    target = target_override or settings.posts_per_day
    results: list[dict] = []

    for generation_round in range(1, settings.max_generation_rounds + 1):
        remaining = target - len(results)
        if remaining <= 0:
            break

        print(f"[PLAN] 第{generation_round}ラウンド: 残り{remaining}本を生成")
        try:
            ideas = plan_ideas(character, recent, remaining)
        except Exception as exc:
            print(f"[PLAN] 企画生成失敗: {exc}")
            ideas = []

        if not ideas:
            print("[PLAN] 企画が生成されなかったため次ラウンドへ")
            continue

        for idea in ideas:
            if len(results) >= target:
                break

            written = _make_valid_script(character, idea, recent)
            if not written:
                continue

            video_id = save_video(
                idea=idea["idea"],
                angle=idea.get("angle", ""),
                title=written["title"],
                script=written["script"],
                status="planned",
            )

            result = {
                "id": video_id,
                "idea": idea,
                "title": written["title"],
                "script": written["script"],
                "description": written.get("description", ""),
                "status": "planned",
            }
            results.append(result)
            recent.insert(0, result)

    if len(results) < target:
        print(
            f"[WARN] 予定{target}本に対して{len(results)}本。"
            "最大生成ラウンドに達したため、この実行ではここまでにします。"
        )

    if render or upload:
        render_results(results, character)
    if upload:
        upload_results(results)

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    export_json(PLAN_DIR / f"{stamp}.json", results)
    return results

def run_private_upload_test() -> list[dict]:
    print("[TEST-UPLOAD] 1本だけ生成し、YouTubeへ必ず非公開(private)で投稿します。")
    results = run_generation(render=True, upload=False, target_override=1)
    upload_results(
        results,
        privacy_status="private",
        force=True,
        max_items=1,
    )
    return results

def run_learning() -> None:
    ensure_runtime_dirs()
    init_db()
    videos = uploaded_videos(20)
    if not videos:
        print("[LEARN] 分析対象の投稿済み動画がまだありません。")
        return

    from youtube.analytics import fetch_video_metrics

    for video in videos:
        try:
            metrics = fetch_video_metrics(video["youtube_video_id"])
            update_metrics(video["id"], metrics)
            note, score = build_learning_note(metrics)
            save_learning_note(video["id"], note, score)
            print(
                f"[LEARN] #{video['id']} views={metrics.get('views', 0)} "
                f"retention={metrics.get('averageViewPercentage', 0)} score={score}"
            )
            print(f"        {note}")
        except Exception as exc:
            print(f"[LEARN] #{video['id']} 失敗: {exc}")

def run_cleanup_uploaded() -> None:
    init_db()
    targets, removed = cleanup_all_uploaded_media()
    print(
        f"[CLEANUP] 投稿済み{targets}本を確認し、"
        f"ローカルファイル{removed}個を削除しました。"
    )

def main() -> None:
    parser = argparse.ArgumentParser(description="成長型AI YouTuber v1")
    parser.add_argument("--show", action="store_true", help="生成結果を詳しく表示")
    parser.add_argument("--render", action="store_true", help="VOICEVOX+FFmpegでShorts動画まで生成")
    parser.add_argument("--upload", action="store_true", help="生成動画をYouTubeへ投稿")
    parser.add_argument(
        "--test-upload",
        action="store_true",
        help="1本だけ生成してYouTubeへ必ず非公開でテスト投稿",
    )
    parser.add_argument("--learn", action="store_true", help="投稿済み動画を分析して学習メモを保存")
    parser.add_argument(
        "--cleanup-uploaded",
        action="store_true",
        help="YouTube投稿済みでPCに残っているMP4/WAVを削除",
    )
    args = parser.parse_args()

    if args.learn:
        run_learning()
        return

    if args.cleanup_uploaded:
        run_cleanup_uploaded()
        return

    if args.test_upload:
        results = run_private_upload_test()
    else:
        results = run_generation(render=args.render or args.upload, upload=args.upload)

    target = 1 if args.test_upload else settings.posts_per_day
    print(f"\n生成完了: {len(results)}本 / 目標 {target}本")
    for item in results:
        print(f"- #{item['id']} {item['title']} [{item['status']}]")
        if args.show:
            print(item["script"])
            print()

if __name__ == "__main__":
    main()
