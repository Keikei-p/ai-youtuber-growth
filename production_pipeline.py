from __future__ import annotations

from datetime import datetime
from pathlib import Path
import wave

from config import settings
from gpu_manager import release_torch_cuda_cache, unload_ollama_model
from runtime_control import (
    ai_video_enabled,
    guest_image_auto_enabled,
    runtime_cancel_requested,
)
from storage import (
    active_guests,
    get_channel_state,
    mark_guest_used,
    update_video_output,
)
from self_improvement import effective_scene_image_count, record_failure
from mirai_engines.composer import MiraiComposer
from mirai_engines.quality_engine import MiraiQualityEngine
from mirai_engines.voice_engine import MiraiVoiceEngine
from studio.asset_store import GENERATED_ROOT
from studio.image_generator import (
    generate_background_image,
    generate_guest_image,
    generate_mirai_image,
)
from studio.video_generator import generate_animatediff_clip
from voice.provider import build_voice_provider
from video.renderer import render_short
from paths import AUDIO_DIR, VIDEO_DIR


def _latest_matching(folder: Path, prefix: str) -> Path | None:
    if not folder.exists():
        return None
    candidates = [
        path
        for path in folder.iterdir()
        if path.is_file()
        and path.name.startswith(prefix)
        and path.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda path: path.stat().st_mtime)


def choose_mirai_expression(script: str, title: str = "") -> str:
    text = f"{title} {script}"
    if any(word in text for word in ("えっ", "まさか", "驚", "！？", "びっくり")):
        return "surprised"
    if any(word in text for word in ("考え", "なぜ", "どうして", "検証", "比較")):
        return "thinking"
    if any(word in text for word in ("重要", "注意", "危険", "結論", "本気")):
        return "serious"
    if any(word in text for word in ("笑", "うれしい", "成功", "最高", "ありがとう")):
        return "smile"
    return "normal"


def _ensure_mirai_visual(item: dict) -> str | None:
    expression = choose_mirai_expression(
        item.get("script", ""),
        item.get("title", ""),
    )
    item["mirai_expression"] = expression

    folder = GENERATED_ROOT / "mirai"
    existing = _latest_matching(folder, f"mirai_{expression}_")
    if existing:
        item["character_image_path"] = str(existing)
        return str(existing)

    try:
        print(f"[PIPELINE][IMAGE] ミライ表情を生成: {expression}")
        path = generate_mirai_image(expression)
        item["character_image_path"] = path
        return path
    except Exception as exc:
        item["visual_warning"] = (
            str(item.get("visual_warning") or "")
            + f" ミライ画像生成失敗: {exc}"
        ).strip()
        record_failure(
            "image.mirai",
            exc,
            {"video_id": item.get("id"), "expression": expression},
        )
        configured = Path(settings.character_image)
        if configured.exists():
            item["character_image_path"] = str(configured)
            print(
                "[PIPELINE][IMAGE] ミライ画像生成失敗のため"
                "既定キャラ画像へフォールバック"
            )
            return str(configured)
        print(f"[PIPELINE][IMAGE] ミライ画像は代替表示へ: {exc}")
        return None


def _background_theme(item: dict, scene_index: int, total_scenes: int) -> str:
    idea = item.get("idea") or {}
    if isinstance(idea, dict):
        topic = str(idea.get("idea") or item.get("title") or "AI experiment")
        angle = str(idea.get("angle") or "")
    else:
        topic = str(idea or item.get("title") or "AI experiment")
        angle = ""
    phase = (
        "opening hook scene"
        if scene_index == 0
        else "explanation and conclusion scene"
    )
    return (
        "futuristic clean anime environment for an AI YouTuber short video, "
        f"topic: {topic}, angle: {angle}, phase: {phase}, "
        f"scene {scene_index + 1} of {total_scenes}, "
        "visually clear, cinematic lighting, vertical composition, "
        "no letters, no logo"
    )


def _generate_backgrounds(item: dict) -> list[str]:
    paths: list[str] = []
    total = effective_scene_image_count()
    for scene_index in range(total):
        try:
            print(
                f"[PIPELINE][IMAGE] #{item['id']} "
                f"シーン背景 {scene_index + 1}/{total} を生成"
            )
            path = generate_background_image(
                _background_theme(item, scene_index, total)
            )
            paths.append(path)
        except Exception as exc:
            item["visual_warning"] = (
                str(item.get("visual_warning") or "")
                + f" 背景{scene_index + 1}生成失敗: {exc}"
            ).strip()
            record_failure(
                "image.background",
                exc,
                {
                    "video_id": item.get("id"),
                    "scene_index": scene_index,
                },
            )
            print(
                "[PIPELINE][IMAGE] 背景は既存/グラデーションへ: "
                f"{exc}"
            )
    item["background_image_paths"] = paths
    item["background_image_path"] = paths[0] if paths else None
    return paths


