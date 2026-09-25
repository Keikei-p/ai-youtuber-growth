from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path

from paths import DATA_DIR, OUTPUT_DIR
from storage import DB_PATH, active_guests
from studio.asset_store import GENERATED_ROOT, INDEX_FILE, PROJECT_ROOT


def _size_bytes(path: Path) -> int:
    if not path.exists():
        return 0
    if path.is_file():
        try:
            return int(path.stat().st_size)
        except OSError:
            return 0
    total = 0
    for child in path.rglob("*"):
        if child.is_file():
            try:
                total += int(child.stat().st_size)
            except OSError:
                pass
    return total


def storage_snapshot() -> dict:
    logs = PROJECT_ROOT / "logs"
    parts = {
        "generated_bytes": _size_bytes(GENERATED_ROOT),
        "output_bytes": _size_bytes(OUTPUT_DIR),
        "data_bytes": _size_bytes(DATA_DIR),
        "logs_bytes": _size_bytes(logs),
    }
    parts["total_bytes"] = sum(parts.values())
    return {
        **parts,
        **{
            key.replace("_bytes", "_mb"): round(value / 1024 / 1024, 2)
            for key, value in parts.items()
        },
    }


def rotate_log(
    path: str | Path,
    *,
    max_bytes: int = 1_500_000,
    backups: int = 2,
) -> int:
    path = Path(path)
    if not path.is_file():
        return 0
    try:
        current_size = int(path.stat().st_size)
    except OSError:
        return 0
    if current_size <= max(100_000, int(max_bytes)):
        return 0

    backups = max(1, min(int(backups), 5))
    oldest = path.with_suffix(path.suffix + f".{backups}")
    try:
        if oldest.exists():
            oldest.unlink()
    except OSError:
        pass

    for index in range(backups - 1, 0, -1):
        source = path.with_suffix(path.suffix + f".{index}")
        target = path.with_suffix(path.suffix + f".{index + 1}")
        if source.exists():
            try:
                source.replace(target)
            except OSError:
                pass

    first = path.with_suffix(path.suffix + ".1")
    try:
        path.replace(first)
        path.touch()
        return current_size
    except OSError:
        return 0


def prune_folder(
    folder: str | Path,
    *,
    keep: int,
    protected: set[Path] | None = None,
) -> tuple[int, int]:
    folder = Path(folder)
    if not folder.exists():
        return 0, 0
    protected_resolved = {
        p.resolve()
        for p in (protected or set())
        if p
    }
    files = [
        path
        for path in folder.iterdir()
        if path.is_file()
    ]
    files.sort(
        key=lambda path: path.stat().st_mtime if path.exists() else 0,
        reverse=True,
    )
    kept_unprotected = 0
    removed = 0
    freed = 0
    for path in files:
        resolved = path.resolve()
        if resolved in protected_resolved:
            continue
        if kept_unprotected < max(0, int(keep)):
            kept_unprotected += 1
            continue
        try:
            size = int(path.stat().st_size)
            path.unlink()
            removed += 1
            freed += size
        except OSError:
            pass
    return removed, freed


def _remove_stale_temp_files(
    roots: list[Path],
    *,
    older_than_seconds: int = 7200,
) -> tuple[int, int]:
    now = time.time()
    removed = 0
    freed = 0
    suffixes = {".tmp", ".temp", ".part", ".partial"}
    for root in roots:
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if not path.is_file() or path.suffix.lower() not in suffixes:
                continue
            try:
                if now - path.stat().st_mtime < older_than_seconds:
                    continue
                size = int(path.stat().st_size)
                path.unlink()
                removed += 1
                freed += size
            except OSError:
                pass
    return removed, freed


def _compact_studio_index(limit: int = 180) -> int:
    if not INDEX_FILE.exists():
        return 0
    try:
        rows = json.loads(INDEX_FILE.read_text(encoding="utf-8"))
        if not isinstance(rows, list):
            rows = []
    except Exception:
        rows = []

    filtered = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        raw_path = str(row.get("path") or "")
        candidate = (
            (PROJECT_ROOT / raw_path).resolve()
            if raw_path and not Path(raw_path).is_absolute()
            else Path(raw_path).resolve()
            if raw_path
            else None
        )
        if candidate is not None and candidate.is_file():
            filtered.append(row)

    filtered = filtered[-max(20, min(int(limit), 250)):]
    old_size = INDEX_FILE.stat().st_size if INDEX_FILE.exists() else 0
    INDEX_FILE.write_text(
        json.dumps(filtered, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    new_size = INDEX_FILE.stat().st_size if INDEX_FILE.exists() else 0
    return max(0, int(old_size - new_size))


def _compact_database() -> list[str]:
    notes: list[str] = []
    if not DB_PATH.exists():
        return notes
    try:
        conn = sqlite3.connect(
            DB_PATH,
            timeout=10.0,
            isolation_level=None,
        )
        try:
            conn.execute("PRAGMA busy_timeout = 10000")
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            conn.execute("PRAGMA optimize")
            conn.execute("VACUUM")
            notes.append("SQLite VACUUM/optimize完了")
        finally:
            conn.close()
    except Exception as exc:
        notes.append(f"SQLite圧縮はスキップ: {exc}")
    return notes


def compact_runtime_storage() -> dict:
    before = storage_snapshot()
    removed = 0
    freed = 0
    notes: list[str] = []

    temp_removed, temp_freed = _remove_stale_temp_files(
        [OUTPUT_DIR, GENERATED_ROOT, DATA_DIR]
    )
    removed += temp_removed
    freed += temp_freed

    protected: set[Path] = set()
    try:
        for guest in active_guests(500):
            raw = str(guest.get("image_path") or "")
            if raw:
                protected.add(Path(raw))
    except Exception:
        pass

    policies = [
        (GENERATED_ROOT / "mirai", 18, set()),
        (GENERATED_ROOT / "backgrounds", 24, set()),
        (GENERATED_ROOT / "thumbnails", 24, set()),
        (GENERATED_ROOT / "videos", 8, set()),
        (GENERATED_ROOT / "guests", 12, protected),
    ]
    for folder, keep, protected_paths in policies:
        count, bytes_freed = prune_folder(
            folder,
            keep=keep,
            protected=protected_paths,
        )
        removed += count
        freed += bytes_freed

    freed += _compact_studio_index()

    log_path = PROJECT_ROOT / "logs" / "webapp.log"
    freed += rotate_log(log_path)

    notes.extend(_compact_database())
    after = storage_snapshot()

    return {
        "removed_files": removed,
        "freed_bytes": max(
            int(freed),
            int(before["total_bytes"] - after["total_bytes"]),
            0,
        ),
        "freed_mb": round(
            max(
                int(freed),
                int(before["total_bytes"] - after["total_bytes"]),
                0,
            )
            / 1024
            / 1024,
            2,
        ),
        "before": before,
        "after": after,
        "notes": notes,
        "policy": (
            "本番完成動画・学習DB・利用中ゲスト画像は保持し、"
            "古いプレビュー/一時ファイル/ログだけを圧縮"
        ),
    }
