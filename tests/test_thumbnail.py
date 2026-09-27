from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from video import thumbnail


class ThumbnailTests(unittest.TestCase):
    def test_three_candidates_are_generated_and_best_selected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bg = root / "bg.jpg"
            char = root / "char.png"
            Image.new("RGB", (720, 1280), (70, 90, 130)).save(bg)
            Image.new("RGBA", (400, 700), (220, 180, 160, 255)).save(char)

            item = {
                "id": 7,
                "title": "AIが自分で成長した結果",
                "idea": {
                    "hook": "昨日より少し賢くなりました",
                },
                "background_image_paths": [str(bg)],
                "character_image_path": str(char),
            }

            with (
                patch.object(
                    thumbnail,
                    "GENERATED_ROOT",
                    root / "generated",
                ),
                patch.object(
                    thumbnail,
                    "record_asset",
                ),
            ):
                result = thumbnail.build_thumbnail_candidates(item)

            self.assertEqual(len(result["candidates"]), 3)
            self.assertTrue(Path(result["path"]).is_file())
            self.assertEqual(
                result["score"],
                max(row["score"] for row in result["candidates"]),
            )


if __name__ == "__main__":
    unittest.main()
