from __future__ import annotations

import argparse

from native_models.voice_v0 import train


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Train Mirai Native Voice v0 from scratch"
    )
    parser.add_argument("--epochs", type=int, default=8)
    parser.add_argument("--lr", type=float, default=2e-4)
    parser.add_argument("--hidden", type=int, default=192)
    args = parser.parse_args()
    train(
        epochs=args.epochs,
        learning_rate=args.lr,
        hidden=args.hidden,
    )


if __name__ == "__main__":
    main()
