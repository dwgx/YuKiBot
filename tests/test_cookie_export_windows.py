from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path


def _load_script_module():
    root = Path(__file__).resolve().parents[1]
    script_path = root / "scripts" / "get_cookies_windows.py"
    spec = importlib.util.spec_from_file_location("get_cookies_windows", script_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load script module: {script_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


cookie_export = _load_script_module()


class CookieExportWindowsTests(unittest.TestCase):
    def test_normalize_site_name_accepts_q_alias(self) -> None:
        self.assertEqual(cookie_export.normalize_site_name("q"), "qzone")
        self.assertEqual(cookie_export.normalize_site_name("qqzone"), "qzone")
        self.assertEqual(cookie_export.normalize_site_name("dy"), "douyin")

    def test_cookie_string_roundtrip(self) -> None:
        cookie_string = "uin=o123; p_skey=abc; skey=def"
        cookie_dict = cookie_export.cookie_string_to_dict(cookie_string)
        self.assertEqual(cookie_dict["uin"], "o123")
        self.assertEqual(cookie_dict["p_skey"], "abc")
        self.assertIn("skey=def", cookie_export.cookie_dict_to_string(cookie_dict))

    def test_build_yukiko_payload_for_bilibili(self) -> None:
        payload = cookie_export.build_yukiko_site_payload(
            "bilibili",
            {"SESSDATA": "sess", "bili_jct": "csrf"},
            "SESSDATA=sess; bili_jct=csrf",
        )
        self.assertEqual(
            payload,
            {
                "enable": True,
                "sessdata": "sess",
                "bili_jct": "csrf",
            },
        )

    def test_build_yukiko_payload_for_qzone(self) -> None:
        payload = cookie_export.build_yukiko_site_payload(
            "qzone",
            {"uin": "o123", "p_skey": "abc"},
            "uin=o123; p_skey=abc",
        )
        self.assertEqual(
            payload,
            {
                "enable": True,
                "cookie": "uin=o123; p_skey=abc",
            },
        )


if __name__ == "__main__":
    unittest.main()
