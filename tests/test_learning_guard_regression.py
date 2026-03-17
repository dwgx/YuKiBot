from __future__ import annotations

import asyncio
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from core.agent import AgentLoop
from core.agent_tools import AgentToolRegistry, _handle_learn_knowledge, register_builtin_tools
from core.engine import YukikoEngine
from core.knowledge_updater import KnowledgeUpdater
from core.memory import MemoryEngine
from core.tools import ToolExecutor
from core.trigger import TriggerEngine, TriggerInput


class _DummyKB:
    def __init__(self) -> None:
        self.add_calls = 0
        self.upserts: list[dict[str, object]] = []

    def add(self, **kwargs):  # type: ignore[no-untyped-def]
        self.add_calls += 1
        return 1

    def upsert_conflict_checked(self, **kwargs):  # type: ignore[no-untyped-def]
        self.upserts.append(kwargs)
        return {"action": "inserted"}


class _DummyModelClient:
    enabled = True

    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload

    async def chat_json(self, messages):  # type: ignore[no-untyped-def]
        _ = messages
        return self.payload


class _DummyExecutor(ToolExecutor):
    def __init__(self) -> None:
        super().__init__(None, None, lambda *args, **kwargs: None, {})


class LearningGuardRegressionTests(unittest.TestCase):
    def _make_memory(self) -> MemoryEngine:
        tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(tmpdir.cleanup)
        memory = MemoryEngine(
            {
                "preferred_name_patterns": [
                    r"(?:以后)?(?:叫我|喊我|称呼我)(?P<name>[^，。！？!?]{1,12})$",
                ]
            },
            Path(tmpdir.name),
            global_config={"control": {"heuristic_rules_enable": True}},
        )
        self.addCleanup(memory.close)
        return memory

    def test_trigger_memory_declare_requires_directed_and_non_hype_context(self) -> None:
        trigger = TriggerEngine({}, {"name": "YuKiKo", "nicknames": ["yukiko"]})
        now = datetime.now(timezone.utc)

        self.assertFalse(
            trigger._looks_like_explicit_memory_declare(
                TriggerInput(
                    conversation_id="group:1",
                    user_id="u1",
                    text="以后都叫我妈妈",
                    mentioned=False,
                    is_private=False,
                    timestamp=now,
                    at_other_user_ids=[],
                    reply_to_user_id="",
                    bot_id="bot",
                )
            )
        )
        self.assertTrue(
            trigger._looks_like_explicit_memory_declare(
                TriggerInput(
                    conversation_id="group:1",
                    user_id="u1",
                    text="以后叫我阿背",
                    mentioned=True,
                    is_private=False,
                    timestamp=now,
                    at_other_user_ids=[],
                    reply_to_user_id="bot",
                    bot_id="bot",
                )
            )
        )

    def test_memory_only_learns_safe_preferred_name(self) -> None:
        memory = self._make_memory()
        memory.add_message(
            conversation_id="group:1",
            user_id="u1",
            user_name="背影",
            role="user",
            content="以后都叫我妈妈",
            timestamp=datetime.now(timezone.utc),
            metadata={
                "is_private": False,
                "mentioned": True,
                "explicit_bot_addressed": True,
                "bot_id": "bot",
            },
        )
        self.assertEqual(memory.get_preferred_name("u1", fallback_name="背影"), "背影")

        memory.add_message(
            conversation_id="group:1",
            user_id="u1",
            user_name="背影",
            role="user",
            content="以后叫我阿背",
            timestamp=datetime.now(timezone.utc),
            metadata={
                "is_private": False,
                "mentioned": True,
                "explicit_bot_addressed": True,
                "bot_id": "bot",
            },
        )
        self.assertEqual(memory.get_preferred_name("u1", fallback_name="背影"), "阿背")

    def test_learn_knowledge_routes_safe_preferred_name_to_memory(self) -> None:
        memory = self._make_memory()
        kb = _DummyKB()

        result = asyncio.run(
            _handle_learn_knowledge(
                {"title": "用户称呼偏好", "content": "以后叫我阿背"},
                {
                    "knowledge_base": kb,
                    "memory_engine": memory,
                    "conversation_id": "group:1",
                    "user_id": "u1",
                    "bot_id": "bot",
                    "is_private": False,
                    "mentioned": True,
                    "explicit_bot_addressed": True,
                    "message_text": "以后叫我阿背",
                    "original_message_text": "@YuKiKo 以后叫我阿背",
                    "config": {"bot": {"name": "YuKiKo", "nicknames": ["yukiko"]}},
                },
            )
        )

        self.assertTrue(result.ok)
        self.assertEqual(kb.add_calls, 0)
        self.assertEqual(memory.get_preferred_name("u1", fallback_name="背影"), "阿背")

    def test_learn_knowledge_blocks_group_hype_name_learning(self) -> None:
        memory = self._make_memory()
        kb = _DummyKB()

        result = asyncio.run(
            _handle_learn_knowledge(
                {"title": "用户称呼偏好", "content": "以后都叫我妈妈"},
                {
                    "knowledge_base": kb,
                    "memory_engine": memory,
                    "conversation_id": "group:1",
                    "user_id": "u1",
                    "bot_id": "bot",
                    "is_private": False,
                    "mentioned": False,
                    "explicit_bot_addressed": False,
                    "message_text": "以后都叫我妈妈",
                    "original_message_text": "以后都叫我妈妈",
                    "config": {"bot": {"name": "YuKiKo", "nicknames": ["yukiko"]}},
                },
            )
        )

        self.assertFalse(result.ok)
        self.assertEqual(kb.add_calls, 0)

    def test_knowledge_updater_blocks_group_profile_learning_without_safe_context(self) -> None:
        updater = KnowledgeUpdater(
            _DummyKB(),
            {
                "control": {"knowledge_learning": "aggressive", "heuristic_rules_enable": True},
                "knowledge_update": {},
            },
            logger=None,
            model_client=_DummyModelClient(
                {"items": [{"kind": "preferred_name", "content": "妈妈", "confidence": 0.95}]}
            ),
        )

        result = asyncio.run(
            updater.update_from_turn_async(
                "group:1",
                "u1",
                "以后都叫我妈妈",
                metadata={
                    "is_private": False,
                    "mentioned": False,
                    "explicit_bot_addressed": False,
                    "bot_id": "bot",
                },
            )
        )
        self.assertEqual(result["saved"], 0)

    def test_inject_user_name_strips_bidi_controls(self) -> None:
        reply = YukikoEngine._inject_user_name("在。", "吉吉国王\u202e\u2066", True)
        self.assertEqual(reply, "吉吉国王，在。")

    def test_music_play_by_id_is_not_high_risk(self) -> None:
        registry = AgentToolRegistry()
        register_builtin_tools(registry, None, None, None, {})
        loop = AgentLoop.__new__(AgentLoop)
        loop.tool_registry = registry
        loop.high_risk_categories = {"admin"}
        loop.high_risk_name_patterns = ()
        loop.high_risk_description_patterns = ()

        self.assertFalse(loop._tool_is_high_risk("music_play_by_id"))

    def test_agent_tool_registry_compat_intent_keyword_toggle(self) -> None:
        registry = AgentToolRegistry()
        self.assertFalse(registry.get_intent_keyword_routing_enabled())
        registry.set_intent_keyword_routing_enabled(True)
        self.assertTrue(registry.get_intent_keyword_routing_enabled())

    def test_vision_retry_runs_when_first_answer_is_empty(self) -> None:
        executor = _DummyExecutor()
        executor._vision_second_pass_enable = True
        retry_calls: list[str] = []

        async def fake_normalize(answer: str, prompt: str) -> str:
            _ = prompt
            return answer.strip()

        async def fake_describe(image_ref: str, prompt: str) -> str:
            _ = image_ref
            retry_calls.append(prompt)
            return "像是在无语地翻白眼"

        executor._normalize_vision_answer = fake_normalize  # type: ignore[method-assign]
        executor._vision_describe = fake_describe  # type: ignore[method-assign]
        executor._build_vision_retry_prompt = lambda query, message_text, animated_hint=False: "retry-prompt"  # type: ignore[assignment]

        result = asyncio.run(
            executor._normalize_vision_answer_with_retry(
                image_ref="data:image/png;base64,aaa",
                answer="",
                prompt="initial",
                query="这个表情什么意思",
                message_text="[动画表情]",
                animated_hint=True,
            )
        )

        self.assertEqual(result, "像是在无语地翻白眼")
        self.assertEqual(retry_calls, ["retry-prompt"])


if __name__ == "__main__":
    unittest.main()
