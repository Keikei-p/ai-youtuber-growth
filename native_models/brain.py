from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from native_models.common import (
    component_model_root,
    component_training_root,
    model_package_status,
    read_json,
)


BOS = 256
EOS = 257
VOCAB_SIZE = 258


class ByteTokenizer:
    """外部tokenizerを使わないUTF-8 byte tokenizer."""

    vocab_size = VOCAB_SIZE
    bos_id = BOS
    eos_id = EOS

    def encode(
        self,
        text: str,
        *,
        bos: bool = False,
        eos: bool = False,
    ) -> list[int]:
        tokens = list(str(text or "").encode("utf-8"))
        if bos:
            tokens.insert(0, BOS)
        if eos:
            tokens.append(EOS)
        return tokens

    def decode(self, tokens: list[int]) -> str:
        raw = bytes(
            int(token)
            for token in tokens
            if 0 <= int(token) <= 255
        )
        return raw.decode("utf-8", errors="ignore")


@dataclass
class BrainConfig:
    context: int = 256
    dim: int = 256
    layers: int = 4
    heads: int = 4
    dropout: float = 0.1
    vocab_size: int = VOCAB_SIZE

    def validate(self) -> None:
        if self.dim % self.heads != 0:
            raise ValueError("dim must be divisible by heads")
        if self.context < 32:
            raise ValueError("context is too small")
        if self.vocab_size != VOCAB_SIZE:
            raise ValueError("native byte vocabulary must be 258")


def build_model(config: BrainConfig):
    """PyTorch上に自前実装したdecoder-only Transformerを構築する."""
    config.validate()
    import torch
    import torch.nn as nn
    import torch.nn.functional as F

    class CausalAttention(nn.Module):
        def __init__(self) -> None:
            super().__init__()
            self.heads = config.heads
            self.head_dim = config.dim // config.heads
            self.qkv = nn.Linear(config.dim, config.dim * 3)
            self.proj = nn.Linear(config.dim, config.dim)
            self.dropout = nn.Dropout(config.dropout)
            mask = torch.tril(
                torch.ones(config.context, config.context)
            ).view(1, 1, config.context, config.context)
            self.register_buffer("mask", mask, persistent=False)

        def forward(self, x):
            batch, steps, channels = x.shape
            qkv = self.qkv(x)
            q, k, v = qkv.chunk(3, dim=-1)
            q = q.view(
                batch, steps, self.heads, self.head_dim
            ).transpose(1, 2)
            k = k.view(
                batch, steps, self.heads, self.head_dim
            ).transpose(1, 2)
            v = v.view(
                batch, steps, self.heads, self.head_dim
            ).transpose(1, 2)
            scores = (q @ k.transpose(-2, -1)) / math.sqrt(
                self.head_dim
            )
            scores = scores.masked_fill(
                self.mask[:, :, :steps, :steps] == 0,
                float("-inf"),
            )
            weights = F.softmax(scores, dim=-1)
            weights = self.dropout(weights)
            out = weights @ v
            out = out.transpose(1, 2).contiguous().view(
                batch, steps, channels
            )
            return self.proj(out)

    class Block(nn.Module):
        def __init__(self) -> None:
            super().__init__()
            self.ln1 = nn.LayerNorm(config.dim)
            self.attn = CausalAttention()
            self.ln2 = nn.LayerNorm(config.dim)
            self.ff = nn.Sequential(
                nn.Linear(config.dim, config.dim * 4),
                nn.GELU(),
                nn.Linear(config.dim * 4, config.dim),
                nn.Dropout(config.dropout),
            )

        def forward(self, x):
            x = x + self.attn(self.ln1(x))
            x = x + self.ff(self.ln2(x))
            return x

    class MiraiBrain(nn.Module):
        def __init__(self) -> None:
            super().__init__()
            self.token = nn.Embedding(
                config.vocab_size,
                config.dim,
            )
            self.position = nn.Embedding(
                config.context,
                config.dim,
            )
            self.blocks = nn.ModuleList(
                [Block() for _ in range(config.layers)]
            )
            self.final_norm = nn.LayerNorm(config.dim)
            self.lm_head = nn.Linear(
                config.dim,
                config.vocab_size,
                bias=False,
            )
            self.lm_head.weight = self.token.weight

        def forward(self, idx, targets=None):
            batch, steps = idx.shape
            if steps > config.context:
                raise ValueError("input exceeds model context")
            positions = torch.arange(
                steps,
                device=idx.device,
            )
            x = self.token(idx) + self.position(positions)[None]
            for block in self.blocks:
                x = block(x)
            logits = self.lm_head(self.final_norm(x))
            loss = None
            if targets is not None:
                loss = F.cross_entropy(
                    logits.reshape(-1, config.vocab_size),
                    targets.reshape(-1),
                )
            return logits, loss

        @torch.no_grad()
        def generate(
            self,
            idx,
            max_new_tokens: int = 256,
            temperature: float = 0.85,
            top_k: int = 40,
        ):
            for _ in range(max(1, int(max_new_tokens))):
                crop = idx[:, -config.context :]
                logits, _ = self(crop)
                logits = logits[:, -1, :] / max(
                    float(temperature),
                    0.05,
                )
                if top_k > 0:
                    values, _ = torch.topk(
                        logits,
                        min(int(top_k), logits.size(-1)),
                    )
                    logits[logits < values[:, [-1]]] = float("-inf")
                probs = F.softmax(logits, dim=-1)
                next_token = torch.multinomial(
                    probs,
                    num_samples=1,
                )
                idx = torch.cat((idx, next_token), dim=1)
                if int(next_token[0, 0]) == EOS:
                    break
            return idx

    return MiraiBrain()


