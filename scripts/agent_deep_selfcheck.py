from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import logging
from pathlib import Path
import sys
from types import SimpleNamespace
from typing import Any

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from core import prompt_loader as _pl
from core.agent import AgentContext, AgentLoop
from core.agent_tools import AgentToolRegistry
from core.thinking import ThinkingEngine
from core.trigger import TriggerEngine, TriggerInput
from plugins.self_learning import Plugin


class _DummyModelClient:
    enabled = True


class _ThinkingModelClient:
    enabled = True

    def __init__(self, response: str = "thinking-ok", should_fail: bool = False) -> None:
        self.response = response
        self.should_fail = should_fail
        self.last_messages: list[dict[str, str]] = []

    async def chat_text(self, messages, max_tokens: int = 0) -> str:  # type: ignore[no-untyped-def]
        _ = max_tokens
        self.last_messages = messages
        if self.should_fail:
            raise RuntimeError("boom")
        return self.response


class _ThinkingPersonality:
    def system_instruction(self, **kwargs) -> str:
        _ = kwargs
        return "SYSTEM_BASE"

    def style_instruction(self, style: str) -> str:
        return f"STYLE:{style}"

    def scene_instruction(self, scene: str) -> str:
        return f"SCENE:{scene}"


class _DummyToolRegistry:
    tool_count = 8

    def select_tools_for_intent(self, message_text: str, perm_level: str) -> list[str]:
        _ = (message_text, perm_level)
        return ["web_search", "analyze_image", "final_answer"]

    def get_schemas_for_prompt_filtered(self, selected_tools: list[str]) -> str:
        return "\n".join(f"- {name}" for name in selected_tools)

    def get_prompt_hints_text(self, section: str, tool_names: list[str] | None = None) -> str:
        _ = (section, tool_names)
        return ""

    def get_dynamic_context(self, payload: dict[str, Any], tool_names: list[str] | None = None) -> str:
        _ = (payload, tool_names)
        return ""


@dataclass
class _Check:
    name: str
    ok: bool
    detail: str = ""


def _check_prompts() -> list[_Check]:
    _pl.reload()
    checks: list[_Check] = []

    agent = _pl.get_dict("agent")
    messages = _pl.get_dict("messages")
    required_agent = ["identity", "output_format", "rules", "network_flow", "reply_style", "tool_usage", "context_rules"]
    required_messages = [
        "mention_only_fallback",
        "mention_only_fallback_with_name",
        "llm_error_fallback",
        "generic_error",
        "no_result",
        "permission_denied",
    ]

    for key in required_agent:
        val = str(agent.get(key, "")).strip()
        checks.append(_Check(name=f"prompt.agent.{key}", ok=bool(val), detail=val[:60]))

    for key in required_messages:
        val = str(messages.get(key, "")).strip()
        checks.append(_Check(name=f"prompt.messages.{key}", ok=bool(val), detail=val[:60]))

    return checks


def _build_loop() -> AgentLoop:
    config = {
        "agent": {
            "enable": True,
            "max_steps": 8,
            "max_tokens": 4096,
            "fallback_on_parse_error": True,
            "allow_silent_on_llm_error": False,
        },
        "admin": {"super_users": ["10001"], "whitelist_groups": [123456]},
        "output": {"verbosity": "medium"},
        "queue": {"process_timeout_seconds": 120},
    }
    return AgentLoop(_DummyModelClient(), _DummyToolRegistry(), config)


def _check_agent_parse_and_prompt() -> list[_Check]:
    loop = _build_loop()
    checks: list[_Check] = []

    parse_cases: list[tuple[str, str]] = [
        ('{"tool":"web_search","args":{"query":"python","mode":"text"}}', "web_search"),
        ('```json\n{"tool":"final_answer","args":{"text":"ok"}}\n```', "final_answer"),
        ('[tool_call(web_search, query="hello", mode="text")]', "web_search"),
        ("我先给你答案", "final_answer"),
    ]
    for idx, (raw, expected_tool) in enumerate(parse_cases, start=1):
        parsed = loop._parse_llm_output(raw)
        actual_tool = parsed.get("tool", "") if isinstance(parsed, dict) else ""
        checks.append(
            _Check(
                name=f"agent.parse_case_{idx}",
                ok=actual_tool == expected_tool,
                detail=f"expected={expected_tool}, actual={actual_tool}",
            )
        )

    now = datetime.now(timezone.utc)
    ctx = AgentContext(
        conversation_id="group:123456",
        user_id="10001",
        user_name="tester",
        group_id=123456,
        bot_id="3223915831",
        is_private=False,
        mentioned=True,
        message_text="这张图里是谁",
        trace_id="selfcheck-ctx",
        media_summary=["image:https://example.com/a.png"],
        raw_segments=[{"type": "image", "data": {"url": "https://example.com/a.png"}}],
        verbosity="medium",
        sender_role="owner",
        is_whitelisted_group=True,
    )

    prompt = loop._build_system_prompt(ctx)
    user_msg = loop._build_user_message(ctx)
    checks.append(_Check("agent.build_system_prompt", ok="## 可用工具" in prompt and "## 身份" in prompt))
    checks.append(
        _Check(
            "agent.build_user_message_media_hint",
            ok=("analyze_image" in user_msg) or ("附带媒体" in user_msg and "image:" in user_msg),
        )
    )
    checks.append(_Check("agent.force_tool_first_image", ok=loop._should_force_image_tool_first(ctx)))

    return checks


