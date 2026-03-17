"""Regression tests for high-risk confirmation loop fix and learn_sticker media strip."""
from __future__ import annotations

import copy
import json
import time
import unittest
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

from core.agent import AgentLoop


class _StubAgentLoop(AgentLoop):
    """Minimal AgentLoop stub for unit-testing guard logic."""

    def __init__(self) -> None:
        # bypass real __init__; set only what the guard methods need
        self.high_risk_control_enable = True
        self.high_risk_default_require_confirmation = True
        self.high_risk_categories: set[str] = {"admin"}
        self.high_risk_pending_ttl_seconds = 180
        self.high_risk_name_patterns = ()
        self.high_risk_description_patterns = ()
        self.high_risk_user_enable_patterns = ()
        self.high_risk_user_disable_patterns = ()
        self.high_risk_use_confirm_token = False
        self.high_risk_confirm_cues = ("确认", "确认执行", "继续执行", "确定执行", "yes")
        self.high_risk_cancel_cues = ("取消", "算了", "不要了", "cancel", "no")
        self._pending_high_risk_actions: dict[str, dict[str, Any]] = {}
        self._admin_ids: set[str] = set()
        self.tool_args_log_max_chars = 600
        self._tool_schemas: dict[str, Any] = {}

    # stubs for methods called by _guard_high_risk_tool_call
    def _tool_is_high_risk(self, tool_name: str) -> bool:
        return True

    def _require_high_risk_confirmation_for_user(self, ctx: Any) -> bool:
        return True

    def _cleanup_pending_high_risk(self, force: bool = False) -> None:
        pass

    @staticmethod
    def _pending_high_risk_key(ctx: Any) -> str:
        return f"{ctx.platform}:{ctx.user_id}"

    def _is_confirmation_text(self, text: str, pending: dict | None = None) -> bool:
        from utils.text import normalize_text
        content = normalize_text(text).lower()
        return any(cue in content for cue in self.high_risk_confirm_cues)

    def _is_cancellation_text(self, text: str, pending: dict | None = None) -> bool:
        from utils.text import normalize_text
        content = normalize_text(text).lower()
        return any(cue in content for cue in self.high_risk_cancel_cues)

    def _build_high_risk_confirm_prompt(self, tool_name, tool_args):
        prompt = f"这是高风险操作: {tool_name}。请回复\u201c确认执行\u201d。"
        return prompt, "", ""


def _make_ctx(message_text: str = "", user_id: str = "u1", platform: str = "qq"):
    return SimpleNamespace(
        trace_id="test-trace",
        message_text=message_text,
        user_id=user_id,
        platform=platform,
    )


