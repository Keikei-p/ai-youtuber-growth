from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


class WebappScriptSyntaxTests(unittest.TestCase):
    def test_embedded_dashboard_javascript_parses(self) -> None:
        node = shutil.which("node")
        if not node:
            self.skipTest("node is unavailable")

        source = Path("webapp.py").read_text(
            encoding="utf-8",
        )
        start = source.index("<script>") + len("<script>")
        end = source.index("</script>", start)
        script = source[start:end]

        with tempfile.TemporaryDirectory() as tmp:
            js = Path(tmp) / "dashboard.js"
            js.write_text(script, encoding="utf-8")
            completed = subprocess.run(
                [node, "--check", str(js)],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
                timeout=20,
            )

        self.assertEqual(
            completed.returncode,
            0,
            msg=(completed.stdout + completed.stderr),
        )


if __name__ == "__main__":
    unittest.main()
