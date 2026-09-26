from __future__ import annotations
import json
import re
import requests

from config import settings
from native_models.brain import MiraiNativeBrainClient, brain_status


_NATIVE = MiraiNativeBrainClient()


def _provider_mode() -> str:
    value = str(
        getattr(settings, "mirai_text_provider", "auto")
        or "auto"
    ).strip().lower()
    return value if value in {"auto", "ollama", "mirai_native"} else "auto"


def _ollama_available() -> bool:
    try:
        r = requests.get(
            f"{settings.ollama_url}/api/tags",
            timeout=2,
        )
        return bool(r.ok)
    except requests.RequestException:
        return False


def text_ai_status() -> dict:
    mode = _provider_mode()
    native = brain_status()
    ollama_ok = _ollama_available() if mode != "mirai_native" else False
    if mode == "mirai_native":
        selected = "mirai_native" if native["ready"] else None
    elif mode == "ollama":
        selected = "ollama" if ollama_ok else None
    else:
        selected = (
            "mirai_native"
            if native.get("production_ready")
            else ("ollama" if ollama_ok else None)
        )
    return {
        "configured": mode,
        "selected": selected,
        "available": bool(selected),
        "native": native,
        "ollama_available": ollama_ok,
        "pretrained_dependency_active": selected == "ollama",
    }


class OllamaClient:
    """
    既存呼び出し互換の文章AI facade。
    名前は互換性のため残すが、auto/native時はQwen/Ollamaを通らない。
    """

    def _selected(self) -> str | None:
        return text_ai_status()["selected"]

    def provider_name(self) -> str:
        return self._selected() or _provider_mode()

    def available(self) -> bool:
        return self._selected() is not None

    def generate(self, prompt: str) -> str:
        selected = self._selected()
        if selected == "mirai_native":
            return _NATIVE.generate(prompt)
        if selected == "ollama":
            r = requests.post(
                f"{settings.ollama_url}/api/generate",
                json={
                    "model": settings.ollama_model,
                    "prompt": prompt,
                    "stream": False,
                    "options": {"temperature": 0.85},
                },
                timeout=180,
            )
            r.raise_for_status()
            return r.json()["response"].strip()
        if _provider_mode() == "mirai_native":
            raise RuntimeError(
                "MIRAI_TEXT_PROVIDER=mirai_nativeですが、"
                "Mirai Native Brainの学習済み重みがありません。"
            )
        raise RuntimeError(
            "文章AIが利用できません。Native Brain学習またはOllamaを確認してください。"
        )

    def generate_json(self, prompt: str):
        raw = self.generate(prompt)
        match = re.search(r"(\[.*\]|\{.*\})", raw, re.S)
        if not match:
            raise ValueError("AI response did not contain JSON")
        return json.loads(match.group(1))
