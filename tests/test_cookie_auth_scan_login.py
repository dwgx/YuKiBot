from __future__ import annotations

import unittest
from unittest.mock import patch

from core import cookie_auth


class CookieAuthScanLoginTests(unittest.TestCase):
    def test_get_cookie_login_guide_normalizes_qq_to_qzone(self) -> None:
        guide = cookie_auth.get_cookie_login_guide("qq")

        self.assertIsNotNone(guide)
        assert guide is not None
        self.assertEqual(guide["platform"], "qzone")
        self.assertEqual(guide["display_name"], "QZone")
        self.assertTrue(str(guide["login_url"]).startswith("https://qzone.qq.com/"))

    @patch("core.cookie_auth.subprocess.Popen")
    @patch("core.cookie_auth._build_browser_login_command", return_value=(["browser.exe", "https://login.douyin.com/"], "Profile 1"))
    def test_prepare_browser_cookie_login_returns_guide_payload(self, build_command, popen) -> None:
        result = cookie_auth.prepare_browser_cookie_login("douyin", browser="edge")

        build_command.assert_called_once_with("edge", "https://login.douyin.com/")
        popen.assert_called_once()
        self.assertTrue(result["ok"])
        self.assertEqual(result["platform"], "douyin")
        self.assertEqual(result["browser"], "edge")
        self.assertEqual(result["profile_directory"], "Profile 1")
        self.assertEqual(result["login_url"], "https://login.douyin.com/")
        self.assertTrue(result["instructions"])
        self.assertIn("scan login", str(result["message"]).lower())

    def test_runtime_capabilities_expose_browser_scan_login(self) -> None:
        def fake_find_spec(name: str):
            return object() if name in {"browser_cookie3", "rookiepy", "bilibili_api"} else None

        def fake_find_browser(browser: str) -> str | None:
            return f"C:/{browser}.exe" if browser in {"edge", "firefox"} else None

        with patch("core.cookie_auth.importlib.util.find_spec", side_effect=fake_find_spec):
            with patch("core.cookie_auth._find_browser_exe", side_effect=fake_find_browser):
                caps = cookie_auth.get_cookie_runtime_capabilities()

        self.assertEqual(caps["browsers"]["installed"], ["edge", "firefox"])
        self.assertEqual(caps["browsers"]["scan_login_supported"], ["edge", "firefox"])
        self.assertTrue(caps["platforms"]["bilibili"]["browser_scan_login"])
        self.assertTrue(caps["platforms"]["douyin"]["browser_scan_login"])
        self.assertTrue(caps["platforms"]["kuaishou"]["browser_scan_login"])
        self.assertTrue(caps["platforms"]["qzone"]["browser_scan_login"])

    @patch("core.cookie_auth._stdin_is_tty", return_value=False)
    @patch("core.cookie_auth.bilibili_qr_login_sync")
    def test_interactive_bilibili_cookie_skips_when_non_tty(self, mock_qr_login, _mock_tty) -> None:
        result = cookie_auth.interactive_bilibili_cookie()

        mock_qr_login.assert_not_called()
        self.assertEqual(result, {"sessdata": "", "bili_jct": ""})

    @patch("core.cookie_auth._stdin_is_tty", return_value=True)
    @patch("core.cookie_auth.bilibili_qr_login_sync", return_value=None)
    @patch("core.cookie_auth._safe_input", side_effect=["1", "4"])
    def test_interactive_bilibili_cookie_scan_fail_then_skip(
        self,
        _mock_safe_input,
        mock_qr_login,
        _mock_tty,
    ) -> None:
        result = cookie_auth.interactive_bilibili_cookie()

        self.assertEqual(mock_qr_login.call_count, 1)
        self.assertEqual(result, {"sessdata": "", "bili_jct": ""})


if __name__ == "__main__":
    unittest.main()
