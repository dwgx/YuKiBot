"""Auto-split from core/agent_tools.py — 知识库 + 爬虫工具"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, quote, unquote, urlencode, urljoin, urlparse, urlunparse

import httpx
from core.agent_tools_types import PromptHint, ToolCallResult, ToolSchema
from core.agent_tools_registry import AgentToolRegistry
from core.napcat_compat import call_napcat_api
from core.recalled_messages import (
    build_conversation_id as _build_recall_conversation_id,
    record_recalled_message as _record_recalled_message,
)
from utils.learning_guard import assess_preferred_name_learning, looks_like_preferred_name_knowledge
from utils.text import clip_text, normalize_matching_text, normalize_text, tokenize

_log = logging.getLogger("yukiko.agent_tools")


def _has_cross_user_profile_access(context: dict[str, Any]) -> bool:
    level = normalize_text(str(context.get("permission_level", ""))).lower()
    return level == "super_admin"


def _resolve_profile_target_user(
    args: dict[str, Any],
    context: dict[str, Any],
) -> tuple[str, ToolCallResult | None]:
    requested_user_id = normalize_text(str(args.get("user_id", "")))
    current_user_id = normalize_text(str(context.get("user_id", "")))
    if _has_cross_user_profile_access(context):
        target_user_id = requested_user_id or current_user_id
        if not target_user_id:
            return "", ToolCallResult(ok=False, error="missing_user_id")
        return target_user_id, None
    if not current_user_id:
        return "", ToolCallResult(ok=False, error="missing_user_id")
    if requested_user_id and requested_user_id != current_user_id:
        return "", ToolCallResult(
            ok=False,
            error="permission_denied:user_scope",
            display="普通用户只能读取或写入自己的画像与事实记忆。",
        )
    return current_user_id, None

def _register_crawler_tools(registry: AgentToolRegistry) -> None:
    """注册知乎/百科/热搜/知识库工具。"""

    registry.register(
        ToolSchema(
            name="get_hot_trends",
            description=(
                "获取全网热搜热榜: 微博热搜、B站热门、抖音热榜、百度热搜。\n"
                "可指定平台(weibo/bilibili/douyin/baidu)或不指定获取全部。\n"
                "使用场景: 用户问最近有什么热点/新闻/热搜时使用。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "platform": {
                        "type": "string",
                        "description": "平台(可选): weibo/bilibili/douyin/baidu，不填获取全部",
                    },
                    "limit": {"type": "integer", "description": "每个平台返回条数(默认10)"},
                },
                "required": [],
            },
            category="search",
        ),
        _handle_get_hot_trends,
    )

    registry.register(
        ToolSchema(
            name="search_zhihu",
            description=(
                "搜索知乎内容或获取知乎热榜。\n"
                "mode=hot 获取热榜，mode=search 搜索内容，mode=answers 获取问题高赞回答。\n"
                "使用场景: 用户问知乎相关问题、想了解某个话题的讨论时使用。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "mode": {"type": "string", "description": "模式: hot(热榜)/search(搜索)/answers(回答)"},
                    "query": {"type": "string", "description": "搜索关键词(search/answers模式必填)"},
                    "question_id": {"type": "string", "description": "知乎问题ID(answers模式)"},
                },
                "required": ["mode"],
            },
            category="search",
        ),
        _handle_search_zhihu,
    )

    registry.register(
        ToolSchema(
            name="lookup_wiki",
            description=(
                "查询百科知识: 同时搜索百度百科和维基百科。\n"
                "使用场景: 用户问某个概念/人物/事件的定义或背景知识时使用。\n"
                "返回百度百科和维基百科的摘要。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "keyword": {"type": "string", "description": "要查询的关键词"},
                },
                "required": ["keyword"],
            },
            category="search",
        ),
        _handle_lookup_wiki,
    )

    registry.register(
        ToolSchema(
            name="search_knowledge",
            description=(
                "搜索知识库: 查找已学习的知识、热梗、百科、事实。\n"
                "知识库独立于对话记忆，存储持久化知识。\n"
                "category可选: fact(事实)/meme(热梗)/wiki(百科)/trend(热搜)/learned(学习)"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "搜索关键词"},
                    "category": {"type": "string", "description": "分类(可选)"},
                },
                "required": ["query"],
            },
            category="search",
        ),
        _handle_search_knowledge,
    )

    registry.register(
        ToolSchema(
            name="learn_knowledge",
            description=(
                "学习新知识: 将信息存入知识库。\n"
                "使用场景: 用户教你新知识、新梗、新概念时使用。\n"
                "category: fact(事实)/meme(热梗)/learned(学习到的)"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "知识标题/名称"},
                    "content": {"type": "string", "description": "知识内容"},
                    "category": {"type": "string", "description": "分类: fact/meme/learned"},
                    "tags": {"type": "string", "description": "标签(逗号分隔)"},
                },
                "required": ["title", "content"],
            },
            category="search",
        ),
        _handle_learn_knowledge,
    )

    async def _handle_remember_user_fact(args: dict[str, Any], context: dict[str, Any]) -> ToolCallResult:
        memory = context.get("memory_engine")
        if memory is None:
            return ToolCallResult(ok=False, error="memory_engine_unavailable")
        user_id, denied = _resolve_profile_target_user(args, context)
        if denied is not None:
            return denied
        fact = normalize_text(str(args.get("fact", "")))
        if not user_id or not fact:
            return ToolCallResult(ok=False, error="missing_user_id_or_fact")
        conversation_id = normalize_text(str(context.get("conversation_id", "")))
        ok = memory.add_user_fact(user_id, fact, conversation_id)
        if ok:
            return ToolCallResult(ok=True, display=f"已记住: {fact[:80]}", data={"user_id": user_id, "fact": fact})
        return ToolCallResult(ok=False, error="save_failed")

    registry.register(
        ToolSchema(
            name="remember_user_fact",
            description=(
                "记住关于用户的事实信息。\n"
                "使用场景: 从图片分析、对话中学到用户身份/偏好/特征时主动存储。\n"
                "例如: 用户的用户名、常用工具、职业、兴趣等。\n"
                "存储后下次对话可直接回忆，无需重新分析。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "user_id": {"type": "string", "description": "用户ID（留空则用当前对话用户）"},
                    "fact": {"type": "string", "description": "要记住的事实（如: Claude用户名=dwgx1337）"},
                },
                "required": ["fact"],
            },
            category="search",
        ),
        _handle_remember_user_fact,
    )

    async def _handle_recall_about_user(args: dict[str, Any], context: dict[str, Any]) -> ToolCallResult:
        """综合回忆关于用户的所有已知信息。"""
        memory = context.get("memory_engine")
        if memory is None:
            return ToolCallResult(ok=False, error="memory_engine_unavailable")
        user_id, denied = _resolve_profile_target_user(args, context)
        if denied is not None:
            return denied

        lines: list[str] = []

        # 1. 用户画像
        profile_summary = memory.get_user_profile_summary(user_id)
        if profile_summary:
            lines.append(f"[画像] {profile_summary}")

        # 2. 显式记忆事实
        facts = memory.get_explicit_facts(user_id, limit=10)
        if facts:
            lines.append(f"[记忆事实] " + "；".join(f[:60] for f in facts))

        # 3. 知识库中关于此用户的记录
        kb = context.get("knowledge_base")
        if kb is not None:
            try:
                kb_results = kb.search(f"user:{user_id}", category="learned", limit=8)
                if kb_results:
                    kb_items = []
                    for entry in kb_results:
                        title = normalize_text(str(getattr(entry, "title", "")))
                        content = normalize_text(str(getattr(entry, "content", "")))
                        if content:
                            kb_items.append(f"{title}: {content[:60]}" if title else content[:60])
                    if kb_items:
                        lines.append(f"[知识库] " + "；".join(kb_items))
            except Exception:
                pass

        # 4. 知识图谱 (knowledge_store)
        if hasattr(memory, "knowledge_get_user_summary"):
            try:
                ks_summary = memory.knowledge_get_user_summary(user_id, limit=10)
                if ks_summary:
                    lines.append(f"[知识图谱] {ks_summary}")
            except Exception:
                pass

        # 5. Agent policies
        policies = memory.get_agent_policies(user_id) if hasattr(memory, "get_agent_policies") else []
        if policies:
            lines.append(f"[偏好指令] " + "；".join(str(p)[:40] for p in policies[:5]))

        if not lines:
            return ToolCallResult(ok=True, display=f"暂无关于用户 {user_id} 的记录", data={"user_id": user_id})

        display = "\n".join(lines)
        return ToolCallResult(ok=True, display=display, data={"user_id": user_id, "items": len(lines)})

    registry.register(
        ToolSchema(
            name="recall_about_user",
            description=(
                "回忆关于某用户的所有已知信息。\n"
                "综合查询: 用户画像、记忆事实、知识库记录、偏好指令。\n"
                "当用户问'你记得我吗'、'你知道我是谁'时使用。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "user_id": {"type": "string", "description": "用户ID（留空则用当前对话用户）"},
                },
                "required": [],
            },
            category="search",
        ),
        _handle_recall_about_user,
    )

    async def _handle_summarize_conversation(args: dict[str, Any], context: dict[str, Any]) -> ToolCallResult:
        """主动生成当前对话摘要并存入归档。"""
        memory = context.get("memory_engine")
        if memory is None:
            return ToolCallResult(ok=False, error="memory_engine_unavailable")
        conversation_id = normalize_text(str(context.get("conversation_id", "")))
        if not conversation_id:
            return ToolCallResult(ok=False, error="no_conversation")
        limit = max(10, min(50, int(args.get("message_count", 20) or 20)))
        recent = memory.get_recent_texts(conversation_id, limit=limit)
        if not recent:
            return ToolCallResult(ok=True, display="对话为空，无需摘要")
        excerpt = "\n".join(recent)[:2000]
        summary = normalize_text(str(args.get("summary", "")))
        if not summary:
            summary = f"最近 {len(recent)} 条消息摘要（用户可通过对话补充）"
        key_facts = [normalize_text(str(f)) for f in (args.get("key_facts", []) or []) if normalize_text(str(f))]
        record_id = memory.save_conversation_summary(
            conversation_id, summary, key_facts=key_facts, message_range=f"last_{limit}"
        )
        if record_id:
            return ToolCallResult(ok=True, display=f"已保存对话摘要 (#{record_id})", data={"id": record_id})
        return ToolCallResult(ok=False, error="save_failed")

    registry.register(
        ToolSchema(
            name="summarize_conversation",
            description=(
                "生成并保存当前对话的摘要。\n"
                "用于长对话中保留关键信息，防止上下文丢失。\n"
                "摘要会在后续对话中作为历史背景注入。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "summary": {"type": "string", "description": "手动摘要文本（留空则自动标注）"},
                    "key_facts": {"type": "array", "items": {"type": "string"}, "description": "关键事实列表"},
                    "message_count": {"type": "integer", "description": "要摘要的消息数（默认20）"},
                },
                "required": [],
            },
            category="search",
        ),
        _handle_summarize_conversation,
    )


async def _handle_get_hot_trends(args: dict[str, Any], context: dict[str, Any]) -> ToolCallResult:
    crawler_hub = context.get("crawler_hub")
    if not crawler_hub:
        return ToolCallResult(ok=False, error="crawler_unavailable", display="爬虫模块未初始化")

    platform = str(args.get("platform", "")).strip().lower()
    limit = min(20, max(3, int(args.get("limit", 10) or 10)))

    try:
        if platform:
            method_map = {
                "weibo": crawler_hub.trends.weibo_hot,
                "bilibili": crawler_hub.trends.bilibili_hot,
                "douyin": crawler_hub.trends.douyin_hot,
                "baidu": crawler_hub.trends.baidu_hot,
            }
            func = method_map.get(platform)
            if not func:
                return ToolCallResult(ok=False, error=f"unknown_platform: {platform}")
            items = await func(limit)
            lines = [f"【{platform}热搜 Top{len(items)}】"]
            for i, item in enumerate(items, 1):
                heat = f" ({item.heat})" if item.heat else ""
                lines.append(f"{i}. {item.title}{heat}")
            return ToolCallResult(ok=True, data={"platform": platform, "count": len(items)},
                                display="\n".join(lines))
        else:
            trends = await crawler_hub.get_trends_cached()
            text = crawler_hub.format_trends_text(trends, limit=limit)
            # 同时存入知识库
            kb = context.get("knowledge_base")
            if kb:
                for plat, items in trends.items():
                    for item in items[:limit]:
                        kb.add("trend", item.title, item.snippet or "", source=plat,
                                tags=[plat], extra={"heat": item.heat, "url": item.url})
            return ToolCallResult(ok=True, data={"platforms": list(trends.keys())}, display=text)
    except Exception as e:
        _log.warning("get_hot_trends_error | %s", e)
        return ToolCallResult(ok=False, error=f"trends_error: {e}")


async def _handle_search_zhihu(args: dict[str, Any], context: dict[str, Any]) -> ToolCallResult:
    crawler_hub = context.get("crawler_hub")
    if not crawler_hub:
        return ToolCallResult(ok=False, error="crawler_unavailable")

    mode = str(args.get("mode", "hot")).strip().lower()
    query = str(args.get("query", "")).strip()
    question_id = str(args.get("question_id", "")).strip()

    try:
        if mode == "hot":
            items = await crawler_hub.zhihu.hot_list(limit=15)
            lines = ["【知乎热榜】"]
            for i, item in enumerate(items, 1):
                heat = f" ({item.heat})" if item.heat else ""
                lines.append(f"{i}. {item.title}{heat}")
            return ToolCallResult(ok=True, data={"count": len(items)}, display="\n".join(lines))

        elif mode == "search" and query:
            items = await crawler_hub.zhihu.search(query, limit=8)
            lines = [f"【知乎搜索: {query}】"]
            for i, item in enumerate(items, 1):
                lines.append(f"{i}. {item.title}")
                if item.snippet:
                    lines.append(f"   {clip_text(item.snippet, 100)}")
            return ToolCallResult(ok=True, data={"count": len(items)}, display="\n".join(lines))

        elif mode == "answers" and question_id:
            items = await crawler_hub.zhihu.get_top_answers(question_id, limit=3)
            lines = [f"【知乎问题 {question_id} 高赞回答】"]
            for i, item in enumerate(items, 1):
                lines.append(f"{i}. {item.title}")
                lines.append(f"   {clip_text(item.snippet, 300)}")
            return ToolCallResult(ok=True, data={"count": len(items)}, display="\n".join(lines))

        return ToolCallResult(ok=False, error="invalid mode or missing query")
    except Exception as e:
        _log.warning("search_zhihu_error | %s", e)
        return ToolCallResult(ok=False, error=f"zhihu_error: {e}")


async def _handle_lookup_wiki(args: dict[str, Any], context: dict[str, Any]) -> ToolCallResult:
    crawler_hub = context.get("crawler_hub")
    if not crawler_hub:
        return ToolCallResult(ok=False, error="crawler_unavailable")

    keyword = str(args.get("keyword", "")).strip()
    if not keyword:
        return ToolCallResult(ok=False, error="missing keyword")

    try:
        results = await crawler_hub.wiki.lookup(keyword)
        if not results:
            return ToolCallResult(ok=False, error="not_found", display=f"未找到 '{keyword}' 的百科信息")

        lines: list[str] = []
        for r in results:
            source_name = "百度百科" if r.source == "baike" else "维基百科"
            lines.append(f"【{source_name}: {r.title}】")
            lines.append(clip_text(r.snippet, 400))
            if r.url:
                lines.append(f"来源: {r.url}")
            lines.append("")

        # 存入知识库
        kb = context.get("knowledge_base")
        if kb:
            for r in results:
                kb.add("wiki", r.title, r.snippet, source=r.source, tags=[keyword])

        return ToolCallResult(ok=True, data={"results": len(results)}, display="\n".join(lines))
    except Exception as e:
        _log.warning("lookup_wiki_error | %s", e)
        return ToolCallResult(ok=False, error=f"wiki_error: {e}")


async def _handle_search_knowledge(args: dict[str, Any], context: dict[str, Any]) -> ToolCallResult:
    kb = context.get("knowledge_base")
    if not kb:
        return ToolCallResult(ok=False, error="knowledge_base_unavailable")

    query = str(args.get("query", "")).strip()
    category = str(args.get("category", "")).strip()
    if not query:
        return ToolCallResult(ok=False, error="missing query")
    current_user_id = normalize_text(str(context.get("user_id", "")))
    current_conversation_id = normalize_text(str(context.get("conversation_id", "")))
    current_group_id = normalize_text(str(context.get("group_id", "")))

    def _build_query_variants(raw_query: str) -> list[str]:
        base = normalize_text(raw_query)
        if not base:
            return []

        variants: list[str] = []
        seen: set[str] = set()

        def _add(item: str) -> None:
            text = normalize_text(item)
            if not text:
                return
            key = text.lower()
            if key in seen:
                return
            seen.add(key)
            variants.append(text)

        _add(base)

        compact = re.sub(r"[，。！？!?,.;；:：\"'“”‘’（）()【】\[\]<>]+", " ", base)
        compact = normalize_text(compact)
        _add(compact)

        stop_words = {
            "喜欢",
            "最喜欢",
            "歌曲",
            "音乐",
            "听",
            "听歌",
            "查询",
            "搜索",
            "查",
            "查下",
            "查一下",
            "相关",
            "内容",
            "信息",
            "什么",
            "哪个",
            "是谁",
            "有吗",
            "一下",
            "给我",
            "帮我",
            "用户",
            "user",
        }

        user_id = normalize_text(str(context.get("user_id", "")))
        user_name_tokens = set(tokenize(normalize_text(str(context.get("user_name", "")))))

        core_terms: list[str] = []
        for token in tokenize(compact):
            if token in stop_words:
                continue
            if token in user_name_tokens:
                continue
            if user_id and token == user_id:
                continue
            if token.isdigit() and len(token) < 4:
                continue
            core_terms.append(token)
            if len(core_terms) >= 6:
                break

        if core_terms:
            _add(" ".join(core_terms))
            for term in core_terms[:4]:
                _add(term)

        if user_id:
            # 配合 user:<id> 标签做用户画像类检索。
            _add(f"user:{user_id}")
            if core_terms:
                _add(f"user:{user_id} {' '.join(core_terms[:3])}")

        return variants[:10]

    def _normalize_entry_tags(entry: Any) -> set[str]:
        raw_tags = getattr(entry, "tags", [])
        if isinstance(raw_tags, str):
            raw_tags = [raw_tags]
        if not isinstance(raw_tags, list):
            return set()
        out: set[str] = set()
        for raw in raw_tags:
            text = normalize_text(str(raw)).lower()
            if text:
                out.add(text)
        return out

    def _scope_score(entry: Any) -> int:
        tags = _normalize_entry_tags(entry)
        score = 0
        if current_user_id and f"user:{current_user_id}".lower() in tags:
            score += 100
        if current_conversation_id and f"conversation:{current_conversation_id}".lower() in tags:
            score += 40
        if current_group_id and f"group:{current_group_id}".lower() in tags:
            score += 20
        return score

    try:
        query_variants = _build_query_variants(query)
        category_variants: list[str] = [category] if category else [""]
        if category:
            category_variants.append("")  # category 限制命不中时自动放宽到全库

        entries: list[Any] = []
        seen_ids: set[int] = set()
        for cat in category_variants:
            for q in query_variants:
                try:
                    rows = kb.search(q, category=cat, limit=8)
                except Exception:
                    rows = []
                for row in rows:
                    rid = int(getattr(row, "id", 0) or 0)
                    if rid and rid in seen_ids:
                        continue
                    if rid:
                        seen_ids.add(rid)
                    entries.append(row)
                if len(entries) >= 8:
                    break
            if len(entries) >= 8:
                break

        if entries:
            entries = sorted(
                entries,
                key=lambda row: (
                    _scope_score(row),
                    float(getattr(row, "created_at", 0.0) or 0.0),
                ),
                reverse=True,
            )

        if not entries:
            return ToolCallResult(
                ok=True,
                data={"count": 0, "query_variants": query_variants},
                display=f"知识库中未找到 '{query}' 相关内容",
            )

        lines = [f"【知识库搜索: {query}】"]
        result_rows: list[dict[str, Any]] = []
        scoped_hits = 0
        for e in entries:
            cat_tag = f"[{e.category}]" if e.category else ""
            scope_score = _scope_score(e)
            if scope_score >= 100:
                scoped_hits += 1
            scope_tag = " (当前用户)" if scope_score >= 100 else ""
            lines.append(f"- {cat_tag} {e.title}{scope_tag}")
            if e.content:
                lines.append(f"  {clip_text(e.content, 200)}")
            tags = [normalize_text(str(item)) for item in (getattr(e, "tags", []) or []) if normalize_text(str(item))]
            result_rows.append(
                {
                    "id": int(getattr(e, "id", 0) or 0),
                    "category": normalize_text(str(getattr(e, "category", ""))),
                    "title": normalize_text(str(getattr(e, "title", ""))),
                    "content": normalize_text(str(getattr(e, "content", ""))),
                    "source": normalize_text(str(getattr(e, "source", ""))),
                    "tags": tags,
                }
            )
        return ToolCallResult(
            ok=True,
            data={
                "count": len(entries),
                "results": result_rows,
                "query_variants": query_variants,
                "scoped_hits": scoped_hits,
            },
            display="\n".join(lines),
        )
    except Exception as e:
        _log.warning("search_knowledge_error | %s", e)
        return ToolCallResult(ok=False, error=f"knowledge_error: {e}")


async def _handle_learn_knowledge(args: dict[str, Any], context: dict[str, Any]) -> ToolCallResult:
    kb = context.get("knowledge_base")
    if not kb:
        return ToolCallResult(ok=False, error="knowledge_base_unavailable")

    def _infer_title_from_content(text: str) -> str:
        body = normalize_text(text)
        if not body:
            return ""
        m = re.match(r"([^，。！？\n:：]{1,40})(?:是|指|叫|一般是|通常是)", body)
        if m:
            return normalize_text(m.group(1))[:40]
        m2 = re.match(r"([^，。！？\n]{1,40})[:：]", body)
        if m2:
            return normalize_text(m2.group(1))[:40]
        fallback = normalize_text(body.split("，", 1)[0].split("。", 1)[0])
        return fallback[:40]

    title = normalize_text(str(args.get("title", "")))
    content = normalize_text(str(args.get("content", "")))
    if not content:
        content = normalize_text(str(args.get("text", "")))
    category = str(args.get("category", "learned")).strip()
    tags_value = args.get("tags", "")
    tags: list[str] = []
    if isinstance(tags_value, str):
        tags = [t.strip() for t in tags_value.split(",") if t.strip()]
    elif isinstance(tags_value, list):
        tags = [normalize_text(str(t)) for t in tags_value if normalize_text(str(t))]

    if not title and content:
        title = _infer_title_from_content(content)
        if title:
            tags = list(dict.fromkeys(tags + ["auto_title"]))

    if not title:
        return ToolCallResult(ok=False, error="missing title")
    if not content:
        return ToolCallResult(ok=False, error="missing content")
    if looks_like_preferred_name_knowledge(title, content, tags):
        cfg = context.get("config", {})
        bot_cfg = cfg.get("bot", {}) if isinstance(cfg, dict) and isinstance(cfg.get("bot"), dict) else {}
        bot_aliases = [bot_cfg.get("name", ""), *(bot_cfg.get("nicknames", []) or []), "yuki", "yukiko", "雪"]
        source_text = normalize_text(str(context.get("message_text", ""))) or normalize_text(
            str(context.get("original_message_text", ""))
        )
        decision = assess_preferred_name_learning(
            source_text or content,
            is_private=bool(context.get("is_private", False)),
            mentioned=bool(context.get("mentioned", False)),
            explicit_bot_addressed=bool(context.get("explicit_bot_addressed", False)),
            bot_aliases=bot_aliases,
            at_other_user_ids=context.get("at_other_user_ids", []) or [],
            reply_to_user_id=normalize_text(str(context.get("reply_to_user_id", ""))),
            bot_id=normalize_text(str(context.get("bot_id", ""))),
        )
        if not decision.allow:
            return ToolCallResult(
                ok=False,
                error=f"preferred_name_guard:{decision.reason}",
                display="群聊称呼学习需要明确点名我、明确声明，并且不能在起哄语境里。",
            )
        memory = context.get("memory_engine")
        if memory is None or not hasattr(memory, "set_preferred_name"):
            return ToolCallResult(ok=False, error="memory_engine_unavailable", display="称呼记忆模块未初始化")
        ok, message, payload = memory.set_preferred_name(
            target_user_id=normalize_text(str(context.get("user_id", ""))),
            preferred_name=decision.candidate,
            actor="agent.learn_knowledge",
            conversation_id=normalize_text(str(context.get("conversation_id", ""))),
            note="Agent 显式学习用户偏好称呼",
            reason="agent_learn_preferred_name",
        )
        if not ok:
            return ToolCallResult(ok=False, error="preferred_name_update_failed", data=payload or {}, display=message)
        preferred_name = normalize_text(str(payload.get("preferred_name", decision.candidate)))
        return ToolCallResult(
            ok=True,
            data=payload or {},
            display=f"已更新用户偏好称呼: {preferred_name or decision.candidate}",
        )
    merged_text = normalize_text(f"{title} {content}")
    if _looks_like_harmful_knowledge_payload(merged_text):
        return ToolCallResult(ok=False, error="unsafe_knowledge_content")
    if category not in ("fact", "meme", "learned"):
        category = "learned"

    normalized_tags: list[str] = []
    seen_tags: set[str] = set()

    def _append_tag(raw: str) -> None:
        tag = normalize_text(str(raw))
        if not tag:
            return
        key = tag.lower()
        if key in seen_tags:
            return
        seen_tags.add(key)
        normalized_tags.append(tag)

    for item in tags:
        _append_tag(item)
    current_user_id = normalize_text(str(context.get("user_id", "")))
    current_conversation_id = normalize_text(str(context.get("conversation_id", "")))
    current_group_id = int(context.get("group_id", 0) or 0)
    if current_user_id:
        _append_tag(f"user:{current_user_id}")
    if current_conversation_id:
        _append_tag(f"conversation:{current_conversation_id}")
    if current_group_id > 0:
        _append_tag(f"group:{current_group_id}")
    normalized_tags = normalized_tags[:20]

    try:
        entry_id = kb.add(category=category, title=title, content=content,
                        source="chat", tags=normalized_tags)
        return ToolCallResult(
            ok=True,
            data={"id": entry_id, "category": category, "tags": normalized_tags},
            display=f"已学习: [{category}] {title}",
        )
    except Exception as e:
        _log.warning("learn_knowledge_error | %s", e)
        return ToolCallResult(ok=False, error=f"learn_error: {e}")


def _looks_like_harmful_knowledge_payload(text: str) -> bool:
    content = normalize_text(text).lower()
    if not content:
        return True
    abusive_tokens = (
        "大便",
        "傻逼",
        "弱智",
        "智障",
        "脑残",
        "废物",
        "狗东西",
        "滚",
    )
    if any(token in content for token in abusive_tokens):
        return True
    # 阻断“以后你叫XX叫YY”这类强制羞辱称呼写入。
    if "以后你叫" in content and "叫他" in content:
        return True
    return False


# ─────────────────────────────────────────────
# Daily Report & User Portrait tools
# ─────────────────────────────────────────────

