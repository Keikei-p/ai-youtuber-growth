from __future__ import annotations

import csv
import math
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
class ImageConfig:
    size: int = 64
    channels: int = 64
    text_dim: int = 64
    timesteps: int = 200
    vocab_size: int = 258

    def validate(self) -> None:
        if self.size not in {32, 64, 96, 128}:
            raise ValueError("native image size must be 32/64/96/128")
        if self.channels < 16:
            raise ValueError("channels too small")
        if self.timesteps < 20:
            raise ValueError("timesteps too small")


def image_paths() -> dict[str, Path]:
    root = component_model_root("image-v0")
    training = component_training_root("image")
    return {
        "root": root,
        "weights": root / "model.pt",
        "meta": root / "model.json",
        "training": training,
        "metadata": training / "metadata.csv",
    }


def dataset_status() -> dict[str, Any]:
    paths = image_paths()
    metadata = paths["metadata"]
    valid = 0
    errors: list[str] = []
    if metadata.is_file():
        with metadata.open(
            "r",
            encoding="utf-8-sig",
            newline="",
        ) as handle:
            for line_no, row in enumerate(
                csv.reader(handle, delimiter="|"),
                start=1,
            ):
                if len(row) < 2:
                    errors.append(f"{line_no}行目: path|caption 形式ではありません")
                    continue
                rel = str(row[0]).strip()
                caption = "|".join(row[1:]).strip()
                path = (paths["training"] / rel).resolve()
                try:
                    path.relative_to(paths["training"].resolve())
                except ValueError:
                    errors.append(f"{line_no}行目: 学習フォルダ外です")
                    continue
                if not path.is_file() or not caption:
                    errors.append(f"{line_no}行目: 画像またはcaptionがありません")
                    continue
                try:
                    with Image.open(path) as im:
                        im.verify()
                except Exception:
                    errors.append(f"{line_no}行目: 画像を読めません")
                    continue
                valid += 1
    return {
        "ready": valid >= 20,
        "valid_count": valid,
        "errors": errors[:50],
        "training_root": str(paths["training"]),
        "metadata_file": str(metadata),
        "detail": "学習開始可能" if valid >= 20 else "最低20枚の権利クリア画像が必要",
    }


def image_status() -> dict[str, Any]:
    torch_available = False
    cuda_available = False
    try:
        import torch
        torch_available = True
        cuda_available = bool(torch.cuda.is_available())
    except Exception:
        pass
    model = model_package_status("image-v0")
    return {
        "code_ready": True,
        "pretrained_dependency": False,
        "torch_available": torch_available,
        "cuda_available": cuda_available,
        "dataset": dataset_status(),
        "model": model,
        "ready": bool(model.get("ready")) and torch_available,
        "quality_stage": "research-64px",
    }


def build_model(config: ImageConfig):
    config.validate()
    import torch
    import torch.nn as nn
    import torch.nn.functional as F

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
                nn.GroupNorm(8, config.channels),
                nn.SiLU(),
                nn.Conv2d(config.channels, config.channels, 3, padding=1),
                nn.GroupNorm(8, config.channels),
                nn.SiLU(),
                nn.Conv2d(config.channels, config.channels, 3, padding=1),
                nn.GroupNorm(8, config.channels),
                nn.SiLU(),
            )
            self.out = nn.Conv2d(config.channels, 3, 3, padding=1)

        def forward(self, x, t, tokens):
            h = self.in_conv(x)
            mask = (tokens != 257).float().unsqueeze(-1)
            emb = self.text(tokens)
            pooled = (emb * mask).sum(1) / mask.sum(1).clamp(min=1.0)
            cond = self.text_proj(pooled)
            time_cond = self.time(t.float().view(-1, 1))
            h = h + (cond + time_cond)[:, :, None, None]
            h = h + self.body(h)
            return self.out(h)

    return Model()


def _schedule(config: ImageConfig, device):
    import torch
    betas = torch.linspace(1e-4, 0.02, config.timesteps, device=device)
    alphas = 1.0 - betas
    alpha_bar = torch.cumprod(alphas, dim=0)
    return betas, alphas, alpha_bar


def _encode_prompt(
    text: str,
    *,
    max_tokens: int = 128,
) -> list[int]:
    tokens = ByteTokenizer().encode(text, eos=True)[:max_tokens]
    return tokens or [257]


def _load_samples() -> list[tuple[Path, str]]:
    paths = image_paths()
    samples: list[tuple[Path, str]] = []
    if not paths["metadata"].is_file():
        return samples
    with paths["metadata"].open(
        "r",
        encoding="utf-8-sig",
        newline="",
    ) as handle:
        for row in csv.reader(handle, delimiter="|"):
            if len(row) < 2:
                continue
            candidate = (paths["training"] / row[0].strip()).resolve()
            caption = "|".join(row[1:]).strip()
            try:
                candidate.relative_to(paths["training"].resolve())
            except ValueError:
                continue
            if candidate.is_file() and caption:
                samples.append((candidate, caption))
    return samples


