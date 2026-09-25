from __future__ import annotations

import io
import json
import shutil
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from PIL import Image, ImageFilter, ImageStat


@dataclass(frozen=True)
class VisualIssue:
    code: str
    severity: str
    message: str


@dataclass(frozen=True)
class VisualReport:
    passed: bool
    score: int
    issues: list[VisualIssue]
    metrics: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "score": self.score,
            "issues": [asdict(issue) for issue in self.issues],
            "metrics": self.metrics,
        }


def _rate(value: str | None) -> float:
    raw = str(value or "0/1")
    try:
        if "/" in raw:
            left, right = raw.split("/", 1)
            denom = float(right)
            return float(left) / denom if denom else 0.0
        return float(raw)
    except Exception:
        return 0.0


def analyze_image(image: Image.Image, *, asset_type: str = "image") -> VisualReport:
    rgb = image.convert("RGB")
    gray = rgb.convert("L")
    width, height = rgb.size
    gray_stats = ImageStat.Stat(gray)
    brightness = float(gray_stats.mean[0])
    contrast = float(gray_stats.stddev[0])
    sharpness = float(ImageStat.Stat(gray.filter(ImageFilter.FIND_EDGES)).mean[0])
    saturation = float(ImageStat.Stat(rgb.convert("HSV").getchannel("S")).mean[0])
    entropy = float(gray.entropy())
    score = 100
    issues: list[VisualIssue] = []

    if width <= 0 or height <= 0:
        issues.append(VisualIssue("invalid_dimensions", "critical", "画像サイズが不正です。"))
        score -= 100
    elif height <= width:
        issues.append(VisualIssue("not_vertical", "warning", "Shorts向けの縦構図になっていません。"))
        score -= 16

    if width < 384 or height < 576:
        issues.append(VisualIssue("low_resolution", "warning", "生成画像の解像度が低すぎます。"))
        score -= 22
    elif width < 512 or height < 768:
        issues.append(VisualIssue("resolution_below_target", "warning", "生成画像が推奨解像度未満です。"))
        score -= 7

    if brightness < 28 or brightness > 232:
        issues.append(VisualIssue("extreme_brightness", "warning", "画像が暗すぎる、または明るすぎます。"))
        score -= 22
    elif brightness < 48 or brightness > 212:
        issues.append(VisualIssue("brightness_weak", "warning", "明るさのバランスが弱めです。"))
        score -= 8

    if contrast < 12:
        issues.append(VisualIssue("very_low_contrast", "warning", "コントラストが低く、のっぺり見えます。"))
        score -= 24
    elif contrast < 28:
        issues.append(VisualIssue("low_contrast", "warning", "コントラストが低めです。"))
        score -= 10

    if sharpness < 1.5:
        issues.append(VisualIssue("very_blurry", "warning", "輪郭が弱く、ぼやけています。"))
        score -= 24
    elif sharpness < 4.0:
        issues.append(VisualIssue("soft_focus", "warning", "画像の輪郭がやや甘いです。"))
        score -= 9

    if entropy < 2.0:
        issues.append(VisualIssue("low_detail", "warning", "画面内の情報量が少なすぎます。"))
        score -= 20
    elif entropy < 4.0:
        issues.append(VisualIssue("detail_below_target", "warning", "細部の情報量が少なめです。"))
        score -= 7

    if asset_type in {"mirai", "guest"} and saturation < 8:
        issues.append(VisualIssue("low_color_separation", "warning", "キャラクターの色分離が弱めです。"))
        score -= 5

    score = max(0, min(int(round(score)), 100))
    passed = not any(i.severity == "critical" for i in issues) and score >= 60
    return VisualReport(
        passed, score, issues,
        {
            "width": width, "height": height,
            "brightness": round(brightness, 2),
            "contrast": round(contrast, 2),
            "sharpness": round(sharpness, 2),
            "saturation": round(saturation, 2),
            "entropy": round(entropy, 3),
            "asset_type": asset_type,
        },
    )


