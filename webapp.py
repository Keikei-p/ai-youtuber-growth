from __future__ import annotations

import io
import json
import mimetypes
import os
import shutil
import subprocess
import sys
import threading
import time
import traceback
import webbrowser
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

from ai_client import OllamaClient
from autonomy_policy import autonomy_state, resolve_approval
from config import settings
from growth_engine import show_growth_state
from gpu_manager import gpu_snapshot
from guest_manager import create_guest_now
from main import run_cleanup_uploaded, run_generation, run_private_upload_test
from maintenance import compact_runtime_storage, rotate_log, storage_snapshot
from quick_test import run_quick_diagnostics
from paths import VIDEO_DIR
from runtime_control import (
    ai_video_enabled,
    auto_upload_enabled,
    automation_enabled,
    guest_appearance_every,
    guest_image_auto_enabled,
    guest_new_every,
    post_times,
    posts_per_day,
    set_ai_video_enabled,
    set_auto_upload_enabled,
    set_automation_enabled,
    set_visual_background_candidates,
    set_visual_candidate_count,
    set_visual_min_score,
    set_visual_retry_rounds,
    set_visual_video_min_score,
    set_guest_appearance_every,
    set_guest_image_auto_enabled,
    set_guest_new_every,
    set_post_times,
    set_posts_per_day,
    set_upload_privacy,
    set_voice_provider_name,
    request_runtime_cancel,
    set_web_interval_seconds,
    upload_privacy,
    voice_provider_name,
    visual_runtime_settings,
    web_interval_seconds,
)
from resource_governor import resource_snapshot
from scheduler import (
    abort_full_test,
    full_test_status,
    prepare_upcoming,
    run_due,
    start_today_full_test,
    tick,
)
from storage import (
    active_guests,
    analytics_history,
    dashboard_videos,
    get_channel_state,
    init_db,
    queued_items,
    set_channel_state,
    video_by_id,
)
from voice.provider import voice_attribution_status, voice_provider_status
from studio.asset_store import GENERATED_ROOT, list_assets
from studio.image_generator import (
    generate_background_image,
    generate_guest_image,
    generate_mirai_image,
    studio_status,
)
from studio.video_generator import (
    ai_video_status,
    generate_animatediff_clip,
)
from self_improvement import improvement_state, run_improvement_review
from mirai_engines.quality_engine import MiraiQualityEngine
from mirai_engines.debug_engine import MiraiDebugEngine
from mirai_engines.visual_learning import VisualLearningMemory

HOST = "127.0.0.1"
PORT = 8765
ROOT = Path(__file__).resolve().parent
LOG_DIR = ROOT / "logs"
LOG_FILE = LOG_DIR / "webapp.log"

_job_lock = threading.Lock()
_state_lock = threading.Lock()
_last_cycle_at: str | None = None
_last_cycle_result = "未実行"
_current_job = ""
_current_job_started_at: str | None = None
_stop_event = threading.Event()
_wake_event = threading.Event()
_http_server: ThreadingHTTPServer | None = None
_storage_cache_at = 0.0
_storage_cache: dict = {}


def _cached_storage_snapshot() -> dict:
    global _storage_cache_at, _storage_cache
    now = time.time()
    if not _storage_cache or now - _storage_cache_at >= 30:
        _storage_cache = storage_snapshot()
        _storage_cache_at = now
    return dict(_storage_cache)


def _append_log(text: str) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    rotate_log(LOG_FILE)
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(f"\n[{stamp}]\n{text.rstrip()}\n")


def _run_captured(label: str, func) -> dict:
    global _current_job, _current_job_started_at

    if not _job_lock.acquire(blocking=False):
        return {"ok": False, "message": "別の処理を実行中です。"}

    with _state_lock:
        _current_job = label
        _current_job_started_at = datetime.now().isoformat(timespec="seconds")

    buf = io.StringIO()
    try:
        with redirect_stdout(buf), redirect_stderr(buf):
            print(f"[WEB] {label} start")
            func()
            print(f"[WEB] {label} end")
        output = buf.getvalue()
        _append_log(output)
        return {"ok": True, "message": f"{label} 完了", "output": output}
    except Exception as exc:
        traceback.print_exc(file=buf)
        output = buf.getvalue()
        _append_log(output)
        return {
            "ok": False,
            "message": f"{label} 失敗: {exc}",
            "output": output,
        }
    finally:
        with _state_lock:
            _current_job = ""
            _current_job_started_at = None
        _job_lock.release()


def _candidate_voicevox_paths() -> list[Path]:
    candidates: list[Path] = []
    if settings.voicevox_exe:
        candidates.append(Path(settings.voicevox_exe))

    if os.name == "nt":
        local = Path(os.environ.get("LOCALAPPDATA", ""))
        program_files = Path(os.environ.get("ProgramFiles", "C:/Program Files"))
        candidates.extend([
            local / "Programs" / "VOICEVOX" / "VOICEVOX.exe",
            local / "VOICEVOX" / "VOICEVOX.exe",
            program_files / "VOICEVOX" / "VOICEVOX.exe",
        ])

    return [p for p in candidates if str(p) and p.exists()]

def _ensure_local_services() -> None:
    if not OllamaClient().available():
        ollama = shutil.which("ollama")
        if ollama:
            try:
                subprocess.Popen(
                    [ollama, "serve"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=(
                        subprocess.CREATE_NO_WINDOW
                        if os.name == "nt" else 0
                    ),
                )
                time.sleep(2)
                _append_log("[SERVICE] Ollama自動起動を実行")
            except Exception as exc:
                _append_log(f"[SERVICE] Ollama自動起動失敗: {exc}")

    voice_status = voice_provider_status()
    if (
        not voice_status["available"]
        and voice_provider_name() == "voicevox"
    ):
        paths = _candidate_voicevox_paths()
        if paths:
            try:
                subprocess.Popen(
                    [str(paths[0])],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=(
                        subprocess.CREATE_NO_WINDOW
                        if os.name == "nt" else 0
                    ),
                )
                time.sleep(6)
                _append_log(f"[SERVICE] VOICEVOX自動起動: {paths[0]}")
            except Exception as exc:
                _append_log(f"[SERVICE] VOICEVOX自動起動失敗: {exc}")

def _cycle_worker() -> None:
    global _last_cycle_at, _last_cycle_result
    while not _stop_event.is_set():
        _wake_event.clear()
        try:
            if automation_enabled():
                _ensure_local_services()
                result = _run_captured("自動サイクル", tick)
                with _state_lock:
                    _last_cycle_at = datetime.now().isoformat(timespec="seconds")
                    _last_cycle_result = result["message"]
        except Exception as exc:
            _append_log(f"[WEB] background cycle error: {exc}")

        wait_for = web_interval_seconds()
        _wake_event.wait(wait_for)


def _read_log_tail(max_chars: int = 12000) -> str:
    if not LOG_FILE.exists():
        return "まだログはありません。"
    text = LOG_FILE.read_text(encoding="utf-8", errors="replace")
    return text[-max_chars:]


def _service_status() -> dict:
    voice_status = voice_provider_status()
    selected_voice = voice_provider_name()
    return {
        "ollama": OllamaClient().available(),
        "voice": bool(voice_status["available"]),
        "voicevox": (
            bool(voice_status["available"])
            if selected_voice == "voicevox"
            else True
        ),
        "ffmpeg": bool(shutil.which("ffmpeg")),
        "youtube_token": Path(settings.youtube_token_file).exists(),
    }


def _engine_status() -> dict:
    voice = voice_provider_status()
    quality_rows = MiraiQualityEngine().recent(1)
    debug_rows = MiraiDebugEngine().recent(1)
    return {
        "voice": {
            "name": "Mirai Voice Engine",
            "available": bool(voice["available"]),
            "detail": f"provider={voice['name']}",
        },
        "composer": {
            "name": "Mirai Composer",
            "available": True,
            "detail": "自作構成・字幕・モーション計画",
        },
        "quality": {
            "name": "Mirai Quality Engine",
            "available": bool(shutil.which("ffprobe")),
            "detail": (
                f"直近 {quality_rows[0].get('score')}点"
                if quality_rows else "品質履歴なし"
            ),
        },
        "debug": {
            "name": "Mirai Debug Engine",
            "available": True,
            "detail": (
                str(debug_rows[0].get("category") or "診断履歴あり")
                if debug_rows else "診断履歴なし"
            ),
        },
        "improvement": {
            "name": "Mirai Improvement Engine",
            "available": True,
            "detail": (
                "自作ロジック + Ollama補助"
                if OllamaClient().available()
                else "自作ロジックのみ"
            ),
        },
        "visual": {
            "name": "Mirai Visual Evolution",
            "available": True,
            "detail": (
                lambda state: (
                    "候補比較・品質ゲート・視聴学習 / "
                    f"memory={state.get('memory_count', 0)} / "
                    f"avg={state.get('avg_score') if state.get('avg_score') is not None else '-'} / "
                    f"profile={state.get('preferred_profile') or '-'}"
                )
            )(VisualLearningMemory().dashboard_state()),
        },
    }


def _rights_status() -> dict:
    voice_credit = voice_attribution_status()
    bgm_configured = bool(str(settings.bgm_file or "").strip())
    return {
        "publish_guard": True,
        "ai_disclosure": True,
        "voice_credit_required": bool(voice_credit["required"]),
        "voice_credit_resolved": bool(voice_credit["resolved"]),
        "voice_credit": str(voice_credit["credit"] or ""),
        "bgm_configured": bgm_configured,
        "bgm_license_ok": (
            (not bgm_configured)
            or bool(settings.bgm_license_confirmed)
        ),
        "ai_video_enabled": bool(ai_video_enabled()),
        "ai_video_license_ok": (
            (not ai_video_enabled())
            or bool(settings.ai_video_license_confirmed)
        ),
    }


def _queue_status() -> list[dict]:
    rows = queued_items()
    return [
        {
            "video_id": row["video_id"],
            "scheduled_for": row["scheduled_for"],
            "title": row["title"],
            "attempts": row["attempts"],
        }
        for row in rows[:20]
    ]


def _video_status() -> list[dict]:
    rows = dashboard_videos(40)
    quality_map: dict[int, dict] = {}
    for report in MiraiQualityEngine().recent(100):
        raw_id = report.get("video_id")
        if raw_id is None:
            continue
        video_id = int(raw_id)
        if video_id not in quality_map:
            quality_map[video_id] = report

    items: list[dict] = []
    for row in rows:
        raw_path = row.get("output_path")
        local_path = Path(raw_path).resolve() if raw_path else None
        file_exists = bool(local_path and local_path.is_file())
        items.append(
            {
                "id": row["id"],
                "created_at": row.get("created_at"),
                "title": row["title"],
                "status": row["status"],
                "queue_status": row.get("queue_status"),
                "scheduled_for": row.get("scheduled_for"),
                "youtube_video_id": row.get("youtube_video_id"),
                "guest_name": row.get("guest_name"),
                "views": row.get("views") or 0,
                "error": row.get("queue_error"),
                "quality_score": (
                    quality_map.get(int(row["id"]), {}).get("score")
                ),
                "quality_passed": (
                    quality_map.get(int(row["id"]), {}).get("passed")
                ),
                "has_local_file": file_exists,
                "preview_url": (
                    f"/media/videos/{int(row['id'])}"
                    if file_exists else None
                ),
            }
        )
    return items

def _studio_assets() -> list[dict]:
    assets = []
    prefix = "assets/generated/"
    for row in list_assets(limit=24):
        item = dict(row)
        relative = str(item.get("path") or "").replace("\\", "/")
        if relative.startswith(prefix):
            relative = relative[len(prefix):]
            item["url"] = "/studio-assets/" + relative
        else:
            item["url"] = None
        assets.append(item)
    return assets


def _guest_status() -> list[dict]:
    rows = active_guests(20)
    return [
        {
            "id": row["id"],
            "name": row["name"],
            "appearances": row["appearances"],
            "has_image": bool(row.get("image_path")),
        }
        for row in rows
    ]


def _growth_status() -> dict:
    history = analytics_history(8)
    return {
        "strategy": get_channel_state(
            "growth_strategy",
            "まだ十分な学習データがありません。",
        ),
        "recent": [
            {
                "video_id": row["video_id"],
                "checkpoint_hours": row["checkpoint_hours"],
                "score": row["score"],
                "views": row["views"],
                "retention": row["avg_view_percentage"],
                "title": row["title"],
            }
            for row in history
        ],
    }


def _windows_autostart_enabled() -> bool:
    if os.name != "nt":
        return False

    try:
        import winreg
        key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            key_path,
            0,
            winreg.KEY_READ,
        ) as key:
            winreg.QueryValueEx(key, "AIYouTuberGrowthWeb")
        return True
    except (FileNotFoundError, OSError):
        return False

