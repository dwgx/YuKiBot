from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import yaml

from core.config_manager import ConfigManager
from core.crypto import DecryptionError, SecretManager


class CryptoRegressionTests(unittest.TestCase):
    def test_secret_manager_invalid_encrypted_value_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            key_file = Path(tmp) / ".secret_key"
            manager = SecretManager(key_file)

            with self.assertRaises(DecryptionError):
                manager.decrypt("ENC(not-a-valid-fernet-token)")

    def test_config_manager_fails_loudly_when_secret_key_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_dir = root / "config"
            storage_dir = root / "storage"
            config_dir.mkdir(parents=True, exist_ok=True)
            storage_dir.mkdir(parents=True, exist_ok=True)

            original_manager = SecretManager(storage_dir / ".secret_key")
            encrypted = original_manager.encrypt("top-secret")

            with open(config_dir / "config.yml", "w", encoding="utf-8") as fh:
                yaml.safe_dump({"api": {"api_key": encrypted}}, fh, allow_unicode=True, sort_keys=False)

            # 模拟重部署后 secret key 被替换。
            (storage_dir / ".secret_key").unlink()
            SecretManager(storage_dir / ".secret_key")

            with self.assertRaisesRegex(RuntimeError, "无法解密"):
                ConfigManager(config_dir, storage_dir)


if __name__ == "__main__":
    unittest.main()
