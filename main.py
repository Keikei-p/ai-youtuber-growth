from __future__ import annotations
import argparse
import json
from datetime import datetime
from pathlib import Path

from config import settings
from guest_manager import select_guest_for_next_video
from legal_guard import publish_gate
from voice.provider import voice_attribution_status
from gpu_manager import unload_ollama_model, release_torch_cuda_cache
from production_pipeline import produce_media
from metadata import build_metadata
from runtime_control import (
    clear_runtime_cancel,
    posts_per_day,
    runtime_cancel_requested,
)
from media_cleanup import cleanup_all_uploaded_media, cleanup_uploaded_media
from paths import AUDIO_DIR, CHARACTER_FILE, PLAN_DIR, VIDEO_DIR, ensure_runtime_dirs
from planner import plan_ideas
from reviewer import review_script
from storage import (
    export_json,
    init_db,
    get_channel_state,
    mark_uploaded,
    recent_videos,
    save_video,
    set_channel_state,
    update_video_output,
)
from writer import fallback_script, rewrite_script, write_script

def load_character() -> dict:
    return json.loads(CHARACTER_FILE.read_text(encoding="utf-8"))

def render_results(results: list[dict], character: dict) -> None:
    """
    互換用ラッパー。
    実処理は production_pipeline で
    画像 → 音声 → 字幕付き編集の順に直列実行する。
    """
    produce_media(results, character)

def upload_results(
    results: list[dict],
    *,
    privacy_status: str | None = None,
    force: bool = False,
    max_items: int | None = None,
    cleanup_local: bool = True,
) -> None:
    if settings.dry_run and not force:
        print("[UPLOAD] DRY_RUN=true のためYouTube投稿は実行しません。")
        return

    from youtube.uploader import upload_video

    effective_privacy = privacy_status or settings.youtube_privacy_status
    candidates = [
        item
        for item in results
        if item.get("output_path")
        and item.get("quality_passed") is not False
    ]
    if max_items is not None:
        candidates = candidates[:max_items]

    for item in candidates:
        try:
            gate = publish_gate(
                item,
                required_credit=(
                    voice_attribution_status()["credit"]
                    if voice_attribution_status()["resolved"]
                    else "__UNRESOLVED_REQUIRED_VOICE_CREDIT__"
                ),
            )
            if not gate["allowed"]:
                item["upload_error"] = (
                    "公開前確認が必要です。"
                    f" approval_id={gate.get('approval_id')}"
                )
                print(
                    f"[LEGAL] #{item['id']} は公開前確認が必要なため"
                    f"YouTube投稿を停止: {gate.get('risks')}"
                )
                continue

            youtube_id = upload_video(
                video_path=Path(item["output_path"]),
                title=item["title"],
                description=item.get("description", ""),
                tags=item.get("tags", []),
                privacy_status=effective_privacy,
                category_id=settings.youtube_category_id,
                default_language=settings.youtube_default_language,
                # ミライはAI音声・AI画像/映像を用いるため安全側で常時開示。
                contains_synthetic_media=True,
            )
            item["youtube_video_id"] = youtube_id
            item["status"] = "uploaded"
            mark_uploaded(item["id"], youtube_id)
            print(
                f"[UPLOAD] #{item['id']} -> YouTube ID {youtube_id} "
                f"[{effective_privacy}]"
            )
            if cleanup_local:
                try:
                    cleanup_uploaded_media(item["id"], item.get("output_path"))
                    if settings.cleanup_after_upload:
                        item["output_path"] = None
                except Exception as cleanup_exc:
                    print(f"[CLEANUP] 投稿は成功済みですが削除処理でエラー: {cleanup_exc}")
            else:
                print("[TEST-UPLOAD] 確認用にローカル動画を残します。")
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

FIRST_EPISODE_STATE_KEY = "mirai_first_episode_completed"