def _check_trigger() -> list[_Check]:
    checks: list[_Check] = []
    now = datetime.now(timezone.utc)
    bot_cfg = {"name": "YuKiKo", "nicknames": ["yuki", "雪"]}

    strict_cfg = {
        "ai_listen_enable": False,
        "delegate_undirected_to_ai": False,
        "followup_reply_window_seconds": 30,
        "followup_max_turns": 3,
    }
    strict_engine = TriggerEngine(strict_cfg, bot_cfg)

    c1 = TriggerInput("group:1", "u1", "哈哈", False, False, now)
    r1 = strict_engine.evaluate(c1, [])
    checks.append(_Check("trigger.strict_undirected_ignore", ok=not r1.should_handle and r1.reason == "not_directed"))

    c2 = TriggerInput("group:1", "u1", "yuki 在吗", False, False, now + timedelta(seconds=1))
    r2 = strict_engine.evaluate(c2, [])
    checks.append(_Check("trigger.alias_call_handle", ok=r2.should_handle and r2.reason == "name_call"))

    strict_engine.mark_reply_target("group:1", "u1", now + timedelta(seconds=2))
    c3 = TriggerInput("group:1", "u1", "继续", False, False, now + timedelta(seconds=3))
    r3 = strict_engine.evaluate(c3, [])
    checks.append(_Check("trigger.followup_window_handle", ok=r3.should_handle and r3.reason == "followup_window"))

    listen_cfg = {
        "ai_listen_enable": True,
        "delegate_undirected_to_ai": True,
        "ai_listen_min_messages": 2,
        "ai_listen_min_unique_users": 2,
        "ai_listen_min_keyword_hits": 1,
        "ai_listen_min_score": 1.0,
        "ai_listen_interval_seconds": 1,
    }
    listen_engine = TriggerEngine(listen_cfg, bot_cfg)
    seed_rows = [
        TriggerInput("group:2", "u1", "这个怎么看", False, False, now),
        TriggerInput("group:2", "u2", "我也想知道", False, False, now + timedelta(milliseconds=500)),
    ]
    probe_observed = False
    observed_reasons: list[str] = []
    for row in seed_rows:
        rs = listen_engine.evaluate(row, [])
        observed_reasons.append(rs.reason)
        probe_observed = probe_observed or bool(rs.listen_probe and rs.should_handle)
    probe = TriggerInput("group:2", "u3", "有人懂这个吗", False, False, now + timedelta(seconds=2))
    r4 = listen_engine.evaluate(probe, [])
    observed_reasons.append(r4.reason)
    probe_observed = probe_observed or bool(r4.listen_probe and r4.should_handle)
    checks.append(
        _Check(
            "trigger.listen_probe_handle",
            ok=probe_observed,
            detail=" -> ".join(observed_reasons),
        )
    )

    return checks


