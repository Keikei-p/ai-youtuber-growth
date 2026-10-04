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
from storage import init_db, set_channel_state


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
        return {"ready": True, "action": "already_running"}

    candidates = _candidate_ollama_paths()
    if not candidates:
        return {
            "ready": False,
            "action": "not_found",
            "detail": "Ollama実行ファイルを検出できません。",
        }

    try:
        subprocess.Popen(
            [str(candidates[0]), "serve"],
            cwd=str(ROOT),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=_creationflags(),
        )
    except Exception as exc:
        return {
            "ready": False,
            "action": "start_failed",
            "detail": str(exc),
        }

    for _ in range(12):
        if _http_ok(url):
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

    try:
        subprocess.Popen(
            [str(candidates[0])],
            cwd=str(candidates[0].parent),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=_creationflags(),
        )
    except Exception as exc:
        return {
            "ready": False,
            "action": "start_failed",
            "detail": str(exc),
        }

    for _ in range(25):
        if _http_ok(url):
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
