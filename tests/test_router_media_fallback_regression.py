from __future__ import annotations

import unittest

from core.router import RouterEngine, RouterInput


class _DisabledModelClient:
    enabled = False


def _build_router() -> RouterEngine:
    config = {
        "routing": {"mode": "ai_full"},
        "bot": {"name": "YuKiKo", "nicknames": []},
    }
    return RouterEngine(
        config=config,
        personality=object(),  # no-model 路径不会访问 personality
        model_client=_DisabledModelClient(),
    )


def _build_payload(**overrides: object) -> RouterInput:
    base = dict(
        text="",
        conversation_id="group:10001",
        user_id="10086",
        user_name="tester",
        trace_id="trace-router-media-fallback",
        mentioned=False,
        is_private=False,
        media_summary=[],
    )
    base.update(overrides)
    return RouterInput(**base)


class RouterMediaFallbackRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def test_directed_image_question_uses_media_analyze_when_model_disabled(
        self,
    ) -> None:
        router = _build_router()
        payload = _build_payload(
            mentioned=True,
            text=(
                "MULTIMODAL_EVENT_AT user mentioned bot and sent multimodal message: "
                "image:[image]\n这是什么"
            ),
            media_summary=["image:[image]"],
        )

        decision = await router.route(payload, plugins=[], tool_methods=[])

        self.assertTrue(decision.should_handle)
        self.assertEqual(decision.action, "search")
        self.assertEqual(decision.reason, "fallback_direct_media_no_model")
        self.assertEqual(decision.tool_args.get("method"), "media.analyze_image")
        self.assertEqual(decision.tool_args.get("query"), "这是什么")

    async def test_followup_image_event_uses_media_analyze_when_model_disabled(
        self,
    ) -> None:
        router = _build_router()
        payload = _build_payload(
            followup_candidate=True,
            text="MULTIMODAL_EVENT user sent multimodal message: image:[image]",
            media_summary=["image:[image]"],
        )

        decision = await router.route(payload, plugins=[], tool_methods=[])

        self.assertTrue(decision.should_handle)
        self.assertEqual(decision.action, "search")
        self.assertEqual(decision.reason, "fallback_followup_media_no_model")
        self.assertEqual(decision.tool_args.get("method"), "media.analyze_image")
        self.assertEqual(decision.tool_args.get("query"), "继续分析这张图")

    async def test_directed_text_without_image_keeps_reply_fallback(self) -> None:
        router = _build_router()
        payload = _build_payload(mentioned=True, text="在吗")

        decision = await router.route(payload, plugins=[], tool_methods=[])

        self.assertTrue(decision.should_handle)
        self.assertEqual(decision.action, "reply")
        self.assertEqual(decision.reason, "fallback_direct_no_model")


if __name__ == "__main__":
    unittest.main()
