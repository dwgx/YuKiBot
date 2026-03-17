from __future__ import annotations

import unittest
from pathlib import Path


class InstallUninstallAccelerationRegressionTests(unittest.TestCase):
    def test_deploy_helper_supports_acceleration_and_forced_sync(self) -> None:
        text = Path("scripts/deploy.py").read_text(encoding="utf-8")
        self.assertIn("--ensure-requirements", text)
        self.assertIn("YUKIKO_PIP_INDEX_URL", text)
        self.assertIn("YUKIKO_PIP_CACHE_DIR", text)
        self.assertIn("YUKIKO_USE_UV", text)
        self.assertIn('print("[deploy] syncing requirements with uv...")', text)
        self.assertIn("--upgrade-strategy", text)

    def test_webui_build_scripts_use_prefer_offline_and_cache_envs(self) -> None:
        sh_text = Path("build-webui.sh").read_text(encoding="utf-8")
        bat_text = Path("build-webui.bat").read_text(encoding="utf-8")
        self.assertIn("YUKIKO_NPM_REGISTRY", sh_text)
        self.assertIn("YUKIKO_NPM_CACHE_DIR", sh_text)
        self.assertIn("--prefer-offline", sh_text)
        self.assertIn("YUKIKO_WEBUI_FORCE_INSTALL", sh_text)
        self.assertIn("YUKIKO_NPM_REGISTRY", bat_text)
        self.assertIn("YUKIKO_NPM_CACHE_DIR", bat_text)
        self.assertIn("--prefer-offline", bat_text)
        self.assertIn("YUKIKO_WEBUI_FORCE_INSTALL", bat_text)

    def test_one_click_uninstall_entrypoints_exist(self) -> None:
        uninstall_sh = Path("uninstall.sh").read_text(encoding="utf-8")
        uninstall_bat = Path("uninstall.bat").read_text(encoding="utf-8")
        uninstall_unix = Path("scripts/uninstall_unix.sh").read_text(encoding="utf-8")
        uninstall_windows = Path("scripts/uninstall_windows.ps1").read_text(encoding="utf-8")
        self.assertIn("--purge-all", uninstall_sh)
        self.assertIn("scripts/yukiko_manager.sh", uninstall_sh)
        self.assertIn("scripts/uninstall_unix.sh", uninstall_sh)
        self.assertIn("--purge-all", uninstall_bat)
        self.assertIn("--no-backup", uninstall_unix)
        self.assertIn("storage/sandbox", uninstall_unix)
        self.assertIn("--purge-all", uninstall_windows)
        self.assertIn("Compress-Archive", uninstall_windows)
        self.assertIn("Runtime data directories removed.", uninstall_windows)


if __name__ == "__main__":
    unittest.main()
