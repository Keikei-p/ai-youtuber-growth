from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from storage import connect, get_channel_state, set_channel_state

_PROFILES = ("detail", "cinematic", "balanced")
_SUFFIX = {
    "detail": {
        "mirai": "crisp clean lineart, coherent anatomy, detailed face, natural hands, sharp focus, clean silhouette, balanced lighting",
        "guest": "crisp clean lineart, coherent anatomy, detailed face, natural hands, sharp focus, clean silhouette, balanced lighting",
        "background": "sharp architectural details, clear depth separation, crisp edges, balanced lighting, uncluttered focal area",
        "image": "sharp focus, crisp edges, balanced lighting, clean composition",
    },
    "cinematic": {
        "mirai": "cinematic contrast, detailed face, controlled highlights, strong subject separation, clean anatomy, polished anime key visual",
        "guest": "cinematic contrast, detailed face, controlled highlights, strong subject separation, clean anatomy, polished anime key visual",
        "background": "cinematic depth, controlled contrast, atmospheric perspective, clear focal hierarchy, polished lighting",
        "image": "cinematic contrast, strong subject separation, polished lighting",
    },
    "balanced": {
        "mirai": "natural balanced color, clean face, readable silhouette, consistent character proportions, artifact-free anime illustration",
        "guest": "natural balanced color, clean face, readable silhouette, consistent character proportions, artifact-free anime illustration",
        "background": "balanced color, clean composition, readable depth, minimal clutter, artifact-free environment illustration",
        "image": "balanced color, clean composition, readable depth, artifact-free",
    },
}


