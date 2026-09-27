from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent
os.chdir(ROOT)

from webapp import run


if __name__ == "__main__":
    # Windowsログオン時の完全自動運転用。
    # ブラウザを勝手に開かず、バックグラウンドだけ常駐させる。
    run(open_browser=False)
