from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
GENERATED_ROOT = PROJECT_ROOT / "assets" / "generated"
INDEX_FILE = PROJECT_ROOT / "assets" / "studio_index.json"


def ensure_dirs() -> None:
    for name in ("mirai", "guests", "backgrounds", "thumbnails", "videos"):
        (GENERATED_ROOT / name).mkdir(parents=True, exist_ok=True)
    INDEX_FILE.parent.mkdir(parents=True, exist_ok=True)
    if not INDEX_FILE.exists():
        INDEX_FILE.write_text("[]", encoding="utf-8")


def _load() -> list[dict]:
    ensure_dirs()
    try:
        data = json.loads(INDEX_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _save(rows: list[dict]) -> None:
    INDEX_FILE.write_text(
        json.dumps(rows, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def record_asset(
    asset_type: str,
    path: str | Path,
    prompt: str,
    *,
    backend: str,
    meta: dict | None = None,
) -> dict:
    path = Path(path).resolve()
    try:
        relative_path = path.relative_to(PROJECT_ROOT).as_posix()
    except ValueError:
        relative_path = str(path)

    row = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "type": asset_type,
        "path": relative_path,
        "prompt": prompt,
        "backend": backend,
        "meta": meta or {},
    }
    rows = _load()
    rows.append(row)
    _save(rows[-500:])
    return row


def list_assets(asset_type: str | None = None, limit: int = 40) -> list[dict]:
    rows = list(reversed(_load()))
    if asset_type:
        rows = [row for row in rows if row.get("type") == asset_type]
    return rows[: max(1, min(int(limit), 200))]