def _ai_video_prompt(item: dict) -> str:
    idea = item.get("idea") or {}
    if isinstance(idea, dict):
        topic = str(idea.get("idea") or item.get("title") or "AI experiment")
        angle = str(idea.get("angle") or "")
    else:
        topic = str(idea or item.get("title") or "AI experiment")
        angle = ""
    return (
        "short vertical anime cinematic B-roll for an AI YouTuber, "
        f"topic: {topic}, angle: {angle}, futuristic clean visual, "
        "smooth subtle motion, no text, no logo, safe for work"
    )


def _generate_ai_video_asset(item: dict) -> str | None:
    if not ai_video_enabled():
        return None
    if get_channel_state("runtime_resource_mode", "").strip() == "urgent":
        print(
            "[PIPELINE][AI-VIDEO] 投稿が近いためAI動画素材を省略し、"
            "軽い本編制作を優先します。"
        )
        return None
    try:
        print(f"[PIPELINE][AI-VIDEO] #{item['id']} 短いAI動画素材を生成")
        path = generate_animatediff_clip(_ai_video_prompt(item))
        item["ai_video_path"] = path
        return path
    except Exception as exc:
        item["visual_warning"] = (
            str(item.get("visual_warning") or "")
            + f" AI動画生成失敗: {exc}"
        ).strip()
        record_failure(
            "pipeline.ai_video",
            exc,
            {"video_id": item.get("id"), "title": item.get("title")},
        )
        print(f"[PIPELINE][AI-VIDEO] 失敗しても本編は継続: {exc}")
        return None


def _ensure_guest_visual(item: dict) -> str | None:
    guest = item.get("guest") or {}
    if not guest:
        return None

    existing = guest.get("image_path")
    if existing and Path(existing).exists():
        return str(existing)

    if not guest_image_auto_enabled():
        return None

    guest_id = int(guest["id"])
    row = next(
        (
            candidate
            for candidate in active_guests(100)
            if int(candidate["id"]) == guest_id
        ),
        None,
    )
    if not row:
        return None

    try:
        print(f"[PIPELINE][IMAGE] ゲスト画像を生成: {guest.get('name', guest_id)}")
        path = generate_guest_image(row)
        guest["image_path"] = path
        item["guest"] = guest
        return path
    except Exception as exc:
        item["visual_warning"] = (
            str(item.get("visual_warning") or "")
            + f" ゲスト画像生成失敗: {exc}"
        ).strip()
        record_failure(
            "image.guest",
            exc,
            {
                "video_id": item.get("id"),
                "guest_id": guest.get("id"),
            },
        )
        print(f"[PIPELINE][IMAGE] ゲスト画像なしで継続: {exc}")
        return None


def prepare_visuals(results: list[dict]) -> None:
    """
    画像工程だけを直列実行する。
    Ollama文章モデルは先にVRAMから降ろし、同時に複数画像を生成しない。
    """
    if not results:
        return

    print("[PIPELINE] STEP 2/4 画像工程開始")
    unload_ollama_model()
    release_torch_cuda_cache()

    for index, item in enumerate(results, start=1):
        print(f"[PIPELINE][IMAGE] {index}/{len(results)} #{item['id']}")
        _ensure_mirai_visual(item)
        _ensure_guest_visual(item)
        _generate_backgrounds(item)
        _generate_ai_video_asset(item)

    release_torch_cuda_cache()
    print("[PIPELINE] STEP 2/4 画像工程完了")


def _wav_duration(path: Path) -> float:
    with wave.open(str(path), "rb") as handle:
        rate = handle.getframerate()
        if rate <= 0:
            raise RuntimeError("音声サンプルレートが不正です。")
        return handle.getnframes() / float(rate)


def synthesize_audio(results: list[dict]) -> None:
    """
    Mirai Voice Engineが文章分割・感情・話速・ピッチ・抑揚・間を決定する。
    VOICEVOXは波形生成providerとして1セグメントずつ使う。
    """
    if not results:
        return

    print("[PIPELINE] STEP 3/5 Mirai Voice工程開始")
    release_torch_cuda_cache()
    engine = MiraiVoiceEngine(build_voice_provider())
    if not engine.available():
        for item in results:
            item["media_error"] = "音声providerを利用できません"
        print("[PIPELINE] 音声provider未起動のため音声工程を中止")
        return

    stamp = datetime.now().strftime("%Y%m%d")
    for index, item in enumerate(results, start=1):
        print(f"[PIPELINE][VOICE] {index}/{len(results)} #{item['id']}")
        audio_path = AUDIO_DIR / f"{stamp}_{item['id']}.wav"
        try:
            voice_result = engine.synthesize(
                item["script"],
                audio_path,
            )
            item["audio_path"] = str(audio_path)
            item["voice_plan"] = voice_result
            print(
                f"[PIPELINE][VOICE] #{item['id']} "
                f"{len(voice_result.get('segments') or [])}セグメント / "
                f"provider={voice_result.get('provider')}"
            )
        except Exception as exc:
            item["media_error"] = f"音声生成失敗: {exc}"
            record_failure(
                "voice.synthesis",
                exc,
                {"video_id": item.get("id")},
            )
            print(f"[PIPELINE][VOICE] #{item['id']} 失敗: {exc}")

    print("[PIPELINE] STEP 3/5 Mirai Voice工程完了")


