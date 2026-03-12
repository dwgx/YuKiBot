from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from core.webui import _build_repo_artifact_urls, _normalize_repo_http_url, _validate_sqlite_upload


class WebuiManagementRegressionTests(unittest.TestCase):
    def test_normalize_repo_http_url_supports_https_and_ssh(self) -> None:
        self.assertEqual(
            _normalize_repo_http_url("https://github.com/dwgx/YuKiKo.git"),
            "https://github.com/dwgx/YuKiKo",
        )
        self.assertEqual(
            _normalize_repo_http_url("git@github.com:dwgx/YuKiKo.git"),
            "https://github.com/dwgx/YuKiKo",
        )
        self.assertEqual(
            _normalize_repo_http_url("ssh://git@github.com/dwgx/YuKiKo.git"),
            "https://github.com/dwgx/YuKiKo",
        )

    def test_build_repo_artifact_urls_returns_github_downloads(self) -> None:
        urls = _build_repo_artifact_urls("https://github.com/dwgx/YuKiKo", "main")
        self.assertEqual(
            urls["windows_zip_url"],
            "https://github.com/dwgx/YuKiKo/archive/refs/heads/main.zip",
        )
        self.assertEqual(
            urls["bootstrap_url"],
            "https://raw.githubusercontent.com/dwgx/YuKiKo/main/bootstrap.sh",
        )

    def test_validate_sqlite_upload_accepts_real_sqlite_db(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "sample.db"
            conn = sqlite3.connect(str(db_path))
            try:
                conn.execute("CREATE TABLE demo (id INTEGER PRIMARY KEY, name TEXT)")
                conn.execute("INSERT INTO demo (name) VALUES ('alpha')")
                conn.commit()
            finally:
                conn.close()

            ok, tables, error = _validate_sqlite_upload(db_path)

        self.assertTrue(ok)
        self.assertEqual(error, "")
        self.assertIn("demo", tables)


if __name__ == "__main__":
    unittest.main()
