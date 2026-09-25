from __future__ import annotations

from pathlib import Path

import requests

from config import settings


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(float(value), high))


class VoicevoxClient:
    def attribution(self) -> str:
        # 既知IDは即解決。未知IDのみ公式Engineのspeaker一覧へ問い合わせる。
        known = {
            0: "四国めたん",
            1: "ずんだもん",
            2: "四国めたん",
            3: "ずんだもん",
            4: "四国めたん",
            5: "ずんだもん",
            6: "四国めたん",
            7: "ずんだもん",
            8: "春日部つむぎ",
            9: "波音リツ",
            10: "雨晴はう",
            11: "玄野武宏",
            13: "青山龍星",
            14: "冥鳴ひまり",
            16: "九州そら",
            22: "ずんだもん",
            36: "四国めたん",
            37: "四国めたん",
            38: "ずんだもん",
            61: "中国うさぎ",
            62: "中国うさぎ",
            63: "中国うさぎ",
            64: "中国うさぎ",
            65: "波音リツ",
            75: "ずんだもん",
            76: "ずんだもん",
        }
        speaker_id = int(settings.voicevox_speaker)
        name = known.get(speaker_id)
        if name:
            return f"VOICEVOX:{name}"

        try:
            response = requests.get(
                f"{settings.voicevox_url}/speakers",
                timeout=3,
            )
            response.raise_for_status()
            for speaker in response.json() or []:
                for style in speaker.get("styles") or []:
                    if int(style.get("id")) == speaker_id:
                        name = str(speaker.get("name") or "").strip()
                        if name:
                            return f"VOICEVOX:{name}"
        except Exception:
            pass
        return ""

    """
    波形生成provider。
    感情・話速・間の決定はMirai Voice Engine側で行い、
    ここではVOICEVOX APIへ安全な範囲で値を渡すだけ。
    """

    name = "voicevox"

    def available(self) -> bool:
        try:
            response = requests.get(
                f"{settings.voicevox_url}/version",
                timeout=2,
            )
            return response.ok
        except requests.RequestException:
            return False

    def synthesize(
        self,
        text: str,
        output_path: Path,
        voice_params: dict | None = None,
    ) -> Path:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        query_response = requests.post(
            f"{settings.voicevox_url}/audio_query",
            params={
                "text": text,
                "speaker": settings.voicevox_speaker,
            },
            timeout=30,
        )
        query_response.raise_for_status()
        query = query_response.json()

        params = voice_params or {}
        if "speed" in params:
            query["speedScale"] = _clamp(params["speed"], 0.5, 2.0)
        if "pitch" in params:
            query["pitchScale"] = _clamp(params["pitch"], -0.15, 0.15)
        if "intonation" in params:
            query["intonationScale"] = _clamp(
                params["intonation"],
                0.0,
                2.0,
            )
        if "volume" in params:
            query["volumeScale"] = _clamp(params["volume"], 0.0, 2.0)

        synthesis = requests.post(
            f"{settings.voicevox_url}/synthesis",
            params={"speaker": settings.voicevox_speaker},
            json=query,
            timeout=120,
        )
        synthesis.raise_for_status()
        output_path.write_bytes(synthesis.content)
        return output_path
