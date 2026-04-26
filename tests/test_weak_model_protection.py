"""弱模型防护测试 — 验证 Agent 对不聪明模型输出的容错和质量检测。

覆盖场景:
- _normalize_tool_call 对弱模型格式的兼容 (function/parameters, action/input)
- _normalize_final_answer_text 对质量问题的检测 (纯标点/乱码/工具格式回显)
- _parse_llm_output 对弱模型常见输出格式的解析
"""
from __future__ import annotations

import unittest

from core.agent import AgentLoop


class WeakModelToolCallNormalizationTests(unittest.TestCase):
    """弱模型工具调用格式兼容测试。"""

    def test_standard_format(self):
        """标准 {"tool":"...","args":{...}} 格式。"""
        result = AgentLoop._normalize_tool_call(
            {"tool": "web_search", "args": {"query": "test"}}
        )
        self.assertEqual(result["tool"], "web_search")
        self.assertEqual(result["args"]["query"], "test")

    def test_openai_function_calling_format(self):
        """OpenAI {"name":"...","arguments":{...}} 格式。"""
        result = AgentLoop._normalize_tool_call(
            {"name": "web_search", "arguments": {"query": "test"}}
        )
        self.assertEqual(result["tool"], "web_search")
        self.assertEqual(result["args"]["query"], "test")

    def test_function_parameters_format(self):
        """弱模型 {"function":"...","parameters":{...}} 格式。"""
        result = AgentLoop._normalize_tool_call(
            {"function": "web_search", "parameters": {"query": "test"}}
        )
        self.assertIsNotNone(result)
        self.assertEqual(result["tool"], "web_search")
        self.assertEqual(result["args"]["query"], "test")

    def test_action_input_format(self):
        """弱模型 {"action":"...","input":{...}} 格式。"""
        result = AgentLoop._normalize_tool_call(
            {"action": "final_answer", "input": {"text": "你好"}}
        )
        self.assertIsNotNone(result)
        self.assertEqual(result["tool"], "final_answer")
        self.assertEqual(result["args"]["text"], "你好")

    def test_name_with_parameters_format(self):
        """弱模型 {"name":"...","parameters":{...}} 格式（没有 arguments）。"""
        result = AgentLoop._normalize_tool_call(
            {"name": "analyze_image", "parameters": {"url": "https://example.com/img.jpg"}}
        )
        self.assertIsNotNone(result)
        self.assertEqual(result["tool"], "analyze_image")
        self.assertEqual(result["args"]["url"], "https://example.com/img.jpg")

    def test_non_dict_returns_none(self):
        """非 dict 输入应返回 None。"""
        self.assertIsNone(AgentLoop._normalize_tool_call("not a dict"))
        self.assertIsNone(AgentLoop._normalize_tool_call([]))
        self.assertIsNone(AgentLoop._normalize_tool_call(None))

    def test_empty_dict_returns_none(self):
        """空 dict 应返回 None。"""
        self.assertIsNone(AgentLoop._normalize_tool_call({}))

    def test_function_must_be_string(self):
        """function 字段必须是字符串。"""
        self.assertIsNone(AgentLoop._normalize_tool_call(
            {"function": 123, "parameters": {}}
        ))


