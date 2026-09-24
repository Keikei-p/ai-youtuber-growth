from __future__ import annotations

import gc
import json
import shutil
import subprocess
import sys
import threading
import time
from contextlib import contextmanager

import requests

from config import settings

_GPU_LOCK = threading.RLock()


def _loaded_ollama_models() -> list[str]:
    try:
        response = requests.get(
            settings.ollama_url.rstrip("/") + "/api/ps",
            timeout=3,
        )
        response.raise_for_status()
        data = response.json()
        models = data.get("models") or []
        names: list[str] = []
        for row in models:
            name = row.get("name") or row.get("model")
            if name:
                names.append(str(name))
        return names
    except Exception:
        return []


def unload_ollama_model(wait_seconds: float = 12.0) -> bool:
    """
    Ollamaサーバー自体は落とさず、VRAM上のモデルだけ解放する。
    APIで解放できない場合は ollama stop をフォールバックに使う。
    """
    target = str(settings.ollama_model)
    try:
        requests.post(
            settings.ollama_url.rstrip("/") + "/api/generate",
            json={
                "model": target,
                "prompt": "",
                "stream": False,
                "keep_alive": 0,
            },
            timeout=10,
        )
    except Exception:
        pass

    deadline = time.monotonic() + max(wait_seconds, 0)
    while time.monotonic() < deadline:
        loaded = _loaded_ollama_models()
        if not any(
            name == target
            or name.startswith(target + ":")
            or target.startswith(name + ":")
            for name in loaded
        ):
            return True
        time.sleep(0.5)

    ollama = shutil.which("ollama")
    if ollama:
        try:
            subprocess.run(
                [ollama, "stop", target],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=15,
                check=False,
                creationflags=(
                    subprocess.CREATE_NO_WINDOW
                    if sys.platform == "win32"
                    else 0
                ),
            )
            time.sleep(1.0)
        except Exception:
            pass

    loaded = _loaded_ollama_models()
    return not any(
        name == target
        or name.startswith(target + ":")
        or target.startswith(name + ":")
        for name in loaded
    )


def release_torch_cuda_cache() -> None:
    if "torch" not in sys.modules:
        gc.collect()
        return

    try:
        import torch

        gc.collect()
        if torch.cuda.is_available():
            try:
                torch.cuda.synchronize()
            except Exception:
                pass
            try:
                torch.cuda.empty_cache()
            except Exception:
                pass
            try:
                torch.cuda.ipc_collect()
            except Exception:
                pass
    except Exception:
        gc.collect()


def gpu_snapshot() -> dict:
    snapshot = {
        "available": False,
        "name": None,
        "memory_used_mb": None,
        "memory_total_mb": None,
        "memory_free_mb": None,
        "loaded_ollama_models": _loaded_ollama_models(),
    }
    nvidia_smi = shutil.which("nvidia-smi")
    if not nvidia_smi:
        return snapshot

    try:
        result = subprocess.run(
            [
                nvidia_smi,
                "--query-gpu=name,memory.used,memory.total,memory.free",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
            creationflags=(
                subprocess.CREATE_NO_WINDOW
                if sys.platform == "win32"
                else 0
            ),
        )
        line = (result.stdout or "").strip().splitlines()[0]
        parts = [part.strip() for part in line.split(",")]
        if len(parts) >= 4:
            snapshot.update(
                {
                    "available": True,
                    "name": parts[0],
                    "memory_used_mb": int(float(parts[1])),
                    "memory_total_mb": int(float(parts[2])),
                    "memory_free_mb": int(float(parts[3])),
                }
            )
    except Exception:
        pass
    return snapshot


@contextmanager
def exclusive_gpu_task(label: str):
    """
    ミライ内のGPU重処理を直列化し、画像処理前にはOllamaのVRAMを解放する。
    外部アプリは強制終了しない。
    """
    with _GPU_LOCK:
        print(f"[GPU] {label}: GPU使用権を取得")
        released = unload_ollama_model()
        print(
            f"[GPU] {label}: Ollama VRAM解放 "
            + ("OK" if released else "未確認")
        )
        release_torch_cuda_cache()
        time.sleep(0.5)
        try:
            yield
        finally:
            release_torch_cuda_cache()
            print(f"[GPU] {label}: GPU使用権を解放")
