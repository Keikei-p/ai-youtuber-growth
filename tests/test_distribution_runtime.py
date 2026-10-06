from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import config
import product_core


class DistributionRuntimeTests(unittest.TestCase):
    def test_packaged_windows_user_data_is_separate(self) -> None:
        with (
            patch.object(config, "is_packaged_runtime", return_value=True),
            patch.dict(
                os.environ,
                {"LOCALAPPDATA": r"C:\Users\tester\AppData\Local"},
                clear=False,
            ),
        ):
            root = config.default_user_data_root()
            data = config._runtime_default("data")
            output = config._runtime_default("output")
            token = config._runtime_default("token.json")

        root_text = str(root).replace("\\", "/")
        self.assertIn(
            "C:/Users/tester/AppData/Local",
            root_text,
        )
        self.assertTrue(
            root_text.endswith(
                "YOROKOBI/MiraiProductionOS"
            )
        )
        self.assertIn("YOROKOBI", data)
        self.assertIn("MiraiProductionOS", output)
        self.assertTrue(token.endswith("token.json"))

    def test_development_runtime_keeps_existing_relative_paths(self) -> None:
        with (
            patch.object(config, "is_packaged_runtime", return_value=False),
            patch.dict(
                os.environ,
                {"MIRAI_USER_DATA_ROOT": ""},
                clear=False,
            ),
        ):
            self.assertEqual(
                config.default_user_data_root(),
                Path("."),
            )
            self.assertEqual(
                config._runtime_default("data"),
                "data",
            )

    def test_windows_release_files_exist(self) -> None:
        for path in (
            "packaging/build_windows.ps1",
            "packaging/build_release.ps1",
            "packaging/MiraiProductionOS.nsi",
            ".github/workflows/release-windows.yml",
        ):
            self.assertTrue(Path(path).is_file(), path)

    def test_installer_preserves_user_data_on_uninstall(self) -> None:
        script = Path(
            "packaging/MiraiProductionOS.nsi"
        ).read_text(encoding="utf-8")
        self.assertIn(
            r"%LOCALAPPDATA%\YOROKOBI\MiraiProductionOS",
            script,
        )
        self.assertNotIn(
            'RMDir /r "$LOCALAPPDATA',
            script,
        )
        self.assertIn(
            'RMDir /r "$INSTDIR"',
            script,
        )

    def test_windows_builder_invokes_pyinstaller_arguments(self) -> None:
        source = Path(
            "packaging/build_windows.ps1"
        ).read_text(encoding="utf-8")
        self.assertIn(
            "& $Python @PyInstallerArgs",
            source,
        )
        self.assertNotIn(
            "& $Python @Args",
            source,
        )

    def test_release_build_outputs_hash_manifest(self) -> None:
        source = Path(
            "packaging/build_release.ps1"
        ).read_text(encoding="utf-8")
        self.assertIn(
            "Get-FileHash -Algorithm SHA256",
            source,
        )
        self.assertIn(
            "release-manifest.json",
            source,
        )
        self.assertIn(
            "Portable.zip",
            source,
        )
        self.assertIn(
            "Setup.exe",
            source,
        )

    def test_product_version_advanced(self) -> None:
        self.assertEqual(
            product_core.PRODUCT_VERSION,
            "0.12.0-beta",
        )


if __name__ == "__main__":
    unittest.main()
