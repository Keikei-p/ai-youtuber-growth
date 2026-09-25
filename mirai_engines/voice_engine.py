from __future__ import annotations

import json
import re
import tempfile
import wave
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Protocol

from storage import get_channel_state


@dataclass(frozen=True)
class VoiceSegment:
    text: str
    emotion: str
    speed: float
    pitch: float
    intonation: float
    volume: float
    pause_ms: int


@dataclass(frozen=True)
class VoicePlan:
    segments: list[VoiceSegment]
    provider: str
    guidance: str


class VoiceProvider(Protocol):
    name: str

    def available(self) -> bool: ...

    def synthesize(
        self,
        text: str,
        output_path: Path,
        voice_params: dict | None = None,
    ) -> Path: ...


def _split_sentences(text: str, max_chars: int = 64) -> list[str]:
    cleaned = re.sub(r"\s+", " ", str(text or "")).strip()
    if not cleaned:
        return []

    raw_parts = re.split(r"(?<=[。！？!?])\s*|\n+", cleaned)
    parts: list[str] = []
    for raw in raw_parts:
        raw = raw.strip()
        if not raw:
            continue
        while len(raw) > max_chars:
            cut = max(
                raw.rfind("、", 0, max_chars + 1),
                raw.rfind("，", 0, max_chars + 1),
                raw.rfind(",", 0, max_chars + 1),
            )
            if cut < max_chars // 2:
                cut = max_chars
            else:
                cut += 1
            parts.append(raw[:cut].strip())
            raw = raw[cut:].strip()
        if raw:
            parts.append(raw)
    return parts


def _emotion_for(text: str) -> str:
    if any(word in text for word in ("危険", "注意", "重要", "絶対", "結論")):
        return "serious"
    if any(word in text for word in ("えっ", "まさか", "驚", "！？", "!?")):
        return "surprised"
    if any(word in text for word in ("嬉", "最高", "成功", "ありがとう", "楽しい")):
        return "positive"
    if any(word in text for word in ("なぜ", "どうして", "考え", "検証", "比較")):
        return "thinking"
    return "neutral"


def _parameters(emotion: str, text: str, index: int, total: int) -> VoiceSegment:
    speed = 1.04
    pitch = 0.0
    intonation = 1.05
    volume = 1.0
    pause_ms = 210

    if emotion == "serious":
        speed = 0.96
        pitch = -0.015
        intonation = 1.12
        pause_ms = 260
    elif emotion == "surprised":
        speed = 1.08
        pitch = 0.025
        intonation = 1.22
        pause_ms = 180
    elif emotion == "positive":
        speed = 1.07
        pitch = 0.015
        intonation = 1.15
        pause_ms = 190
    elif emotion == "thinking":
        speed = 0.98
        pitch = -0.005
        intonation = 1.08
        pause_ms = 240

    if index == 0:
        speed = min(speed + 0.03, 1.15)
        intonation = min(intonation + 0.05, 1.35)
    if index == total - 1:
        pause_ms = 120

    return VoiceSegment(
        text=text,
        emotion=emotion,
        speed=round(speed, 3),
        pitch=round(pitch, 3),
        intonation=round(intonation, 3),
        volume=round(volume, 3),
        pause_ms=pause_ms,
    )


class MiraiVoiceEngine:
    """
    ミライ側で文章分割・感情・話速・ピッチ・抑揚・間を決める。
    providerは波形生成だけを担当する交換可能な部品。
    """

    def __init__(self, provider: VoiceProvider):
        self.provider = provider

    def available(self) -> bool:
        return bool(self.provider.available())

    def plan(self, script: str) -> VoicePlan:
        sentences = _split_sentences(script)
        total = len(sentences)
        guidance = get_channel_state("autonomous_script_guidance", "").strip()
        segments = [
            _parameters(_emotion_for(text), text, index, total)
            for index, text in enumerate(sentences)
        ]
        return VoicePlan(
            segments=segments,
            provider=getattr(self.provider, "name", self.provider.__class__.__name__),
            guidance=guidance,
        )

    @staticmethod
    def _append_wav(
        destination: wave.Wave_write,
        source_path: Path,
        expected: tuple[int, int, int] | None,
    ) -> tuple[int, int, int]:
        with wave.open(str(source_path), "rb") as source:
            fmt = (
                source.getnchannels(),
                source.getsampwidth(),
                source.getframerate(),
            )
            if expected is not None and fmt != expected:
                raise RuntimeError(
                    f"音声フォーマットが途中で変化しました: {fmt} != {expected}"
                )
            if expected is None:
                destination.setnchannels(fmt[0])
                destination.setsampwidth(fmt[1])
                destination.setframerate(fmt[2])
            destination.writeframes(source.readframes(source.getnframes()))
            return fmt

    @staticmethod
    def _append_silence(
        destination: wave.Wave_write,
        fmt: tuple[int, int, int],
        pause_ms: int,
    ) -> None:
        channels, sample_width, rate = fmt
        frames = max(0, int(rate * max(pause_ms, 0) / 1000))
        destination.writeframes(b"\x00" * frames * channels * sample_width)

    def synthesize(self, script: str, output_path: Path) -> dict:
        plan = self.plan(script)
        if not plan.segments:
            raise ValueError("音声化する文章がありません。")
        if not self.available():
            raise RuntimeError(
                f"音声provider {plan.provider} を利用できません。"
            )

        output_path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="mirai_voice_") as tmp:
            tmp_dir = Path(tmp)
            segment_paths: list[Path] = []

            for index, segment in enumerate(plan.segments):
                target = tmp_dir / f"segment_{index:03d}.wav"
                self.provider.synthesize(
                    segment.text,
                    target,
                    voice_params={
                        "speed": segment.speed,
                        "pitch": segment.pitch,
                        "intonation": segment.intonation,
                        "volume": segment.volume,
                    },
                )
                segment_paths.append(target)

            fmt: tuple[int, int, int] | None = None
            with wave.open(str(output_path), "wb") as destination:
                for index, (segment, source_path) in enumerate(
                    zip(plan.segments, segment_paths)
                ):
                    fmt = self._append_wav(destination, source_path, fmt)
                    if fmt and index < len(plan.segments) - 1:
                        self._append_silence(destination, fmt, segment.pause_ms)

        sidecar = output_path.with_suffix(".voice.json")
        sidecar.write_text(
            json.dumps(asdict(plan), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return {
            "output_path": str(output_path),
            "plan_path": str(sidecar),
            "provider": plan.provider,
            "segments": [asdict(segment) for segment in plan.segments],
        }
