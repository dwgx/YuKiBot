"""上下文感知的高风险操作确认系统

根据用户意图和上下文自动判断是否需要确认：
- 明确指向的操作（"踢掉这个人"）→ 直接执行
- 模糊批量操作（"踢掉很多人"）→ 需要确认
- 异常操作（管理员突然要求大量操作）→ 需要确认
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Any

from utils.text import normalize_text


@dataclass(slots=True)
class ConfirmationDecision:
    """确认决策结果"""
    requires_confirmation: bool
    reason: str
    confidence: float  # 0.0-1.0，置信度
    suggested_prompt: str = ""


class ContextAwareConfirmation:
    """上下文感知的确认系统"""

    # 明确指向的操作模式（不需要确认）
    EXPLICIT_PATTERNS = [
        r"踢掉?\s*(?:这个人|他|她|ta|@\S+|\d+)",
        r"禁言\s*(?:这个人|他|她|ta|@\S+|\d+)",
        r"删除?\s*(?:这条|这个|这些)\s*(?:消息|记录)",
        r"拉黑\s*(?:这个|这些)\s*(?:人|用户|群)",
        r"移除\s*(?:这个|这些)\s*(?:人|用户|成员)",
    ]

    # 批量/模糊操作模式（需要确认）
    BATCH_PATTERNS = [
        r"踢掉?\s*(?:所有|全部|很多|一堆|这些|全)",
        r"禁言\s*(?:所有|全部|很多|一堆|这些|全)",
        r"删除?\s*(?:所有|全部|很多|一堆)\s*(?:消息|记录|人)",
        r"清空|清理|批量",
    ]

    # 异常操作关键词（需要确认）
    ANOMALY_KEYWORDS = [
        "全部", "所有", "一起", "批量", "大量", "很多",
        "清空", "清理", "删光", "踢光", "全踢",
    ]

    def __init__(self):
        self._explicit_re = [re.compile(p, re.IGNORECASE) for p in self.EXPLICIT_PATTERNS]
        self._batch_re = [re.compile(p, re.IGNORECASE) for p in self.BATCH_PATTERNS]
        self._recent_operations: dict[str, list[dict[str, Any]]] = {}

    def should_confirm(
        self,
        tool_name: str,
        tool_args: dict[str, Any],
        user_message: str,
        conversation_id: str,
        user_id: str,
        is_admin: bool = False,
    ) -> ConfirmationDecision:
        """判断是否需要确认

        Args:
            tool_name: 工具名称
            tool_args: 工具参数
            user_message: 用户原始消息
            conversation_id: 会话 ID
            user_id: 用户 ID
            is_admin: 是否是管理员

        Returns:
            ConfirmationDecision: 确认决策
        """
        msg = normalize_text(user_message).lower()

        # 1. 检查是否是明确指向的操作
        if self._is_explicit_operation(msg, tool_args):
            return ConfirmationDecision(
                requires_confirmation=False,
                reason="explicit_target",
                confidence=0.9,
            )

        # 2. 检查是否是批量操作
        if self._is_batch_operation(msg, tool_args):
            return ConfirmationDecision(
                requires_confirmation=True,
                reason="batch_operation",
                confidence=0.85,
                suggested_prompt=self._build_batch_confirm_prompt(tool_name, tool_args),
            )

        # 3. 检查操作频率异常
        if self._is_anomalous_frequency(tool_name, conversation_id, user_id):
            return ConfirmationDecision(
                requires_confirmation=True,
                reason="anomalous_frequency",
                confidence=0.8,
                suggested_prompt=f"你在短时间内多次执行 {tool_name}，请确认是否继续？",
            )

        # 4. 检查是否包含异常关键词
        if self._contains_anomaly_keywords(msg):
            return ConfirmationDecision(
                requires_confirmation=True,
                reason="anomaly_keywords",
                confidence=0.75,
                suggested_prompt=f"检测到批量操作关键词，请确认是否执行 {tool_name}？",
            )

        # 5. 默认：管理员的单个明确操作不需要确认
        if is_admin and self._is_single_target_operation(tool_args):
            return ConfirmationDecision(
                requires_confirmation=False,
                reason="admin_single_target",
                confidence=0.7,
            )

        # 6. 其他情况：需要确认
        return ConfirmationDecision(
            requires_confirmation=True,
            reason="default_safe",
            confidence=0.6,
            suggested_prompt=f"请确认是否执行 {tool_name}？",
        )

    def _is_explicit_operation(self, message: str, tool_args: dict[str, Any]) -> bool:
        """检查是否是明确指向的操作"""
        # 检查消息中是否有明确指向模式
        for pattern in self._explicit_re:
            if pattern.search(message):
                return True

        # 检查参数中是否有明确的单个目标
        if self._is_single_target_operation(tool_args):
            # 如果参数明确且消息中没有批量关键词，认为是明确操作
            if not any(kw in message for kw in self.ANOMALY_KEYWORDS):
                return True

        return False

    def _is_batch_operation(self, message: str, tool_args: dict[str, Any]) -> bool:
        """检查是否是批量操作"""
        # 检查消息中是否有批量操作模式
        for pattern in self._batch_re:
            if pattern.search(message):
                return True

        # 检查参数中是否有多个目标
        if isinstance(tool_args, dict):
            for key in ["user_ids", "target_user_ids", "group_ids"]:
                value = tool_args.get(key)
                if isinstance(value, list) and len(value) > 1:
                    return True

        return False

    def _is_single_target_operation(self, tool_args: dict[str, Any]) -> bool:
        """检查是否是单目标操作"""
        if not isinstance(tool_args, dict):
            return False

        # 检查是否有单个明确的目标参数
        single_target_keys = ["user_id", "target_user_id", "group_id", "message_id"]
        for key in single_target_keys:
            value = normalize_text(str(tool_args.get(key, "")))
            if value and value != "0":
                return True

        return False

    def _contains_anomaly_keywords(self, message: str) -> bool:
        """检查是否包含异常关键词"""
        return any(kw in message for kw in self.ANOMALY_KEYWORDS)

    def _is_anomalous_frequency(
        self,
        tool_name: str,
        conversation_id: str,
        user_id: str,
        window_seconds: int = 60,
        threshold: int = 3,
    ) -> bool:
        """检查操作频率是否异常"""
        key = f"{conversation_id}:{user_id}"
        now = time.time()

        # 清理过期记录
        if key in self._recent_operations:
            self._recent_operations[key] = [
                op for op in self._recent_operations[key]
                if now - op["timestamp"] < window_seconds
            ]

        # 记录当前操作
        if key not in self._recent_operations:
            if len(self._recent_operations) > 5000:
                self._recent_operations.clear()
            self._recent_operations[key] = []

        self._recent_operations[key].append({
            "tool_name": tool_name,
            "timestamp": now,
        })

        # 检查同类操作频率
        same_tool_count = sum(
            1 for op in self._recent_operations[key]
            if op["tool_name"] == tool_name
        )

        return same_tool_count > threshold

    def _build_batch_confirm_prompt(self, tool_name: str, tool_args: dict[str, Any]) -> str:
        """构建批量操作确认提示"""
        targets = []
        if isinstance(tool_args, dict):
            for key in ["user_ids", "target_user_ids", "group_ids"]:
                value = tool_args.get(key)
                if isinstance(value, list) and value:
                    targets.append(f"{len(value)} 个目标")
                    break

        target_desc = targets[0] if targets else "多个目标"
        return f"这是批量操作：{tool_name}（{target_desc}）。请确认是否执行？"

    def clear_history(self, conversation_id: str | None = None, user_id: str | None = None) -> None:
        """清理操作历史"""
        if conversation_id and user_id:
            key = f"{conversation_id}:{user_id}"
            self._recent_operations.pop(key, None)
        elif conversation_id:
            # 清理整个会话的历史
            keys_to_remove = [k for k in self._recent_operations if k.startswith(f"{conversation_id}:")]
            for key in keys_to_remove:
                self._recent_operations.pop(key, None)
        else:
            # 清理所有历史
            self._recent_operations.clear()
