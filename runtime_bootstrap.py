from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

from config import settings
from runtime_control import execution_mode, voice_provider_name
from storage import get_channel_state, init_db, set_channel_state


ROOT = Path(__file__).resolve().parent


def _http_ok(url: str, timeout: float = 2.0) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return 200 <= int(response.status) < 500
    except Exception:
        return False


def _creationflags() -> int:
    return (
        subprocess.CREATE_NO_WINDOW
        if os.name == "nt"
        else 0
    )


def _spawn_service(command: list[str], *, cwd: Path, name: str) -> subprocess.Popen:
    """Start a local service without a console and preserve its stderr for diagnosis."""
    log_path = ROOT / "logs" / ("runtime-" + name + ".log")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("ab") as service_log:
        return subprocess.Popen(
            command,
            cwd=str(cwd),
            stdout=service_log,
            stderr=subprocess.STDOUT,
            creationflags=_creationflags(),
        )


def _service_launch_guard(name: str) -> tuple[bool, str]:
    """Bound repeated service startups when the backend never becomes healthy."""
    key = "runtime_service_launch_" + name
    now = int(time.time())
    try:
        previous = json.loads(get_channel_state(key, "{}") or "{}")
        count = max(0, int(previous.get("attempts", 0)))
        last = int(previous.get("last_attempt", 0))
        # Retry once per 15m, then at most once per 6h after 3 failures.
        cooldown = 6 * 3600 if count >= 3 else 15 * 60
        if last and 0 <= now - last < cooldown:
            return False, f"service launch backoff ({cooldown - (now - last)}s remaining)"
        set_channel_state(key, json.dumps({
            "attempts": min(count + 1, 3), "last_attempt": now,
        }))
    except Exception as exc:
        return False, f"service launch guard unavailable: {type(exc).__name__}"
    return True, ""


def _service_ready(name: str) -> None:
    try:
        set_channel_state("runtime_service_launch_" + name, "{}")
    except Exception:
        pass


def _candidate_ollama_paths() -> list[Path]:
    values: list[Path] = []
    resolved = shutil.which("ollama")
    if resolved:
        values.append(Path(resolved))
    if os.name == "nt":
        local = Path(os.environ.get("LOCALAPPDATA", ""))
        programs = Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
        values.extend([
            local / "Programs" / "Ollama" / "ollama.exe",
            local / "Ollama" / "ollama.exe",
            programs / "Ollama" / "ollama.exe",
        ])
    seen: set[str] = set()
    result: list[Path] = []
    for path in values:
        key = str(path).lower()
        if key in seen:
            continue
        seen.add(key)
        if path.is_file():
            result.append(path)
    return result


def _candidate_voicevox_paths() -> list[Path]:
    values: list[Path] = []
    configured = str(settings.voicevox_exe or "").strip()
    if configured:
        values.append(Path(configured))
    if os.name == "nt":
        local = Path(os.environ.get("LOCALAPPDATA", ""))
        programs = Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
        values.extend([
            local / "Programs" / "VOICEVOX" / "VOICEVOX.exe",
            local / "VOICEVOX" / "VOICEVOX.exe",
            programs / "VOICEVOX" / "VOICEVOX.exe",
        ])
    seen: set[str] = set()
    result: list[Path] = []
    for path in values:
        key = str(path).lower()
        if key in seen:
            continue
        seen.add(key)
        if path.is_file():
            result.append(path)
    return result


def _start_ollama() -> dict[str, Any]:
    url = str(settings.ollama_url).rstrip("/") + "/api/tags"
    if _http_ok(url):
        _service_ready("ollama")
        return {"ready": True, "action": "already_running"}

    candidates = _candidate_ollama_paths()
    if not candidates:
        return {
            "ready": False,
            "action": "not_found",
            "detail": "Ollama実行ファイルを検出できません。",
        }

    allowed, guard_detail = _service_launch_guard("ollama")
    if not allowed:
        return {"ready": False, "action": "cooldown", "detail": guard_detail}

    try:
        _spawn_service([str(candidates[0]), "serve"], cwd=ROOT, name="ollama")
    except Exception as exc:
        return {
            "ready": False,
            "action": "start_failed",
            "detail": str(exc),
        }

    for _ in range(12):
        if _http_ok(url):
            _service_ready("ollama")
            return {
                "ready": True,
                "action": "started",
                "path": str(candidates[0]),
            }
        time.sleep(1)

    return {
        "ready": False,
        "action": "start_timeout",
        "path": str(candidates[0]),
    }


def _start_voicevox() -> dict[str, Any]:
    if voice_provider_name() != "voicevox":
        return {
            "ready": True,
            "action": "not_required",
            "provider": voice_provider_name(),
        }

    url = str(settings.voicevox_url).rstrip("/") + "/version"
    if _http_ok(url):
        _service_ready("voicevox")
        return {"ready": True, "action": "already_running"}

    candidates = _candidate_voicevox_paths()
    if not candidates:
        return {
            "ready": False,
            "action": "not_found",
            "detail": (
                "VOICEVOXを標準インストール場所でも検出できません。"
            ),
        }

    allowed, guard_detail = _service_launch_guard("voicevox")
    if not allowed:
        return {"ready": False, "action": "cooldown", "detail": guard_detail}

    try:
        _spawn_service([str(candidates[0])], cwd=candidates[0].parent, name="voicevox")
    except Exception as exc:
        return {
            "ready": False,
            "action": "start_failed",
            "detail": str(exc),
        }

    for _ in range(25):
        if _http_ok(url):
            _service_ready("voicevox")
            return {
                "ready": True,
                "action": "started",
                "path": str(candidates[0]),
            }
        time.sleep(1)

    return {
        "ready": False,
        "action": "start_timeout",
        "path": str(candidates[0]),
    }


def bootstrap_runtime() -> dict[str, Any]:
    init_db()
    ffmpeg = shutil.which("ffmpeg")
    mode = execution_mode()

    ollama = _start_ollama()
    voice = (
        _start_voicevox()
        if mode == "local"
        else {
            "ready": True,
            "action": "cloud_media",
        }
    )
    result = {
        "checked_at": datetime.now().astimezone().isoformat(
            timespec="seconds"
        ),
        "execution_mode": mode,
        "ollama": ollama,
        "voice": voice,
        "ffmpeg": {
            "ready": bool(ffmpeg) or mode == "cloud",
            "path": ffmpeg or "",
        },
    }
    blockers: list[str] = []
    if not ollama.get("ready"):
        blockers.append("ollama")
    if not voice.get("ready"):
        blockers.append("voice")
    if not result["ffmpeg"]["ready"]:
        blockers.append("ffmpeg")
    result["ready"] = not blockers
    result["blockers"] = blockers

    set_channel_state(
        "runtime_bootstrap_last",
        json.dumps(result, ensure_ascii=False),
    )
    return result


def main() -> None:
    result = bootstrap_runtime()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    # 投稿済み動画の配送はサービス未起動でも可能なため、
    # bootstrap不足だけでWindows cycle全体を異常終了させない。
    raise SystemExit(0)


if __name__ == "__main__":
    main()
