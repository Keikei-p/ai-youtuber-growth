from __future__ import annotations

import csv
import random
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from PIL import Image

from native_models.brain import ByteTokenizer
from native_models.common import (
    component_model_root,
    component_training_root,
    model_package_status,
    read_json,
    write_json,
)


@dataclass
class VideoConfig:
    size: int = 64
    channels: int = 48
    text_dim: int = 64
    frames: int = 8
    vocab_size: int = 258

    def validate(self) -> None:
        if self.size not in {32, 64, 96, 128}:
            raise ValueError("native video size must be 32/64/96/128")
        if self.frames < 2:
            raise ValueError("frames must be >=2")


def video_paths() -> dict[str, Path]:
    root = component_model_root("video-v0")
    training = component_training_root("video")
    return {
        "root": root,
        "weights": root / "model.pt",
        "meta": root / "model.json",
        "training": training,
        "metadata": training / "metadata.csv",
    }


def _frame_files(folder: Path) -> list[Path]:
    allowed = {".png", ".jpg", ".jpeg", ".webp"}
    return sorted(
        p for p in folder.iterdir()
        if p.is_file() and p.suffix.lower() in allowed
    )


def dataset_status() -> dict[str, Any]:
    paths = video_paths()
    valid = 0
    errors: list[str] = []
    if paths["metadata"].is_file():
        with paths["metadata"].open(
            "r",
            encoding="utf-8-sig",
            newline="",
        ) as handle:
            for line_no, row in enumerate(
                csv.reader(handle, delimiter="|"),
                start=1,
            ):
                if len(row) < 2:
                    errors.append(f"{line_no}行目: clip_dir|caption 形式ではありません")
                    continue
                folder = (paths["training"] / row[0].strip()).resolve()
                caption = "|".join(row[1:]).strip()
                try:
                    folder.relative_to(paths["training"].resolve())
                except ValueError:
                    errors.append(f"{line_no}行目: 学習フォルダ外です")
                    continue
                if not folder.is_dir() or len(_frame_files(folder)) < 2 or not caption:
                    errors.append(f"{line_no}行目: 2枚以上の連続frameとcaptionが必要")
                    continue
                valid += 1
    return {
        "ready": valid >= 10,
        "valid_count": valid,
        "errors": errors[:50],
        "training_root": str(paths["training"]),
        "metadata_file": str(paths["metadata"]),
        "detail": "学習開始可能" if valid >= 10 else "最低10クリップ必要",
    }


def video_status() -> dict[str, Any]:
    torch_available = False
    cuda_available = False
    try:
        import torch
        torch_available = True
        cuda_available = bool(torch.cuda.is_available())
    except Exception:
        pass
    model = model_package_status("video-v0")
    return {
        "code_ready": True,
        "pretrained_dependency": False,
        "torch_available": torch_available,
        "cuda_available": cuda_available,
        "dataset": dataset_status(),
        "model": model,
        "ready": bool(model.get("ready")) and torch_available,
        "quality_stage": "research-frame-predictor",
    }


def _encode_prompt(text: str) -> list[int]:
    return ByteTokenizer().encode(text, eos=True)[:128] or [257]


def build_model(config: VideoConfig):
    config.validate()
    import torch.nn as nn

    class Model(nn.Module):
        def __init__(self) -> None:
            super().__init__()
            self.text = nn.Embedding(config.vocab_size, config.text_dim)
            self.text_proj = nn.Linear(config.text_dim, config.channels)
            self.time = nn.Sequential(
                nn.Linear(1, config.channels),
                nn.SiLU(),
                nn.Linear(config.channels, config.channels),
            )
            self.in_conv = nn.Conv2d(3, config.channels, 3, padding=1)
            self.body = nn.Sequential(
                nn.GroupNorm(6, config.channels),
                nn.SiLU(),
                nn.Conv2d(config.channels, config.channels, 3, padding=1),
                nn.GroupNorm(6, config.channels),
                nn.SiLU(),
                nn.Conv2d(config.channels, config.channels, 3, padding=1),
                nn.SiLU(),
            )
            self.out = nn.Conv2d(config.channels, 3, 3, padding=1)

        def forward(self, frame, phase, tokens):
            mask = (tokens != 257).float().unsqueeze(-1)
            emb = self.text(tokens)
            pooled = (emb * mask).sum(1) / mask.sum(1).clamp(min=1.0)
            cond = self.text_proj(pooled) + self.time(
                phase.float().view(-1, 1)
            )
            h = self.in_conv(frame)
            h = h + cond[:, :, None, None]
            residual = self.out(h + self.body(h))
            return (frame + 0.25 * residual).clamp(-1, 1)

    return Model()