def _status_payload() -> dict:
    with _state_lock:
        last_cycle_at = _last_cycle_at
        last_cycle_result = _last_cycle_result
        current_job = _current_job
        current_job_started_at = _current_job_started_at

    services = _service_status()

    return {
        "automation_enabled": automation_enabled(),
        "auto_upload_enabled": auto_upload_enabled(),
        "privacy": upload_privacy(),
        "interval_seconds": web_interval_seconds(),
        "posts_per_day": posts_per_day(),
        "post_times": post_times(),
        "guest_every": guest_appearance_every(),
        "guest_new_every": guest_new_every(),
        "guest_image_auto_enabled": guest_image_auto_enabled(),
        "ai_video_enabled": ai_video_enabled(),
        "ai_video": ai_video_status(),
        "improvement": improvement_state(),
        "autonomy": autonomy_state(),
        "resource": resource_snapshot(),
        "studio": studio_status(),
        "gpu": gpu_snapshot(),
        "studio_assets": _studio_assets(),
        "visual_runtime": visual_runtime_settings(),
        "storage": _cached_storage_snapshot(),
        "quick_diagnostics_last": get_channel_state(
            "quick_diagnostics_last",
            "",
        ),
        "services": services,
        "voice_provider": {
            **voice_provider_status(),
            "selected": voice_provider_name(),
        },
        "engines": _engine_status(),
        "rights": _rights_status(),
        "system_ready": all(services.values()),
        "queue": _queue_status(),
        "videos": _video_status(),
        "guests": _guest_status(),
        "growth": _growth_status(),
        "full_test": full_test_status(),
        "last_cycle_at": last_cycle_at,
        "last_cycle_result": last_cycle_result,
        "current_job": current_job,
        "current_job_started_at": current_job_started_at,
        "autostart_enabled": _windows_autostart_enabled(),
        "wake_task_enabled": (
            get_channel_state("wake_task_enabled", "false")
            .strip()
            .lower()
            == "true"
        ),
        "remote_access_status": get_channel_state(
            "remote_access_status",
            "未確認",
        ),
        "remote_access_url": get_channel_state(
            "remote_access_url",
            "",
        ),
        "log_tail": _read_log_tail(),
    }


def _install_windows_autostart() -> str:
    if os.name != "nt":
        return "Windows以外ではこの自動起動登録は使えません。"

    import winreg

    pythonw = Path(sys.executable).with_name("pythonw.exe")
    launcher = ROOT / "web_launcher.pyw"
    command = f'"{pythonw}" "{launcher}"'

    key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
    with winreg.OpenKey(
        winreg.HKEY_CURRENT_USER,
        key_path,
        0,
        winreg.KEY_SET_VALUE,
    ) as key:
        winreg.SetValueEx(
            key,
            "AIYouTuberGrowthWeb",
            0,
            winreg.REG_SZ,
            command,
        )

    return "PC起動時のWebアプリ自動起動を登録しました。"


def _run_automation_script(
    script_name: str,
    *args: str,
) -> str:
    if os.name != "nt":
        raise RuntimeError("Windows専用機能です。")

    script = ROOT / "automation" / script_name
    if not script.exists():
        raise FileNotFoundError(str(script))

    powershell = shutil.which("powershell")
    if not powershell:
        raise RuntimeError("powershell.exe が見つかりません。")

    process = subprocess.run(
        [
            powershell,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script),
            *[str(arg) for arg in args],
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        creationflags=(
            subprocess.CREATE_NO_WINDOW
            if os.name == "nt" else 0
        ),
    )
    output = ((process.stdout or "") + "\n" + (process.stderr or "")).strip()
    if process.returncode != 0:
        raise RuntimeError(
            output[-3000:]
            or f"{script_name} の実行に失敗しました。"
        )
    return output or "完了"


def _install_wake_task(interval_minutes: int = 60) -> str:
    output = _run_automation_script(
        "install_windows_task.ps1",
        "-IntervalMinutes",
        str(max(15, int(interval_minutes))),
    )
    set_channel_state("wake_task_enabled", "true")
    return output


def _remove_wake_task() -> str:
    output = _run_automation_script(
        "remove_windows_task.ps1",
    )
    set_channel_state("wake_task_enabled", "false")
    return output


def _night_test_mode() -> str:
    # 無料・安全なローカルテスト。YouTubeには自動投稿しない。
    set_automation_enabled(True)
    set_auto_upload_enabled(False)
    set_upload_privacy("private")
    set_ai_video_enabled(False)
    wake_output = _install_wake_task(60)
    return (
        "夜間テスト運用を有効化しました。\n"
        "・自動運転: ON\n"
        "・YouTube自動投稿: OFF\n"
        "・公開設定: private\n"
        "・AI動画実験: OFF\n"
        "・スリープ復帰チェック: 60分ごと\n\n"
        + wake_output
    )


def _run_today_full_test() -> dict:
    wake_message = ""
    try:
        wake_message = _install_wake_task(30)
    except Exception as exc:
        wake_message = (
            "WakeToRun登録は失敗しましたが、"
            f"Webアプリ稼働中のテストは続行します: {exc}"
        )
        _append_log("[FULL-TEST] " + wake_message)

    result = start_today_full_test(
        target=3,
        end_hour=18,
    )
    print("[FULL-TEST] " + wake_message)
    print(
        json.dumps(
            result,
            ensure_ascii=False,
            indent=2,
        )
    )
    return result