# ---------------------------------------------------------------------------
# Test 1: High-risk confirmation loop fix
# ---------------------------------------------------------------------------
class HighRiskConfirmLoopTests(unittest.TestCase):

    def test_confirm_same_tool_allows_even_if_args_drift(self) -> None:
        """确认执行后，即使 LLM 第二轮参数轻微变化，同 tool_name 也应放行。"""
        agent = _StubAgentLoop()

        # 第一轮：首次拦截
        ctx1 = _make_ctx("@妈妈 拉黑本群")
        args_v1 = {"target": "group:123", "action": "ban"}
        result1 = agent._guard_high_risk_tool_call(ctx1, "admin_command", args_v1)
        self.assertTrue(result1)  # 应返回确认提示

        # 第二轮：用户回复"确认执行"，但 LLM 生成的参数有轻微变化
        ctx2 = _make_ctx("确认执行")
        args_v2 = {"target": "group:123", "action": "ban", "reason": "user request"}
        result2 = agent._guard_high_risk_tool_call(ctx2, "admin_command", args_v2)
        self.assertEqual(result2, "")  # 应放行，不再循环确认

    def test_confirm_overrides_args_with_saved_copy(self) -> None:
        """确认后应使用首次拦截时保存的 tool_args，防止参数漂移。"""
        agent = _StubAgentLoop()

        ctx1 = _make_ctx("拉黑用户")
        original_args = {"target": "user:456", "action": "ban"}
        agent._guard_high_risk_tool_call(ctx1, "admin_command", original_args)

        # 确认时 LLM 给了不同参数
        ctx2 = _make_ctx("确认执行")
        drifted_args = {"target": "user:789", "action": "kick"}
        agent._guard_high_risk_tool_call(ctx2, "admin_command", drifted_args)

        # drifted_args 应被覆盖为原始保存的参数
        self.assertEqual(drifted_args["target"], "user:456")
        self.assertEqual(drifted_args["action"], "ban")

    def test_cancel_clears_pending(self) -> None:
        """取消后 pending 应被清理。"""
        agent = _StubAgentLoop()

        ctx1 = _make_ctx("拉黑")
        agent._guard_high_risk_tool_call(ctx1, "admin_command", {"action": "ban"})
        self.assertTrue(agent._pending_high_risk_actions)

        ctx2 = _make_ctx("取消")
        result = agent._guard_high_risk_tool_call(ctx2, "admin_command", {"action": "ban"})
        self.assertIn("取消", result)
        self.assertFalse(agent._pending_high_risk_actions)

    def test_different_tool_evicts_old_pending(self) -> None:
        """不同工具名应覆盖旧 pending。"""
        agent = _StubAgentLoop()

        ctx1 = _make_ctx("操作A")
        agent._guard_high_risk_tool_call(ctx1, "tool_a", {"x": 1})

        ctx2 = _make_ctx("操作B")
        result = agent._guard_high_risk_tool_call(ctx2, "tool_b", {"y": 2})
        self.assertTrue(result)  # 新的确认提示

        # pending 应只有 tool_b
        key = "qq:u1"
        self.assertEqual(agent._pending_high_risk_actions[key]["tool_name"], "tool_b")


# ---------------------------------------------------------------------------
# Test 2: learn_sticker media strip in final_answer
# ---------------------------------------------------------------------------
class LearnStickerMediaStripTests(unittest.TestCase):

    def test_sticker_tool_strips_media_from_final_answer(self) -> None:
        """learn_sticker 成功后，final_answer 的媒体字段应被清空。"""
        steps = [
            {"step": 0, "tool": "learn_sticker", "result": {"ok": True}},
            {"step": 1, "tool": "final_answer", "result": "done"},
        ]
        # Simulate the stripping logic from agent.py
        _STICKER_LIKE_TOOLS = {
            "learn_sticker", "correct_sticker",
            "send_emoji", "send_sticker", "send_face",
        }
        sticker_tool_used = any(
            s.get("tool") in _STICKER_LIKE_TOOLS and s.get("result")
            for s in steps
        )
        self.assertTrue(sticker_tool_used)

        # Simulate final_answer with leaked media
        image_url = "https://example.com/sticker.png"
        image_urls = ["https://example.com/sticker.png"]
        video_url = ""
        audio_file = ""
        message_text = "学习这个表情包"

        if sticker_tool_used and (image_url or image_urls or video_url or audio_file):
            user_wants_preview = any(
                kw in message_text
                for kw in ("预览", "发出来看看", "看看效果", "/preview")
            )
            if not user_wants_preview:
                image_url = ""
                image_urls = []
                video_url = ""
                audio_file = ""

        self.assertEqual(image_url, "")
        self.assertEqual(image_urls, [])

    def test_sticker_tool_preserves_media_when_preview_requested(self) -> None:
        """用户要求预览时，媒体字段应保留。"""
        steps = [
            {"step": 0, "tool": "learn_sticker", "result": {"ok": True}},
        ]
        _STICKER_LIKE_TOOLS = {
            "learn_sticker", "correct_sticker",
            "send_emoji", "send_sticker", "send_face",
        }
        sticker_tool_used = any(
            s.get("tool") in _STICKER_LIKE_TOOLS and s.get("result")
            for s in steps
        )
        image_url = "https://example.com/sticker.png"
        message_text = "学习这个表情包，发出来看看"

        if sticker_tool_used and image_url:
            user_wants_preview = any(
                kw in message_text
                for kw in ("预览", "发出来看看", "看看效果", "/preview")
            )
            if not user_wants_preview:
                image_url = ""

        self.assertEqual(image_url, "https://example.com/sticker.png")

    def test_non_sticker_tool_keeps_media(self) -> None:
        """非表情包工具不应触发媒体清空。"""
        steps = [
            {"step": 0, "tool": "analyze_image", "result": {"ok": True}},
        ]
        _STICKER_LIKE_TOOLS = {
            "learn_sticker", "correct_sticker",
            "send_emoji", "send_sticker", "send_face",
        }
        sticker_tool_used = any(
            s.get("tool") in _STICKER_LIKE_TOOLS and s.get("result")
            for s in steps
        )
        self.assertFalse(sticker_tool_used)


