from __future__ import annotations

import unittest
from pathlib import Path


class LinuxScriptsRegressionTests(unittest.TestCase):
    def test_manager_script_contains_napcat_status_and_cleanup_flow(self) -> None:
        text = Path("scripts/yukiko_manager.sh").read_text(encoding="utf-8")
        self.assertIn("cmd_napcat_status()", text)
        self.assertIn("cmd_doctor()", text)
        self.assertIn("cmd_backup()", text)
        self.assertIn("cmd_restore()", text)
        self.assertIn("napcat-status [--method-only|--quiet]", text)
        self.assertIn("doctor [options]", text)
        self.assertIn("backup [options]", text)
        self.assertIn("restore --file FILE [options]", text)
        self.assertIn("--fast", text)
        self.assertIn("--no-auto-rollback", text)
        self.assertIn("rollback_update", text)
        self.assertIn("--keep-napcat", text)
        self.assertIn("--purge-data", text)
        self.assertIn("--backup-dir", text)
        self.assertIn("--no-backup", text)
        self.assertIn("uninstall_napcat()", text)
        self.assertIn("wait_webui_health()", text)
        self.assertIn('"$py_cmd" "$ROOT_DIR/scripts/deploy.py" --ensure-requirements', text)
        self.assertIn("needs_webui_deps", text)
        self.assertIn('bash "$ROOT_DIR/build-webui.sh"', text)
        self.assertIn("--strict", text)
        self.assertIn("warnings are treated as failures", text)
        self.assertIn("python venv ready", text)
        self.assertIn("service enabled at boot", text)

    def test_install_script_contains_onebot_access_token_and_extended_detection(self) -> None:
        text = Path("install.sh").read_text(encoding="utf-8")
        self.assertIn("--onebot-access-token", text)
        self.assertIn("--fast", text)
        self.assertIn("ONEBOT_ACCESS_TOKEN_INPUT", text)
        self.assertIn('upsert_env "ONEBOT_ACCESS_TOKEN"', text)
        self.assertIn("/opt/QQ/resources/app/napcat/napcat.mjs", text)
        self.assertIn("list-unit-files --type=service", text)
        self.assertIn("--skip-post-check", text)
        self.assertIn("--post-check-timeout", text)
        self.assertIn("--pip-index-url", text)
        self.assertIn("--npm-registry", text)
        self.assertIn("--use-uv", text)
        self.assertIn("apply_acceleration_env", text)
        self.assertIn('bash "$ROOT_DIR/build-webui.sh"', text)
        self.assertIn("run_post_deploy_checks()", text)
        self.assertIn("strict post-deploy checks", text)


if __name__ == "__main__":
    unittest.main()
