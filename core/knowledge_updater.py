from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from utils.text import clip_text, normalize_text


@dataclass(slots=True)
class KnowledgeCandidate:
    title: str
    content: str
    confidence: float
    is_correction: bool = False


class KnowledgeUpdater:
    """LLM-first chat-to-knowledge updater."""

    _TOOL_ECHO_CUES = ("[cq:", '"tool"', '"tool_result"', "http://", "https://")

    def __init__(self, knowledge_base: Any, config: dict[str, Any], logger: Any, model_client: Any = None):
        self.knowledge_base = knowledge_base
        self.logger = logger
        self.model_client = model_client
        control = config.get("control", {}) if isinstance(config, dict) else {}
        knowledge_cfg = config.get("knowledge_update", {}) if isinstance(config, dict) else {}
        if not isinstance(knowledge_cfg, dict):
            knowledge_cfg = {}
        learning_mode = normalize_text(str(control.get("knowledge_learning", "aggressive"))).lower()
        self.heuristic_rules_enable = bool(control.get("heuristic_rules_enable", False))
        self.enable = learning_mode in {"aggressive", "auto", "on", "true", "1"}
        self.min_confidence = max(0.0, min(1.0, float(control.get("knowledge_min_confidence", 0.62))))
        self.max_per_turn = max(1, int(control.get("knowledge_max_per_turn", 6)))
        self.require_explicit_user_fact = bool(control.get("knowledge_require_explicit_user_fact", True))
        self.block_speculative_facts = bool(control.get("knowledge_block_speculative", True))
        self.block_tool_echo = bool(control.get("knowledge_block_tool_echo", True))
        explicit_fact_cues = self._normalize_text_list(knowledge_cfg.get("explicit_fact_cues", [])) if self.heuristic_rules_enable else []
        speculative_cues = self._normalize_text_list(knowledge_cfg.get("speculative_cues", [])) if self.heuristic_rules_enable else []
        fragment_short_cues = self._normalize_text_list(knowledge_cfg.get("fragment_short_cues", [])) if self.heuristic_rules_enable else []
        self.explicit_fact_cues = tuple(explicit_fact_cues)
        self.speculative_cues = tuple(speculative_cues)
        self.fragment_only_texts = (
            set(self._normalize_text_list(knowledge_cfg.get("fragment_only_texts", [])))
            if self.heuristic_rules_enable
            else set()
        )
        self.fragment_short_cues = tuple(fragment_short_cues)
        self.fragment_short_max_len = max(0, int(knowledge_cfg.get("fragment_short_max_len", 8))) if self.heuristic_rules_enable else 0
        self.question_prefixes = tuple(self._normalize_text_list(knowledge_cfg.get("question_prefixes", []))) if self.heuristic_rules_enable else ()
        self.question_tokens = tuple(self._normalize_text_list(knowledge_cfg.get("question_tokens", []))) if self.heuristic_rules_enable else ()
        self.name_preference_patterns = (
            self._compile_regex_list(
                values=knowledge_cfg.get("name_preference_patterns", []),
                key_name="knowledge_update.name_preference_patterns",
            )
            if self.heuristic_rules_enable
            else ()
        )
        self.name_preference_blocklist = (
            tuple(self._normalize_text_list(knowledge_cfg.get("name_preference_blocklist", [])))
            if self.heuristic_rules_enable
            else ()
        )
        self.name_preference_block_patterns = (
            self._compile_regex_list(
                values=knowledge_cfg.get("name_preference_block_patterns", []),
                key_name="knowledge_update.name_preference_block_patterns",
            )
            if self.heuristic_rules_enable
            else ()
        )
        self.invalid_fact_titles = (
            set(self._normalize_text_list(knowledge_cfg.get("invalid_fact_titles", [])))
            if self.heuristic_rules_enable
            else set()
        )
        self.invalid_fact_title_patterns = (
            self._compile_regex_list(
                values=knowledge_cfg.get("invalid_fact_title_patterns", []),
                key_name="knowledge_update.invalid_fact_title_patterns",
            )
            if self.heuristic_rules_enable
            else ()
        )
        self.llm_extractor_enable = bool(knowledge_cfg.get("llm_extractor_enable", True))
        self.llm_timeout_seconds = max(6, min(45, int(knowledge_cfg.get("llm_timeout_seconds", 18))))

    @staticmethod
    def _normalize_text_list(values: Any) -> list[str]:
        if isinstance(values, str):
            values = [values]
        if not isinstance(values, list):
            return []
        out: list[str] = []
        for item in values:
            text = normalize_text(str(item))
            if text:
                out.append(text)
        return out

    def _compile_regex_list(self, values: Any, key_name: str) -> tuple[re.Pattern[str], ...]:
        patterns: list[re.Pattern[str]] = []
        for raw in self._normalize_text_list(values):
            try:
                patterns.append(re.compile(raw))
            except re.error as exc:
                if self.logger is not None:
                    self.logger.warning("knowledge_regex_invalid | key=%s | pattern=%s | err=%s", key_name, raw, exc)
        return tuple(patterns)

    def _looks_like_tool_echo(self, text: str) -> bool:
        low = normalize_text(text).lower()
        if "{" in low and "}" in low and ":" in low and '"' in low:
            return True
        return any(cue in low for cue in self._TOOL_ECHO_CUES)

    def _contains_speculative_cue(self, text: str) -> bool:
        if not self.heuristic_rules_enable:
            return False
        return any(cue in text for cue in self.speculative_cues)

    def _is_explicit_user_fact_text(self, text: str) -> bool:
        if not self.heuristic_rules_enable:
            return False
        return any(cue in text for cue in self.explicit_fact_cues)

    def _is_question_like_text(self, text: str) -> bool:
        if not self.heuristic_rules_enable:
            return False
        t = normalize_text(text)
        if not t:
            return False
        if any(t.startswith(prefix) for prefix in self.question_prefixes):
            return True
        if t.endswith(("吗", "呢", "？", "?")):
            return True
        if self.question_tokens and len(t) <= 40 and any(token in t for token in self.question_tokens):
            return True
        return False

    def _looks_like_noise_fact(self, text: str) -> bool:
        t = normalize_text(text)
        if not t:
            return True
        if len(t) <= 1:
            return True
        if self._is_question_like_text(t):
            return True
        return False

    def _is_blocked_name_preference(self, value: str) -> bool:
        content = normalize_text(value)
        if not content:
            return True
        if any(token and token in content for token in self.name_preference_blocklist):
            return True
        if any(pattern.search(content) for pattern in self.name_preference_block_patterns):
            return True
        return False

    def _is_fragment_like_statement(self, text: str) -> bool:
        if not self.heuristic_rules_enable:
            return False
        content = normalize_text(text)
        if not content:
            return True
        if content in self.fragment_only_texts:
            return True
        if self.fragment_short_max_len > 0 and len(content) <= self.fragment_short_max_len:
            if any(cue in content for cue in self.fragment_short_cues):
                return True
        return False

    def _is_valid_fact_title(self, title: str) -> bool:
        t = normalize_text(title)
        if not t:
            return False
        if len(t) < 2 or len(t) > 24:
            return False
        if t in self.invalid_fact_titles:
            return False
        if any(p.search(t) for p in self.invalid_fact_title_patterns):
            return False
        return True

    @staticmethod
    def _clean_title(title: str) -> str:
        return normalize_text(title)

    def _can_use_llm_extractor(self) -> bool:
        client = self.model_client
        return bool(
            self.llm_extractor_enable
            and client is not None
            and bool(getattr(client, "enabled", False))
        )

    async def _extract_candidates_llm(
        self,
        user_text: str,
        user_id: str,
        conversation_id: str,
    ) -> list[KnowledgeCandidate]:
        text = normalize_text(user_text)
        uid = normalize_text(user_id)
        if not text or not uid:
            return []
        if not self._can_use_llm_extractor():
            return []
        if self._is_fragment_like_statement(text):
            return []
        if self.block_tool_echo and self._looks_like_tool_echo(text):
            return []

        system_prompt = (
            "你是聊天知识抽取器。"
            "任务：从用户单条消息中抽取“可入库的稳定事实/偏好/更正”。"
            "只输出 JSON。格式："
            '{"items":[{"kind":"fact|preference|music_preference|preferred_name|correction","title":"...","content":"...","confidence":0.0,"is_correction":false}]}\n'
            "规则：\n"
            "1. 问句、口水话、工具回显、链接堆砌、猜测语气(可能/也许/好像等)默认不抽取。\n"
            "2. 偏好类 title 用语义标题，不要写 user 前缀。\n"
            "3. correction 表示“同一title的新值覆盖旧值”。\n"
            "4. 不确定就返回空数组。"
        )
        payload = {
            "conversation_id": conversation_id,
            "user_id": uid,
            "text": text,
        }
        try:
            raw = await asyncio.wait_for(
                self.model_client.chat_json(
                    [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
                    ]
                ),
                timeout=float(self.llm_timeout_seconds),
            )
        except Exception as exc:
            if self.logger is not None:
                self.logger.debug("knowledge_llm_extract_fail | user=%s | err=%s", uid, exc)
            return []

        rows = raw.get("items", []) if isinstance(raw, dict) else []
        if not isinstance(rows, list):
            return []

        out: list[KnowledgeCandidate] = []
        for item in rows[: max(1, self.max_per_turn * 2)]:
            if not isinstance(item, dict):
                continue
            kind = normalize_text(str(item.get("kind", "fact"))).lower() or "fact"
            content = clip_text(normalize_text(str(item.get("content", ""))), 160)
            if not content or self._looks_like_noise_fact(content):
                continue
            if self.block_speculative_facts and self._contains_speculative_cue(content):
                continue
            if self._is_question_like_text(content):
                continue

            is_correction = bool(item.get("is_correction", False) or kind == "correction")
            confidence_raw = item.get("confidence", 0.76)
            try:
                confidence = float(confidence_raw)
            except (TypeError, ValueError):
                confidence = 0.76
            confidence = max(0.0, min(1.0, confidence))

            if kind == "preferred_name":
                preferred = clip_text(content, 24)
                if self._is_blocked_name_preference(preferred):
                    continue
                out.append(
                    KnowledgeCandidate(
                        title=f"user:{uid}:preferred_name",
                        content=preferred,
                        confidence=max(confidence, 0.86),
                        is_correction=False,
                    )
                )
                continue

            if kind in {"preference", "music_preference"}:
                pref_title = (
                    f"user:{uid}:music_preference"
                    if kind == "music_preference"
                    else f"user:{uid}:preference"
                )
                out.append(
                    KnowledgeCandidate(
                        title=pref_title,
                        content=clip_text(content, 80),
                        confidence=max(confidence, 0.74),
                        is_correction=False,
                    )
                )
                continue

            raw_title = clip_text(normalize_text(str(item.get("title", ""))), 40)
            title = clip_text(self._clean_title(raw_title), 40)
            if not self._is_valid_fact_title(title):
                continue
            if self._looks_like_noise_fact(title):
                continue
            out.append(
                KnowledgeCandidate(
                    title=title,
                    content=content,
                    confidence=max(confidence, 0.7),
                    is_correction=is_correction,
                )
            )

        dedup: dict[str, KnowledgeCandidate] = {}
        for item in out:
            prev = dedup.get(item.title)
            if prev is None or item.confidence > prev.confidence or item.is_correction:
                dedup[item.title] = item
        return list(dedup.values())[: self.max_per_turn]

    def _persist_candidates(
        self,
        *,
        conversation_id: str,
        user_id: str,
        candidates: list[KnowledgeCandidate],
        timestamp: datetime | None = None,
    ) -> dict[str, int]:
        if not candidates:
            return {"candidates": 0, "saved": 0, "updated": 0}
        now = (timestamp or datetime.now(timezone.utc)).timestamp()
        saved = 0
        updated = 0
        for item in candidates:
            if item.confidence < self.min_confidence:
                continue
            source = "user_correction" if item.is_correction else "chat_auto"
            extra = {
                "confidence": item.confidence,
                "conversation_id": conversation_id,
                "user_id": user_id,
                "updated_at": now,
                "source_type": source,
            }
            res = self.knowledge_base.upsert_conflict_checked(
                category="learned",
                title=item.title,
                content=item.content,
                source=source,
                tags=[
                    "chat",
                    f"conv:{conversation_id}",
                    f"user:{user_id}",
                ]
                + (["user_profile"] if item.title.startswith(f"user:{user_id}:") else []),
                extra=extra,
                confidence=item.confidence,
                update_mode="auto",
                mark_correction=item.is_correction,
            )
            if res.get("action") == "updated":
                updated += 1
            elif res.get("action") == "inserted":
                saved += 1

        if (saved or updated) and self.logger is not None:
            self.logger.info(
                "knowledge_auto_update | conversation=%s | user=%s | candidates=%d | inserted=%d | updated=%d",
                conversation_id,
                user_id,
                len(candidates),
                saved,
                updated,
            )
        return {"candidates": len(candidates), "saved": saved, "updated": updated}

    async def update_from_turn_async(
        self,
        conversation_id: str,
        user_id: str,
        user_text: str,
        bot_reply: str = "",
        timestamp: datetime | None = None,
    ) -> dict[str, int]:
        if not self.enable or self.knowledge_base is None:
            return {"candidates": 0, "saved": 0, "updated": 0}

        llm_candidates = await self._extract_candidates_llm(
            user_text=user_text,
            user_id=user_id,
            conversation_id=conversation_id,
        )
        candidates = llm_candidates

        if candidates:
            dedup: dict[str, KnowledgeCandidate] = {}
            for item in candidates:
                prev = dedup.get(item.title)
                if prev is None or item.confidence > prev.confidence or item.is_correction:
                    dedup[item.title] = item
            candidates = list(dedup.values())[: self.max_per_turn]

        return self._persist_candidates(
            conversation_id=conversation_id,
            user_id=user_id,
            candidates=candidates,
            timestamp=timestamp,
        )

    def update_from_turn(
        self,
        conversation_id: str,
        user_id: str,
        user_text: str,
        bot_reply: str = "",
        timestamp: datetime | None = None,
    ) -> dict[str, int]:
        """同步兼容入口：内部转发到 LLM 异步提取。"""
        try:
            loop = asyncio.get_running_loop()
            if loop.is_running():
                # 避免在已运行事件循环中阻塞。主链路应使用 update_from_turn_async。
                if self.logger is not None:
                    self.logger.debug("knowledge_update_sync_called_in_event_loop")
                return {"candidates": 0, "saved": 0, "updated": 0}
        except RuntimeError:
            pass
        return asyncio.run(
            self.update_from_turn_async(
                conversation_id=conversation_id,
                user_id=user_id,
                user_text=user_text,
                bot_reply=bot_reply,
                timestamp=timestamp,
            )
        )
