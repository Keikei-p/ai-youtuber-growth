from __future__ import annotations

import unittest

from voice import mirai_backend


class MiraiNativeBackendSafetyTests(unittest.TestCase):
    def test_default_backend_is_explicitly_not_ready(self) -> None:
        self.assertFalse(mirai_backend.available())
        info = mirai_backend.describe()
        self.assertFalse(info["ready"])
        with self.assertRaises(RuntimeError):
            mirai_backend.synthesize("test", {})


if __name__ == "__main__":
    unittest.main()
