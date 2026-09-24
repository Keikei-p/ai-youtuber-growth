from __future__ import annotations
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent
os.chdir(ROOT)

from webapp import run

if __name__ == "__main__":
    run(open_browser=True)
