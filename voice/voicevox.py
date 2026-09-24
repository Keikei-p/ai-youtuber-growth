from __future__ import annotations
from pathlib import Path
import requests
from config import settings

class VoicevoxClient:
    def available(self) -> bool:
        try:
            r=requests.get(f"{settings.voicevox_url}/version",timeout=2)
            return r.ok
        except requests.RequestException:
            return False

    def synthesize(self,text:str,output_path:Path)->Path:
        output_path.parent.mkdir(parents=True,exist_ok=True)
        q=requests.post(
            f"{settings.voicevox_url}/audio_query",
            params={"text":text,"speaker":settings.voicevox_speaker},
            timeout=30,
        )
        q.raise_for_status()
        s=requests.post(
            f"{settings.voicevox_url}/synthesis",
            params={"speaker":settings.voicevox_speaker},
            json=q.json(),
            timeout=120,
        )
        s.raise_for_status()
        output_path.write_bytes(s.content)
        return output_path
