"""Tests for scripts/napcat_config_helper.py — NapCat config generation and injection."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import napcat_config_helper as helper  # noqa: E402


class NapCatConfigGenerationTests(unittest.TestCase):
    """Test onebot11 config generation."""

    def test_generate_default_config_structure(self):
        config = helper.generate_onebot11_config(port="8081", token="test_token_123")
        self.assertIn("wsClients", config)
        self.assertIsInstance(config["wsClients"], list)
        self.assertEqual(len(config["wsClients"]), 1)
        ws = config["wsClients"][0]
        self.assertTrue(ws["enable"])
        self.assertEqual(ws["url"], "ws://127.0.0.1:8081/onebot/v11/ws")
        self.assertEqual(ws["token"], "test_token_123")
        self.assertEqual(ws["reconnectInterval"], 5000)

    def test_generate_custom_host_and_port(self):
        config = helper.generate_onebot11_config(port="9090", token="abc", host="192.168.1.100")
        ws = config["wsClients"][0]
        self.assertEqual(ws["url"], "ws://192.168.1.100:9090/onebot/v11/ws")
        self.assertEqual(ws["token"], "abc")

    def test_config_has_required_top_level_keys(self):
        config = helper.generate_onebot11_config(port="8081", token="t")
        for key in ("httpServers", "httpClients", "wsServers", "wsClients",
                     "enableLocalFile2Url", "debug", "heartInterval",
                     "messagePostFormat", "token", "GroupLocalTime"):
            self.assertIn(key, config, f"Missing key: {key}")

    def test_config_serializable_to_json(self):
        config = helper.generate_onebot11_config(port="8081", token="test")
        text = json.dumps(config, indent=2, ensure_ascii=False)
        self.assertIn('"ws://127.0.0.1:8081/onebot/v11/ws"', text)

    def test_message_post_format_is_array(self):
        """NapCat + nonebot-adapter-onebot requires array format."""
        config = helper.generate_onebot11_config(port="8081", token="t")
        self.assertEqual(config["messagePostFormat"], "array")

    def test_empty_token_generates_with_empty_string(self):
        """When .env is also empty/missing, token should be empty."""
        with patch.object(helper, "ENV_FILE", new=Path("/nonexistent_path_xyz")):
            config = helper.generate_onebot11_config(port="8081", token="")
            self.assertEqual(config["wsClients"][0]["token"], "")

    def test_heart_interval_reasonable(self):
        config = helper.generate_onebot11_config(port="8081", token="t")
        self.assertGreaterEqual(config["heartInterval"], 10000)
        self.assertLessEqual(config["heartInterval"], 60000)


class NapCatWsClientEntryTests(unittest.TestCase):
    """Test single wsClient entry generation."""

    def test_generate_ws_client_entry(self):
        entry = helper.generate_ws_client_entry(port="8081", token="my_token")
        self.assertTrue(entry["enable"])
        self.assertEqual(entry["url"], "ws://127.0.0.1:8081/onebot/v11/ws")
        self.assertEqual(entry["token"], "my_token")
        self.assertEqual(entry["reconnectInterval"], 5000)

    def test_generate_ws_client_entry_custom_host(self):
        entry = helper.generate_ws_client_entry(port="9999", token="t", host="10.0.0.1")
        self.assertEqual(entry["url"], "ws://10.0.0.1:9999/onebot/v11/ws")


class NapCatEnvReadTests(unittest.TestCase):
    """Test .env reading for config generation."""

    def test_read_env_from_file(self):
        """Test _read_env with a temporary env file."""
        content = "PORT=9999\nONEBOT_ACCESS_TOKEN=my_secret_token\n"
        with tempfile.NamedTemporaryFile(mode="w", suffix=".env", delete=False, encoding="utf-8") as f:
            f.write(content)
            tmp_path = Path(f.name)
        try:
            with patch.object(helper, "ENV_FILE", new=tmp_path):
                self.assertEqual(helper._read_env("PORT"), "9999")
                self.assertEqual(helper._read_env("ONEBOT_ACCESS_TOKEN"), "my_secret_token")
                self.assertEqual(helper._read_env("MISSING_KEY", "fallback"), "fallback")
        finally:
            tmp_path.unlink(missing_ok=True)

    def test_read_env_missing_file(self):
        with patch.object(helper, "ENV_FILE", new=Path("/nonexistent_env_file_xyz")):
            self.assertEqual(helper._read_env("PORT", "8081"), "8081")

    def test_read_env_skips_comments(self):
        content = "# PORT=9999\nPORT=8888\n"
        with tempfile.NamedTemporaryFile(mode="w", suffix=".env", delete=False, encoding="utf-8") as f:
            f.write(content)
            tmp_path = Path(f.name)
        try:
            with patch.object(helper, "ENV_FILE", new=tmp_path):
                self.assertEqual(helper._read_env("PORT"), "8888")
        finally:
            tmp_path.unlink(missing_ok=True)


class NapCatConfigPathDetectionTests(unittest.TestCase):
    """Test NapCat config directory detection."""

    def test_find_napcat_config_dir_returns_none_on_fresh_system(self):
        """On a system without NapCat, should return None."""
        with patch.object(helper, "NAPCAT_CONFIG_SEARCH_PATHS", new=[Path("/nonexistent_napcat_dir_xyz")]):
            with patch.object(helper, "_EXTRA_SEARCH_ROOTS", new=[]):
                result = helper.find_napcat_config_dir()
                self.assertIsNone(result)

    def test_find_existing_config_returns_none_without_dir(self):
        result = helper.find_existing_onebot11_config(Path("/nonexistent_napcat_dir_xyz"))
        self.assertIsNone(result)

    def test_find_all_configs_returns_empty_without_dir(self):
        result = helper.find_all_onebot11_configs(Path("/nonexistent_napcat_dir_xyz"))
        self.assertEqual(result, [])

    def test_find_all_configs_finds_multiple(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            d = Path(tmpdir)
            (d / "onebot11_12345.json").write_text("{}", encoding="utf-8")
            (d / "onebot11_67890.json").write_text("{}", encoding="utf-8")
            (d / "other.json").write_text("{}", encoding="utf-8")
            result = helper.find_all_onebot11_configs(d)
            self.assertEqual(len(result), 2)
            names = {r.name for r in result}
            self.assertIn("onebot11_12345.json", names)
            self.assertIn("onebot11_67890.json", names)


class NapCatConfigInjectionTests(unittest.TestCase):
    """Test injecting YuKiKo config into existing NapCat configs."""

    def _make_napcat_config(self, ws_clients: list[dict] | None = None) -> dict:
        """Create a minimal NapCat onebot11 config."""
        return {
            "httpServers": [],
            "httpClients": [],
            "wsServers": [],
            "wsClients": ws_clients or [],
            "token": "",
            "messagePostFormat": "array",
        }

    def test_inject_adds_new_entry_to_empty_wsclients(self):
        """Should add a new wsClient entry when list is empty."""
        config = self._make_napcat_config()
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8") as f:
            json.dump(config, f)
            config_path = Path(f.name)
        try:
            result = helper.inject_into_existing_config(
                config_path, port="8081", token="test_token"
            )
            self.assertTrue(result)
            updated = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertEqual(len(updated["wsClients"]), 1)
            ws = updated["wsClients"][0]
            self.assertTrue(ws["enable"])
            self.assertEqual(ws["url"], "ws://127.0.0.1:8081/onebot/v11/ws")
            self.assertEqual(ws["token"], "test_token")
        finally:
            config_path.unlink(missing_ok=True)

    def test_inject_updates_existing_entry(self):
        """Should update an existing entry matching the port."""
        existing_ws = {
            "enable": False,
            "url": "ws://127.0.0.1:8081/onebot/v11/ws",
            "token": "old_token",
            "reconnectInterval": 3000,
        }
        config = self._make_napcat_config(ws_clients=[existing_ws])
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8") as f:
            json.dump(config, f)
            config_path = Path(f.name)
        try:
            result = helper.inject_into_existing_config(
                config_path, port="8081", token="new_token"
            )
            self.assertTrue(result)
            updated = json.loads(config_path.read_text(encoding="utf-8"))
            # Should still have exactly 1 entry, not add a duplicate
            self.assertEqual(len(updated["wsClients"]), 1)
            ws = updated["wsClients"][0]
            self.assertTrue(ws["enable"])
            self.assertEqual(ws["token"], "new_token")
        finally:
            config_path.unlink(missing_ok=True)

    def test_inject_preserves_other_wsclients(self):
        """Should not touch other wsClient entries (e.g. Koishi, AstrBot)."""
        other_ws = {
            "enable": True,
            "url": "ws://127.0.0.1:5140/onebot",
            "token": "koishi_token",
        }
        config = self._make_napcat_config(ws_clients=[other_ws])
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8") as f:
            json.dump(config, f)
            config_path = Path(f.name)
        try:
            result = helper.inject_into_existing_config(
                config_path, port="8081", token="yukiko_token"
            )
            self.assertTrue(result)
            updated = json.loads(config_path.read_text(encoding="utf-8"))
            # Should have 2 entries: the original Koishi one + new YuKiKo one
            self.assertEqual(len(updated["wsClients"]), 2)
            urls = [ws["url"] for ws in updated["wsClients"]]
            self.assertIn("ws://127.0.0.1:5140/onebot", urls)
            self.assertIn("ws://127.0.0.1:8081/onebot/v11/ws", urls)
        finally:
            config_path.unlink(missing_ok=True)

    def test_inject_dry_run_does_not_modify_file(self):
        """dry_run=True should not write to the file."""
        config = self._make_napcat_config()
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8") as f:
            json.dump(config, f)
            config_path = Path(f.name)
        try:
            original_content = config_path.read_text(encoding="utf-8")
            result = helper.inject_into_existing_config(
                config_path, port="8081", token="test", dry_run=True
            )
            self.assertTrue(result)
            # File should be unchanged
            self.assertEqual(config_path.read_text(encoding="utf-8"), original_content)
        finally:
            config_path.unlink(missing_ok=True)

    def test_inject_creates_backup(self):
        """Should create a .bak.* backup file before modifying."""
        config = self._make_napcat_config()
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8") as f:
            json.dump(config, f)
            config_path = Path(f.name)
        try:
            helper.inject_into_existing_config(
                config_path, port="8081", token="test"
            )
            backups = list(config_path.parent.glob(f"{config_path.stem}.bak.*"))
            self.assertGreater(len(backups), 0, "No backup file created")
        finally:
            config_path.unlink(missing_ok=True)
            for bak in config_path.parent.glob(f"{config_path.stem}.bak.*"):
                bak.unlink(missing_ok=True)

    def test_inject_no_change_when_already_up_to_date(self):
        """Should return False when config is already up to date."""
        ws_entry = {
            "enable": True,
            "url": "ws://127.0.0.1:8081/onebot/v11/ws",
            "token": "same_token",
            "reconnectInterval": 5000,
        }
        config = self._make_napcat_config(ws_clients=[ws_entry])
        config["token"] = "same_token"
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8") as f:
            json.dump(config, f)
            config_path = Path(f.name)
        try:
            result = helper.inject_into_existing_config(
                config_path, port="8081", token="same_token"
            )
            self.assertFalse(result)
        finally:
            config_path.unlink(missing_ok=True)

    def test_inject_updates_top_level_token(self):
        """Should update the top-level token field too."""
        config = self._make_napcat_config()
        config["token"] = "old_top_token"
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8") as f:
            json.dump(config, f)
            config_path = Path(f.name)
        try:
            helper.inject_into_existing_config(
                config_path, port="8081", token="new_token"
            )
            updated = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertEqual(updated["token"], "new_token")
        finally:
            config_path.unlink(missing_ok=True)


class NapCatInjectAutoTests(unittest.TestCase):
    """Test inject_auto full flow."""

    def test_inject_auto_returns_1_when_no_config_dir(self):
        with patch.object(helper, "find_napcat_config_dir", return_value=None):
            result = helper.inject_auto(port="8081", token="t")
            self.assertEqual(result, 1)

    def test_inject_auto_creates_default_when_no_configs(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir)
            with patch.object(helper, "find_napcat_config_dir", return_value=config_dir):
                result = helper.inject_auto(port="8081", token="test_token")
                self.assertEqual(result, 0)
                generic = config_dir / "onebot11.json"
                self.assertTrue(generic.exists())
                config = json.loads(generic.read_text(encoding="utf-8"))
                self.assertEqual(len(config["wsClients"]), 1)
                self.assertEqual(config["wsClients"][0]["token"], "test_token")


if __name__ == "__main__":
    unittest.main()
