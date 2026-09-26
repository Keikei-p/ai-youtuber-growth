from __future__ import annotations

import argparse

from native_models.lab import print_status


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Mirai Native Model Lab"
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="self-owned model migration status",
    )
    args = parser.parse_args()
    print_status()


if __name__ == "__main__":
    main()