def analyze_video_probe(
    probe: dict, *, file_size: int, sampled_frame: VisualReport | None = None
) -> VisualReport:
    streams = probe.get("streams") or []
    video = next((r for r in streams if r.get("codec_type") == "video"), None)
    fmt = probe.get("format") or {}
    width = int((video or {}).get("width") or 0)
    height = int((video or {}).get("height") or 0)
    fps = _rate((video or {}).get("avg_frame_rate") or (video or {}).get("r_frame_rate"))
    try:
        duration = float(fmt.get("duration") or 0)
    except Exception:
        duration = 0.0
    score = 100
    issues: list[VisualIssue] = []

    if video is None:
        issues.append(VisualIssue("video_missing", "critical", "映像ストリームがありません。"))
        score -= 100
    else:
        if not (height > width > 0):
            issues.append(VisualIssue("not_vertical", "warning", "AI動画素材が縦構図ではありません。"))
            score -= 18
        if width < 320 or height < 480:
            issues.append(VisualIssue("video_resolution_low", "warning", "AI動画素材の解像度が低すぎます。"))
            score -= 25
        elif width < 384 or height < 576:
            score -= 8
        if fps < 5:
            issues.append(VisualIssue("video_fps_too_low", "warning", "AI動画素材のfpsが低すぎます。"))
            score -= 25
        elif fps < 8:
            score -= 8

    if duration <= 0.25:
        issues.append(VisualIssue("video_too_short", "critical", "AI動画素材が短すぎます。"))
        score -= 45
    if file_size < 20_000:
        issues.append(VisualIssue("video_file_too_small", "warning", "AI動画素材のファイルサイズが小さすぎます。"))
        score -= 25

    if sampled_frame is not None:
        score = int(round(score * 0.65 + sampled_frame.score * 0.35))
        for issue in sampled_frame.issues:
            if issue.code in {"very_blurry", "very_low_contrast", "extreme_brightness", "low_detail"}:
                issues.append(VisualIssue("frame_" + issue.code, "warning", "代表フレーム: " + issue.message))

    score = max(0, min(int(score), 100))
    passed = not any(i.severity == "critical" for i in issues) and score >= 60
    return VisualReport(
        passed, score, issues,
        {
            "width": width, "height": height, "fps": round(fps, 3),
            "duration": round(duration, 3), "file_size": int(file_size),
            "sample_frame_score": sampled_frame.score if sampled_frame else None,
        },
    )


class MiraiVisualQualityEngine:
    def inspect_image(self, image_or_path: Image.Image | str | Path, *, asset_type: str = "image") -> dict[str, Any]:
        if isinstance(image_or_path, Image.Image):
            return analyze_image(image_or_path, asset_type=asset_type).as_dict()
        path = Path(image_or_path)
        if not path.is_file():
            return VisualReport(False, 0, [VisualIssue("file_missing", "critical", "画像ファイルがありません。")], {"path": str(path)}).as_dict()
        with Image.open(path) as image:
            return analyze_image(image, asset_type=asset_type).as_dict()

    def inspect_video_asset(self, video_path: str | Path, *, asset_type: str = "ai_video") -> dict[str, Any]:
        path = Path(video_path)
        if not path.is_file():
            return VisualReport(False, 0, [VisualIssue("file_missing", "critical", "動画素材ファイルがありません。")], {"path": str(path)}).as_dict()
        ffprobe = shutil.which("ffprobe")
        if not ffprobe:
            return VisualReport(False, 0, [VisualIssue("ffprobe_missing", "critical", "ffprobeがなく動画素材を検査できません。")], {"path": str(path)}).as_dict()
        result = subprocess.run(
            [ffprobe, "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
            capture_output=True, text=True, timeout=30, check=True,
        )
        probe = json.loads(result.stdout or "{}")
        try:
            duration = float((probe.get("format") or {}).get("duration") or 0)
        except Exception:
            duration = 0.0
        sampled = None
        ffmpeg = shutil.which("ffmpeg")
        if ffmpeg:
            try:
                frame = subprocess.run(
                    [ffmpeg, "-v", "error", "-ss", f"{max(0.0, duration * 0.5):.3f}", "-i", str(path),
                     "-frames:v", "1", "-f", "image2pipe", "-vcodec", "png", "-"],
                    capture_output=True, timeout=30, check=True,
                )
                if frame.stdout:
                    with Image.open(io.BytesIO(frame.stdout)) as image:
                        sampled = analyze_image(image, asset_type=asset_type)
            except Exception:
                sampled = None
        return analyze_video_probe(probe, file_size=path.stat().st_size, sampled_frame=sampled).as_dict()