def _check_self_learning_and_thinking() -> list[_Check]:
    checks: list[_Check] = []

    registry = AgentToolRegistry()
    plugin = Plugin()
    asyncio.run(plugin.setup({"enabled": True}, SimpleNamespace(agent_tool_registry=registry)))

    plugin._stats["total_sessions"] = 2
    plugin._stats["successful_skills"] = 1
    plugin._cache_loaded = True
    plugin._skill_cache = {"json_helper": {"name": "json_helper"}}

    dynamic_context = registry.get_dynamic_context(
        {"ctx": None, "config": {}, "selected_tools": ["learn_from_web"]},
        tool_names=["learn_from_web"],
    )
    checks.append(
        _Check(
            "self_learning.dynamic_context",
            ok="自学习状态" in dynamic_context and "执行后端: disabled" in dynamic_context,
            detail=dynamic_context[:80],
        )
    )

    tools_guidance = registry.get_prompt_hints_text(
        "tools_guidance",
        tool_names=["learn_from_web", "send_devlog", "list_my_skills"],
    )
    checks.append(
        _Check(
            "self_learning.prompt_hints",
            ok="自我学习流程" in tools_guidance and "send_devlog" in tools_guidance,
            detail=tools_guidance[:80],
        )
    )

    registry.select_tools_for_intent = lambda message_text, perm_level: [  # type: ignore[method-assign]
        "learn_from_web",
        "send_devlog",
        "list_my_skills",
    ]
    loop = AgentLoop(
        _DummyModelClient(),
        registry,
        {"agent": {"enable": True}, "admin": {"super_users": []}},
    )
    ctx = AgentContext(
        conversation_id="group:1",
        user_id="10001",
        user_name="tester",
        group_id=1,
        bot_id="99999",
        is_private=False,
        mentioned=True,
        message_text="继续学习 Agent 状态编排",
    )
    prompt = loop._build_system_prompt(ctx)
    checks.append(
        _Check(
            "agent.self_learning_dynamic_prompt",
            ok="## 动态上下文" in prompt and "自学习状态" in prompt,
        )
    )

    thinking_model = _ThinkingModelClient(response="thinking-ok")
    thinking = ThinkingEngine(
        {"bot": {"name": "YuKiKo", "allow_thinking": True}},
        _ThinkingPersonality(),
        thinking_model,
    )
    reply = asyncio.run(
        thinking.generate_reply(
            user_text="她刚才是不是不开心",
            memory_context=["小雨: 我刚才真的有点难过"],
            related_memories=["她前几天也说过自己压力比较大"],
            reply_style="serious",
            search_summary="标题: 群聊回复链\n摘要: reply 和 @ 优先于旧记忆",
            user_profile_summary="妈妈：日常口语；偏短句",
            trigger_reason="mentioned",
            current_user_name="妈妈",
            recent_speakers=[("20002", "小雨", "我刚才真的有点难过")],
            compat_context="【群聊关系兼容层】\n- 当前主要回应对象: 妈妈(QQ:10001)",
            affinity_hint="关系热度 Lv.4 好朋友 / 好感度 66/100",
            mood_hint="当前心情: slightly_melancholy",
        )
    )
    thinking_payload = (
        thinking_model.last_messages[1]["content"]
        if len(thinking_model.last_messages) >= 2
        else ""
    )
    checks.append(
        _Check(
            "thinking.payload_context",
            ok=(
                reply == "thinking-ok"
                and "触发信息: mentioned" in thinking_payload
                and "最近活跃用户" in thinking_payload
                and "关系热度" in thinking_payload
                and "工具结果(搜索)" in thinking_payload
            ),
            detail=thinking_payload[:80],
        )
    )

    thinking_logger = logging.getLogger("yukiko.thinking")
    old_disabled = thinking_logger.disabled
    thinking_logger.disabled = True
    try:
        fallback_reply = asyncio.run(
            ThinkingEngine(
                {"bot": {"name": "YuKiKo", "allow_thinking": True}},
                _ThinkingPersonality(),
                _ThinkingModelClient(should_fail=True),
            ).generate_reply(
                user_text="这个问题怎么处理",
                memory_context=[],
                related_memories=[],
                reply_style="short",
            )
        )
    finally:
        thinking_logger.disabled = old_disabled
    checks.append(
        _Check(
            "thinking.fallback_on_error",
            ok=bool(fallback_reply),
            detail=fallback_reply[:60],
        )
    )

    return checks


def main() -> int:
    checks: list[_Check] = []
    checks.extend(_check_prompts())
    checks.extend(_check_agent_parse_and_prompt())
    checks.extend(_check_trigger())
    checks.extend(_check_self_learning_and_thinking())

    passed = [c for c in checks if c.ok]
    failed = [c for c in checks if not c.ok]

    print("== YuKiKo Agent Deep Selfcheck ==")
    print(f"total={len(checks)} pass={len(passed)} fail={len(failed)}")
    for item in checks:
        mark = "PASS" if item.ok else "FAIL"
        detail = f" | {item.detail}" if item.detail else ""
        print(f"[{mark}] {item.name}{detail}")

    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
