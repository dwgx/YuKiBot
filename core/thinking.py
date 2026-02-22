from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from core.personality import PersonalityEngine
from services.skiapi import SkiAPIClient


@dataclass(slots=True)
class ThinkingInput:
    text: str
    trigger_reason: str
    sensitive_context: str
    memory_context: list[str]
    related_memories: list[str]


@dataclass(slots=True)
class ThinkingDecision:
    action: str
    reason: str
    reply_style: str = "casual"
    query: str = ""
    prompt: str = ""


class ThinkingEngine:
    def __init__(
        self,
        config: dict[str, Any],
        personality: PersonalityEngine,
        skiapi: SkiAPIClient,
    ):
        bot_cfg = config.get("bot", {})
        search_cfg = config.get("search", {})
        image_cfg = config.get("image", {})
        self.bot_name = str(bot_cfg.get("name", "yukiko"))
        self.language = str(bot_cfg.get("language", "zh"))
        self.allow_thinking = bool(bot_cfg.get("allow_thinking", True))
        self.allow_search = bool(bot_cfg.get("allow_search", True)) and bool(
            search_cfg.get("enable", True)
        )
        self.allow_image = bool(bot_cfg.get("allow_image", True)) and bool(image_cfg.get("enable", True))
        self.personality = personality
        self.skiapi = skiapi

    async def decide(self, payload: ThinkingInput) -> ThinkingDecision:
        fallback = self._rule_decide(payload)
        if not self.allow_thinking or not self.skiapi.enabled:
            return fallback

        try:
            llm_decision = await self._llm_decide(payload, fallback)
        except Exception:
            return fallback
        return llm_decision or fallback

    def _rule_decide(self, payload: ThinkingInput) -> ThinkingDecision:
        text = payload.text.strip()
        lower = text.lower()
        if not text:
            return ThinkingDecision(action="ignore", reason="empty_message", reply_style="short")

        if self.allow_image:
            if lower.startswith("/draw "):
                return ThinkingDecision(
                    action="generate_image",
                    reason="draw_command",
                    reply_style="short",
                    prompt=text[6:].strip(),
                )
            if any(keyword in lower for keyword in ("画一张", "来张图", "帮我画", "生成图片")):
                return ThinkingDecision(
                    action="generate_image",
                    reason="image_request",
                    reply_style="casual",
                    prompt=text,
                )

        search_cues = (
            "搜索",
            "查一下",
            "搜一下",
            "最新",
            "新闻",
            "资料",
            "官网",
            "教程",
            "天气",
            "价格",
        )
        if self.allow_search and any(cue in text for cue in search_cues):
            return ThinkingDecision(
                action="search",
                reason="search_cue",
                reply_style="serious",
                query=text,
            )

        if payload.trigger_reason == "random_trigger" and len(text) < 8:
            return ThinkingDecision(action="ignore", reason="random_skip", reply_style="short")

        if len(text) < 20:
            style = "short"
        elif any(word in text for word in ("方案", "架构", "设计", "风险", "问题")):
            style = "serious"
        elif len(text) > 120:
            style = "long"
        else:
            style = "casual"
        return ThinkingDecision(action="reply", reason="default_reply", reply_style=style)

    async def _llm_decide(
        self,
        payload: ThinkingInput,
        fallback: ThinkingDecision,
    ) -> ThinkingDecision | None:
        allowed_actions = ["reply", "ignore"]
        if self.allow_search:
            allowed_actions.append("search")
        if self.allow_image:
            allowed_actions.append("generate_image")

        system_prompt = (
            "你是 Yukiko 的决策器。请在内部思考，不要暴露思维链。"
            "只输出 JSON。\n"
            "输出格式：\n"
            '{"action":"reply|search|generate_image|ignore","reason":"...","reply_style":"casual|serious|short|long","query":"...","prompt":"..."}\n'
            "规则：\n"
            f"- 允许动作：{', '.join(allowed_actions)}\n"
            "- 随机弱触发优先选择 ignore。\n"
            "- 仅在需要外部或时效信息时使用 search。\n"
            "- 明确绘图意图时使用 generate_image。\n"
            "- reason 保持简短。"
        )
        user_payload = {
            "message": payload.text,
            "trigger_reason": payload.trigger_reason,
            "sensitive_context": payload.sensitive_context,
            "memory_context": payload.memory_context[-6:],
            "related_memories": payload.related_memories[:4],
            "fallback": fallback.__dict__,
        }
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
        ]
        data = await self.skiapi.chat_json(messages)
        if not data:
            return None

        action = str(data.get("action", fallback.action)).strip()
        if action not in allowed_actions:
            action = fallback.action
        reply_style = str(data.get("reply_style", fallback.reply_style)).strip()
        if reply_style not in {"casual", "serious", "short", "long"}:
            reply_style = fallback.reply_style
        reason = str(data.get("reason", fallback.reason)).strip() or fallback.reason
        query = str(data.get("query", fallback.query)).strip()
        prompt = str(data.get("prompt", fallback.prompt)).strip()
        if action == "search" and not query:
            query = payload.text
        if action == "generate_image" and not prompt:
            prompt = payload.text
        return ThinkingDecision(
            action=action,
            reason=reason,
            reply_style=reply_style,
            query=query,
            prompt=prompt,
        )

    async def generate_reply(
        self,
        user_text: str,
        memory_context: list[str],
        related_memories: list[str],
        reply_style: str,
        search_summary: str = "",
        sensitive_context: str = "",
    ) -> str:
        if self.skiapi.enabled:
            try:
                prompt = self.personality.system_instruction(bot_name=self.bot_name, language=self.language)
                style = self.personality.style_instruction(reply_style)
                tools_note = (
                    "你可以参考搜索摘要和记忆信息，但不要虚构来源。"
                    "内部思考不要输出。"
                )
                messages = [
                    {
                        "role": "system",
                        "content": f"{prompt}\n{style}\n{tools_note}",
                    },
                    {
                        "role": "user",
                        "content": self._compose_reply_payload(
                            user_text=user_text,
                            memory_context=memory_context,
                            related_memories=related_memories,
                            search_summary=search_summary,
                            sensitive_context=sensitive_context,
                        ),
                    },
                ]
                output = (await self.skiapi.chat_text(messages)).strip()
                if output:
                    return output
            except Exception:
                pass
        return self._fallback_reply(user_text, search_summary, sensitive_context)

    @staticmethod
    def _compose_reply_payload(
        user_text: str,
        memory_context: list[str],
        related_memories: list[str],
        search_summary: str,
        sensitive_context: str,
    ) -> str:
        blocks = [f"用户消息:\n{user_text}"]
        if memory_context:
            blocks.append("最近对话:\n" + "\n".join(f"- {item}" for item in memory_context[-8:]))
        if related_memories:
            blocks.append("长期记忆检索:\n" + "\n".join(f"- {item}" for item in related_memories[:5]))
        if search_summary:
            blocks.append(f"联网搜索摘要:\n{search_summary}")
        if sensitive_context:
            blocks.append(f"敏感词上下文摘要:\n{sensitive_context}")
        blocks.append("请直接给最终回复内容，不要输出 JSON。")
        return "\n\n".join(blocks)

    @staticmethod
    def _fallback_reply(user_text: str, search_summary: str, sensitive_context: str) -> str:
        if search_summary:
            lines = [line for line in search_summary.splitlines() if line.startswith(("1.", "2.", "3."))]
            brief = "\n".join(lines[:3]) if lines else "暂时没有有效搜索结果。"
            return f"我先帮你查到这些信息：\n{brief}"
        if sensitive_context:
            return "我在。这个话题我可以认真陪你聊，先告诉我你最想先解决哪一部分。"
        if re.search(r"[?？]$", user_text.strip()):
            return "我在，收到你的问题了。你可以再补一点背景，我给你更准确的答复。"
        return "收到。我在这，继续说。"
