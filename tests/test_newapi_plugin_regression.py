from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

from plugins import newapi_plugin


class _FakeClient:
    async def close(self) -> None:
        return None


class NewAPIPluginRegressionTests(unittest.IsolatedAsyncioTestCase):
    def test_parse_payment_amount_supports_suffix_units(self) -> None:
        self.assertEqual(newapi_plugin._parse_payment_amount("200"), 200)
        self.assertEqual(newapi_plugin._parse_payment_amount("200M"), 200_000_000)
        self.assertEqual(newapi_plugin._parse_payment_amount("1w"), 10_000)
        self.assertEqual(newapi_plugin._parse_payment_amount(""), None)

    def test_online_payment_args_detection_accepts_amount_with_method(self) -> None:
        self.assertTrue(newapi_plugin._looks_like_online_payment_args("200 微信"))
        self.assertTrue(newapi_plugin._looks_like_online_payment_args("1000M alipay"))
        self.assertFalse(newapi_plugin._looks_like_online_payment_args("兑换码ABC123"))

    async def test_topup_amount_with_method_is_forwarded_to_pay(self) -> None:
        with patch("plugins.newapi_plugin._get_client", new=AsyncMock(return_value=_FakeClient())), patch(
            "plugins.newapi_plugin._cmd_pay",
            new=AsyncMock(return_value="PAY_OK"),
        ) as mocked_pay:
            result = await newapi_plugin._cmd_topup("200 支付宝", {})

        self.assertEqual(result, "PAY_OK")
        mocked_pay.assert_awaited_once_with("200 支付宝", {})


if __name__ == "__main__":
    unittest.main()