# ---------------------------------------------------------------------------
# Test 3: _looks_like_image_question no longer matches Chinese keywords
# ---------------------------------------------------------------------------
class ImageQuestionKeywordTests(unittest.TestCase):

    def test_chinese_keywords_no_longer_trigger(self) -> None:
        """中文关键词不应再触发图片提问判定。"""
        for text in ["图里有什么", "这张图是什么", "看图说话", "截图给我看看", "照片里是谁"]:
            self.assertFalse(
                AgentLoop._looks_like_image_question(text),
                f"Should not trigger for: {text}",
            )

    def test_explicit_control_tokens_still_trigger(self) -> None:
        """/analyze 等控制 token 仍应触发。"""
        self.assertTrue(AgentLoop._looks_like_image_question("/analyze this"))
        self.assertTrue(AgentLoop._looks_like_image_question("mode=analyze"))
        self.assertTrue(AgentLoop._looks_like_image_question("ocr=true"))


# ---------------------------------------------------------------------------
# Test 4: image generation intent should not steal learn_sticker turns
# ---------------------------------------------------------------------------
class ImageGenerationIntentRegressionTests(unittest.TestCase):

    def test_learning_sticker_multimodal_text_is_not_generation_request(self) -> None:
        text = "MULTIMODAL_EVENT_AT user mentioned bot and sent multimodal message: image:[动画表情] 学习表情包"
        self.assertFalse(AgentLoop._looks_like_image_generation_request(text))

    def test_generation_request_still_matches_real_create_image_intent(self) -> None:
        self.assertTrue(AgentLoop._looks_like_image_generation_request("我生成一张RTX5090图片"))
        self.assertTrue(AgentLoop._looks_like_image_generation_request("帮我画个猫猫表情包"))
        self.assertTrue(AgentLoop._looks_like_image_generation_request("生成一只小猫"))

    def test_generation_request_does_not_overmatch_text_tasks(self) -> None:
        self.assertFalse(AgentLoop._looks_like_image_generation_request("生成一份日报"))
        self.assertFalse(AgentLoop._looks_like_image_generation_request("生成JSON配置"))


# ---------------------------------------------------------------------------
# Test 5: Log truncation helper
# ---------------------------------------------------------------------------
class LogTruncationTests(unittest.TestCase):

    def test_short_args_not_truncated(self) -> None:
        agent = _StubAgentLoop()
        args = {"key": "value"}
        result = agent._truncate_tool_args_for_log(args)
        self.assertNotIn("truncated", result)

    def test_long_args_truncated_with_marker(self) -> None:
        agent = _StubAgentLoop()
        args = {"data": "x" * 1000}
        result = agent._truncate_tool_args_for_log(args)
        self.assertIn("truncated", result)
        self.assertLessEqual(len(result), 700)  # 600 + marker


if __name__ == "__main__":
    unittest.main()
