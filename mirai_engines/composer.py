from __future__ import annotations

import re
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class ScenePlan:
    text: str
    duration: float
    motion: str
    background_index: int
    use_ai_video: bool
    emphasis: str


@dataclass(frozen=True)
class CompositionPlan:
    scenes: list[ScenePlan]
    total_duration: float
    version: int = 1


def _subtitle_chunks(script: str, max_chars: int = 30) -> list[str]:
    text = re.sub(r"\s+", " ", str(script or "")).strip()
    if not text:
        return [""]

    sentences = [
        item.strip()
        for item in re.split(r"(?<=[。！？!?])\s*", text)
        if item.strip()
    ]
    chunks: list[str] = []
    for sentence in sentences:
        current = sentence
        while len(current) > max_chars:
            candidates = [
                current.rfind(mark, 0, max_chars + 1)
                for mark in ("、", "，", ",", "・")
            ]
            cut = max(candidates)
            if cut < max_chars // 2:
                cut = max_chars
            else:
                cut += 1
            chunks.append(current[:cut].strip())
            current = current[cut:].strip()
        if current:
            chunks.append(current)
    return chunks or [text[:max_chars]]


def _emphasis(text: str) -> str:
    if any(x in text for x in ("結論", "重要", "注意", "ポイント")):
        return "strong"
    if any(x in text for x in ("？", "?", "なぜ", "どうして")):
        return "question"
    if any(x in text for x in ("！", "!", "最高", "成功")):
        return "energy"
    return "normal"


class MiraiComposer:
    """
    台本から字幕単位・場面時間・カメラモーション・背景選択を決める。
    FFmpegはこの計画を実行するだけ。
    """

    MOTIONS = ("push_in", "pan_left", "pan_right", "soft_hold")

    def plan(
        self,
        script: str,
        audio_duration: float,
        background_count: int = 1,
        has_ai_video: bool = False,
    ) -> dict:
        chunks = _subtitle_chunks(script)
        audio_duration = max(float(audio_duration), 1.0)
        weights = [max(len(chunk), 6) for chunk in chunks]
        weight_sum = float(sum(weights)) or 1.0

        raw = [audio_duration * weight / weight_sum for weight in weights]
        minimum = min(0.8, audio_duration / max(len(chunks), 1))
        durations = [max(minimum, value) for value in raw]
        scale = audio_duration / max(sum(durations), 0.001)
        durations = [value * scale for value in durations]

        scenes: list[ScenePlan] = []
        for index, (text, duration) in enumerate(zip(chunks, durations)):
            scenes.append(
                ScenePlan(
                    text=text,
                    duration=round(duration, 4),
                    motion=self.MOTIONS[index % len(self.MOTIONS)],
                    background_index=index % max(int(background_count), 1),
                    use_ai_video=bool(has_ai_video and index == 0),
                    emphasis=_emphasis(text),
                )
            )

        plan = CompositionPlan(
            scenes=scenes,
            total_duration=round(sum(scene.duration for scene in scenes), 4),
        )
        return asdict(plan)

    @staticmethod
    def validate(plan: dict, expected_duration: float) -> list[str]:
        issues: list[str] = []
        scenes = plan.get("scenes") or []
        if not scenes:
            return ["scene_missing"]
        if len(scenes) > 24:
            issues.append("too_many_scenes")
        for scene in scenes:
            text = str(scene.get("text") or "")
            if len(text) > 42:
                issues.append("subtitle_too_long")
                break
        total = sum(float(scene.get("duration") or 0) for scene in scenes)
        if abs(total - float(expected_duration)) > max(0.5, expected_duration * 0.03):
            issues.append("duration_mismatch")
        return issues