def _consume_full_test_request() -> None:
    request_path = ROOT / "automation" / "full_test_request.json"
    if not request_path.exists():
        return

    try:
        request = json.loads(
            request_path.read_text(
                encoding="utf-8",
            )
        )
    except Exception as exc:
        _append_log(
            f"[FULL-TEST] request読込失敗: {exc}"
        )
        return

    request_id = str(request.get("request_id") or "").strip()
    if not request_id:
        return

    consumed = get_channel_state(
        "full_test_consumed_request_id",
        "",
    ).strip()
    if consumed == request_id:
        return

    today = datetime.now().strftime("%Y-%m-%d")
    if str(request.get("date") or "") != today:
        return

    end_hour = int(request.get("end_hour") or 18)
    if datetime.now().hour >= end_hour:
        set_channel_state(
            "full_test_consumed_request_id",
            request_id,
        )
        _append_log(
            "[FULL-TEST] requestは終了時刻後のため"
            "開始せず消化扱いにしました。"
        )
        return

    # 二重起動防止を先に記録。
    set_channel_state(
        "full_test_consumed_request_id",
        request_id,
    )

    def runner():
        result = _run_captured(
            "今日18時まで3本完全テスト",
            _run_today_full_test,
        )
        _append_log(result.get("message", ""))

    threading.Thread(
        target=runner,
        name="full-test-request",
        daemon=True,
    ).start()


def _remote_access_enable() -> str:
    output = _run_automation_script(
        "remote_access_enable.ps1",
    )
    remote_url = ""
    for line in output.splitlines():
        if line.startswith("REMOTE_URL="):
            remote_url = line.split("=", 1)[1].strip()
            break

    set_channel_state("remote_access_status", "enabled")
    if remote_url:
        set_channel_state("remote_access_url", remote_url)
    return (
        "プライベートリモート管理を有効化しました。\n"
        + (f"{remote_url}\n" if remote_url else "")
        + "Tailscale tailnet内からのみアクセスできます。"
    )


def _remote_access_disable() -> str:
    output = _run_automation_script(
        "remote_access_disable.ps1",
    )
    set_channel_state("remote_access_status", "disabled")
    set_channel_state("remote_access_url", "")
    return output


def _remote_access_refresh() -> str:
    output = _run_automation_script(
        "remote_access_status.ps1",
    )
    remote_url = ""
    state = "not_installed"
    for line in output.splitlines():
        line = line.strip()
        if line.startswith("CONNECTED="):
            state = "enabled"
            remote_url = line.split("=", 1)[1].strip()
            break
        if line == "CONNECTED":
            state = "enabled"
        elif line == "NOT_CONNECTED":
            state = "not_connected"
        elif line == "NOT_INSTALLED":
            state = "not_installed"
        elif line.startswith("STATE="):
            state = line.lower()

    set_channel_state("remote_access_status", state)
    if remote_url:
        set_channel_state("remote_access_url", remote_url)
    elif state != "enabled":
        set_channel_state("remote_access_url", "")

    return output


