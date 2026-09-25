from __future__ import annotations

import io
import wave
from pathlib import Path

import requests

from config import settings


class MiraiLocalClient:
    """
    自作ミライ音声ソフトとのローカルHTTP接続。

    GET /health
      -> {"ok": true, "engine": "mirai-tts"}

    POST /synthesize
      JSON:
        {
          "text": "...",
          "voice_params": {
            "speed": 1.0,
            "pitch": 0.0,
            "intonation": 1.0,
            "volume": 1.0
          }
        }
      -> audio/wav bytes
    """

    name = "mirai-local"

    def __init__(self, base_url: str | None = None):
        self.base_url = str(
            base_url or settings.mirai_tts_url
        ).strip().rstrip("/")

    def attribution(self) -> str:
        return ""

    def available(self) -> bool:
        if not self.base_url:
            return False
        try:
            response = requests.get(
                f"{self.base_url}/health",
                timeout=2,
            )
            if not response.ok:
                return False
            data = response.json()
            return bool(data.get("ok"))
        except Exception:
            return False

    @staticmethod
    def _validate_wav(data: bytes) -> None:
        if len(data) < 44 or not data.startswith(b"RIFF"):
            raise RuntimeError(
                "Mirai Local TTSが有効なWAVを返しませんでした。"
            )
        try:
            with wave.open(io.BytesIO(data), "rb") as handle:
                if handle.getnchannels() not in {1, 2}:
                    raise RuntimeError(
                        "Mirai Local TTSのチャンネル数が不正です。"
                    )
                if handle.getframerate() < 8000:
                    raise RuntimeError(
                        "Mirai Local TTSのサンプルレートが低すぎます。"
                    )
                if handle.getnframes() <= 0:
                    raise RuntimeError(
                        "Mirai Local TTSの音声フレームが空です。"
                    )
        except wave.Error as exc:
            raise RuntimeError(
                "Mirai Local TTSのWAV形式を読み取れません。"
            ) from exc

    def synthesize(
        self,
        text: str,
        output_path: Path,
        voice_params: dict | None = None,
    ) -> Path:
        cleaned = str(text or "").strip()
        if not cleaned:
            raise ValueError("音声化する文章がありません。")

        output_path.parent.mkdir(parents=True, exist_ok=True)
        timeout = max(
            10,
            min(
                int(settings.mirai_tts_timeout_seconds),
                600,
            ),
        )
        response = requests.post(
            f"{self.base_url}/synthesize",
            json={
                "text": cleaned,
                "voice_params": voice_params or {},
            },
            timeout=timeout,
        )
        response.raise_for_status()
        data = bytes(response.content)
        self._validate_wav(data)
        output_path.write_bytes(data)
        return output_path
