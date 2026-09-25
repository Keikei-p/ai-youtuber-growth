from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any

from storage import connect


@dataclass(frozen=True)
class Diagnosis:
    category: str
    likely_cause: str
    confidence: float
    safe_actions: list[str]
    requires_approval: bool
    evidence: list[str]


def diagnose(
    stage: str,
    message: str,
    context: dict[str, Any] | None = None,
    occurrences: int = 1,
) -> Diagnosis:
    text = f"{stage} {message}".lower()
    evidence: list[str] = []
    actions: list[str] = []
    category = "unknown"
    cause = "原因を一意に特定できません。追加ログが必要です。"
    confidence = 0.35
    approval = False

    if any(x in text for x in ("device(s) is/are busy", "device unavailable", "cuda-capable device")):
        category = "gpu_contention"
        cause = "別プロセスまたは前工程がGPUを保持し、CUDAデバイスを確保できていない可能性が高いです。"
        confidence = 0.9
        evidence.append("CUDA device busy/unavailable")
        actions += ["Ollama VRAMを解放", "GPU工程を直列化", "再発時はAI動画をOFF"]
    elif any(x in text for x in ("out of memory", "cuda out of memory", "oom")):
        category = "gpu_memory"
        cause = "VRAM不足です。解像度・シーン数・同時ロードモデルの合計がGPU容量を超えています。"
        confidence = 0.94
        evidence.append("out of memory")
        actions += ["シーン画像数を減らす", "AI動画をOFF", "モデルをCPUへ退避"]
    elif "voicevox" in text and any(x in text for x in ("connection", "refused", "起動", "timeout")):
        category = "voice_service"
        cause = "音声providerへ接続できていません。サービス停止または起動待ちの可能性があります。"
        confidence = 0.9
        evidence.append("VOICEVOX connection/service error")
        actions += ["音声サービス状態確認", "次回サイクルで再試行"]
    elif "ffmpeg" in text or "ffprobe" in text:
        category = "media_tool"
        cause = "動画処理ツールの未検出、入力破損、またはエンコード処理失敗の可能性があります。"
        confidence = 0.8
        evidence.append("FFmpeg/ffprobe関連")
        actions += ["入力ファイル存在確認", "メディア検査", "軽量レンダリングへフォールバック"]
    elif any(x in text for x in ("youtube", "oauth", "token", "credential", "unauthorized")):
        category = "youtube_auth_or_upload"
        cause = "YouTube認証またはアップロードAPI側の失敗です。"
        confidence = 0.83
        evidence.append("YouTube/OAuth/token関連")
        actions += ["token状態確認", "private投稿で再試行"]
        approval = True
    elif "database is locked" in text or "sqlite" in text and "locked" in text:
        category = "database_contention"
        cause = "SQLiteへの同時書き込み競合です。"
        confidence = 0.98
        evidence.append("database is locked")
        actions += ["WAL/busy timeout確認", "長時間トランザクションを避ける"]
    elif any(
        x in text
        for x in (
            "exact_duplicate_script",
            "duplicate_script",
            "content duplicate",
        )
    ):
        category = "content_duplicate"
        cause = (
            "直近動画と同一または実質同じ台本を再生成しています。"
            "企画ローテーションまたはフック分散が不足しています。"
        )
        confidence = 0.97
        evidence.append("duplicate script/content")
        actions += [
            "直近企画を候補から除外",
            "実績型・改善型・新規実験を分散",
            "別フックへ切り替え",
        ]
    elif any(x in text for x in ("json", "ollama", "ai response")):
        category = "text_model_output"
        cause = "ローカルLLMの出力形式が期待したJSON/文章形式から外れた可能性があります。"
        confidence = 0.72
        evidence.append("JSON/Ollama output error")
        actions += ["安全テンプレートへフォールバック", "出力スキーマを再指示"]

    if occurrences >= 2:
        evidence.append(f"同一失敗 {occurrences}回")
    if context:
        evidence.append("工程コンテキストあり")

    return Diagnosis(
        category=category,
        likely_cause=cause,
        confidence=round(confidence, 2),
        safe_actions=actions,
        requires_approval=approval,
        evidence=evidence,
    )


class MiraiDebugEngine:
    def _ensure_table(self) -> None:
        with connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS debug_diagnoses (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    message TEXT NOT NULL,
                    diagnosis_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_debug_created
                ON debug_diagnoses(created_at);
                """
            )

    def diagnose_and_store(
        self,
        stage: str,
        message: str,
        *,
        context: dict[str, Any] | None = None,
        occurrences: int = 1,
    ) -> dict:
        diagnosis = diagnose(stage, message, context, occurrences)
        payload = asdict(diagnosis)
        payload["context"] = context or {}
        self._ensure_table()
        with connect() as conn:
            conn.execute(
                """
                INSERT INTO debug_diagnoses (
                    created_at, stage, message, diagnosis_json
                ) VALUES (?, ?, ?, ?)
                """,
                (
                    datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    stage,
                    str(message)[:2000],
                    json.dumps(payload, ensure_ascii=False),
                ),
            )
        return payload

    def recent(self, limit: int = 20) -> list[dict]:
        self._ensure_table()
        with connect() as conn:
            rows = conn.execute(
                """
                SELECT created_at, stage, message, diagnosis_json
                FROM debug_diagnoses
                ORDER BY id DESC
                LIMIT ?
                """,
                (max(1, min(int(limit), 100)),),
            ).fetchall()
        result: list[dict] = []
        for row in rows:
            try:
                payload = json.loads(row["diagnosis_json"])
            except Exception:
                payload = {}
            payload.update(
                {
                    "created_at": row["created_at"],
                    "stage": row["stage"],
                    "message": row["message"],
                }
            )
            result.append(payload)
        return result