def _load_clips() -> list[tuple[list[Path], str]]:
    paths = video_paths()
    out: list[tuple[list[Path], str]] = []
    if not paths["metadata"].is_file():
        return out
    with paths["metadata"].open(
        "r",
        encoding="utf-8-sig",
        newline="",
    ) as handle:
        for row in csv.reader(handle, delimiter="|"):
            if len(row) < 2:
                continue
            folder = (paths["training"] / row[0].strip()).resolve()
            caption = "|".join(row[1:]).strip()
            try:
                folder.relative_to(paths["training"].resolve())
            except ValueError:
                continue
            if folder.is_dir() and caption:
                frames = _frame_files(folder)
                if len(frames) >= 2:
                    out.append((frames, caption))
    return out


def _tensor_image(path: Path, size: int, device):
    import torch
    with Image.open(path) as raw:
        im = raw.convert("RGB").resize((size, size))
    data = torch.tensor(
        list(im.getdata()),
        dtype=torch.float32,
        device=device,
    ).view(size, size, 3).permute(2, 0, 1)
    return data / 127.5 - 1.0


def train(
    *,
    steps: int = 2000,
    learning_rate: float = 2e-4,
    size: int = 64,
    channels: int = 48,
) -> dict:
    import torch
    import torch.nn.functional as F

    clips = _load_clips()
    if len(clips) < 10:
        raise RuntimeError("Native Video v0は最低10クリップ必要です。")
    config = VideoConfig(size=size, channels=channels)
    config.validate()
    device = "cuda" if torch.cuda.is_available() else "cpu"
    model = build_model(config).to(device)
    model.train()
    optimizer = torch.optim.AdamW(model.parameters(), lr=learning_rate)
    random.seed(42)
    losses: list[float] = []

    for step in range(1, max(1, int(steps)) + 1):
        frames, caption = random.choice(clips)
        index = random.randrange(len(frames) - 1)
        current = _tensor_image(frames[index], config.size, device).unsqueeze(0)
        target = _tensor_image(frames[index + 1], config.size, device).unsqueeze(0)
        phase = torch.tensor(
            [index / max(len(frames) - 1, 1)],
            device=device,
        )
        tokens = torch.tensor(
            [_encode_prompt(caption)],
            dtype=torch.long,
            device=device,
        )
        optimizer.zero_grad(set_to_none=True)
        pred = model(current, phase, tokens)
        loss = F.l1_loss(pred, target)
        loss.backward()
        torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
        optimizer.step()
        losses.append(float(loss.detach().cpu()))
        if step == 1 or step % 100 == 0:
            print(
                f"[MIRAI-VIDEO] step={step}/{steps} "
                f"loss={losses[-1]:.4f} device={device}"
            )

    paths = video_paths()
    torch.save(model.state_dict(), paths["weights"])
    meta = {
        "name": "Mirai Native Video v0",
        "version": "0.1.0",
        "ready": True,
        "production_approved": False,
        "artifacts": ["model.pt"],
        "architecture": asdict(config),
        "training": {
            "clips": len(clips),
            "steps": int(steps),
            "final_loss": round(losses[-1], 6),
            "device": device,
            "trained_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        },
        "pretrained_dependency": False,
    }
    write_json(paths["meta"], meta)
    return meta


def generate_frames(
    start_image: Image.Image,
    prompt: str,
) -> list[Image.Image]:
    import torch

    paths = video_paths()
    meta = read_json(paths["meta"])
    if not paths["weights"].is_file() or not meta.get("ready"):
        raise RuntimeError("Mirai Native Video v0の学習済み重みがありません。")
    raw = meta.get("architecture") or {}
    config = VideoConfig(
        **{
            key: raw[key]
            for key in asdict(VideoConfig()).keys()
            if key in raw
        }
    )
    device = "cuda" if torch.cuda.is_available() else "cpu"
    model = build_model(config).to(device)
    model.load_state_dict(
        torch.load(paths["weights"], map_location=device, weights_only=True)
    )
    model.eval()
    im = start_image.convert("RGB").resize((config.size, config.size))
    data = torch.tensor(
        list(im.getdata()),
        dtype=torch.float32,
        device=device,
    ).view(config.size, config.size, 3).permute(2, 0, 1)
    current = (data / 127.5 - 1.0).unsqueeze(0)
    tokens = torch.tensor(
        [_encode_prompt(prompt)],
        dtype=torch.long,
        device=device,
    )
    frames: list[Image.Image] = []
    with torch.no_grad():
        for index in range(config.frames):
            pixels = (
                ((current[0].clamp(-1, 1) + 1.0) * 127.5)
                .byte()
                .permute(1, 2, 0)
                .cpu()
                .numpy()
            )
            frames.append(Image.fromarray(pixels, mode="RGB"))
            phase = torch.tensor(
                [index / max(config.frames - 1, 1)],
                device=device,
            )
            current = model(current, phase, tokens)
    return frames
