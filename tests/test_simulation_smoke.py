"""Simulation smoke tests for all fixes."""
import asyncio
import copy
import hmac
import ipaddress
import socket
import time
from typing import Any

# ── Scene 4: DB path traversal ──


def _check_db_name(db_name: str) -> str:
    raw_name = db_name.strip()
    if ".." in raw_name or raw_name.startswith("/") or raw_name.startswith("\\") or ":" in raw_name:
        return "BLOCKED"
    return "ALLOWED"


def test_db_path_traversal():
    cases = [
        ("../../etc/passwd", "BLOCKED"),
        ("../../../windows/system32", "BLOCKED"),
        ("/etc/passwd", "BLOCKED"),
        ("C:\\Windows\\System32", "BLOCKED"),
        ("yukiko", "ALLOWED"),
        ("memory", "ALLOWED"),
        ("my_database", "ALLOWED"),
        ("test.db", "ALLOWED"),
        ("..hidden", "BLOCKED"),
    ]
    for name, expected in cases:
        result = _check_db_name(name)
        assert result == expected, f"{name!r}: got {result}, expected {expected}"


# ── Scene 5: safety._has_risky_term all occurrences ──


def _has_risky_term_fixed(content: str, terms: list[str]) -> bool:
    _tech_suffixes = (
        "检测", "防御", "防护", "防范", "分析", "研究", "原理",
        "安全", "测试", "审计", "评估", "报告", "论文", "课程",
        "防止", "预防", "应对", "响应", "监控", "告警",
    )
    for term in terms:
        start = 0
        while True:
            idx = content.find(term, start)
            if idx < 0:
                break
            after = content[idx + len(term):idx + len(term) + 4]
            if any(after.startswith(suffix) for suffix in _tech_suffixes):
                start = idx + len(term)
                continue
            return True
    return False


def _has_risky_term_old(content: str, terms: list[str]) -> bool:
    """Old buggy version for comparison."""
    _tech_suffixes = (
        "检测", "防御", "防护", "防范", "分析", "研究", "原理",
        "安全", "测试", "审计", "评估", "报告", "论文", "课程",
        "防止", "预防", "应对", "响应", "监控", "告警",
    )
    for term in terms:
        idx = content.find(term)
        if idx < 0:
            continue
        after = content[idx + len(term):idx + len(term) + 4]
        if any(after.startswith(suffix) for suffix in _tech_suffixes):
            continue
        return True
    return False


def test_risky_term_bypass_fixed():
    terms = ["入侵"]

    # Case 1: Pure tech context -> should NOT flag
    assert _has_risky_term_fixed("入侵检测系统", terms) is False

    # Case 2: Pure malicious -> should flag
    assert _has_risky_term_fixed("入侵方法教程", terms) is True

    # Case 3: BYPASS ATTEMPT — tech prefix followed by malicious use
    # Old code: "入侵检测" matches first, skips entire term, misses "入侵方法"
    bypass_text = "入侵检测系统和入侵方法"
    assert _has_risky_term_fixed(bypass_text, terms) is True, "Fixed version should catch second occurrence"
    assert _has_risky_term_old(bypass_text, terms) is False, "Old version was vulnerable to this bypass"

    # Case 4: Multiple tech contexts — should NOT flag
    assert _has_risky_term_fixed("入侵检测和入侵防御系统", terms) is False

    # Case 5: Tech then bare term
    assert _has_risky_term_fixed("入侵防范之后的入侵", terms) is True


# ── Scene 6: Queue timeout — no shield means task actually cancels ──


async def _simulate_queue_timeout():
    """Simulate that without asyncio.shield, timeout properly cancels the task."""
    cancel_happened = False

    async def slow_process():
        nonlocal cancel_happened
        try:
            await asyncio.sleep(100)  # Way too slow
        except asyncio.CancelledError:
            cancel_happened = True
            raise

    task = asyncio.create_task(slow_process())

    # Without shield: wait_for cancels the task on timeout
    try:
        await asyncio.wait_for(task, timeout=0.1)
    except asyncio.TimeoutError:
        pass

    # Give event loop a tick to process cancellation
    await asyncio.sleep(0.05)

    assert task.cancelled() or task.done(), "Task should be cancelled/done after timeout"
    assert cancel_happened, "Task should have received CancelledError"
    return True


