from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
os.chdir(ROOT)

time.sleep(3)

pythonw = Path(sys.executable).with_name("pythonw.exe")
launcher = ROOT / "web_launcher.pyw"

subprocess.Popen(
    [str(pythonw), str(launcher)],
    cwd=str(ROOT),
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
    creationflags=(
        subprocess.CREATE_NO_WINDOW
        if os.name == "nt" else 0
    ),
)