def render_videos(results: list[dict], character: dict) -> None:
    """
    Mirai Composerが動画構成を決め、FFmpeg rendererは計画を実行する。
    完成後はMirai Quality Engineで投稿可能か検査する。
    """
    if not results:
        return

    print("[PIPELINE] STEP 4/5 Mirai Composer/編集工程開始")
    release_torch_cuda_cache()
    stamp = datetime.now().strftime("%Y%m%d")
    composer = MiraiComposer()
    quality_engine = MiraiQualityEngine()

    for index, item in enumerate(results, start=1):
        print(f"[PIPELINE][EDIT] {index}/{len(results)} #{item['id']}")
        audio_raw = item.get("audio_path")
        if not audio_raw:
            print(f"[PIPELINE][EDIT] #{item['id']} 音声なしのためスキップ")
            continue

        audio_path = Path(audio_raw)
        video_path = VIDEO_DIR / f"{stamp}_{item['id']}.mp4"
        video_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            audio_duration = _wav_duration(audio_path)
            backgrounds = item.get("background_image_paths") or []
            composition_plan = composer.plan(
                script=item["script"],
                audio_duration=audio_duration,
                background_count=max(len(backgrounds), 1),
                has_ai_video=bool(item.get("ai_video_path")),
            )
            plan_issues = composer.validate(
                composition_plan,
                audio_duration,
            )
            item["composition_plan"] = composition_plan
            if plan_issues:
                item["composition_warnings"] = plan_issues
                record_failure(
                    "composer.plan_validation",
                    ",".join(plan_issues),
                    {"video_id": item.get("id")},
                )

            render_short(
                title=item["title"],
                script=item["script"],
                audio_path=audio_path,
                output_path=video_path,
                character_name=character["name"],
                guest_name=(item.get("guest") or {}).get("name"),
                guest_image_path=(item.get("guest") or {}).get("image_path"),
                character_image_path=item.get("character_image_path"),
                background_image_path=item.get("background_image_path"),
                background_image_paths=backgrounds,
                ai_video_path=item.get("ai_video_path"),
                composition_plan=composition_plan,
            )

            quality = quality_engine.inspect(
                video_path,
                video_id=int(item["id"]),
                composition_plan=composition_plan,
            )
            item["quality"] = quality
            item["quality_passed"] = bool(quality.get("passed"))
            item["output_path"] = str(video_path)

            if item["quality_passed"]:
                item["status"] = "rendered"
                update_video_output(
                    item["id"],
                    str(video_path),
                    status="rendered",
                )
                guest = item.get("guest") or {}
                if guest.get("id"):
                    mark_guest_used(int(guest["id"]))
                print(
                    f"[PIPELINE][QUALITY] #{item['id']} "
                    f"PASS {quality.get('score')}/100"
                )
            else:
                item["status"] = "quality_failed"
                update_video_output(
                    item["id"],
                    str(video_path),
                    status="quality_failed",
                )
                issues = [
                    issue.get("code")
                    for issue in quality.get("issues") or []
                ]
                record_failure(
                    "quality.validation",
                    f"品質検査不合格: {issues}",
                    {
                        "video_id": item.get("id"),
                        "score": quality.get("score"),
                        "issues": issues,
                    },
                )
                print(
                    f"[PIPELINE][QUALITY] #{item['id']} "
                    f"FAIL {quality.get('score')}/100 / {issues}"
                )

            print(f"[PIPELINE][EDIT] #{item['id']} 完成: {video_path}")
        except Exception as exc:
            item["media_error"] = f"動画編集失敗: {exc}"
            item["quality_passed"] = False
            record_failure(
                "video.render",
                exc,
                {"video_id": item.get("id")},
            )
            print(f"[PIPELINE][EDIT] #{item['id']} 失敗: {exc}")

    print("[PIPELINE] STEP 4/5 Composer/Quality工程完了")


def produce_media(results: list[dict], character: dict) -> None:
    if runtime_cancel_requested():
        print("[PIPELINE] 安全停止要求のためメディア制作を開始しません。")
        return

    prepare_visuals(results)
    if runtime_cancel_requested():
        print("[PIPELINE] 安全停止要求: 画像工程後に停止します。")
        return

    synthesize_audio(results)
    if runtime_cancel_requested():
        print("[PIPELINE] 安全停止要求: 音声工程後に停止します。")
        return

    render_videos(results, character)
