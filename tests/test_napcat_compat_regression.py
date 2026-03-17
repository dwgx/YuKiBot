from __future__ import annotations

import unittest
from types import SimpleNamespace

import core.webui as webui
from core.agent_tools import _napcat_api_call
from core.napcat_compat import normalize_napcat_api_kwargs, resolve_napcat_api_name


class _StubBot:
    def __init__(self) -> None:
        self.self_id = "99123"
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def call_api(self, api: str, **kwargs):
        self.calls.append((api, dict(kwargs)))
        if api == "get_status":
            return {"status": "ok", "retcode": 0, "data": {"online": True, "good": True}}
        if api == "get_version_info":
            return {
                "status": "ok",
                "retcode": 0,
                "data": {
                    "app_name": "NapCat",
                    "app_version": "4.8.120",
                    "protocol_version": "OneBot v11",
                }
            }
        if api == "send_private_msg":
            return {"status": "ok", "retcode": 0, "data": {"message_id": "10001"}}
        return {"status": "ok", "retcode": 0, "data": {}}


class NapCatCompatRegressionTests(unittest.IsolatedAsyncioTestCase):
    def test_resolve_napcat_api_name_maps_legacy_aliases(self) -> None:
        self.assertEqual(resolve_napcat_api_name("set_group_sign"), "send_group_sign")
        self.assertEqual(resolve_napcat_api_name("get_group_notice"), "_get_group_notice")
        self.assertEqual(resolve_napcat_api_name("send_group_message"), "send_group_msg")
        self.assertEqual(resolve_napcat_api_name("send_group_sign"), "send_group_sign")

    def test_normalize_napcat_api_kwargs_stringifies_nested_ids_only(self) -> None:
        payload = normalize_napcat_api_kwargs(
            "send_private_msg",
            {
                "user_id": 123456789,
                "message": "hello",
                "count": 20,
                "nodes": [
                    {"user_id": 22334455, "content": "hi"},
                    {"data": {"qq": 99887766, "text": "ping"}},
                ],
            },
        )

        self.assertEqual(payload["user_id"], "123456789")
        self.assertEqual(payload["count"], 20)
        self.assertEqual(payload["nodes"][0]["user_id"], "22334455")
        self.assertEqual(payload["nodes"][1]["data"]["qq"], "99887766")

    async def test_agent_tools_wrapper_normalizes_message_id(self) -> None:
        captured: dict[str, object] = {}

        async def fake_api_call(api: str, **kwargs):
            captured["api"] = api
            captured["kwargs"] = dict(kwargs)
            return {"ok": True}

        result = await _napcat_api_call(
            {"api_call": fake_api_call},
            "delete_msg",
            "ok",
            message_id=12345678901234567890,
        )

        self.assertTrue(result.ok)
        self.assertEqual(captured["api"], "delete_msg")
        self.assertEqual(
            captured["kwargs"],
            {"message_id": "12345678901234567890"},
        )

    async def test_agent_tools_wrapper_maps_legacy_sign_alias_to_official_api(self) -> None:
        captured: dict[str, object] = {}

        async def fake_api_call(api: str, **kwargs):
            captured["api"] = api
            captured["kwargs"] = dict(kwargs)
            return {"ok": True}

        result = await _napcat_api_call(
            {"api_call": fake_api_call},
            "set_group_sign",
            "ok",
            group_id=123456,
        )

        self.assertTrue(result.ok)
        self.assertEqual(captured["api"], "send_group_sign")
        self.assertEqual(captured["kwargs"], {"group_id": "123456"})

    async def test_webui_calls_and_diagnostics_use_napcat_compat_layer(self) -> None:
        original_get_runtime = webui._get_onebot_runtime
        original_engine = webui._engine
        bot = _StubBot()

        async def fake_get_runtime(bot_id: str = ""):
            self.assertEqual(bot_id, "bot-a")
            return bot

        webui._get_onebot_runtime = fake_get_runtime
        webui._engine = SimpleNamespace(
            agent_tool_registry=SimpleNamespace(
                _schemas={
                    "send_private_msg": SimpleNamespace(category="napcat"),
                    "memory_update": SimpleNamespace(category="general"),
                }
            )
        )

        try:
            send_result = await webui._onebot_call(
                "send_private_msg",
                bot_id="bot-a",
                user_id=778899,
                message="hello",
            )
            diagnostics = await webui._collect_napcat_status(bot_id="bot-a")
        finally:
            webui._get_onebot_runtime = original_get_runtime
            webui._engine = original_engine

        self.assertEqual(send_result, {"message_id": "10001"})
        self.assertEqual(bot.calls[0][0], "send_private_msg")
        self.assertEqual(bot.calls[0][1]["user_id"], "778899")
        self.assertTrue(diagnostics["availability"]["onebot_connected"])
        self.assertTrue(diagnostics["availability"]["status_api_ok"])
        self.assertTrue(diagnostics["availability"]["version_api_ok"])
        self.assertTrue(diagnostics["compatibility"]["string_id_normalization_active"])
        self.assertTrue(diagnostics["compatibility"]["string_id_preferred_by_version"])
        self.assertEqual(diagnostics["integration"]["registered_napcat_tools"], 1)
        self.assertIn("ffmpeg_ready", diagnostics["linux"])


if __name__ == "__main__":
    unittest.main()
