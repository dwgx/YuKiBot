from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from utils.text import normalize_text


def _safe_text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    return ""


def _safe_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    return {}


@dataclass(slots=True)
class PromptPolicy:
    enable: bool
    low_iq_mode: bool
    global_prefix: str
    global_suffix: str
    persona_override: str
    channels: dict[str, dict[str, str]]
    tool_injections: dict[str, str]

    @classmethod
    def from_config(cls, config: dict[str, Any] | None) -> "PromptPolicy":
        raw = config if isinstance(config, dict) else {}
        pc = _safe_dict(raw.get("prompt_control"))
        ch = _safe_dict(pc.get("channels"))
        ti = _safe_dict(pc.get("tool_injections"))

        channels: dict[str, dict[str, str]] = {}
        for name, payload in ch.items():
            if not isinstance(name, str):
                continue
            data = _safe_dict(payload)
            channels[name.strip().lower()] = {
                "prefix": _safe_text(data.get("prefix")),
                "suffix": _safe_text(data.get("suffix")),
            }

        tool_injections: dict[str, str] = {}
        for key, value in ti.items():
            if not isinstance(key, str):
                continue
            text = _safe_text(value)
            if text:
                tool_injections[key.strip().lower()] = text

        return cls(
            enable=bool(pc.get("enable", True)),
            low_iq_mode=bool(pc.get("low_iq_mode", False)),
            global_prefix=_safe_text(pc.get("global_prefix")),
            global_suffix=_safe_text(pc.get("global_suffix")),
            persona_override=_safe_text(pc.get("persona_override")),
            channels=channels,
            tool_injections=tool_injections,
        )

    def compose_prompt(self, channel: str, base_prompt: str, tool_name: str = "") -> str:
        base = _safe_text(base_prompt)
        if not self.enable:
            return base

        channel_key = normalize_text(channel).lower()
        channel_cfg = self.channels.get(channel_key, {})
        channel_prefix = _safe_text(channel_cfg.get("prefix"))
        channel_suffix = _safe_text(channel_cfg.get("suffix"))
        tool_hint = self.get_tool_injection(tool_name) if tool_name else ""
        low_iq_hint = self._low_iq_hint(channel_key)

        parts = [
            self.global_prefix,
            channel_prefix,
            low_iq_hint,
            tool_hint,
            base,
            channel_suffix,
            self.global_suffix,
        ]
        return "\n\n".join(item for item in parts if item)

    def get_tool_injection(self, tool_name: str) -> str:
        if not self.enable:
            return ""
        raw = normalize_text(tool_name).lower()
        if not raw:
            return self.tool_injections.get("*", "")
        candidates = [
            raw,
            raw.replace(".", "_"),
        ]
        for key in candidates:
            text = self.tool_injections.get(key)
            if text:
                return text
        return self.tool_injections.get("*", "")

    def build_tool_guidance_block(self) -> str:
        if not self.enable:
            return ""
        rows: list[str] = []
        for key, text in sorted(self.tool_injections.items()):
            if not text:
                continue
            name = key.replace("_", ".") if "." not in key and key != "*" else key
            rows.append(f"- {name}: {text}")
        return "\n".join(rows)

    def _low_iq_hint(self, channel: str) -> str:
        if not self.low_iq_mode:
            return ""
        hints = {
            "router": (
                "低智商模型强化规则:\n"
                "- 只能输出单个 JSON 对象，禁止 Markdown、代码块、解释文字\n"
                "- 缺字段时填默认值，不得省略 should_handle/action/reason/confidence"
            ),
            "agent": (
                "低智商模型强化规则:\n"
                "- 每轮只允许调用一个工具\n"
                "- 输出必须是 {\"tool\":\"...\",\"args\":{...}} 格式\n"
                "- 任务完成时必须调用 final_answer，text 不能为空"
            ),
            "thinking": (
                "低智商模型强化规则:\n"
                "- 先结论，再给 1-3 条关键点\n"
                "- 不确定就明确写“不确定”，禁止编造"
            ),
            "vision": (
                "低智商模型强化规则:\n"
                "- 只描述看得见的内容，不臆测\n"
                "- 先给结论，再给证据点"
            ),
            "video": (
                "低智商模型强化规则:\n"
                "- 先描述画面，再补充可见文字和动作\n"
                "- 不确定的内容要标注“疑似”"
            ),
        }
        return hints.get(channel, "")
