from __future__ import annotations
import json
import re
import requests
from config import settings

class OllamaClient:
    def available(self) -> bool:
        try:
            r = requests.get(f"{settings.ollama_url}/api/tags", timeout=2)
            return r.ok
        except requests.RequestException:
            return False

    def generate(self, prompt: str) -> str:
        r = requests.post(
            f"{settings.ollama_url}/api/generate",
            json={
                "model": settings.ollama_model,
                "prompt": prompt,
                "stream": False,
                "options": {"temperature": 0.85}
            },
            timeout=180,
        )
        r.raise_for_status()
        return r.json()["response"].strip()

    def generate_json(self, prompt: str):
        raw = self.generate(prompt)
        match = re.search(r"(\[.*\]|\{.*\})", raw, re.S)
        if not match:
            raise ValueError("AI response did not contain JSON")
        return json.loads(match.group(1))
