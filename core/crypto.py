"""本地密钥加密模块 — 用于保护 config.yml 中的敏感值（API key / cookie / token）。

加密值格式: ENC(base64_ciphertext)
密钥文件:   storage/.secret_key（自动生成，务必加入 .gitignore）
"""
from __future__ import annotations

import base64
import logging
import os
from pathlib import Path

_log = logging.getLogger("yukiko.crypto")

try:
    from cryptography.fernet import Fernet, InvalidToken
    _HAS_CRYPTO = True
except ImportError:
    _HAS_CRYPTO = False
    Fernet = None  # type: ignore[assignment,misc]
    InvalidToken = Exception  # type: ignore[assignment,misc]

_ENC_PREFIX = "ENC("
_ENC_SUFFIX = ")"


class SecretManager:
    """管理本地加密密钥，提供 encrypt / decrypt / 递归解密 config dict。"""

    def __init__(self, key_file: Path | None = None):
        self._key_file = key_file or Path("storage/.secret_key")
        self._fernet: Fernet | None = None  # type: ignore[assignment]
        if _HAS_CRYPTO:
            self._fernet = Fernet(self._load_or_create_key())

    # ── public ────────────────────────────────────────────────
    def encrypt(self, plaintext: str) -> str:
        """加密明文，返回 ENC(...) 格式字符串。"""
        if not self._fernet:
            raise RuntimeError("cryptography 库未安装，无法加密。pip install cryptography")
        token = self._fernet.encrypt(plaintext.encode("utf-8"))
        # Fernet token 本身已是 url-safe base64，直接存储即可
        return f"{_ENC_PREFIX}{token.decode('ascii')}{_ENC_SUFFIX}"

    def decrypt(self, value: str) -> str:
        """解密 ENC(...) 字符串；非加密值原样返回。兼容新旧两种格式。"""
        if not self.is_encrypted(value):
            return value
        if not self._fernet:
            _log.error("发现 ENC() 加密值但 cryptography 未安装，请 pip install cryptography")
            return ""
        inner = value[len(_ENC_PREFIX):-len(_ENC_SUFFIX)]
        # 新格式: inner 直接是 Fernet token (gAAAAA... 开头)
        # 旧格式: inner 是 base64(Fernet token)，需要先 decode 一层
        for candidate in (inner.encode("ascii"), base64.urlsafe_b64decode(inner.encode("ascii"))):
            try:
                return self._fernet.decrypt(candidate).decode("utf-8")
            except (InvalidToken, Exception):
                continue
        _log.error("ENC() 解密失败: 新旧格式均无法解密")
        return ""

    @staticmethod
    def is_encrypted(value: object) -> bool:
        return isinstance(value, str) and value.startswith(_ENC_PREFIX) and value.endswith(_ENC_SUFFIX)

    def decrypt_dict(self, data: object) -> object:
        """递归解密 dict/list 中所有 ENC() 值。"""
        if isinstance(data, dict):
            return {k: self.decrypt_dict(v) for k, v in data.items()}
        if isinstance(data, list):
            return [self.decrypt_dict(v) for v in data]
        if isinstance(data, str):
            return self.decrypt(data)
        return data

    # ── private ───────────────────────────────────────────────
    def _load_or_create_key(self) -> bytes:
        if self._key_file.exists():
            raw = self._key_file.read_bytes().strip()
            if raw:
                return raw
        key = Fernet.generate_key()
        self._key_file.parent.mkdir(parents=True, exist_ok=True)
        self._key_file.write_bytes(key + b"\n")
        # 尝试设置文件权限（仅 owner 可读写）
        try:
            os.chmod(self._key_file, 0o600)
        except OSError:
            pass
        _log.info("已生成加密密钥: %s", self._key_file)
        return key