def _first_episode_package() -> dict:
    """初回だけ使う固定の自己紹介。2本目以降は通常の学習型企画へ戻す。"""
    script = (
        "はじめまして、ミライです。今日から完全AIユーチューバーとして活動を始めます。"
        "このチャンネルでは、企画、台本、音声、画像、動画づくり、そして投稿後の分析まで、"
        "AIができるだけ自分で進めます。まだ最初は完璧じゃありません。"
        "でも、再生数や視聴維持率、みなさんの反応を学びながら、投稿するたびに少しずつ進化していきます。"
        "AIの私がどこまで成長できるのか。今日が、その第1話です。ぜひ見守ってください。"
    )
    return {
        "idea": {
            "idea": "今日から完全AIユーチューバーになるミライの第1話",
            "angle": "完全AI運営と、投稿後のデータから成長していく実験を短く宣言する自己紹介",
            "hook": "今日から、完全AIユーチューバーになります。",
            "experiment_type": "new",
        },
        "written": {
            "title": "今日から、完全AIユーチューバーになります。【第1話】",
            "script": script,
        },
        "metadata": {
            "title": "今日から、完全AIユーチューバーになります。【第1話】",
            "description": (
                "はじめまして、AIユーチューバー「ミライ」です。\n\n"
                "企画・台本・音声・画像・動画制作・投稿後の分析まで、AI中心で運営し、"
                "視聴データや反応をもとに少しずつ改善していくチャンネルです。\n"
                "まだ第1話。ここからどこまで成長できるのか、一緒に見届けてください。\n\n"
                "※この動画にはAIで生成・編集した音声・画像・映像が含まれます。\n\n"
                "#AIユーチューバー #完全AI運営 #AI #ミライ #AI成長記録"
            ),
            "tags": [
                "AIユーチューバー", "完全AI運営", "AI", "ミライ",
                "AI成長記録", "AI動画", "自動運営", "生成AI", "AIチャンネル",
            ],
        },
    }