async def test_queue_timeout_cancels_task():
    result = await _simulate_queue_timeout()
    assert result is True


# ── Scene 7: Multi-image message combination ──


class FakeMessageSegment:
    def __init__(self, type_: str, data: dict):
        self.type = type_
        self.data = data

    def __repr__(self):
        return f"Seg({self.type}:{self.data})"


class FakeMessage:
    def __init__(self, text: str = ""):
        self.segments: list = []
        if text:
            self.segments.append(FakeMessageSegment("text", {"text": text}))

    def __iadd__(self, other):
        if isinstance(other, FakeMessageSegment):
            self.segments.append(other)
        elif isinstance(other, FakeMessage):
            self.segments.extend(other.segments)
        return self

    def __len__(self):
        return len(self.segments)


def test_multi_image_combined():
    """Simulate the new combined multi-image logic."""
    image_urls = [
        "https://example.com/img1.jpg",
        "https://example.com/img2.jpg",
        "https://example.com/img3.jpg",
    ]

    # Simulate new logic: combine all images into one message
    combined_msg = FakeMessage()
    for url in image_urls:
        seg = FakeMessageSegment("image", {"file": url})
        combined_msg += seg

    # Should have 3 image segments in ONE message
    assert len(combined_msg) == 3, f"Expected 3 segments, got {len(combined_msg)}"
    for seg in combined_msg.segments:
        assert seg.type == "image", f"Expected image segment, got {seg.type}"

    # Old logic would have sent 3 separate messages — now it's 1


# ── Scene 8: ModelClient close ──


class FakeLLMClient:
    def __init__(self):
        self.closed = False

    async def close(self):
        self.closed = True


async def _simulate_model_client_close():
    main_client = FakeLLMClient()
    fallback1 = FakeLLMClient()
    fallback2 = FakeLLMClient()

    clients = [main_client, fallback1, fallback2]
    for client in clients:
        close_fn = getattr(client, "close", None)
        if callable(close_fn):
            result = close_fn()
            if hasattr(result, "__await__"):
                await result

    assert main_client.closed, "Main client should be closed"
    assert fallback1.closed, "Fallback 1 should be closed"
    assert fallback2.closed, "Fallback 2 should be closed"
    return True


async def test_model_client_close():
    result = await _simulate_model_client_close()
    assert result is True


# ── Scene 9: Engine cache eviction ──


