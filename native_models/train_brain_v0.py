from __future__ import annotations

import argparse
import hashlib
import json
import random
from dataclasses import asdict
from datetime import datetime, timezone

from native_models.brain import (
    BrainConfig,
    ByteTokenizer,
    brain_paths,
    build_model,
)
from native_models.common import write_json


def _load_corpus() -> tuple[list[int], dict]:
    paths = brain_paths()
    tokenizer = ByteTokenizer()
    chunks: list[list[int]] = []
    digest = hashlib.sha256()
    files = sorted(paths["training"].rglob("*.txt"))
    for path in files:
        text = path.read_text(encoding="utf-8").strip()
        if not text:
            continue
        raw = text.encode("utf-8")
        digest.update(raw)
        chunks.append(tokenizer.encode(text, eos=True))
    tokens = [token for chunk in chunks for token in chunk]
    return tokens, {
        "files": len(chunks),
        "tokens": len(tokens),
        "sha256": digest.hexdigest(),
    }


def train(
    *,
    steps: int = 1000,
    batch_size: int = 8,
    learning_rate: float = 3e-4,
    context: int = 256,
    dim: int = 256,
    layers: int = 4,
    heads: int = 4,
) -> dict:
    import torch

    tokens, dataset = _load_corpus()
    if len(tokens) < max(4096, context * 4):
        raise RuntimeError(
            "Mirai Native Brain学習文書が不足しています。"
            " data/native_training/brain/*.txt にUTF-8文書を追加してください。"
        )

    config = BrainConfig(
        context=context,
        dim=dim,
        layers=layers,
        heads=heads,
    )
    config.validate()
    model = build_model(config)
    device = "cuda" if torch.cuda.is_available() else "cpu"
    model.to(device)
    model.train()
    optimizer = torch.optim.AdamW(
        model.parameters(),
        lr=float(learning_rate),
        weight_decay=0.01,
    )
    stream = torch.tensor(tokens, dtype=torch.long)
    max_start = len(tokens) - config.context - 2

    random.seed(42)
    torch.manual_seed(42)
    losses: list[float] = []
    for step in range(1, max(1, int(steps)) + 1):
        starts = [
            random.randint(0, max_start)
            for _ in range(max(1, int(batch_size)))
        ]
        x = torch.stack(
            [stream[s : s + config.context] for s in starts]
        ).to(device)
        y = torch.stack(
            [stream[s + 1 : s + config.context + 1] for s in starts]
        ).to(device)

        optimizer.zero_grad(set_to_none=True)
        _, loss = model(x, y)
        loss.backward()
        torch.nn.utils.clip_grad_norm_(
            model.parameters(),
            max_norm=1.0,
        )
        optimizer.step()
        losses.append(float(loss.detach().cpu()))
        if step == 1 or step % 50 == 0:
            print(
                f"[MIRAI-BRAIN] step={step}/{steps} "
                f"loss={losses[-1]:.4f} device={device}"
            )

    paths = brain_paths()
    torch.save(model.state_dict(), paths["weights"])
    meta = {
        "name": "Mirai Native Brain v0",
        "version": "0.1.0",
        "ready": True,
        "production_approved": False,
        "artifacts": ["model.pt"],
        "architecture": asdict(config),
        "training": {
            **dataset,
            "steps": int(steps),
            "batch_size": int(batch_size),
            "learning_rate": float(learning_rate),
            "final_loss": round(losses[-1], 6),
            "device": device,
            "trained_at": datetime.now(
                timezone.utc
            ).isoformat(timespec="seconds"),
        },
        "pretrained_dependency": False,
        "tokenizer": "mirai-byte-v0",
    }
    write_json(paths["meta"], meta)
    print(
        "[MIRAI-BRAIN] 学習完了: "
        f"{paths['weights']} / loss={losses[-1]:.4f}"
    )
    return meta


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Train Mirai Native Brain v0 from scratch"
    )
    parser.add_argument("--steps", type=int, default=1000)
    parser.add_argument("--batch-size", type=int, default=8)
    parser.add_argument("--lr", type=float, default=3e-4)
    parser.add_argument("--context", type=int, default=256)
    parser.add_argument("--dim", type=int, default=256)
    parser.add_argument("--layers", type=int, default=4)
    parser.add_argument("--heads", type=int, default=4)
    args = parser.parse_args()
    train(
        steps=args.steps,
        batch_size=args.batch_size,
        learning_rate=args.lr,
        context=args.context,
        dim=args.dim,
        layers=args.layers,
        heads=args.heads,
    )


if __name__ == "__main__":
    main()
