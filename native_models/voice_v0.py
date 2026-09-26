from __future__ import annotations

import io
import math
import wave
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from native_models.brain import ByteTokenizer
from native_models.common import read_json
from voice.model_manager import (
    MODEL_META_FILE,
    MODEL_ROOT,
    inspect_training_dataset,
)


@dataclass
class VoiceConfig:
    sample_rate: int = 22050
    n_fft: int = 1024
    hop_length: int = 256
    hidden: int = 192
    layers: int = 2
    vocab_size: int = 258
    frames_per_byte: int = 6

    def validate(self) -> None:
        if self.sample_rate < 16000:
            raise ValueError("sample rate too low")
        if self.n_fft < 256 or self.n_fft % 2:
            raise ValueError("n_fft must be even and >=256")
        if self.hop_length <= 0:
            raise ValueError("hop_length must be positive")


def weights_path() -> Path:
    MODEL_ROOT.mkdir(parents=True, exist_ok=True)
    return MODEL_ROOT / "model.pt"


def _encode_text(text: str) -> list[int]:
    return ByteTokenizer().encode(text, eos=True)[:256] or [257]


def build_model(config: VoiceConfig):
    config.validate()
    import torch
    import torch.nn as nn
    import torch.nn.functional as F

    bins = config.n_fft // 2 + 1

    class Model(nn.Module):
        def __init__(self) -> None:
            super().__init__()
            self.embedding = nn.Embedding(
                config.vocab_size,
                config.hidden,
            )
            self.encoder = nn.GRU(
                input_size=config.hidden,
                hidden_size=config.hidden,
                num_layers=config.layers,
                batch_first=True,
                bidirectional=True,
                dropout=0.1 if config.layers > 1 else 0.0,
            )
            self.proj = nn.Sequential(
                nn.Linear(config.hidden * 2, config.hidden * 2),
                nn.SiLU(),
                nn.Linear(config.hidden * 2, bins),
            )

        def forward(self, tokens, target_frames: int):
            x = self.embedding(tokens)
            x, _ = self.encoder(x)
            x = x.transpose(1, 2)
            x = F.interpolate(
                x,
                size=max(4, int(target_frames)),
                mode="linear",
                align_corners=False,
            ).transpose(1, 2)
            return self.proj(x).transpose(1, 2)

    return Model()


def _read_pcm(path: Path):
    import torch
    import torch.nn.functional as F

    with wave.open(str(path), "rb") as handle:
        channels = int(handle.getnchannels())
        sample_width = int(handle.getsampwidth())
        sample_rate = int(handle.getframerate())
        frames = handle.readframes(handle.getnframes())

    if sample_width != 2:
        raise RuntimeError(
            "Native Voice v0は16-bit PCM WAVを学習対象にします。"
        )

    import array
    pcm = array.array("h")
    pcm.frombytes(frames)
    data = torch.tensor(
        list(pcm),
        dtype=torch.float32,
    )
    if channels > 1:
        data = data.view(-1, channels).mean(dim=1)
    data = data / 32768.0

    if sample_rate != 22050:
        target = max(
            1,
            int(round(data.numel() * 22050 / sample_rate)),
        )
        data = F.interpolate(
            data.view(1, 1, -1),
            size=target,
            mode="linear",
            align_corners=False,
        ).view(-1)
    return data


def _magnitude(waveform, config: VoiceConfig):
    import torch
    window = torch.hann_window(
        config.n_fft,
        device=waveform.device,
    )
    spec = torch.stft(
        waveform,
        n_fft=config.n_fft,
        hop_length=config.hop_length,
        win_length=config.n_fft,
        window=window,
        return_complex=True,
    )
    return torch.log1p(spec.abs())


