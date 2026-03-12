"""增强的知识库和记忆库召回系统

优化召回策略：
1. 多阶段召回：关键词 → 语义 → 时间衰减
2. 重排序：相关性 + 新鲜度 + 用户偏好
3. 上下文感知：根据对话历史调整召回权重
"""
from __future__ import annotations

import math
import re
import time
from collections import Counter
from dataclasses import dataclass
from typing import Any

from utils.text import normalize_text, tokenize


@dataclass(slots=True)
class RecallResult:
    """召回结果"""
    entry_id: int
    title: str
    content: str
    category: str
    source: str
    score: float  # 综合得分
    relevance_score: float  # 相关性得分
    freshness_score: float  # 新鲜度得分
    created_at: float
    tags: list[str]


class EnhancedRecallSystem:
    """增强的召回系统"""

    def __init__(self):
        self._query_history: list[tuple[str, float]] = []  # (query, timestamp)
        self._max_history = 100

    def recall(
        self,
        query: str,
        knowledge_entries: list[Any],
        memory_entries: list[Any],
        conversation_history: list[dict[str, Any]] | None = None,
        limit: int = 10,
        min_score: float = 0.3,
    ) -> list[RecallResult]:
        """增强召回

        Args:
            query: 查询文本
            knowledge_entries: 知识库条目
            memory_entries: 记忆库条目
            conversation_history: 对话历史
            limit: 返回数量
            min_score: 最小得分阈值

        Returns:
            排序后的召回结果
        """
        # 记录查询历史
        self._query_history.append((query, time.time()))
        if len(self._query_history) > self._max_history:
            self._query_history = self._query_history[-self._max_history:]

        # 提取查询关键词
        query_keywords = self._extract_keywords(query)

        # 提取对话上下文关键词
        context_keywords = self._extract_context_keywords(conversation_history)

        # 合并所有条目
        all_entries = []
        for entry in knowledge_entries:
            all_entries.append(("knowledge", entry))
        for entry in memory_entries:
            all_entries.append(("memory", entry))

        # 计算每个条目的得分
        results: list[RecallResult] = []
        now = time.time()

        for source_type, entry in all_entries:
            # 提取条目信息
            entry_id = getattr(entry, "id", 0)
            title = normalize_text(str(getattr(entry, "title", "")))
            content = normalize_text(str(getattr(entry, "content", "")))
            category = normalize_text(str(getattr(entry, "category", "")))
            source = normalize_text(str(getattr(entry, "source", "")))
            created_at = float(getattr(entry, "created_at", 0))
            tags = getattr(entry, "tags", [])

            # 计算相关性得分
            relevance_score = self._calculate_relevance(
                query=query,
                query_keywords=query_keywords,
                context_keywords=context_keywords,
                title=title,
                content=content,
                tags=tags,
            )

            # 计算新鲜度得分
            freshness_score = self._calculate_freshness(
                created_at=created_at,
                now=now,
                category=category,
            )

            # 计算综合得分
            score = self._calculate_final_score(
                relevance_score=relevance_score,
                freshness_score=freshness_score,
                source_type=source_type,
                category=category,
            )

            if score >= min_score:
                results.append(RecallResult(
                    entry_id=entry_id,
                    title=title,
                    content=content,
                    category=category,
                    source=source,
                    score=score,
                    relevance_score=relevance_score,
                    freshness_score=freshness_score,
                    created_at=created_at,
                    tags=tags if isinstance(tags, list) else [],
                ))

        # 排序并返回
        results.sort(key=lambda x: x.score, reverse=True)
        return results[:limit]

    def _extract_keywords(self, text: str) -> set[str]:
        """提取关键词"""
        tokens = tokenize(normalize_text(text))
        # 过滤停用词和短词
        keywords = {
            token for token in tokens
            if len(token) >= 2 and not self._is_stop_word(token)
        }
        return keywords

    def _extract_context_keywords(
        self,
        conversation_history: list[dict[str, Any]] | None,
        max_messages: int = 5,
    ) -> set[str]:
        """从对话历史提取上下文关键词"""
        if not conversation_history:
            return set()

        keywords: set[str] = set()
        recent_messages = conversation_history[-max_messages:]

        for msg in recent_messages:
            content = normalize_text(str(msg.get("content", "")))
            msg_keywords = self._extract_keywords(content)
            keywords.update(msg_keywords)

        return keywords

    def _calculate_relevance(
        self,
        query: str,
        query_keywords: set[str],
        context_keywords: set[str],
        title: str,
        content: str,
        tags: list[str],
    ) -> float:
        """计算相关性得分"""
        score = 0.0

        # 1. 标题匹配（权重最高）
        title_lower = title.lower()
        query_lower = query.lower()

        if query_lower in title_lower:
            score += 1.0
        elif any(kw in title_lower for kw in query_keywords):
            score += 0.6

        # 2. 内容匹配
        content_lower = content.lower()
        if query_lower in content_lower:
            score += 0.5

        # 关键词匹配
        content_keywords = self._extract_keywords(content)
        keyword_overlap = len(query_keywords & content_keywords)
        if query_keywords:
            score += 0.4 * (keyword_overlap / len(query_keywords))

        # 3. 标签匹配
        tags_lower = {normalize_text(str(tag)).lower() for tag in tags}
        if any(kw in tags_lower for kw in query_keywords):
            score += 0.3

        # 4. 上下文关键词匹配（加权较低）
        if context_keywords:
            context_overlap = len(context_keywords & content_keywords)
            score += 0.2 * (context_overlap / len(context_keywords))

        return min(score, 2.0)  # 限制最大值

    def _calculate_freshness(
        self,
        created_at: float,
        now: float,
        category: str,
    ) -> float:
        """计算新鲜度得分（时间衰减）"""
        if created_at <= 0:
            return 0.5  # 默认中等新鲜度

        age_seconds = now - created_at
        age_days = age_seconds / 86400

        # 不同类别的衰减速度不同
        if category in {"trend", "meme"}:
            # 热搜和热梗衰减快
            half_life_days = 3
        elif category in {"fact", "wiki", "learned"}:
            # 事实知识衰减慢
            half_life_days = 90
        else:
            # 默认衰减速度
            half_life_days = 30

        # 指数衰减
        freshness = math.exp(-age_days / half_life_days)
        return freshness

    def _calculate_final_score(
        self,
        relevance_score: float,
        freshness_score: float,
        source_type: str,
        category: str,
    ) -> float:
        """计算最终得分"""
        # 基础权重
        relevance_weight = 0.7
        freshness_weight = 0.3

        # 根据类别调整权重
        if category in {"trend", "meme"}:
            # 热搜和热梗更看重新鲜度
            relevance_weight = 0.5
            freshness_weight = 0.5
        elif category in {"fact", "wiki"}:
            # 事实知识更看重相关性
            relevance_weight = 0.8
            freshness_weight = 0.2

        # 计算加权得分
        score = (
            relevance_score * relevance_weight +
            freshness_score * freshness_weight
        )

        # 记忆库条目略微加权（更个性化）
        if source_type == "memory":
            score *= 1.1

        return score

    def _is_stop_word(self, word: str) -> bool:
        """判断是否是停用词"""
        # 简化的停用词列表
        stop_words = {
            "的", "了", "是", "在", "我", "你", "他", "她", "它",
            "这", "那", "有", "个", "和", "与", "或", "但", "吗",
            "呢", "吧", "啊", "哦", "嗯", "哈", "呀",
        }
        return word in stop_words

    def get_query_trends(self, window_seconds: int = 3600) -> Counter[str]:
        """获取查询趋势（最近的热门查询）"""
        now = time.time()
        cutoff = now - window_seconds

        recent_queries = [
            query for query, timestamp in self._query_history
            if timestamp >= cutoff
        ]

        # 提取关键词并统计
        keyword_counter: Counter[str] = Counter()
        for query in recent_queries:
            keywords = self._extract_keywords(query)
            keyword_counter.update(keywords)

        return keyword_counter
