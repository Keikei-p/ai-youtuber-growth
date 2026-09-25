from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image

from maintenance import storage_snapshot
from mirai_engines.visual_learning import VisualLearningMemory
from mirai_engines.visual_quality_engine import MiraiVisualQualityEngine
from runtime_control import visual_runtime_settings
from storage import connect, set_channel_state


def run_quick_diagnostics() -> dict:
    """
    重いAIモデルや本番素材を生成せず、数MB未満の一時データだけで
    DB・Visual Quality・FFmpeg・ライブ設定を確認する。
    一時ファイルはTemporaryDirectoryで自動削除する。
    """
    checks: dict[str, dict] = {}

    try:
        with connect() as conn:
            row = conn.execute("SELECT 1 AS ok").fetchone()
        checks["database"] = {
            "ok": bool(row and int(row["ok"]) == 1),
            "detail": "SQLite read/write path ready",
        }
    except Exception as exc:
        checks["database"] = {"ok": False, "detail": str(exc)}

    try:
        image = Image.effect_noise((512, 768), 65).convert("RGB")
        report = MiraiVisualQualityEngine().inspect_image(
            image,
            asset_type="background",
        )
        checks["visual_quality"] = {
            "ok": bool(report.get("passed")),
            "detail": f"score={report.get('score')}/100",
        }
    except Exception as exc:
        checks["visual_quality"] = {"ok": False, "detail": str(exc)}

    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        checks["ffmpeg"] = {
            "ok": False,
            "detail": "ffmpeg/ffprobe not found",
        }
    else:
        try:
            with tempfile.TemporaryDirectory(prefix="mirai_quick_") as tmp:
                target = Path(tmp) / "smoke.mp4"
                subprocess.run(
                    [
                        ffmpeg,
                        "-y",
                        "-f",
                        "lavfi",
                        "-i",
                        "testsrc=size=360x640:rate=12",
                        "-t",
                        "0.7",
                        "-an",
                        "-c:v",
                        "libx264",
                        "-preset",
                        "ultrafast",
                        "-pix_fmt",
                        "yuv420p",
                        str(target),
                    ],
                    check=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=25,
                )
                report = MiraiVisualQualityEngine().inspect_video_asset(
                    target,
                    asset_type="quick_test",
                )
                checks["ffmpeg"] = {
                    "ok": target.is_file()
                    and int(report.get("score") or 0) >= 60,
                    "detail": (
                        f"temporary video={target.stat().st_size} bytes / "
                        f"visual={report.get('score')}/100"
                    ),
                }
        except Exception as exc:
            checks["ffmpeg"] = {"ok": False, "detail": str(exc)}

    try:
        live = visual_runtime_settings()
        learning = VisualLearningMemory().dashboard_state()
        checks["live_settings"] = {
            "ok": True,
            "detail": {
                "visual": live,
                "learning_memory": learning.get("memory_count", 0),
            },
        }
    except Exception as exc:
        checks["live_settings"] = {"ok": False, "detail": str(exc)}

    passed = all(bool(value.get("ok")) for value in checks.values())
    result = {
        "ok": passed,
        "mode": "lightweight",
        "persistent_test_media": False,
        "checks": checks,
        "storage": storage_snapshot(),
        "finished_at": datetime.now(timezone.utc).isoformat(
            timespec="seconds"
        ),
    }
    set_channel_state(
        "quick_diagnostics_last",
        json.dumps(result, ensure_ascii=False),
    )
    return result