def train(
    *,
    epochs: int = 8,
    learning_rate: float = 2e-4,
    hidden: int = 192,
) -> dict:
    import torch
    import torch.nn.functional as F

    dataset = inspect_training_dataset()
    samples = dataset.get("samples") or []
    if len(samples) < 10:
        raise RuntimeError(
            "Mirai Native Voice v0は最低10本の自分で権利を持つWAVが必要です。"
        )

    config = VoiceConfig(hidden=hidden)
    config.validate()
    device = "cuda" if torch.cuda.is_available() else "cpu"
    model = build_model(config).to(device)
    model.train()
    optimizer = torch.optim.AdamW(model.parameters(), lr=learning_rate)
    losses: list[float] = []

    for epoch in range(1, max(1, int(epochs)) + 1):
        for sample in samples:
            waveform = _read_pcm(Path(sample["audio"])).to(device)
            target = _magnitude(waveform, config)
            tokens = torch.tensor(
                [_encode_text(sample["text"])],
                dtype=torch.long,
                device=device,
            )
            optimizer.zero_grad(set_to_none=True)
            pred = model(tokens, int(target.shape[-1]))[0]
            loss = F.l1_loss(pred, target)
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            optimizer.step()
            losses.append(float(loss.detach().cpu()))
        print(
            f"[MIRAI-VOICE] epoch={epoch}/{epochs} "
            f"loss={sum(losses[-len(samples):]) / len(samples):.4f} "
            f"device={device}"
        )

    MODEL_ROOT.mkdir(parents=True, exist_ok=True)
    torch.save(model.state_dict(), weights_path())
    meta = {
        "name": "Mirai Native Voice v0",
        "version": "0.1.0",
        "ready": True,
        "runtime_implemented": True,
        "artifacts": ["model.pt"],
        "architecture": asdict(config),
        "training": {
            "samples": len(samples),
            "minutes": float(dataset.get("total_minutes") or 0.0),
            "epochs": int(epochs),
            "final_loss": round(losses[-1], 6),
            "device": device,
            "trained_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        },
        "pretrained_dependency": False,
    }
    MODEL_META_FILE.write_text(
        __import__("json").dumps(meta, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return meta


def _griffin_lim(magnitude, config: VoiceConfig, iterations: int = 24):
    import torch

    window = torch.hann_window(
        config.n_fft,
        device=magnitude.device,
    )
    phase = torch.rand_like(magnitude) * (2 * math.pi)
    complex_spec = torch.polar(magnitude, phase)
    waveform = None
    for _ in range(max(1, int(iterations))):
        waveform = torch.istft(
            complex_spec,
            n_fft=config.n_fft,
            hop_length=config.hop_length,
            win_length=config.n_fft,
            window=window,
        )
        rebuilt = torch.stft(
            waveform,
            n_fft=config.n_fft,
            hop_length=config.hop_length,
            win_length=config.n_fft,
            window=window,
            return_complex=True,
        )
        phase = torch.angle(rebuilt)
        complex_spec = torch.polar(magnitude, phase)
    assert waveform is not None
    return waveform


def synthesize_wav_bytes(
    text: str,
    voice_params: dict | None = None,
) -> bytes:
    import torch

    cleaned = str(text or "").strip()
    if not cleaned:
        raise ValueError("音声化する文章がありません。")
    meta = read_json(MODEL_META_FILE)
    if not weights_path().is_file() or not meta.get("ready"):
        raise RuntimeError("Mirai Native Voice v0の学習済み重みがありません。")

    raw = meta.get("architecture") or {}
    config = VoiceConfig(
        **{
            key: raw[key]
            for key in asdict(VoiceConfig()).keys()
            if key in raw
        }
    )
    device = "cuda" if torch.cuda.is_available() else "cpu"
    model = build_model(config).to(device)
    model.load_state_dict(
        torch.load(
            weights_path(),
            map_location=device,
            weights_only=True,
        )
    )
    model.eval()

    tokens_raw = _encode_text(cleaned)
    tokens = torch.tensor(
        [tokens_raw],
        dtype=torch.long,
        device=device,
    )
    frames = max(
        24,
        min(1800, len(tokens_raw) * config.frames_per_byte),
    )
    with torch.no_grad():
        log_mag = model(tokens, frames)[0]
        magnitude = torch.expm1(log_mag).clamp(min=0.0)
        waveform = _griffin_lim(
            magnitude,
            config,
            iterations=24,
        ).clamp(-1.0, 1.0)

    params = voice_params or {}
    volume = max(0.2, min(float(params.get("volume", 1.0)), 2.0))
    waveform = (waveform * volume).clamp(-1.0, 1.0)
    pcm = (waveform.cpu() * 32767.0).short().numpy().tobytes()

    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(config.sample_rate)
        handle.writeframes(pcm)
    return buffer.getvalue()