class VisualLearningMemory:
    def _ensure_tables(self) -> None:
        with connect() as conn:
            conn.executescript("""
            CREATE TABLE IF NOT EXISTS visual_learning (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                asset_type TEXT NOT NULL,
                path TEXT NOT NULL DEFAULT '',
                prompt TEXT NOT NULL DEFAULT '',
                backend TEXT NOT NULL DEFAULT '',
                profile TEXT NOT NULL DEFAULT '',
                score INTEGER NOT NULL,
                passed INTEGER NOT NULL,
                accepted INTEGER NOT NULL,
                metrics_json TEXT NOT NULL DEFAULT '{}',
                meta_json TEXT NOT NULL DEFAULT '{}'
            );
            CREATE INDEX IF NOT EXISTS idx_visual_learning_type
            ON visual_learning(asset_type, accepted, score);
            CREATE INDEX IF NOT EXISTS idx_visual_learning_path
            ON visual_learning(path);
            CREATE TABLE IF NOT EXISTS video_visual_profiles (
                video_id INTEGER PRIMARY KEY,
                created_at TEXT NOT NULL,
                profile_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS visual_performance (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                video_id INTEGER NOT NULL,
                checkpoint_hours INTEGER NOT NULL,
                captured_at TEXT NOT NULL,
                avg_view_percentage REAL NOT NULL DEFAULT 0,
                views INTEGER NOT NULL DEFAULT 0,
                analytics_score REAL NOT NULL DEFAULT 0,
                UNIQUE(video_id, checkpoint_hours)
            );
            """)

    def record_result(self, *, asset_type: str, path: str | Path | None, prompt: str,
                      backend: str, profile: str, score: int, passed: bool,
                      accepted: bool, metrics: dict | None = None, meta: dict | None = None) -> None:
        self._ensure_tables()
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with connect() as conn:
            conn.execute("""
                INSERT INTO visual_learning (
                    created_at, asset_type, path, prompt, backend, profile,
                    score, passed, accepted, metrics_json, meta_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                now, str(asset_type), str(path or ""), str(prompt or ""),
                str(backend or ""), str(profile or ""), int(score),
                1 if passed else 0, 1 if accepted else 0,
                json.dumps(metrics or {}, ensure_ascii=False),
                json.dumps(meta or {}, ensure_ascii=False),
            ))

    def _decode(self, row: dict) -> dict:
        for key in ("metrics_json", "meta_json"):
            try:
                row[key[:-5]] = json.loads(row.pop(key) or "{}")
            except Exception:
                row[key[:-5]] = {}
                row.pop(key, None)
        row["passed"] = bool(row.get("passed"))
        row["accepted"] = bool(row.get("accepted"))
        row["score"] = int(row.get("score") or 0)
        return row

    def recent(self, *, asset_type: str | None = None, limit: int = 30) -> list[dict]:
        self._ensure_tables()
        sql = "SELECT * FROM visual_learning"
        params: list[Any] = []
        if asset_type:
            sql += " WHERE asset_type = ?"
            params.append(asset_type)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(max(1, min(int(limit), 200)))
        with connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._decode(dict(r)) for r in rows]

    def find_by_path(self, path: str | Path) -> dict | None:
        target = str(Path(path).resolve())
        for row in self.recent(limit=200):
            if not row.get("accepted") or not row.get("path"):
                continue
            try:
                if str(Path(row["path"]).resolve()) == target:
                    return row
            except Exception:
                if str(row["path"]) == str(path):
                    return row
        return None

    def strategy_state(self) -> dict:
        raw = get_channel_state("visual_strategy", "")
        try:
            data = json.loads(raw) if raw else {}
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def recommended_profile(self, asset_type: str) -> str:
        strategy_profile = str(self.strategy_state().get("preferred_profile") or "")
        if strategy_profile in _PROFILES:
            return strategy_profile
        scores: dict[str, list[int]] = defaultdict(list)
        for row in self.recent(asset_type=asset_type, limit=80):
            p = str(row.get("profile") or "")
            if row.get("accepted") and row.get("passed") and p in _PROFILES:
                scores[p].append(int(row.get("score") or 0))
        candidates = [(sum(v) / len(v), len(v), p) for p, v in scores.items() if len(v) >= 2]
        if candidates:
            candidates.sort(reverse=True)
            return candidates[0][2]
        return "detail"

    def profile_for_attempt(self, preferred: str, attempt_index: int) -> str:
        order = []
        for value in (preferred, "detail", "cinematic", "balanced"):
            if value in _PROFILES and value not in order:
                order.append(value)
        return order[int(attempt_index) % len(order)]

    def evolve_prompt(self, base_prompt: str, *, asset_type: str, profile: str) -> str:
        profile = profile if profile in _PROFILES else "detail"
        suffix = _SUFFIX[profile].get(asset_type) or _SUFFIX[profile]["image"]
        return f"{base_prompt}, visual quality target: {suffix}"

    def save_video_profile(self, video_id: int, profile: dict) -> None:
        self._ensure_tables()
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with connect() as conn:
            conn.execute("""
                INSERT INTO video_visual_profiles (video_id, created_at, profile_json)
                VALUES (?, ?, ?)
                ON CONFLICT(video_id) DO UPDATE SET
                    created_at = excluded.created_at,
                    profile_json = excluded.profile_json
            """, (int(video_id), now, json.dumps(profile, ensure_ascii=False)))

    def record_performance(self, *, video_id: int, checkpoint_hours: int,
                           avg_view_percentage: float, views: int,
                           analytics_score: float) -> None:
        self._ensure_tables()
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with connect() as conn:
            conn.execute("""
                INSERT INTO visual_performance (
                    video_id, checkpoint_hours, captured_at,
                    avg_view_percentage, views, analytics_score
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(video_id, checkpoint_hours) DO UPDATE SET
                    captured_at = excluded.captured_at,
                    avg_view_percentage = excluded.avg_view_percentage,
                    views = excluded.views,
                    analytics_score = excluded.analytics_score
            """, (int(video_id), int(checkpoint_hours), now,
                  float(avg_view_percentage or 0), int(views or 0),
                  float(analytics_score or 0)))

    def build_strategy(self) -> dict:
        self._ensure_tables()
        with connect() as conn:
            perf = [dict(r) for r in conn.execute(
                "SELECT * FROM visual_performance ORDER BY captured_at DESC, id DESC LIMIT 120"
            ).fetchall()]
            raw_profiles = conn.execute(
                "SELECT video_id, profile_json FROM video_visual_profiles"
            ).fetchall()
        profiles = {}
        for r in raw_profiles:
            try:
                profiles[int(r["video_id"])] = json.loads(r["profile_json"] or "{}")
            except Exception:
                profiles[int(r["video_id"])] = {}
        latest = {}
        for r in perf:
            latest.setdefault(int(r["video_id"]), r)
        joined = [(r, profiles[v]) for v, r in latest.items() if v in profiles]
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        if len(joined) < 3:
            strategy = {
                "status": "collecting", "sample_size": len(joined),
                "preferred_profile": None, "preferred_expression": None,
                "ai_video_signal": "insufficient", "updated_at": now,
            }
            set_channel_state("visual_strategy", json.dumps(strategy, ensure_ascii=False))
            return strategy
        joined.sort(key=lambda pair: (
            float(pair[0].get("avg_view_percentage") or 0),
            float(pair[0].get("analytics_score") or 0),
            int(pair[0].get("views") or 0),
        ), reverse=True)
        top = joined[:max(2, (len(joined) + 1) // 2)]
        profile_counter = Counter()
        expression_counter = Counter()
        ai_yes, ai_no, scores = [], [], []
        for perf_row, visual in top:
            for p in visual.get("profiles") or []:
                if p in _PROFILES:
                    profile_counter[p] += 1
            e = str(visual.get("expression") or "")
            if e:
                expression_counter[e] += 1
            retention = float(perf_row.get("avg_view_percentage") or 0)
            (ai_yes if visual.get("ai_video_used") else ai_no).append(retention)
            if visual.get("avg_visual_score") is not None:
                scores.append(float(visual["avg_visual_score"]))
        ai_signal = "insufficient"
        if len(ai_yes) >= 2 and len(ai_no) >= 2:
            yes_avg, no_avg = sum(ai_yes) / len(ai_yes), sum(ai_no) / len(ai_no)
            ai_signal = "positive" if yes_avg > no_avg * 1.05 else "negative" if yes_avg < no_avg * 0.95 else "neutral"
        strategy = {
            "status": "active", "sample_size": len(joined), "top_sample_size": len(top),
            "preferred_profile": profile_counter.most_common(1)[0][0] if profile_counter else None,
            "preferred_expression": expression_counter.most_common(1)[0][0] if expression_counter else None,
            "ai_video_signal": ai_signal,
            "avg_visual_score_top": round(sum(scores) / len(scores), 2) if scores else None,
            "updated_at": now,
        }
        set_channel_state("visual_strategy", json.dumps(strategy, ensure_ascii=False))
        return strategy

    def dashboard_state(self) -> dict:
        recent = self.recent(limit=80)
        accepted = [r for r in recent if r.get("accepted")]
        scores = [int(r.get("score") or 0) for r in accepted]
        strategy = self.strategy_state()
        return {
            "memory_count": len(recent),
            "accepted_count": len(accepted),
            "avg_score": round(sum(scores) / len(scores), 1) if scores else None,
            "preferred_profile": strategy.get("preferred_profile") or self.recommended_profile("mirai"),
            "strategy": strategy,
        }