def train(
    *,
    steps: int = 2000,
    learning_rate: float = 2e-4,
    size: int = 64,
    channels: int = 64,
) -> dict:
    import torch
    import torch.nn.functional as F

    samples = _load_samples()
    if len(samples) < 20:
        raise RuntimeError("Native Image v0は最低20枚の学習画像が必要です。")

    config = ImageConfig(size=size, channels=channels)
    config.validate()
    device = "cuda" if torch.cuda.is_available() else "cpu"
    model = build_model(config).to(device)
    model.train()
    optimizer = torch.optim.AdamW(model.parameters(), lr=learning_rate)
    _, _, alpha_bar = _schedule(config, device)
    random.seed(42)
    losses: list[float] = []

    for step in range(1, max(1, int(steps)) + 1):
        path, caption = random.choice(samples)
        with Image.open(path) as raw:
            im = raw.convert("RGB").resize((config.size, config.size))
        data = torch.tensor(
            list(im.getdata()),
            dtype=torch.float32,
            device=device,
        ).view(config.size, config.size, 3).permute(2, 0, 1)
        x0 = (data / 127.5 - 1.0).unsqueeze(0)
        t_index = random.randrange(config.timesteps)
        t = torch.tensor(
            [t_index / max(config.timesteps - 1, 1)],
            device=device,
        )
        noise = torch.randn_like(x0)
        a = alpha_bar[t_index].sqrt()
        b = (1.0 - alpha_bar[t_index]).sqrt()
        noisy = a * x0 + b * noise
        tokens = _encode_prompt(caption)
        tok = torch.tensor([tokens], dtype=torch.long, device=device)

        optimizer.zero_grad(set_to_none=True)
        pred = model(noisy, t, tok)
        loss = F.mse_loss(pred, noise)
        loss.backward()
        torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
        optimizer.step()
        losses.append(float(loss.detach().cpu()))
        if step == 1 or step % 100 == 0:
            print(
                f"[MIRAI-IMAGE] step={step}/{steps} "
                f"loss={losses[-1]:.4f} device={device}"
            )

    paths = image_paths()
    torch.save(model.state_dict(), paths["weights"])
    meta = {
        "name": "Mirai Native Image v0",
        "version": "0.1.0",
        "ready": True,
        "production_approved": False,
        "artifacts": ["model.pt"],
        "architecture": asdict(config),
        "training": {
            "samples": len(samples),
            "steps": int(steps),
            "final_loss": round(losses[-1], 6),
            "device": device,
            "trained_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        },
        "pretrained_dependency": False,
    }
    write_json(paths["meta"], meta)
    return meta


def generate(
    prompt: str,
    *,
    seed: int = 42,
) -> Image.Image:
    import torch

    paths = image_paths()
    meta = read_json(paths["meta"])
    if not paths["weights"].is_file() or not meta.get("ready"):
        raise RuntimeError("Mirai Native Image v0の学習済み重みがありません。")
    cfg = meta.get("architecture") or {}
    config = ImageConfig(
        **{
            key: cfg[key]
            for key in asdict(ImageConfig()).keys()
            if key in cfg
        }
    )
    device = "cuda" if torch.cuda.is_available() else "cpu"
    model = build_model(config).to(device)
    model.load_state_dict(
        torch.load(paths["weights"], map_location=device, weights_only=True)
    )
    model.eval()
    betas, alphas, alpha_bar = _schedule(config, device)
    generator = torch.Generator(device=device).manual_seed(int(seed))
    x = torch.randn(
        1, 3, config.size, config.size,
        generator=generator,
        device=device,
    )
    tokens = torch.tensor(
        [_encode_prompt(prompt)],
        dtype=torch.long,
        device=device,
    )
    with torch.no_grad():
        for index in reversed(range(config.timesteps)):
            t = torch.tensor(
                [index / max(config.timesteps - 1, 1)],
                device=device,
            )
            eps = model(x, t, tokens)
            alpha = alphas[index]
            abar = alpha_bar[index]
            mean = (
                x - (1 - alpha) / (1 - abar).sqrt() * eps
            ) / alpha.sqrt()
            if index > 0:
                noise = torch.randn(
                    x.shape,
                    generator=generator,
                    device=device,
                )
                x = mean + betas[index].sqrt() * noise
            else:
                x = mean
    pixels = (
        ((x[0].clamp(-1, 1) + 1.0) * 127.5)
        .byte()
        .permute(1, 2, 0)
        .cpu()
        .numpy()
    )
    return Image.fromarray(pixels, mode="RGB")