def _update_and_restart() -> None:
    """
    ユーザー操作としての再起動は不要。
    設定変更はDBから即時反映し、Pythonコード更新時だけ裏で
    新プロセスへ自動引継ぎする。
    """
    git = shutil.which("git")
    if not git:
        raise RuntimeError("Gitが見つかりません")

    status = subprocess.run(
        [git, "status", "--porcelain"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    if status.stdout.strip():
        raise RuntimeError(
            "ローカル変更があるため自動更新を中止しました。"
            "未保存の変更を確認してください。"
        )

    before = subprocess.run(
        [git, "rev-parse", "HEAD"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()

    pull = subprocess.run(
        [git, "pull", "--ff-only"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    print(pull.stdout.strip() or "Already up to date.")

    after = subprocess.run(
        [git, "rev-parse", "HEAD"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()

    if before == after:
        print("[UPDATE] すでに最新版です。プロセス入替は不要です。")
        return

    changed = subprocess.run(
        [git, "diff", "--name-only", before, after],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    needs_process_swap = any(
        Path(name).suffix.lower() in {".py", ".pyw"}
        for name in changed
    )
    print("[UPDATE] 変更: " + ", ".join(changed[:30]))

    if not needs_process_swap:
        print("[UPDATE] Pythonコード変更なし。再起動なしで更新完了。")
        return

    print("[UPDATE] Pythonコードを安全に自動引継ぎします。手動再起動は不要です。")
    helper = ROOT / "restart_helper.pyw"
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    subprocess.Popen(
        [str(pythonw), str(helper)],
        cwd=str(ROOT),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=(
            subprocess.CREATE_NO_WINDOW
            if os.name == "nt" else 0
        ),
    )

    if _http_server is not None:
        threading.Timer(0.8, _http_server.shutdown).start()

def _install_studio_dependencies() -> None:
    requirements = ROOT / "requirements-studio.txt"
    if not requirements.exists():
        raise FileNotFoundError("requirements-studio.txt が見つかりません")

    python = Path(sys.executable)
    if os.name == "nt" and python.name.lower() == "pythonw.exe":
        python = python.with_name("python.exe")

    _append_log("[STUDIO-INSTALL] AIスタジオ依存関係の導入を開始")
    process = subprocess.Popen(
        [
            str(python),
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "-r",
            str(requirements),
        ],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )

    if process.stdout is not None:
        for line in process.stdout:
            line = line.rstrip()
            if line:
                _append_log("[STUDIO-INSTALL] " + line)

    return_code = process.wait()
    if return_code != 0:
        raise RuntimeError(
            "AIスタジオの導入に失敗しました。ログを確認してください。"
        )
    _append_log("[STUDIO-INSTALL] 導入完了。画像生成を実行できます。")
    print("[STUDIO] 導入完了。画像生成を実行できます。")


def _remove_windows_autostart() -> str:
    if os.name != "nt":
        return "Windows以外ではこの自動起動解除は使えません。"

    import winreg

    key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            key_path,
            0,
            winreg.KEY_SET_VALUE,
        ) as key:
            winreg.DeleteValue(key, "AIYouTuberGrowthWeb")
        return "PC起動時の自動起動を解除しました。"
    except FileNotFoundError:
        return "自動起動登録はありません。"


HTML = r"""<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>ミライ AI YouTuber 管理</title>
<style>
:root{font-family:Inter,"Yu Gothic UI",Meiryo,sans-serif;color:#eef5ff;background:#08111f}
*{box-sizing:border-box}body{margin:0;background:linear-gradient(135deg,#07111f,#10223c 55%,#151a36);min-height:100vh}
.wrap{max-width:1280px;margin:auto;padding:20px}.top{display:flex;gap:16px;align-items:center;justify-content:space-between;flex-wrap:wrap}
h1{margin:0;font-size:28px}.sub{color:#a8bad2;font-size:14px}.grid{display:grid;grid-template-columns:repeat(12,1fr);gap:14px;margin-top:18px;grid-auto-flow:dense}
.card{grid-column:span 4;background:rgba(14,27,48,.88);border:1px solid #263d5d;border-radius:18px;padding:18px;box-shadow:0 18px 50px rgba(0,0,0,.22)}
.card.wide{grid-column:span 8}.card.half{grid-column:span 6}.card.full{grid-column:span 12}.card h2{font-size:17px;margin:0 0 14px}
.row{display:flex;gap:10px;align-items:center;justify-content:space-between;padding:8px 0;border-bottom:1px solid #20344e}.row:last-child{border:0}
.badge{padding:5px 9px;border-radius:999px;font-size:12px;background:#233b59}.ok{background:#124d3c}.ng{background:#5b2630}
button,select,input{border:1px solid #355275;background:#102844;color:#fff;border-radius:10px;padding:10px 12px}
button{cursor:pointer;font-weight:700}button.primary{background:#2274db}button.danger{background:#8b3041}.actions{display:flex;gap:8px;flex-wrap:wrap}
.queue{display:grid;gap:9px}.q{padding:11px;border-radius:12px;background:#0b1b30}.small{font-size:12px;color:#9cb0c9}.strategy{line-height:1.7;background:#0b1b30;padding:13px;border-radius:12px}
pre{white-space:pre-wrap;word-break:break-word;background:#06101c;padding:14px;border-radius:12px;max-height:330px;overflow:auto;color:#bcd0e7}
.toggle{display:flex;align-items:center;gap:8px}.hero{display:flex;gap:12px;align-items:center}.orb{width:52px;height:52px;border-radius:50%;background:radial-gradient(circle at 30% 30%,#c4dcff,#6598ef 45%,#243c7c);box-shadow:0 0 30px #4c82e855}
.studio-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:12px;margin-top:14px}.asset{background:#0b1b30;border-radius:12px;overflow:hidden;border:1px solid #20344e}.asset img{display:block;width:100%;aspect-ratio:2/3;object-fit:cover;background:#06101c}.asset video{display:block;width:100%;aspect-ratio:9/16;object-fit:contain;background:#000}.asset .meta{padding:9px}.studio-status{margin:8px 0 14px;padding:10px;border-radius:12px;background:#0b1b30}
.library-list{display:grid;gap:12px}.library-item{background:#0b1b30;border:1px solid #20344e;border-radius:14px;padding:14px}.library-head{display:flex;gap:12px;align-items:flex-start;justify-content:space-between;flex-wrap:wrap}.library-meta{display:flex;gap:7px;flex-wrap:wrap;margin-top:8px}.library-item details{margin-top:12px}.library-item summary{cursor:pointer;font-weight:700;color:#dcecff}.library-item video{display:block;width:min(100%,360px);max-height:640px;margin-top:10px;border-radius:12px;background:#000}.library-images{display:grid;grid-template-columns:repeat(auto-fill,minmax(130px,1fr));gap:10px;margin-top:12px}.library-images img{width:100%;aspect-ratio:2/3;object-fit:cover;border-radius:10px;background:#06101c}.linkbtn{display:inline-block;text-decoration:none;border:1px solid #355275;background:#102844;color:#fff;border-radius:10px;padding:8px 10px;font-size:12px;font-weight:700}
@media(max-width:900px){.card,.card.wide,.card.half{grid-column:span 12}.wrap{padding:12px}h1{font-size:22px}button,select,input{max-width:100%}.actions input,.actions select{min-width:0!important;flex:1 1 180px}}
</style>
</head>
<body>
<div class="wrap">
  <div class="top">
    <div class="hero"><div class="orb"></div><div><h1>ミライ AI YouTuber 管理</h1><div class="sub">企画 → 制作 → 投稿 → 分析 → 改善を自動運転</div></div></div>
    <div class="actions">
      <button class="primary" onclick="runAction('cycle')">今すぐ1サイクル実行</button>
      <button onclick="refresh()">更新</button>
    </div>
  </div>

  <div class="grid">
    <section class="card">
      <h2>自動運転</h2>
      <div class="row"><span>Web自動運転</span><button id="automationBtn" onclick="toggleAutomation()"></button></div>
      <div class="row"><span>YouTube自動投稿</span><button id="uploadBtn" onclick="toggleUpload()"></button></div>
      <div class="row"><span>公開設定</span>
        <select id="privacy" onchange="savePrivacy()">
          <option value="private">非公開</option>
          <option value="unlisted">限定公開</option>
          <option value="public">公開</option>
        </select>
      </div>
      <div class="row"><span>チェック間隔</span>
        <select id="interval" onchange="saveInterval()">
          <option value="300">5分</option><option value="600">10分</option><option value="900">15分</option><option value="1800">30分</option>
        </select>
      </div>
      <div class="small" id="lastCycle"></div>
    </section>

    <section class="card">
      <h2>システム状態</h2>
      <div id="services"></div>
      <div class="actions" style="margin-top:12px">
        <button onclick="runAction('services')">AIサービスを起動/再確認</button>
      </div>
    </section>

    <section class="card half">
      <h2>Mirai 自作エンジン</h2>
      <p class="small">判断・構成・品質・原因究明・改善はミライ側。外部ツールはprovider/実行器として使用します。</p>
      <div id="engineStatus"></div>
    </section>

    <section class="card half">
      <h2>権利・公開安全</h2>
      <p class="small">自動投稿前に、音声クレジット・第三者素材・AI開示・高リスク内容を安全側で確認します。</p>
      <div id="rightsStatus"></div>
    </section>

    <section class="card">
      <h2>運用設定</h2>
      <div class="row"><span>1日投稿数</span><input id="postsPerDay" type="number" min="1" max="10" style="width:92px"></div>
      <div class="row"><span>投稿時刻</span><input id="postTimes" placeholder="09:00,15:00,21:00" style="width:190px"></div>
      <div class="row"><span>ゲスト出演</span><input id="guestEvery" type="number" min="0" max="100" style="width:92px"></div>
      <div class="row"><span>新ゲスト</span><input id="guestNewEvery" type="number" min="0" max="500" style="width:92px"></div>
      <div class="row"><span>音声provider</span>
        <select id="voiceProvider" style="min-width:180px">
          <option value="voicevox">VOICEVOX（移行用）</option>
          <option value="mirai_local">Mirai Local TTS（自作）</option>
        </select>
      </div>
      <div class="small">Mirai Local TTSはローカルAPI接続。切替は再起動なしで次の音声生成から反映されます。</div>
      <div class="actions" style="margin-top:12px"><button class="primary" onclick="saveOperationSettings()">運用設定を保存</button></div>
    </section>

    <section class="card full">
      <h2>今日18時まで3本・完全自動テスト</h2>
      <div id="fullTestStatus" class="studio-status small"></div>
      <div class="actions">
        <button class="primary" onclick="runAction('full_test_today')">3本完全テストを開始/確認</button>
      </div>
      <div class="small" style="margin-top:8px">通常制作フローで3本作成し、YouTubeへprivateで時刻分散して自動投稿します。18時で打ち切り、通常設定へ戻ります。</div>
    </section>

    <section class="card wide">
      <h2>投稿キュー</h2>
      <div id="queue" class="queue"></div>
      <div class="actions" style="margin-top:12px">
        <button class="primary" onclick="runAction('generate_one')">通常運転テスト：1本制作</button>
        <button class="primary" onclick="runPrivateTest()">通常運転フルテスト：YouTube非公開</button>
        <button onclick="runAction('prepare')">次の投稿枠を準備</button>
        <button onclick="runAction('due')">投稿時刻を確認</button>
      </div>
      <div class="small" style="margin-top:8px">
        1本制作はYouTubeへ投稿しません。フルテストは制作→YouTube非公開投稿まで確認し、完成動画はPCにも残します。
      </div>
    </section>

    <section class="card">
      <h2>ゲストAI</h2>
      <div id="guests"></div>
      <div class="actions" style="margin-top:12px">
        <button onclick="runAction('guest')">新ゲストを今すぐ生成</button>
      </div>
    </section>

    <section class="card full">
      <h2>AIスタジオ</h2>
      <div id="studioStatus" class="studio-status small"></div>
      <div class="actions" style="margin-bottom:12px">
        <button id="studioInstallBtn" onclick="runAction('studio_install')">AIスタジオをPCへ導入</button>
      </div>
      <div class="row"><span>ゲスト画像を自動生成</span><button id="guestImageAutoBtn" onclick="toggleGuestImageAuto()"></button></div>
      <div class="row"><span>AI動画素材を自動生成</span><button id="aiVideoBtn" onclick="toggleAiVideo()"></button></div>
      <div id="aiVideoStatus" class="small" style="margin:8px 0 12px"></div>
      <div class="actions" style="margin-top:12px">
        <select id="miraiExpression">
          <option value="normal">ミライ通常</option>
          <option value="smile">ミライ笑顔</option>
          <option value="wink">ミライウインク</option>
          <option value="surprised">ミライ驚き</option>
          <option value="thinking">ミライ考え中</option>
          <option value="serious">ミライ真剣</option>
        </select>
        <button onclick="runStudioMirai()">ミライ画像生成</button>
      </div>
      <div class="actions" style="margin-top:10px">
        <input id="backgroundTheme" placeholder="背景テーマ 例: 未来の青いスタジオ" style="min-width:260px;flex:1">
        <button onclick="runStudioBackground()">背景生成</button>
      </div>
      <div class="actions" style="margin-top:10px">
        <input id="aiVideoPrompt" placeholder="AI動画テスト 例: futuristic blue AI studio" style="min-width:260px;flex:1">
        <button onclick="runStudioAiVideo()">AI動画を1本テスト</button>
      </div>
      <div class="actions" style="margin-top:10px">
        <select id="studioGuestSelect" style="min-width:220px"></select>
        <button onclick="runStudioGuest()">選択ゲスト画像生成</button>
      </div>
      <div id="studioAssets" class="studio-grid"></div>
    </section>

    <section class="card full">
      <h2>生成ライブラリ</h2>
      <p class="small">完成動画・投稿状況・生成画像をここで確認できます。動画はプレビューを開いた時だけ読み込みます。</p>
      <h3 style="font-size:14px;margin:16px 0 10px">完成動画</h3>
      <div id="videos" class="library-list"></div>
      <h3 style="font-size:14px;margin:20px 0 10px">生成画像</h3>
      <div id="libraryImages" class="library-images"></div>
    </section>

    <section class="card full">
      <h2>軽量・再起動なし運用</h2>
      <p class="small">本番画質は落とさず、テストと保存容量だけを軽くします。下の画質設定は保存直後から次の生成へ反映されます。</p>
      <div class="row"><span>キャラ候補数</span><input id="visualCandidates" type="number" min="1" max="4" style="width:92px"></div>
      <div class="row"><span>背景候補数</span><input id="visualBackgroundCandidates" type="number" min="1" max="3" style="width:92px"></div>
      <div class="row"><span>低品質時の再生成</span><input id="visualRetries" type="number" min="0" max="2" style="width:92px"></div>
      <div class="row"><span>画像の最低品質点</span><input id="visualMinScore" type="number" min="40" max="95" style="width:92px"></div>
      <div class="row"><span>AI動画の最低品質点</span><input id="visualVideoMinScore" type="number" min="40" max="95" style="width:92px"></div>
      <div class="actions" style="margin-top:12px">
        <button class="primary" onclick="saveVisualSettings()">画質設定を即時反映</button>
        <button onclick="runAction('quick_test')">軽量クイックテスト</button>
        <button onclick="runAction('compact_storage')">容量を安全に最適化</button>
      </div>
      <div id="quickOpsStatus" class="studio-status small" style="margin-top:12px"></div>
      <div class="small">クイックテストは一時ファイルだけを使い、AIモデルの重い本番生成やYouTube投稿は行いません。</div>
    </section>

    <section class="card full">
      <h2>AI改善センター</h2>
      <p class="small">失敗を記録し、同じ失敗を繰り返さないよう負荷設定を学習します。コード変更案は勝手に適用しません。</p>
      <div class="actions" style="margin-bottom:12px">
        <button class="primary" onclick="runImprovementReview()">失敗と成績をAI分析</button>
      </div>
      <div id="resourceStatus" class="studio-status small"></div>
      <div id="improvementReport" class="strategy"></div>
      <h3 style="font-size:14px;margin:18px 0 8px">確認が必要な変更だけ</h3>
      <div id="approvalList"></div>
      <h3 style="font-size:14px;margin:18px 0 8px">最近の失敗学習</h3>
      <div id="failureHistory"></div>
    </section>

    <section class="card wide">
      <h2>ミライの成長戦略</h2>
      <div id="strategy" class="strategy"></div>
      <div id="analytics" style="margin-top:12px"></div>
    </section>

    <section class="card full">
      <h2>スマホ・外出先リモート管理</h2>
      <p class="small">Tailscaleのプライベートネットワークだけで管理画面を共有します。一般公開はしません。</p>
      <div class="row"><span>状態</span><span id="remoteAccessStatus" class="badge"></span></div>
      <div id="remoteAccessUrl" class="small" style="word-break:break-all;margin:8px 0"></div>
      <div class="actions">
        <button class="primary" onclick="runAction('remote_on')">リモート管理を有効化</button>
        <button onclick="runAction('remote_status')">状態を確認</button>
        <button class="danger" onclick="safeStop()">安全停止</button>
        <button onclick="runAction('remote_off')">解除</button>
      </div>
      <div class="small" style="margin-top:8px">初回だけPCとスマホへTailscaleを導入し、同じtailnetへログインする必要があります。</div>
    </section>

    <section class="card">
      <h2>PC自動起動</h2>
      <p class="small">PCを起動した時に、このWebアプリを自動で起動できます。</p>
      <div class="row"><span>現在</span><span id="autostartStatus" class="badge"></span></div>
      <div class="actions">
        <button onclick="runAction('autostart_on')">自動起動を登録</button>
        <button onclick="runAction('autostart_off')">解除</button>
      </div>
      <div class="row"><span>スリープ復帰自動運転</span><span id="wakeTaskStatus" class="badge"></span></div>
      <div class="actions" style="margin-top:10px">
        <button class="primary" onclick="runNightTest()">夜間テスト運用を開始</button>
        <button onclick="runAction('wake_task_on')">スリープ復帰を登録</button>
        <button onclick="runAction('wake_task_off')">解除</button>
      </div>
      <div class="small" style="margin-top:8px">夜間テストではローカル動画だけ制作し、YouTube自動投稿はOFFにします。</div>
      <div class="actions" style="margin-top:12px">
        <button onclick="smartUpdate()">最新版を自動反映（手動再起動不要）</button>
      </div>
    </section>

    <section class="card full">
      <h2>ログ</h2>
      <div class="actions" style="margin-bottom:10px">
        <button onclick="runAction('cleanup')">投稿済み動画をPCから掃除</button>
      </div>
      <pre id="logs"></pre>
    </section>
  </div>
</div>

<script>
let state=null;
async function api(path, body=null){
  const opt=body?{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}:{};
  const r=await fetch(path,opt);
  const data=await r.json();
  if(!r.ok) throw new Error(data.message||'error');
  return data;
}
function badge(ok){return '<span class="badge '+(ok?'ok':'ng')+'">'+(ok?'OK':'NG')+'</span>'}
async function refresh(){
  try{
    state=await api('/api/status');
    automationBtn.textContent=state.automation_enabled?'ON':'OFF';
    automationBtn.className=state.automation_enabled?'primary':'';
    uploadBtn.textContent=state.auto_upload_enabled?'ON':'OFF';
    uploadBtn.className=state.auto_upload_enabled?'danger':'';
    privacy.value=state.privacy;
    interval.value=String(state.interval_seconds);
    lastCycle.textContent='最終サイクル: '+(state.last_cycle_at||'未実行')+' / '+state.last_cycle_result;
    document.title=(state.system_ready?'✓ ':'⚠ ')+'ミライ AI YouTuber 管理';
    services.innerHTML=[
      ['Ollama',state.services.ollama],
      ['Voice '+escapeHtml((state.voice_provider||{}).name||''),state.services.voicevox],
      ['FFmpeg',state.services.ffmpeg],
      ['YouTube認証',state.services.youtube_token]
    ].map(x=>'<div class="row"><span>'+x[0]+'</span>'+badge(x[1])+'</div>').join('');
    const engines=state.engines||{};
    engineStatus.innerHTML=Object.values(engines).map(x=>
      '<div class="row"><span><b>'+escapeHtml(x.name)+'</b><div class="small">'+escapeHtml(x.detail||'')+'</div></span>'+badge(Boolean(x.available))+'</div>'
    ).join('');
    const rights=state.rights||{};
    const creditDetail=rights.voice_credit_resolved
      ? (rights.voice_credit||'不要')
      : '必須クレジット未解決';
    rightsStatus.innerHTML=[
      ['公開前リスクガード',Boolean(rights.publish_guard),'危険内容は自動投稿しない'],
      ['AI生成開示',Boolean(rights.ai_disclosure),'YouTubeへAI生成として送信'],
      ['音声クレジット',Boolean(rights.voice_credit_resolved),creditDetail],
      ['BGM権利',Boolean(rights.bgm_license_ok),rights.bgm_configured?'権利確認フラグ':'BGM未設定'],
      ['AI動画モデル',Boolean(rights.ai_video_license_ok),rights.ai_video_enabled?'ライセンス確認':'AI動画OFF']
    ].map(x=>
      '<div class="row"><span><b>'+escapeHtml(x[0])+'</b><div class="small">'+escapeHtml(x[2])+'</div></span>'+badge(x[1])+'</div>'
    ).join('');
    if(document.activeElement!==postsPerDay) postsPerDay.value=state.posts_per_day;
    if(document.activeElement!==postTimes) postTimes.value=state.post_times;
    if(document.activeElement!==guestEvery) guestEvery.value=state.guest_every;
    if(document.activeElement!==guestNewEvery) guestNewEvery.value=state.guest_new_every;
    const voiceState=state.voice_provider||{};
    if(document.activeElement!==voiceProvider) voiceProvider.value=voiceState.selected||'voicevox';
    autostartStatus.textContent=state.autostart_enabled?'登録済み':'未登録';
    autostartStatus.className='badge '+(state.autostart_enabled?'ok':'');
    wakeTaskStatus.textContent=state.wake_task_enabled?'登録済み':'未登録';
    wakeTaskStatus.className='badge '+(state.wake_task_enabled?'ok':'');
    const remoteOn=state.remote_access_status==='enabled';
    remoteAccessStatus.textContent=remoteOn?'有効':(state.remote_access_status||'未確認');
    remoteAccessStatus.className='badge '+(remoteOn?'ok':'');
    remoteAccessUrl.textContent=state.remote_access_url?('外出先URL: '+state.remote_access_url):'外出先URLはまだありません。';
    if(state.current_job){
      lastCycle.textContent='実行中: '+state.current_job+' / 開始 '+(state.current_job_started_at||'');
    }
    const ft=state.full_test||{};
    const ftSlots=(ft.slots||[]).map(x=>String(x).slice(11,16)).join(' / ');
    fullTestStatus.textContent='状態: '+(ft.status||'未実行')+' / 投稿 '+(ft.uploaded||0)+'/'+(ft.target||3)+(ftSlots?' / 予定 '+ftSlots:'')+(ft.end_at?' / 終了 '+String(ft.end_at).slice(11,16):'');
    queue.innerHTML=state.queue.length?state.queue.map(x=>'<div class="q"><b>'+escapeHtml(x.title)+'</b><div class="small">'+x.scheduled_for+' / #'+x.video_id+' / retry '+x.attempts+'</div></div>').join(''):'<div class="small">キューなし</div>';
    guests.innerHTML=state.guests.length?state.guests.map(x=>'<div class="row"><span>'+escapeHtml(x.name)+'</span><span class="small">'+x.appearances+'回 '+(x.has_image?'画像あり':'画像未生成')+'</span></div>').join(''):'<div class="small">まだゲストなし</div>';
    guestImageAutoBtn.textContent=state.guest_image_auto_enabled?'ON':'OFF';
    guestImageAutoBtn.className=state.guest_image_auto_enabled?'primary':'';
    aiVideoBtn.textContent=state.ai_video_enabled?'ON':'OFF';
    aiVideoBtn.className=state.ai_video_enabled?'danger':'';
    aiVideoStatus.textContent='AI動画: '+(state.ai_video.available?'利用可能':'未準備')+' / '+escapeHtml(state.ai_video.backend||'')+' / '+state.ai_video.frames+' frames / '+state.ai_video.steps+' steps / '+state.ai_video.size.join('x')+(state.ai_video_enabled?' / 自動生成ON':' / 自動生成OFF');
    const backend=state.studio.selected||'未接続';
    const gpu=state.studio.gpu_name||state.gpu.name||'CPU';
    const cuda=state.studio.cuda_available?('CUDA '+(state.studio.cuda_version||'')):'CUDA未検出';
    const vram=state.gpu.memory_total_mb?(' / VRAM空き '+state.gpu.memory_free_mb+'MB / '+state.gpu.memory_total_mb+'MB'):'';
    const ollamaLoaded=(state.gpu.loaded_ollama_models||[]).length?(' / Ollama: '+state.gpu.loaded_ollama_models.join(', ')):' / Ollama VRAM解放済み';
    studioStatus.textContent='画像エンジン: '+backend+' / Diffusers '+(state.studio.diffusers_installed?'導入済み':'未導入')+' / '+gpu+' / '+cuda+vram+ollamaLoaded+' / Model: '+state.studio.model;
    const installing=state.current_job==='AIスタジオ導入';
    studioInstallBtn.style.display=state.studio.diffusers_installed&&!installing?'none':'inline-block';
    studioInstallBtn.disabled=installing;
    studioInstallBtn.textContent=installing?'AIスタジオ導入中…':'AIスタジオをPCへ導入';
    studioGuestSelect.innerHTML=state.guests.length?state.guests.map(x=>'<option value="'+x.id+'">'+escapeHtml(x.name)+'</option>').join(''):'<option value="">ゲストなし</option>';
    studioAssets.innerHTML=state.studio_assets.length?state.studio_assets.map(x=>{
      const isVideo=(x.type==='ai_video'||x.type==='motion_video'||String(x.url||'').toLowerCase().endsWith('.mp4'));
      const media=x.url
        ? (isVideo
          ? '<video controls preload="none" src="'+escapeHtml(x.url)+'"></video>'
          : '<img src="'+escapeHtml(x.url)+'?t='+encodeURIComponent(x.created_at||'')+'" loading="lazy">')
        : '';
      const name=(x.meta&&x.meta.guest_name)?' / '+escapeHtml(x.meta.guest_name):'';
      return '<div class="asset">'+media+'<div class="meta"><b>'+escapeHtml(x.type||'asset')+name+'</b><div class="small">'+escapeHtml(x.backend||'')+' / '+escapeHtml(x.created_at||'')+'</div></div></div>';
    }).join(''):'<div class="small">まだ生成素材がありません。</div>';
    videos.innerHTML=state.videos.length?state.videos.map(x=>{
      const local=x.has_local_file
        ? '<span class="badge ok">PC動画あり</span>'
        : '<span class="badge">PC動画なし</span>';
      const yt=x.youtube_video_id
        ? '<span class="badge ok">YouTube投稿済み</span>'
        : '<span class="badge">未投稿</span>';
      const queue=x.scheduled_for
        ? '<span class="badge">予定 '+escapeHtml(x.scheduled_for)+'</span>'
        : '';
      const guest=x.guest_name
        ? '<span class="badge">Guest '+escapeHtml(x.guest_name)+'</span>'
        : '';
      const quality=x.quality_score===null||x.quality_score===undefined
        ? '<span class="badge">Quality未検査</span>'
        : '<span class="badge '+(x.quality_passed?'ok':'ng')+'">Quality '+Number(x.quality_score)+'/100 '+(x.quality_passed?'PASS':'FAIL')+'</span>';
      const err=x.error
        ? '<div class="small" style="color:#ff9aa8;margin-top:8px">エラー: '+escapeHtml(x.error)+'</div>'
        : '';
      const preview=x.preview_url
        ? '<details><summary>▶ 動画プレビュー</summary><video controls preload="none" src="'+escapeHtml(x.preview_url)+'"></video></details>'
        : '<div class="small" style="margin-top:10px">ローカル動画はありません。投稿済み動画は自動掃除された可能性があります。</div>';
      const youtube=x.youtube_video_id
        ? '<a class="linkbtn" target="_blank" rel="noopener" href="https://youtu.be/'+encodeURIComponent(x.youtube_video_id)+'">YouTubeで開く</a>'
        : '';
      return '<div class="library-item">'
        +'<div class="library-head"><div><b>#'+x.id+' '+escapeHtml(x.title)+'</b>'
        +'<div class="small">'+escapeHtml(x.created_at||'')+' / '+escapeHtml(x.status||'')+' / '+Number(x.views||0)+' views</div></div>'
        +youtube+'</div>'
        +'<div class="library-meta">'+local+yt+queue+guest+quality+'</div>'
        +err+preview+'</div>';
    }).join(''):'<div class="small">まだ動画履歴がありません。</div>';
    libraryImages.innerHTML=state.studio_assets.length?state.studio_assets.slice(0,40).map(x=>{
      if(!x.url) return '';
      const label=(x.meta&&x.meta.guest_name)?x.meta.guest_name:(x.meta&&x.meta.expression)?('ミライ '+x.meta.expression):(x.meta&&x.meta.theme)?x.meta.theme:x.type;
      const isVideo=(x.type==='ai_video'||x.type==='motion_video'||String(x.url).toLowerCase().endsWith('.mp4'));
      const media=isVideo
        ? '<video controls preload="none" style="width:100%;border-radius:10px;background:#000" src="'+escapeHtml(x.url)+'"></video>'
        : '<img src="'+escapeHtml(x.url)+'?t='+encodeURIComponent(x.created_at||'')+'" loading="lazy">';
      return '<div>'+media+'<div class="small">'+escapeHtml(label||x.type||'素材')+'</div></div>';
    }).join(''):'<div class="small">まだ生成素材がありません。</div>';
    const vr=state.visual_runtime||{};
    if(document.activeElement!==visualCandidates) visualCandidates.value=vr.candidate_count??2;
    if(document.activeElement!==visualBackgroundCandidates) visualBackgroundCandidates.value=vr.background_candidates??1;
    if(document.activeElement!==visualRetries) visualRetries.value=vr.retry_rounds??1;
    if(document.activeElement!==visualMinScore) visualMinScore.value=vr.min_score??60;
    if(document.activeElement!==visualVideoMinScore) visualVideoMinScore.value=vr.video_min_score??60;
    const storage=state.storage||{};
    let quickText='容量: '+Number(storage.total_mb||0).toFixed(2)+'MB';
    if(state.quick_diagnostics_last){
      try{
        const quick=JSON.parse(state.quick_diagnostics_last);
        quickText+=' / 軽量テスト: '+(quick.ok?'PASS':'要確認')+' / '+escapeHtml(quick.finished_at||'');
      }catch(e){}
    }
    quickOpsStatus.textContent=quickText;

    const rs=state.resource||{};
    const rmem=(rs.memory||{});
    const rgpu=(rs.gpu||{});
    const idle=(rs.user_idle_seconds===null||rs.user_idle_seconds===undefined)?'不明':Math.round(rs.user_idle_seconds)+'秒';
    resourceStatus.textContent='省負荷モード / PC無操作 '+idle+' / RAM空き '+(rmem.available_mb??'?')+'MB / VRAM空き '+(rgpu.memory_free_mb??'?')+'MB';
    const pending=(state.autonomy&&state.autonomy.pending)||[];
    approvalList.innerHTML=pending.length?pending.map(x=>{
      return '<div class="library-item"><div class="library-head"><div><b>'+escapeHtml(x.title||x.action_type)+'</b><div class="small">'+escapeHtml(x.action_type)+' / '+escapeHtml(x.created_at||'')+'</div></div></div><div class="small" style="margin-top:8px">'+escapeHtml(x.reason||'')+'</div><div class="actions" style="margin-top:10px"><button class="primary" onclick="resolveApproval('+x.id+',true)">承認</button><button onclick="resolveApproval('+x.id+',false)">却下</button></div></div>';
    }).join(''):'<div class="small">承認待ちはありません。安全な学習・生成・話し方改善は自動で進みます。</div>';
    const report=(state.improvement&&state.improvement.report)||{};
    const recs=(report.recommendations||[]).map(x=>'・['+escapeHtml(x.priority||'')+'] '+escapeHtml(x.action||'')+' — '+escapeHtml(x.reason||'')).join('\n');
    improvementReport.textContent=(report.summary||'まだAI改善分析を実行していません。')+(recs?'\n\n'+recs:'')+(report.requires_code_change?'\n\n※コード変更候補あり。自動適用はせず、承認後に更新します。':'');
    failureHistory.innerHTML=(state.improvement&&state.improvement.recent_failures||[]).length
      ? state.improvement.recent_failures.map(x=>'<div class="row"><span>'+escapeHtml(x.stage)+'</span><span class="small">'+escapeHtml(x.created_at)+' / '+escapeHtml(String(x.message||'').slice(0,120))+'</span></div>').join('')
      : '<div class="small">記録された失敗はまだありません。</div>';
    strategy.textContent=state.growth.strategy;
    analytics.innerHTML=state.growth.recent.map(x=>'<div class="row"><span>#'+x.video_id+' '+escapeHtml(x.title)+'</span><span class="small">'+x.checkpoint_hours+'h / score '+Number(x.score).toFixed(1)+' / '+x.views+' views</span></div>').join('');
    logs.textContent=state.log_tail;
  }catch(e){alert(e.message)}
}
function escapeHtml(v){return String(v??'').replace(/[&<>"']/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m]))}
async function toggleAutomation(){await api('/api/settings',{automation_enabled:!state.automation_enabled});refresh()}
async function toggleGuestImageAuto(){
  await api('/api/settings',{guest_image_auto_enabled:!state.guest_image_auto_enabled});
  refresh();
}
async function toggleAiVideo(){
  if(!state.ai_video_enabled && !confirm('AI動画はGTX 1070では重い処理です。1本ずつ直列生成でONにしますか？')) return;
  await api('/api/settings',{ai_video_enabled:!state.ai_video_enabled});
  refresh();
}
async function runImprovementReview(){
  const data=await api('/api/action',{action:'improvement_review'});
  alert(data.message);
  setTimeout(refresh,1000);
}
async function resolveApproval(id,approve){
  const label=approve?'承認':'却下';
  if(!confirm(label+'しますか？')) return;
  const data=await api('/api/action',{action:'resolve_approval',request_id:id,approve});
  alert(data.message);
  refresh();
}
async function runStudioMirai(){
  const data=await api('/api/action',{action:'studio_mirai',expression:miraiExpression.value});
  alert(data.message);
  setTimeout(refresh,1000);
}
async function runStudioBackground(){
  const theme=backgroundTheme.value.trim();
  if(!theme){alert('背景テーマを入力してください');return;}
  const data=await api('/api/action',{action:'studio_background',theme});
  alert(data.message);
  setTimeout(refresh,1000);
}
async function runStudioAiVideo(){
  const prompt=aiVideoPrompt.value.trim();
  if(!prompt){alert('AI動画の内容を入力してください');return;}
  if(!confirm('短いAI動画を1本だけ生成します。GTX 1070では時間がかかる場合があります。実行しますか？')) return;
  const data=await api('/api/action',{action:'studio_ai_video',prompt});
  alert(data.message);
  setTimeout(refresh,1200);
}
async function runStudioGuest(){
  const guestId=Number(studioGuestSelect.value||0);
  if(!guestId){alert('ゲストを選択してください');return;}
  const data=await api('/api/action',{action:'studio_guest',guest_id:guestId});
  alert(data.message);
  setTimeout(refresh,1000);
}
async function toggleUpload(){
  if(!state.auto_upload_enabled && state.privacy==='public' && !confirm('公開設定で自動投稿をONにします。よろしいですか？')) return;
  await api('/api/settings',{auto_upload_enabled:!state.auto_upload_enabled});refresh()
}
async function savePrivacy(){
  const v=privacy.value;
  if(v==='public'&&!confirm('公開に切り替えます。自動投稿ON時は一般公開されます。よろしいですか？')){privacy.value=state.privacy;return}
  await api('/api/settings',{privacy:v});refresh()
}
async function saveInterval(){await api('/api/settings',{interval_seconds:Number(interval.value)});refresh()}
async function saveOperationSettings(){
  const body={
    posts_per_day:Number(postsPerDay.value),
    post_times:postTimes.value,
    guest_every:Number(guestEvery.value),
    guest_new_every:Number(guestNewEvery.value)
  };
  const data=await api('/api/settings',body);
  alert(data.message);
  refresh();
}

async function saveVisualSettings(){
  try{
    await api('/api/settings',{
      visual_candidate_count:Number(visualCandidates.value),
      visual_background_candidates:Number(visualBackgroundCandidates.value),
      visual_retry_rounds:Number(visualRetries.value),
      visual_min_score:Number(visualMinScore.value),
      visual_video_min_score:Number(visualVideoMinScore.value)
    });
    await refresh();
    alert('画質設定を再起動なしで反映しました。');
  }catch(e){alert(e.message)}
}
async function runPrivateTest(){
  if(!confirm('通常運転と同じ制作フローで1本生成し、YouTubeへ非公開でテスト投稿します。完成動画はPCにも残します。実行しますか？')) return;
  await runAction('private_test');
}
async function runNightTest(){
  const data=await api('/api/action',{action:'night_test'});
  alert(data.message);
  refresh();
}
async function safeStop(){
  if(!confirm('自動生成・自動投稿を停止し、未投稿の完全テストキューも止めます。実行しますか？')) return;
  const data=await api('/api/action',{action:'safe_stop'});
  alert(data.message);
  refresh();
}
async function smartUpdate(){
  if(!confirm('最新版を取り込みます。設定変更は即時反映、Pythonコード更新時だけ裏で自動引継ぎします。続けますか？')) return;
  try{
    const data=await api('/api/action',{action:'update_smart'});
    alert(data.message+'\n手動でPCやアプリを再起動する必要はありません。');
    let tries=0;
    const timer=setInterval(async()=>{
      tries++;
      try{
        await api('/api/status');
        clearInterval(timer);
        await refresh();
      }catch(e){
        if(tries>30) clearInterval(timer);
      }
    },1000);
  }catch(e){alert(e.message)}
}
async function runAction(action){
  const data=await api('/api/action',{action});
  alert(data.message);
  setTimeout(refresh,500);
}
refresh();
setInterval(refresh,15000);
</script>
</body></html>"""


class Handler(BaseHTTPRequestHandler):
    server_version = "MiraiWeb/1.0"

    def _json(self, payload: dict, status: int = 200) -> None:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", "0") or 0)
        if not length:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/":
            raw = HTML.encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return

        if path == "/api/status":
            self._json(_status_payload())
            return

        if path.startswith("/media/videos/"):
            raw_id = path[len("/media/videos/"):].strip("/")
            try:
                video_id = int(raw_id)
            except ValueError:
                self._json({"message": "invalid video id"}, 400)
                return

            row = video_by_id(video_id)
            raw_path = (row or {}).get("output_path")
            if not raw_path:
                self._json({"message": "video file not found"}, 404)
                return

            candidate = Path(raw_path).resolve()
            video_root = VIDEO_DIR.resolve()
            try:
                candidate.relative_to(video_root)
            except ValueError:
                self._json({"message": "invalid video path"}, 400)
                return
            if not candidate.is_file():
                self._json({"message": "video file not found"}, 404)
                return

            file_size = candidate.stat().st_size
            range_header = self.headers.get("Range", "")
            content_type = (
                mimetypes.guess_type(candidate.name)[0]
                or "video/mp4"
            )

            if range_header.startswith("bytes="):
                raw_range = range_header[6:].split(",", 1)[0]
                start_raw, end_raw = (
                    raw_range.split("-", 1)
                    if "-" in raw_range
                    else (raw_range, "")
                )
                try:
                    start = int(start_raw) if start_raw else 0
                    end = (
                        int(end_raw)
                        if end_raw else file_size - 1
                    )
                except ValueError:
                    self.send_response(416)
                    self.end_headers()
                    return
                start = max(0, start)
                end = min(end, file_size - 1)
                if start > end or start >= file_size:
                    self.send_response(416)
                    self.send_header(
                        "Content-Range",
                        f"bytes */{file_size}",
                    )
                    self.end_headers()
                    return

                length = end - start + 1
                self.send_response(206)
                self.send_header("Content-Type", content_type)
                self.send_header("Accept-Ranges", "bytes")
                self.send_header(
                    "Content-Range",
                    f"bytes {start}-{end}/{file_size}",
                )
                self.send_header("Content-Length", str(length))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                with candidate.open("rb") as media:
                    media.seek(start)
                    remaining = length
                    while remaining > 0:
                        chunk = media.read(min(1024 * 1024, remaining))
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        remaining -= len(chunk)
                return

            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(file_size))
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            with candidate.open("rb") as media:
                shutil.copyfileobj(media, self.wfile)
            return

        if path.startswith("/studio-assets/"):
            relative = unquote(path[len("/studio-assets/"):])
            root = GENERATED_ROOT.resolve()
            candidate = (root / relative).resolve()
            try:
                candidate.relative_to(root)
            except ValueError:
                self._json({"message": "invalid asset path"}, 400)
                return
            if not candidate.is_file():
                self._json({"message": "asset not found"}, 404)
                return
            raw = candidate.read_bytes()
            content_type = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "private, max-age=300")
            self.end_headers()
            self.wfile.write(raw)
            return

        self._json({"message": "not found"}, 404)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        try:
            body = self._read_json()

            if path == "/api/settings":
                if "automation_enabled" in body:
                    set_automation_enabled(bool(body["automation_enabled"]))
                if "auto_upload_enabled" in body:
                    enabled = bool(body["auto_upload_enabled"])
                    if enabled and not Path(settings.youtube_token_file).exists():
                        raise ValueError("YouTube認証が未完了のため自動投稿をONにできません")
                    set_auto_upload_enabled(enabled)
                if "privacy" in body:
                    set_upload_privacy(str(body["privacy"]))
                if "interval_seconds" in body:
                    set_web_interval_seconds(int(body["interval_seconds"]))
                if "posts_per_day" in body:
                    set_posts_per_day(int(body["posts_per_day"]))
                if "post_times" in body:
                    set_post_times(str(body["post_times"]))
                if "guest_every" in body:
                    set_guest_appearance_every(int(body["guest_every"]))
                if "guest_new_every" in body:
                    set_guest_new_every(int(body["guest_new_every"]))
                if "voice_provider" in body:
                    set_voice_provider_name(str(body["voice_provider"]))
                if "guest_image_auto_enabled" in body:
                    set_guest_image_auto_enabled(
                        bool(body["guest_image_auto_enabled"])
                    )
                if "ai_video_enabled" in body:
                    enabled = bool(body["ai_video_enabled"])
                    if enabled and not settings.ai_video_license_confirmed:
                        raise ValueError(
                            "AI動画モデルの公開/商用利用条件が未確認です。"
                            " AI_VIDEO_LICENSE_CONFIRMED=true は確認後だけ設定してください。"
                        )
                    set_ai_video_enabled(enabled)
                if "visual_candidate_count" in body:
                    set_visual_candidate_count(int(body["visual_candidate_count"]))
                if "visual_background_candidates" in body:
                    set_visual_background_candidates(
                        int(body["visual_background_candidates"])
                    )
                if "visual_retry_rounds" in body:
                    set_visual_retry_rounds(int(body["visual_retry_rounds"]))
                if "visual_min_score" in body:
                    set_visual_min_score(int(body["visual_min_score"]))
                if "visual_video_min_score" in body:
                    set_visual_video_min_score(
                        int(body["visual_video_min_score"])
                    )

                _wake_event.set()
                self._json({"ok": True, "message": "設定を保存しました。自動運転へ反映します。"})
                return

            if path == "/api/action":
                action = str(body.get("action") or "")

                if action == "safe_stop":
                    request_runtime_cancel()
                    set_automation_enabled(False)
                    set_auto_upload_enabled(False)
                    set_ai_video_enabled(False)
                    cancelled = abort_full_test("安全停止")
                    _wake_event.set()
                    self._json({
                        "ok": True,
                        "message": (
                            "安全停止しました。"
                            f" 未投稿テストキュー {cancelled}件を停止。"
                            " 現在の小工程は終了後、次工程へ進みません。"
                        ),
                    })
                    return

                if action == "resolve_approval":
                    request_id = int(body.get("request_id") or 0)
                    approve = bool(body.get("approve"))
                    message = resolve_approval(request_id, approve)
                    self._json({"ok": True, "message": message})
                    return

                if action == "studio_mirai":
                    expression = str(body.get("expression") or "normal")
                    label = f"ミライ画像生成({expression})"
                    func = lambda: print(generate_mirai_image(expression))
                elif action == "studio_background":
                    theme = str(body.get("theme") or "").strip()
                    if not theme:
                        self._json({"message": "背景テーマが必要です"}, 400)
                        return
                    label = "背景画像生成"
                    func = lambda: print(generate_background_image(theme))
                elif action == "studio_ai_video":
                    prompt = str(body.get("prompt") or "").strip()
                    if not prompt:
                        self._json({"message": "AI動画プロンプトが必要です"}, 400)
                        return
                    label = "AI動画テスト生成"
                    func = lambda: print(generate_animatediff_clip(prompt))
                elif action == "studio_guest":
                    guest_id = int(body.get("guest_id") or 0)
                    row = next(
                        (
                            guest for guest in active_guests(100)
                            if int(guest["id"]) == guest_id
                        ),
                        None,
                    )
                    if not row:
                        self._json({"message": "ゲストが見つかりません"}, 404)
                        return
                    label = f"ゲスト画像生成({row['name']})"
                    func = lambda: print(generate_guest_image(row))
                else:
                    label = ""
                    func = None

                if func is not None:
                    def studio_runner():
                        result = _run_captured(label, func)
                        _append_log(result.get("message", ""))

                    threading.Thread(
                        target=studio_runner,
                        daemon=True,
                    ).start()
                    self._json(
                        {"ok": True, "message": f"{label}を開始しました。"}
                    )
                    return

                actions = {
                    "full_test_today": (
                        "今日18時まで3本完全テスト",
                        _run_today_full_test,
                    ),
                    "cycle": ("1サイクル", tick),
                    "prepare": ("動画準備", prepare_upcoming),
                    "due": ("投稿時刻確認", run_due),
                    "private_test": ("通常運転フルテスト（非公開）", run_private_upload_test),
                    "generate_one": (
                        "通常運転テスト（1本制作）",
                        lambda: run_generation(
                            render=True,
                            upload=False,
                            target_override=1,
                        ),
                    ),
                    "guest": ("新ゲスト生成", create_guest_now),
                    "cleanup": ("投稿済みファイル掃除", run_cleanup_uploaded),
                    "quick_test": (
                        "軽量クイックテスト",
                        lambda: print(json.dumps(
                            run_quick_diagnostics(),
                            ensure_ascii=False,
                            indent=2,
                        )),
                    ),
                    "compact_storage": (
                        "安全な容量最適化",
                        lambda: print(json.dumps(
                            compact_runtime_storage(),
                            ensure_ascii=False,
                            indent=2,
                        )),
                    ),
                    "services": ("AIサービス起動確認", _ensure_local_services),
                    "improvement_review": (
                        "AI改善分析",
                        lambda: print(json.dumps(
                            run_improvement_review(),
                            ensure_ascii=False,
                            indent=2,
                        )),
                    ),
                    "studio_install": (
                        "AIスタジオ導入",
                        _install_studio_dependencies,
                    ),
                    "autostart_on": (
                        "PC自動起動登録",
                        lambda: print(_install_windows_autostart()),
                    ),
                    "autostart_off": (
                        "PC自動起動解除",
                        lambda: print(_remove_windows_autostart()),
                    ),
                    "wake_task_on": (
                        "スリープ復帰自動運転登録",
                        lambda: print(_install_wake_task(60)),
                    ),
                    "wake_task_off": (
                        "スリープ復帰自動運転解除",
                        lambda: print(_remove_wake_task()),
                    ),
                    "remote_on": (
                        "プライベートリモート管理有効化",
                        lambda: print(_remote_access_enable()),
                    ),
                    "remote_status": (
                        "プライベートリモート管理状態確認",
                        lambda: print(_remote_access_refresh()),
                    ),
                    "remote_off": (
                        "プライベートリモート管理解除",
                        lambda: print(_remote_access_disable()),
                    ),
                    "night_test": (
                        "夜間テスト運用",
                        lambda: print(_night_test_mode()),
                    ),
                    "update_smart": (
                        "最新版を自動反映",
                        _update_and_restart,
                    ),
                }

                if action not in actions:
                    self._json({"message": "unknown action"}, 400)
                    return

                label, func = actions[action]

                if action == "update_smart":
                    result = _run_captured(label, func)
                    _append_log(result.get("message", ""))
                    self._json(
                        {
                            "ok": result["ok"],
                            "message": result["message"],
                        },
                        200 if result["ok"] else 500,
                    )
                    return

                def runner():
                    result = _run_captured(label, func)
                    _append_log(result.get("message", ""))

                threading.Thread(target=runner, daemon=True).start()
                self._json({"ok": True, "message": f"{label}を開始しました。"})
                return

            self._json({"message": "not found"}, 404)
        except Exception as exc:
            self._json({"message": str(exc)}, 500)

    def log_message(self, format: str, *args) -> None:
        return


def run(open_browser: bool = True) -> None:
    global _http_server

    init_db()
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    url = f"http://{HOST}:{PORT}"

    try:
        server = ThreadingHTTPServer((HOST, PORT), Handler)
        _http_server = server
    except OSError:
        if open_browser:
            webbrowser.open(url)
        return

    _ensure_local_services()
    _consume_full_test_request()

    worker = threading.Thread(
        target=_cycle_worker,
        name="mirai-cycle",
        daemon=True,
    )
    worker.start()

    print(f"[WEB] ミライ管理画面: {url}")
    if open_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        _stop_event.set()
        server.server_close()


if __name__ == "__main__":
    run(open_browser=True)