def brain_paths() -> dict[str, Path]:
    root = component_model_root("brain-v0")
    training = component_training_root("brain")
    return {
        "root": root,
        "weights": root / "model.pt",
        "meta": root / "model.json",
        "training": training,
    }


def corpus_status() -> dict[str, Any]:
    root = brain_paths()["training"]
    files = sorted(root.rglob("*.txt"))
    total_bytes = 0
    total_chars = 0
    for path in files:
        try:
            text = path.read_text(encoding="utf-8")
        except Exception:
            continue
        total_chars += len(text)
        total_bytes += len(text.encode("utf-8"))
    return {
        "ready": total_bytes >= 4096,
        "files": len(files),
        "characters": total_chars,
        "bytes": total_bytes,
        "training_root": str(root),
        "detail": (
            "学習開始可能"
            if total_bytes >= 4096
            else "UTF-8 .txt 学習文書が不足しています"
        ),
    }


def brain_status() -> dict[str, Any]:
    package = model_package_status("brain-v0")
    corpus = corpus_status()
    torch_available = False
    cuda_available = False
    try:
        import torch
        torch_available = True
        cuda_available = bool(torch.cuda.is_available())
    except Exception:
        pass
    return {
        "code_ready": True,
        "pretrained_dependency": False,
        "torch_available": torch_available,
        "cuda_available": cuda_available,
        "dataset": corpus,
        "model": package,
        "ready": bool(package.get("ready")) and torch_available,
        "production_ready": (
            bool(package.get("ready"))
            and bool(package.get("production_approved"))
            and torch_available
        ),
    }


class MiraiNativeBrainClient:
    def __init__(self) -> None:
        self.tokenizer = ByteTokenizer()
        self._model = None
        self._config: BrainConfig | None = None
        self._device = "cpu"

    def available(self) -> bool:
        return bool(brain_status()["ready"])

    def _load(self) -> None:
        if self._model is not None:
            return
        import torch

        paths = brain_paths()
        meta = read_json(paths["meta"])
        raw_config = meta.get("architecture") or {}
        config = BrainConfig(
            **{
                key: raw_config[key]
                for key in asdict(BrainConfig()).keys()
                if key in raw_config
            }
        )
        config.validate()
        model = build_model(config)
        state = torch.load(
            paths["weights"],
            map_location="cpu",
            weights_only=True,
        )
        model.load_state_dict(state)
        device = (
            "cuda"
            if torch.cuda.is_available()
            else "cpu"
        )
        model.to(device)
        model.eval()
        self._model = model
        self._config = config
        self._device = device

    def generate(
        self,
        prompt: str,
        *,
        max_new_tokens: int = 320,
        temperature: float = 0.85,
    ) -> str:
        self._load()
        import torch

        assert self._model is not None
        assert self._config is not None
        prefix = self.tokenizer.encode(
            str(prompt or ""),
            bos=True,
        )
        prefix = prefix[-self._config.context :]
        idx = torch.tensor(
            [prefix],
            dtype=torch.long,
            device=self._device,
        )
        result = self._model.generate(
            idx,
            max_new_tokens=max_new_tokens,
            temperature=temperature,
            top_k=40,
        )
        generated = result[0, len(prefix) :].tolist()
        return self.tokenizer.decode(generated).strip()

    def generate_json(self, prompt: str):
        raw = self.generate(prompt)
        match = re.search(r"(\[.*\]|\{.*\})", raw, re.S)
        if not match:
            raise ValueError(
                "Mirai Native Brain response did not contain JSON"
            )
        return json.loads(match.group(1))
