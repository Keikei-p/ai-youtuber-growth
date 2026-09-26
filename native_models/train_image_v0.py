from __future__ import annotations

import argparse

from native_models.image_v0 import train


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Train Mirai Native Image v0 from scratch"
    )
    parser.add_argument("--steps", type=int, default=2000)
    parser.add_argument("--lr", type=float, default=2e-4)
    parser.add_argument("--size", type=int, default=64)
    parser.add_argument("--channels", type=int, default=64)
    args = parser.parse_args()
    train(
        steps=args.steps,
        learning_rate=args.lr,
        size=args.size,
        channels=args.channels,
    )


if __name__ == "__main__":
    main()