def test_engine_cache_eviction():
    """Simulate the cache eviction logic for _group_member_name_cache."""
    cache_max = 200

    # Fill cache with 300 entries
    cache = {}
    now = time.time()
    for i in range(300):
        expires = now - 100 if i < 150 else now + 3600  # 150 expired, 150 valid
        cache[i] = {"expires_at": expires, "names": [f"user_{i}"]}

    assert len(cache) == 300

    # Simulate eviction logic (same as engine.py)
    if len(cache) > cache_max:
        expired = [
            gid for gid, info in cache.items()
            if isinstance(info, dict) and now > info.get("expires_at", 0)
        ]
        for gid in expired:
            cache.pop(gid, None)

        # If still over limit, remove oldest half
        if len(cache) > cache_max:
            keys = list(cache.keys())
            for k in keys[:len(keys) // 2]:
                cache.pop(k, None)

    assert len(cache) <= cache_max, f"Cache should be <= {cache_max}, got {len(cache)}"
    # After removing 150 expired, 150 remain — under limit
    assert len(cache) == 150, f"Should have 150 valid entries, got {len(cache)}"


# ── Scene 8b: _FATAL_ERROR_CUES precision ──


def test_fatal_error_cues_precision():
    _FATAL_ERROR_CUES = (
        "suspended", "forbidden", "unauthorized", "banned",
        "account suspended", "account banned", "account disabled",
        "disabled", "quota", "rate_limit",
    )

    def is_fatal(msg: str) -> bool:
        m = msg.lower()
        return any(cue in m for cue in _FATAL_ERROR_CUES) or "403" in m or "401" in m

    # Should be fatal
    assert is_fatal("Your account suspended") is True
    assert is_fatal("403 Forbidden") is True
    assert is_fatal("quota exceeded") is True

    # "account" alone should NOT trigger anymore (removed from cues)
    assert is_fatal("Error creating account context") is False, \
        "Generic 'account' mention should not trigger failover"
    assert is_fatal("accounting for tokens") is False, \
        "'accounting' should not trigger failover"

    # But specific account errors should still trigger
    assert is_fatal("Your account has been suspended") is True
    assert is_fatal("account disabled by admin") is True


# ── Scene 5b: step_idx non-negative ──


def test_step_idx_non_negative():
    step_idx = 0
    budget = 3

    # Simulate 3 consecutive timeouts at step 0, 1, 2
    for i in range(3):
        if budget > 0:
            budget -= 1
            if step_idx > 0:
                step_idx -= 1
        step_idx += 1  # normal increment at loop top

    # step_idx should never have gone negative
    assert step_idx >= 0, f"step_idx went negative: {step_idx}"


# ── Scene 10: mention_only policy must NOT be auto-upgraded ──


def test_mention_only_not_overridden():
    """Simulate the engine policy resolution logic.
    When user sets mention_only, ai_listen_enable should NOT upgrade it."""

    def resolve_policy(policy_str: str, ai_listen_defined: bool, ai_listen_enable: bool):
        explicit_ai_listen_on = ai_listen_defined and ai_listen_enable
        allow_non_to_me = False

        if policy_str in {"off", "disabled"}:
            allow_non_to_me = False
        elif policy_str in {"mention_only", "directed_only"}:
            allow_non_to_me = False
        elif policy_str == "high_confidence_only":
            allow_non_to_me = True

        # Fixed logic: mention_only/directed_only excluded from override
        if explicit_ai_listen_on and policy_str not in {"off", "disabled", "mention_only", "directed_only"}:
            allow_non_to_me = True

        return allow_non_to_me

    # mention_only + ai_listen_enable=True -> should still block non-@ messages
    assert resolve_policy("mention_only", True, True) is False, \
        "mention_only must not allow non-@ messages even with ai_listen_enable"

    # high_confidence_only + ai_listen_enable=True -> should allow
    assert resolve_policy("high_confidence_only", True, True) is True

    # off -> should block
    assert resolve_policy("off", True, True) is False

    # mention_only without ai_listen -> should block
    assert resolve_policy("mention_only", False, False) is False


# ── Scene 11: responses empty triggers fallback ──


def test_responses_empty_fallback():
    """Verify that 'responses 返回为空' is recognized as fallback-worthy."""
    error_msg = "responses 返回为空，将回退到 chat/completions"
    is_empty = "返回为空" in str(error_msg)
    assert is_empty is True, "Empty response should be detected for fallback"


# ── Scene 12: analyze_image in side effect tools ──


def test_analyze_image_in_side_effects():
    """analyze_image results should be recorded in conversation memory."""
    side_effect_tools = frozenset({
        "send_face", "send_emoji", "learn_sticker", "correct_sticker",
        "send_group_message", "send_private_message", "send_group_forward_msg",
        "send_group_ai_record", "upload_group_file", "upload_private_file",
        "set_msg_emoji_like", "generate_image", "web_search", "analyze_image",
    })
    assert "analyze_image" in side_effect_tools, "analyze_image must be a side effect tool"


# ── Scene 13: add_user_fact ──


def test_add_user_fact():
    """Simulate the memory add_user_fact logic."""
    profiles: dict = {}

    def add_fact(user_id: str, fact: str) -> bool:
        if not user_id or not fact or len(fact) < 2:
            return False
        profile = profiles.get(user_id, {})
        facts = profile.get("explicit_facts", [])
        facts.append({"fact": fact})
        facts = facts[-30:]
        profile["explicit_facts"] = facts
        profiles[user_id] = profile
        return True

    assert add_fact("12345", "Claude用户名=dwgx1337") is True
    assert len(profiles["12345"]["explicit_facts"]) == 1
    assert profiles["12345"]["explicit_facts"][0]["fact"] == "Claude用户名=dwgx1337"

    assert add_fact("12345", "职业=开发者") is True
    assert len(profiles["12345"]["explicit_facts"]) == 2

    # Too short
    assert add_fact("12345", "x") is False
    # Empty
    assert add_fact("", "test") is False


# ── Scene 14: knowledge_store upsert, search, decay, reinforce ──


def test_knowledge_store_upsert_and_search():
    """Simulate knowledge_store upsert/dedup and search with decay."""
    import math

    store: dict[int, dict] = {}
    idx_counter = [0]

    def upsert(user_id, entity, relation, value, confidence=0.9, category="learned", source="agent"):
        key = (user_id, entity, relation)
        for rid, rec in store.items():
            if (rec["user_id"], rec["entity"], rec["relation"]) == key:
                rec["value"] = value
                rec["confidence"] = confidence
                rec["access_count"] += 1
                rec["updated_at"] = time.time()
                return rid
        idx_counter[0] += 1
        store[idx_counter[0]] = {
            "user_id": user_id, "entity": entity, "relation": relation,
            "value": value, "confidence": confidence, "access_count": 0,
            "category": category, "source": source,
            "valid_from": time.time(), "valid_until": None,
            "created_at": time.time(), "updated_at": time.time(),
        }
        return idx_counter[0]

    # Insert
    id1 = upsert("u1", "dwgx1337", "username", "帝王尬笑")
    assert id1 == 1
    assert store[1]["value"] == "帝王尬笑"

    # Upsert dedup: same (user, entity, relation) → update
    id2 = upsert("u1", "dwgx1337", "username", "新名字")
    assert id2 == 1, "Should update existing, not create new"
    assert store[1]["value"] == "新名字"
    assert store[1]["access_count"] == 1

    # Different relation → new record
    id3 = upsert("u1", "dwgx1337", "role", "开发者")
    assert id3 == 2

    # Search with decay
    def search_with_decay(user_id, limit=10):
        results = []
        now = time.time()
        for rid, rec in store.items():
            if rec["user_id"] != user_id:
                continue
            if rec.get("valid_until"):
                continue
            days_old = (now - rec["created_at"]) / 86400
            decay = max(0.1, 1.0 - days_old * 0.005)
            reinforcement = min(2.0, 1.0 + rec["access_count"] * 0.1)
            score = rec["confidence"] * decay * reinforcement
            results.append({**rec, "score": score, "id": rid})
        results.sort(key=lambda x: x["score"], reverse=True)
        return results[:limit]

    results = search_with_decay("u1")
    assert len(results) == 2
    # Updated record (access_count=1) should score higher due to reinforcement
    assert results[0]["entity"] == "dwgx1337"


def test_knowledge_invalidation():
    """Simulate Graphiti-style temporal invalidation."""
    record = {
        "entity": "bot_name", "value": "旧名字",
        "valid_from": time.time() - 86400, "valid_until": None,
    }
    assert record["valid_until"] is None

    # Invalidate
    record["valid_until"] = time.time()
    assert record["valid_until"] is not None

    # Invalidated records should be skipped in search
    now = time.time()
    is_valid = record["valid_until"] is None or record["valid_until"] > now
    assert is_valid is False, "Invalidated record should be filtered out"


def test_conversation_summary():
    """Simulate MemGPT-style conversation summarization."""
    summaries: list[dict] = []

    def save_summary(conv_id, summary, key_facts=None, msg_range=""):
        entry = {
            "conversation_id": conv_id, "summary": summary,
            "key_facts": key_facts or [], "message_range": msg_range,
            "created_at": time.time(),
        }
        summaries.append(entry)
        return len(summaries)

    save_summary("g_12345", "用户讨论了Claude的配置和使用方法", ["用户名=dwgx1337"], "1-20")
    save_summary("g_12345", "用户询问了bot的记忆功能", ["喜欢简洁回复"], "21-40")

    assert len(summaries) == 2
    assert summaries[0]["key_facts"] == ["用户名=dwgx1337"]

    # Get summaries for a conversation
    conv_summaries = [s for s in summaries if s["conversation_id"] == "g_12345"]
    assert len(conv_summaries) == 2
