from __future__ import annotations
import shutil
import sys
from pathlib import Path
import requests

from config import settings
from paths import (
    CHARACTER_FILE,
    CLIENT_SECRET_FILE,
    DATA_DIR,
    OUTPUT_DIR,
    TOKEN_FILE,
    ensure_runtime_dirs,
)

def check(name: str, ok: bool, detail: str) -> bool:
    mark = "OK" if ok else "NG"
    print(f"[{mark}] {name}: {detail}")
    return ok

def main() -> int:
    ensure_runtime_dirs()
    results=[]

    results.append(check("Python", sys.version_info >= (3, 11), sys.version.split()[0]))
    results.append(check("FFmpeg", shutil.which("ffmpeg") is not None, shutil.which("ffmpeg") or "見つかりません"))
    results.append(check("ffprobe", shutil.which("ffprobe") is not None, shutil.which("ffprobe") or "見つかりません"))
    results.append(check("Character", CHARACTER_FILE.exists(), str(CHARACTER_FILE)))
    results.append(check("Data dir", DATA_DIR.exists(), str(DATA_DIR)))
    results.append(check("Output dir", OUTPUT_DIR.exists(), str(OUTPUT_DIR)))

    try:
        r=requests.get(f"{settings.ollama_url}/api/tags",timeout=3)
        results.append(check("Ollama", r.ok, f"{settings.ollama_url} / model={settings.ollama_model}"))
    except requests.RequestException as exc:
        results.append(check("Ollama", False, f"{settings.ollama_url} / {exc}"))

    try:
        r=requests.get(f"{settings.voicevox_url}/version",timeout=3)
        detail=f"{settings.voicevox_url} / version={r.text.strip() if r.ok else r.status_code}"
        results.append(check("VOICEVOX", r.ok, detail))
    except requests.RequestException as exc:
        results.append(check("VOICEVOX", False, f"{settings.voicevox_url} / {exc}"))

    # YouTube credentials are optional until upload setup starts.
    print(
        f"[INFO] YouTube client secret: {'あり' if CLIENT_SECRET_FILE.exists() else '未設定'} "
        f"({CLIENT_SECRET_FILE})"
    )
    print(
        f"[INFO] YouTube token: {'あり' if TOKEN_FILE.exists() else '未認証'} "
        f"({TOKEN_FILE})"
    )
    print(f"[INFO] DRY_RUN={settings.dry_run} / privacy={settings.youtube_privacy_status}")

    core_ok=all(results)
    print()
    print("PC/サーバー共通コア環境:", "READY" if core_ok else "要確認")
    return 0 if core_ok else 1

if __name__ == "__main__":
    raise SystemExit(main())