def run_generation(
    render: bool = False,
    upload: bool = False,
    target_override: int | None = None,
) -> list[dict]:
    """
    低負荷の段階式パイプライン。

    STEP 1: Ollama中心の文章工程を1本ずつ完了
    STEP 2: 画像を1枚ずつ生成
    STEP 3: VOICEVOX音声を1本ずつ生成
    STEP 4: FFmpegで画像+音声+字幕を1本ずつ編集
    STEP 5: 必要な場合のみYouTube投稿

    GPU負荷の異なる工程を同時実行しない。
    """
    ensure_runtime_dirs()
    init_db()
    clear_runtime_cancel()
    character = load_character()
    recent = recent_videos(30)
    target = target_override or posts_per_day()
    results: list[dict] = []
    first_episode_pending = (
        get_channel_state(FIRST_EPISODE_STATE_KEY, "").strip().lower() != "true"
    )
    first_episode_reserved = False

    print(f"[PIPELINE] STEP 1/4 文章工程開始 / 目標 {target}本")

    failed_rounds = 0
    max_attempts = max(
        target * settings.max_generation_rounds,
        settings.max_generation_rounds,
    )

    while len(results) < target and failed_rounds < max_attempts:
        if runtime_cancel_requested():
            print("[PIPELINE] 安全停止要求のため文章工程を終了します。")
            break
        sequence = len(results) + 1
        print(
            f"[PIPELINE][TEXT] {sequence}/{target} "
            "企画→台本→品質確認→メタデータ"
        )

        is_first_episode = first_episode_pending and not first_episode_reserved
        if is_first_episode:
            package = _first_episode_package()
            idea = dict(package["idea"])
            written = dict(package["written"])
            metadata = dict(package["metadata"])
            guest = None
            first_episode_reserved = True
            print("[EPISODE-1] 初投稿専用: 完全AIユーチューバー開始動画を生成します。")
        else:
            try:
                ideas = plan_ideas(character, recent, 1)
            except Exception as exc:
                print(f"[PLAN] 企画生成失敗: {exc}")
                ideas = []

            if not ideas:
                failed_rounds += 1
                continue

            idea = dict(ideas[0])

            # 第2話以降は従来どおり、学習戦略を使った完全自動企画。
            guest = select_guest_for_next_video(
                prepare_image=False,
            )
            if guest:
                idea["guest"] = guest

            written = _make_valid_script(character, idea, recent)
            if not written:
                failed_rounds += 1
                continue

            metadata = build_metadata(
                written=written,
                idea=idea,
                script=written["script"],
                guest=guest,
            )

        video_id = save_video(
            idea=idea["idea"],
            angle=idea.get("angle", ""),
            title=metadata["title"],
            script=written["script"],
            description=metadata["description"],
            tags=metadata["tags"],
            guest_id=(int(guest["id"]) if guest else None),
            status="planned",
        )

        result = {
            "id": video_id,
            "idea": idea,
            "title": metadata["title"],
            "script": written["script"],
            "description": metadata["description"],
            "tags": metadata["tags"],
            "guest": guest,
            "status": "planned",
            "first_episode": is_first_episode,
        }
        results.append(result)
        recent.insert(0, result)
        failed_rounds = 0

    print(
        f"[PIPELINE] STEP 1/4 文章工程完了: "
        f"{len(results)}/{target}本"
    )

    # 文章工程終了後にOllamaのVRAMを明示解放。
    unload_ollama_model()
    release_torch_cuda_cache()

    if len(results) < target:
        print(
            f"[WARN] 予定{target}本に対して{len(results)}本。"
            "文章工程の再試行上限に達したため、この実行ではここまでにします。"
        )

    if (render or upload) and not runtime_cancel_requested():
        render_results(results, character)
        for item in results:
            if (
                item.get("first_episode")
                and item.get("output_path")
                and item.get("quality_passed") is not False
            ):
                set_channel_state(FIRST_EPISODE_STATE_KEY, "true")
                set_channel_state("mirai_first_episode_video_id", str(item["id"]))
                print(
                    "[EPISODE-1] 第1話の完成を記録。"
                    "次回から通常の完全学習型自動投稿へ移行します。"
                )
    elif render or upload:
        print("[PIPELINE] 安全停止要求のためメディア工程をスキップします。")

    if upload:
        print("[PIPELINE] STEP 5/5 YouTube投稿工程開始")
        upload_results(results)
        print("[PIPELINE] STEP 5/5 YouTube投稿工程完了")

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
        cleanup_local=False,
    )
    return results

def run_learning() -> None:
    from growth_engine import run_growth_cycle, show_growth_state

    ensure_runtime_dirs()
    init_db()
    run_growth_cycle()
    show_growth_state()

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
    parser.add_argument(
        "--learn",
        action="store_true",
        help="24h/72h/7dの成長分析を実行して戦略を更新",
    )
    parser.add_argument(
        "--growth-status",
        action="store_true",
        help="現在のミライの成長戦略と最近の分析を表示",
    )
    parser.add_argument(
        "--cleanup-uploaded",
        action="store_true",
        help="YouTube投稿済みでPCに残っているMP4/WAVを削除",
    )
    args = parser.parse_args()

    if args.learn:
        run_learning()
        return

    if args.growth_status:
        from growth_engine import show_growth_state
        show_growth_state()
        return

    if args.cleanup_uploaded:
        run_cleanup_uploaded()
        return

    if args.test_upload:
        results = run_private_upload_test()
    else:
        results = run_generation(render=args.render or args.upload, upload=args.upload)

    target = 1 if args.test_upload else posts_per_day()
    print(f"\n生成完了: {len(results)}本 / 目標 {target}本")
    for item in results:
        print(f"- #{item['id']} {item['title']} [{item['status']}]")
        if args.show:
            print(item["script"])
            print()

if __name__ == "__main__":
    main()
