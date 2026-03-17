from __future__ import annotations

import asyncio
import unittest

from core.thinking import ThinkingEngine


class _StubPersonality:
    def system_instruction(self, **kwargs) -> str:
        _ = kwargs
        return "SYSTEM_BASE"

    def style_instruction(self, style: str) -> str:
        return f"STYLE:{style}"

    def scene_instruction(self, scene: str) -> str:
        return f"SCENE:{scene}"


class _StubPromptPolicy:
    def compose_prompt(self, channel: str, base_prompt: str) -> str:
        return f"{base_prompt}\nPOLICY:{channel}"


class _CaptureModelClient:
    enabled = True

    def __init__(self, response: str = "模型回复", should_fail: bool = False) -> None:
        self.response = response
        self.should_fail = should_fail
        self.calls: list[dict[str, object]] = []

    async def chat_text(self, messages, max_tokens: int = 0) -> str:  # type: ignore[no-untyped-def]
        self.calls.append({"messages": messages, "max_tokens": max_tokens})
        if self.should_fail:
            raise RuntimeError("boom")
        return self.response


class ThinkingEngineRegressionTests(unittest.TestCase):
    def _make_engine(self, model_client: _CaptureModelClient, allow_thinking: bool = True) -> ThinkingEngine:
        engine = ThinkingEngine.__new__(ThinkingEngine)
        engine.bot_name = "YuKiKo"
        engine.language = "zh"
        engine.allow_thinking = allow_thinking
        engine.default_source_links = 3
        engine.personality = _StubPersonality()
        engine.model_client = model_client
        engine.prompt_policy = _StubPromptPolicy()
        engine.memory_recall_level = "light"
        return engine

    def test_build_payload_includes_recent_speakers_and_context(self) -> None:
        engine = self._make_engine(_CaptureModelClient())

        payload = engine._build_payload(
            user_text="她刚才是不是不开心",
            trigger_reason="mentioned",
            memory_context=["用户: 她刚才突然不说话了", "机器人: 我看到你在担心她"],
            related_memories=["她前几天也说过自己压力有点大"],
            search_summary="标题: 情绪识别\n摘要: 突然沉默可能意味着情绪波动",
            sensitive_context="需要避免下定论",
            user_profile_summary="妈妈：日常口语；偏短句；动漫；情绪偏焦虑",
            scene_tag="chat",
            compat_context="【群聊关系兼容层】\n- 当前主要回应对象: 妈妈(QQ:10001)",
            affinity_hint="关系热度 Lv.4 好朋友 / 好感度 66/100",
            mood_hint="当前心情: slightly_melancholy",
            current_user_name="妈妈",
            recent_speakers=[
                ("20002", "小雨", "我刚才真的有点难过"),
                ("30003", "阿风", "先别想太多"),
            ],
        )

        self.assertIn("触发信息: mentioned", payload)
        self.assertIn("【情感状态】", payload)
        self.assertIn("用户画像（妈妈", payload)
        self.assertIn("【群聊关系兼容层】", payload)
        self.assertIn("最近活跃用户", payload)
        self.assertIn("小雨(QQ:20002)", payload)
        self.assertIn("最近对话", payload)
        self.assertIn("相关长期记忆", payload)
        self.assertIn("工具结果(搜索)", payload)
        self.assertIn("风险上下文", payload)

    def test_generate_reply_uses_llm_prompt_path(self) -> None:
        model_client = _CaptureModelClient(response="模型回复")
        engine = self._make_engine(model_client)

        reply = asyncio.run(
            engine.generate_reply(
                user_text="帮我看看这个群聊里谁在接谁的话",
                memory_context=["小雨: 我刚才真的有点难过"],
                related_memories=["她最近工作压力比较大"],
                reply_style="serious",
                search_summary="标题: 群聊回复链\n摘要: reply 和 @ 优先于旧记忆",
                user_profile_summary="用户：表达偏正式；偏短句",
                trigger_reason="mentioned",
                scene_hint="chat",
                current_user_name="测试用户",
                recent_speakers=[("20002", "小雨", "我刚才真的有点难过")],
                compat_context="【群聊关系兼容层】\n- 当前主要回应对象: 测试用户(QQ:10001)",
                affinity_hint="关系热度 Lv.3",
                mood_hint="当前心情: calm",
            )
        )

        self.assertEqual(reply, "模型回复")
        self.assertEqual(len(model_client.calls), 1)
        messages = model_client.calls[0]["messages"]
        self.assertIn("POLICY:thinking", messages[0]["content"])
        self.assertIn("STYLE:serious", messages[0]["content"])
        self.assertIn("SCENE:chat", messages[0]["content"])
        self.assertIn("最近活跃用户", messages[1]["content"])
        self.assertIn("工具结果(搜索)", messages[1]["content"])

    def test_generate_reply_falls_back_when_thinking_disabled(self) -> None:
        engine = self._make_engine(_CaptureModelClient(), allow_thinking=False)

        reply = asyncio.run(
            engine.generate_reply(
                user_text="在吗",
                memory_context=[],
                related_memories=[],
                reply_style="short",
            )
        )

        self.assertEqual(reply, "我在，你继续说。")

    def test_generate_reply_falls_back_when_llm_fails(self) -> None:
        engine = self._make_engine(_CaptureModelClient(should_fail=True), allow_thinking=True)

        reply = asyncio.run(
            engine.generate_reply(
                user_text="这个问题怎么处理",
                memory_context=[],
                related_memories=[],
                reply_style="serious",
            )
        )

        self.assertTrue(bool(reply))


if __name__ == "__main__":
    unittest.main()
