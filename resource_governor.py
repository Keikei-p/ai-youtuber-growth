from __future__ import annotations

import ctypes
import os
import platform
from dataclasses import dataclass

from config import settings
from gpu_manager import gpu_snapshot


@dataclass(frozen=True)
class ResourceDecision:
    allowed: bool
    mode: str
    reason: str
    snapshot: dict


def _windows_idle_seconds() -> float | None:
    if os.name != "nt":
        return None

    class LASTINPUTINFO(ctypes.Structure):
        _fields_ = [
            ("cbSize", ctypes.c_uint),
            ("dwTime", ctypes.c_uint),
        ]

    info = LASTINPUTINFO()
    info.cbSize = ctypes.sizeof(info)
    try:
        if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
            return None
        tick = ctypes.windll.kernel32.GetTickCount()
        elapsed_ms = (tick - info.dwTime) & 0xFFFFFFFF
        return elapsed_ms / 1000.0
    except Exception:
        return None


def _memory_snapshot() -> dict:
    if os.name == "nt":
        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        status = MEMORYSTATUSEX()
        status.dwLength = ctypes.sizeof(status)
        try:
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(
                ctypes.byref(status)
            ):
                mb = 1024 * 1024
                return {
                    "total_mb": int(status.ullTotalPhys / mb),
                    "available_mb": int(status.ullAvailPhys / mb),
                    "load_percent": int(status.dwMemoryLoad),
                }
        except Exception:
            pass

    # Linux/macOS系の軽量フォールバック。
    try:
        if platform.system() == "Linux":
            values: dict[str, int] = {}
            with open("/proc/meminfo", "r", encoding="utf-8") as handle:
                for line in handle:
                    key, raw = line.split(":", 1)
                    values[key] = int(raw.strip().split()[0])
            total = values.get("MemTotal", 0) // 1024
            available = values.get("MemAvailable", 0) // 1024
            load = int((1 - available / total) * 100) if total else None
            return {
                "total_mb": total or None,
                "available_mb": available or None,
                "load_percent": load,
            }
    except Exception:
        pass

    return {
        "total_mb": None,
        "available_mb": None,
        "load_percent": None,
    }


def resource_snapshot() -> dict:
    return {
        "gpu": gpu_snapshot(),
        "memory": _memory_snapshot(),
        "user_idle_seconds": _windows_idle_seconds(),
    }


def background_production_decision(
    *,
    urgent: bool = False,
) -> ResourceDecision:
    """
    自動運転用の省負荷判定。
    手動実行はこの判定を通さず、ユーザー操作を優先する。
    """
    snapshot = resource_snapshot()
    memory = snapshot["memory"]
    gpu = snapshot["gpu"]
    idle = snapshot["user_idle_seconds"]

    minimum_memory = int(settings.resource_min_memory_mb)
    minimum_idle = int(settings.resource_min_idle_seconds)
    minimum_gpu = int(settings.resource_min_gpu_free_mb)

    if (
        not urgent
        and idle is not None
        and idle < minimum_idle
    ):
        return ResourceDecision(
            False,
            "defer",
            (
                "PC操作中のため重い自動生成を後回し "
                f"(無操作 {idle:.0f}秒 / 基準 {minimum_idle}秒)"
            ),
            snapshot,
        )

    available_memory = memory.get("available_mb")
    if (
        available_memory is not None
        and available_memory < minimum_memory
    ):
        return ResourceDecision(
            False,
            "defer",
            (
                "空きメモリが少ないため自動生成を後回し "
                f"({available_memory}MB / 基準 {minimum_memory}MB)"
            ),
            snapshot,
        )

    gpu_free = gpu.get("memory_free_mb")
    loaded_ollama = gpu.get("loaded_ollama_models") or []
    # Ollama分は生成直前に解放できるため、Ollamaだけ載っている時は許可。
    if (
        gpu_free is not None
        and gpu_free < minimum_gpu
        and not loaded_ollama
    ):
        return ResourceDecision(
            False,
            "defer",
            (
                "GPU空きVRAMが少ないため自動生成を後回し "
                f"({gpu_free}MB / 基準 {minimum_gpu}MB)"
            ),
            snapshot,
        )

    mode = "urgent" if urgent else "eco"
    return ResourceDecision(
        True,
        mode,
        "省負荷条件を満たしています",
        snapshot,
    )