class WeakModelFinalAnswerQualityTests(unittest.TestCase):
    """弱模型 final_answer 质量检测测试。"""

    def test_normal_text_passes(self):
        """正常文本应通过。"""
        result = AgentLoop._normalize_final_answer_text("你好，这是一段正常回复")
        self.assertEqual(result, "你好，这是一段正常回复")

    def test_empty_text_returns_empty(self):
        """空文本应返回空。"""
        result = AgentLoop._normalize_final_answer_text("")
        self.assertEqual(result, "")

    def test_whitespace_only_returns_empty(self):
        """纯空白应返回空。"""
        result = AgentLoop._normalize_final_answer_text("   \n\t  ")
        self.assertEqual(result, "")

    def test_pure_punctuation_returns_empty(self):
        """纯标点应返回空。"""
        result = AgentLoop._normalize_final_answer_text("，。！？...")
        self.assertEqual(result, "")

    def test_tool_format_echo_returns_empty(self):
        """回显工具调用格式应返回空。"""
        result = AgentLoop._normalize_final_answer_text(
            '{"tool":"web_search","args":{"query":"test"}}'
        )
        self.assertEqual(result, "")

    def test_function_format_echo_returns_empty(self):
        """回显 function 格式应返回空。"""
        result = AgentLoop._normalize_final_answer_text(
            '{"function":"analyze_image","parameters":{"url":"x"}}'
        )
        self.assertEqual(result, "")

    def test_english_refusal_normalized(self):
        """英文拒绝应被归一化为中文。"""
        result = AgentLoop._normalize_final_answer_text(
            "I can't help with that request. I'm not able to generate sexually explicit content."
        )
        self.assertIn("不能帮你处理", result)

    def test_text_with_punctuation_passes(self):
        """包含标点的正常文本应通过。"""
        result = AgentLoop._normalize_final_answer_text("你好！今天天气不错。")
        self.assertEqual(result, "你好！今天天气不错。")

    def test_json_with_unrelated_keys_passes(self):
        """不含 tool/function/name 的 JSON 不应被误拦。"""
        result = AgentLoop._normalize_final_answer_text(
            '{"result": "ok", "data": "test"}'
        )
        # 不含 tool/function/name，不应被拦截
        self.assertTrue(bool(result))


class WeakModelParseIntegrationTests(unittest.TestCase):
    """弱模型输出解析集成测试。"""

    def _make_parser(self) -> AgentLoop:
        loop = AgentLoop.__new__(AgentLoop)
        loop.fallback_on_parse_error = True
        return loop

    def test_parse_function_parameters_json(self):
        """弱模型 function/parameters JSON 格式应被正确解析。"""
        loop = self._make_parser()
        parsed = loop._parse_llm_output(
            '{"function":"web_search","parameters":{"query":"python教程"}}'
        )
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["tool"], "web_search")
        self.assertEqual(parsed["args"]["query"], "python教程")

    def test_parse_action_input_json(self):
        """弱模型 action/input JSON 格式应被正确解析。"""
        loop = self._make_parser()
        parsed = loop._parse_llm_output(
            '{"action":"final_answer","input":{"text":"好的"}}'
        )
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["tool"], "final_answer")
        self.assertEqual(parsed["args"]["text"], "好的")

    def test_parse_xml_tool_call_tag(self):
        """<tool_call> 标签包裹的 JSON 应被正确解析。"""
        loop = self._make_parser()
        parsed = loop._parse_llm_output(
            '<tool_call>{"tool":"web_search","args":{"query":"hello"}}</tool_call>'
        )
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["tool"], "web_search")

    def test_parse_thinking_before_json(self):
        """<thinking> 块后跟 JSON 应被正确解析。"""
        loop = self._make_parser()
        parsed = loop._parse_llm_output(
            '<thinking>让我想想</thinking>{"tool":"final_answer","args":{"text":"答案"}}'
        )
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["tool"], "final_answer")

    def test_parse_tool_use_xml(self):
        """<tool_use> 标签格式应被正确解析。"""
        loop = self._make_parser()
        parsed = loop._parse_llm_output(
            '<tool_use>web_search {"query":"python"}</tool_use>'
        )
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["tool"], "web_search")

    def test_parse_bracket_tool_call(self):
        """[tool_call(name, key="value")] 格式应被正确解析。"""
        loop = self._make_parser()
        parsed = loop._parse_llm_output(
            '[tool_call(web_search, query="python教程")]'
        )
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["tool"], "web_search")
        self.assertEqual(parsed["args"]["query"], "python教程")


if __name__ == "__main__":
    unittest.main()
