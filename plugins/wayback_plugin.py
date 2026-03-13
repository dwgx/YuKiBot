"""Wayback plugin: Internet Archive compatible historical crawler tools.

This plugin registers Agent-only tools for:
1) Listing snapshots from Wayback CDX API
2) Extracting text from a snapshot with robust charset handling
3) Building a year-level snapshot timeline
"""
from __future__ import annotations

import contextlib
import asyncio
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from html import unescape
from typing import Any, Awaitable, Callable, TypeVar
from urllib.parse import quote, unquote, urlparse

import httpx
from utils.text import clip_text, normalize_text

_log = logging.getLogger("yukiko.plugin.wayback")

_WAYBACK_CDX_URL = "https://web.archive.org/cdx/search/cdx"
_WAYBACK_AVAILABLE_URL = "https://archive.org/wayback/available"
_DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

_WAYBACK_URL_RE = re.compile(r"^https?://web\.archive\.org/web/([^/]+)/(.+)$", re.I)
_TS_HEAD_RE = re.compile(r"^(\d{4,14})")
_META_CHARSET_RE = re.compile(
    r"<meta[^>]+charset=['\"]?\s*([a-zA-Z0-9_\-]+)\s*['\"]?",
    re.I,
)
_META_CT_CHARSET_RE = re.compile(
    r"<meta[^>]+http-equiv=['\"]content-type['\"][^>]*content=['\"][^>]*charset=([a-zA-Z0-9_\-]+)",
    re.I,
)
_CONTENT_TYPE_CHARSET_RE = re.compile(r"charset=([a-zA-Z0-9_\-]+)", re.I)

_SCRIPT_RE = re.compile(r"<script[^>]*>[\s\S]*?</script>", re.I)
_STYLE_RE = re.compile(r"<style[^>]*>[\s\S]*?</style>", re.I)
_COMMENT_RE = re.compile(r"<!--[\s\S]*?-->")
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_MULTI_NL_RE = re.compile(r"\n{3,}")
_MULTI_SP_RE = re.compile(r"[ \t]{2,}")
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
_T = TypeVar("_T")


@dataclass(slots=True)
class _Snapshot:
    timestamp: str
    original: str
    statuscode: str = ""
    mimetype: str = ""
    length: str = ""
    digest: str = ""

    def ui_url(self) -> str:
        return _build_snapshot_url(self.timestamp, self.original, raw=False)

    def raw_url(self) -> str:
        return _build_snapshot_url(self.timestamp, self.original, raw=True)


def _normalize_timestamp(raw: str) -> str:
    digits = "".join(ch for ch in str(raw or "") if ch.isdigit())
    if not digits:
        return ""
    if len(digits) >= 14:
        return digits[:14]
    if len(digits) == 12:
        return digits + "00"
    if len(digits) == 10:
        return digits + "0000"
    if len(digits) == 8:
        return digits + "000000"
    if len(digits) == 6:
        return digits + "01000000"
    if len(digits) == 4:
        return digits + "0101000000"
    return ""


def _ts_year_window(year: int) -> tuple[str, str]:
    y = max(1900, min(2099, int(year)))
    return f"{y}0101000000", f"{y}1231235959"


def _year_of_ts(ts: str) -> int | None:
    norm = _normalize_timestamp(ts)
    if len(norm) != 14:
        return None
    with contextlib.suppress(ValueError):
        return int(norm[:4])
    return None


def _ts_window_around(target_ts: str, days: int = 120) -> tuple[str, str]:
    norm = _normalize_timestamp(target_ts)
    if len(norm) != 14:
        return "", ""
    try:
        dt = datetime(
            year=int(norm[0:4]),
            month=int(norm[4:6]),
            day=int(norm[6:8]),
            hour=int(norm[8:10]),
            minute=int(norm[10:12]),
            second=int(norm[12:14]),
            tzinfo=timezone.utc,
        )
    except Exception:
        return "", ""
    left = dt - timedelta(days=max(1, int(days)))
    right = dt + timedelta(days=max(1, int(days)))
    return (
        left.strftime("%Y%m%d%H%M%S"),
        right.strftime("%Y%m%d%H%M%S"),
    )


def _normalize_url(raw: str) -> str:
    text = normalize_text(str(raw or ""))
    if not text:
        return ""
    if _WAYBACK_URL_RE.match(text):
        return text
    parsed = urlparse(text)
    if parsed.scheme in {"http", "https"} and parsed.netloc:
        return text
    if not parsed.scheme and "." in parsed.path:
        # For historical archives, http often has better old coverage than https.
        return f"http://{text}"
    return ""


def _url_variants(url: str) -> list[str]:
    """Generate protocol/domain variants to improve Wayback hit rate."""
    base = _normalize_url(url)
    if not base or _WAYBACK_URL_RE.match(base):
        return [base] if base else []
    parsed = urlparse(base)
    host = normalize_text(parsed.netloc).lower()
    path = parsed.path or ""
    if parsed.params:
        path += ";" + parsed.params
    if parsed.query:
        path += "?" + parsed.query
    if parsed.fragment:
        path += "#" + parsed.fragment

    hosts = [host]
    if host.startswith("www."):
        hosts.append(host[4:])
    elif host:
        hosts.append("www." + host)

    out: list[str] = []
    for h in hosts:
        for scheme in ("http", "https"):
            candidate = f"{scheme}://{h}{path}"
            if candidate not in out:
                out.append(candidate)
    return out


def _build_snapshot_url(timestamp: str, original: str, *, raw: bool) -> str:
    ts = _normalize_timestamp(timestamp)
    base = normalize_text(str(original or ""))
    if not base.startswith(("http://", "https://")):
        base = "http://" + base.lstrip("/")
    safe_original = quote(base, safe=":/?&=#%+-._~")
    marker = "id_/" if raw else "/"
    return f"https://web.archive.org/web/{ts}{marker}{safe_original}"


def _parse_wayback_url(url: str) -> _Snapshot | None:
    m = _WAYBACK_URL_RE.match(normalize_text(str(url or "")))
    if not m:
        return None
    token = m.group(1)
    tail = m.group(2)
    ts_match = _TS_HEAD_RE.match(token)
    if not ts_match:
        return None
    ts = _normalize_timestamp(ts_match.group(1))
    original = unquote(tail)
    if not original.startswith(("http://", "https://")):
        original = "http://" + original.lstrip("/")
    return _Snapshot(timestamp=ts, original=original)


def _pretty_ts(ts: str) -> str:
    val = _normalize_timestamp(ts)
    if len(val) != 14:
        return ts
    return (
        f"{val[0:4]}-{val[4:6]}-{val[6:8]} "
        f"{val[8:10]}:{val[10:12]}:{val[12:14]}"
    )


def _score_decoded_text(text: str) -> float:
    if not text:
        return -1e9
    sample = text[:5000]
    cjk = len(re.findall(r"[\u4e00-\u9fff]", sample))
    alnum = len(re.findall(r"[A-Za-z0-9]", sample))
    repl = sample.count("\ufffd")
    ctrl = sum(1 for ch in sample if ord(ch) < 32 and ch not in "\n\r\t")
    weird = sum(sample.count(ch) for ch in ("Ã", "â", "Ð", "×", "�"))
    return (cjk * 2.8) + (alnum * 0.2) - (repl * 30) - (ctrl * 8) - (weird * 6)


def _extract_charset_from_headers(content_type: str) -> str:
    m = _CONTENT_TYPE_CHARSET_RE.search(content_type or "")
    return normalize_text(m.group(1)).lower() if m else ""


def _extract_charset_from_meta(raw_bytes: bytes) -> str:
    head = raw_bytes[:8192].decode("latin-1", errors="ignore")
    m = _META_CHARSET_RE.search(head)
    if m:
        return normalize_text(m.group(1)).lower()
    m = _META_CT_CHARSET_RE.search(head)
    if m:
        return normalize_text(m.group(1)).lower()
    return ""


def _decode_best(
    raw: bytes,
    *,
    response_encoding: str = "",
    content_type: str = "",
) -> tuple[str, str]:
    ordered: list[str] = []
    for item in (
        normalize_text(response_encoding).lower(),
        _extract_charset_from_headers(content_type),
        _extract_charset_from_meta(raw),
        "utf-8",
        "gb18030",
        "gbk",
        "big5",
        "shift_jis",
        "latin-1",
    ):
        if item and item not in ordered:
            ordered.append(item)

    best_text = ""
    best_enc = "utf-8"
    best_score = -1e9
    for enc in ordered:
        with contextlib.suppress(Exception):
            text = raw.decode(enc, errors="replace")
            score = _score_decoded_text(text)
            if score > best_score:
                best_score = score
                best_text = text
                best_enc = enc

    if best_text:
        return best_text, best_enc
    return raw.decode("utf-8", errors="ignore"), "utf-8"


def _extract_title(html: str) -> str:
    m = _TITLE_RE.search(html or "")
    if not m:
        return ""
    title = _HTML_TAG_RE.sub("", m.group(1))
    title = unescape(title)
    return normalize_text(title)


def _html_to_text(html: str, max_chars: int) -> str:
    text = _SCRIPT_RE.sub("", html or "")
    text = _STYLE_RE.sub("", text)
    text = _COMMENT_RE.sub("", text)
    text = re.sub(r"<(?:br|p|div|h[1-6]|li|tr|blockquote|section|article)[^>]*>", "\n", text, flags=re.I)
    text = _HTML_TAG_RE.sub("", text)
    text = unescape(text)
    text = _MULTI_SP_RE.sub(" ", text)
    text = _MULTI_NL_RE.sub("\n\n", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if len(text) > max_chars:
        text = text[:max_chars] + "\n...(truncated)"
    return text


def _lines_excerpt(text: str, max_lines: int = 12, max_chars: int = 1600) -> str:
    lines = [normalize_text(line) for line in (text or "").splitlines()]
    lines = [line for line in lines if line]
    if not lines:
        return ""
    out = "\n".join(lines[:max_lines])
    return clip_text(out, max_chars)


class Plugin:
    name = "wayback_plugin"
    description = "Agent internal tools for Wayback Machine snapshot lookup and extraction."
    agent_tool = True
    internal_only = True
    rules: list[str] = []
    args_schema: dict[str, str] = {}

    def __init__(self) -> None:
        self._enabled = True
        self._timeout_seconds = 18.0
        self._operation_budget_seconds = 14.0
        self._variant_stagger_seconds = 0.05
        self._variant_max_parallel = 2
        self._cdx_max_retries = 2
        self._cdx_retry_base_delay = 0.8
        self._default_limit = 8
        self._max_text_chars = 12000
        self._llm_input_chars = 6500
        self._llm_max_tokens = 900
        self._retry_nearby = 2
        self._include_non_200_default = False
        self._ua = _DEFAULT_UA
        self._client: httpx.AsyncClient | None = None
        self._model_client: Any = None
        self._cache: dict[str, tuple[list[_Snapshot], float]] = {}
        self._cache_ttl = 300.0

    async def setup(self, config: dict[str, Any], context: Any) -> None:
        self._enabled = bool(config.get("enabled", True))
        self._timeout_seconds = float(config.get("timeout_seconds", 18.0))
        budget_default = max(8.0, min(self._timeout_seconds - 2.0, 14.0))
        self._operation_budget_seconds = float(config.get("operation_budget_seconds", budget_default))
        self._operation_budget_seconds = max(4.0, self._operation_budget_seconds)
        self._variant_stagger_seconds = float(config.get("variant_stagger_seconds", 0.05))
        self._variant_stagger_seconds = max(0.01, min(1.2, self._variant_stagger_seconds))
        self._variant_max_parallel = int(config.get("variant_max_parallel", 2))
        self._variant_max_parallel = max(1, min(4, self._variant_max_parallel))
        self._cdx_max_retries = int(config.get("cdx_max_retries", 2))
        self._cdx_max_retries = max(1, min(5, self._cdx_max_retries))
        self._cdx_retry_base_delay = float(config.get("cdx_retry_base_delay", 0.8))
        self._cdx_retry_base_delay = max(0.2, min(5.0, self._cdx_retry_base_delay))
        self._default_limit = max(1, min(20, int(config.get("default_limit", 8))))
        self._max_text_chars = max(2000, min(30000, int(config.get("max_text_chars", 12000))))
        self._llm_input_chars = max(1200, min(self._max_text_chars, int(config.get("llm_input_chars", 6500))))
        self._llm_max_tokens = max(300, min(2200, int(config.get("llm_max_tokens", 900))))
        self._retry_nearby = max(0, min(5, int(config.get("retry_nearby_snapshots", 2))))
        self._include_non_200_default = bool(config.get("include_non_200_default", False))
        self._ua = normalize_text(str(config.get("user_agent", _DEFAULT_UA))) or _DEFAULT_UA
        self._model_client = getattr(context, "model_client", None)

        if not self._enabled:
            _log.info("wayback_plugin disabled")
            return

        registry = getattr(context, "agent_tool_registry", None)
        if registry is None:
            _log.warning("wayback_plugin setup skipped: no agent_tool_registry")
            return

        self._register_tools(registry)
        _log.info(
            "wayback_plugin setup | timeout=%.1fs | budget=%.1fs | parallel=%d | default_limit=%d | max_text=%d",
            self._timeout_seconds,
            self._operation_budget_seconds,
            self._variant_max_parallel,
            self._default_limit,
            self._max_text_chars,
        )

    async def teardown(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    async def handle(self, message: str, context: dict[str, Any]) -> str:
        return "wayback_plugin is an internal Agent tool plugin."

    def _register_tools(self, registry: Any) -> None:
        from core.agent_tools import PromptHint, ToolSchema

        registry.register(
            ToolSchema(
                name="wayback_lookup",
                description=(
                    "List Wayback snapshots for a website/page. "
                    "Use for historical web research (e.g., 2010 homepage state)."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "url": {"type": "string", "description": "Target page URL or domain."},
                        "year": {"type": "integer", "description": "Optional year filter, e.g. 2010."},
                        "from_ts": {"type": "string", "description": "Optional start timestamp YYYYMMDDhhmmss."},
                        "to_ts": {"type": "string", "description": "Optional end timestamp YYYYMMDDhhmmss."},
                        "limit": {"type": "integer", "description": "How many snapshots to return (1-20)."},
                        "include_non_200": {"type": "boolean", "description": "Whether to include non-200 snapshots."},
                    },
                    "required": ["url"],
                },
                category="search",
                group="search",
            ),
            self._handle_wayback_lookup,
        )

        registry.register(
            ToolSchema(
                name="wayback_extract",
                description=(
                    "Resolve a Wayback snapshot then extract readable page text "
                    "with charset repair (supports common Chinese historical pages)."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "url": {"type": "string", "description": "Original URL or an existing Wayback snapshot URL."},
                        "year": {"type": "integer", "description": "Optional target year, e.g. 2010."},
                        "timestamp": {"type": "string", "description": "Optional target timestamp YYYYMMDDhhmmss."},
                        "instruction": {"type": "string", "description": "Optional extraction goal for focused answer."},
                        "max_chars": {"type": "integer", "description": "Optional max extracted text length."},
                    },
                    "required": ["url"],
                },
                category="search",
                group="search",
            ),
            self._handle_wayback_extract,
        )

        registry.register(
            ToolSchema(
                name="wayback_timeline",
                description="Return yearly snapshot counts for a URL from Wayback archive.",
                parameters={
                    "type": "object",
                    "properties": {
                        "url": {"type": "string", "description": "Target URL or domain."},
                        "from_year": {"type": "integer", "description": "Optional start year."},
                        "to_year": {"type": "integer", "description": "Optional end year."},
                        "limit": {"type": "integer", "description": "Max snapshots scanned from CDX (100-5000)."},
                    },
                    "required": ["url"],
                },
                category="search",
                group="search",
            ),
            self._handle_wayback_timeline,
        )

        registry.register_prompt_hint(
            PromptHint(
                source="wayback_plugin",
                section="rules",
                content=(
                        "For historical-web questions, prefer wayback_lookup + wayback_extract "
                    "before generic web search."
                ),
                priority=35,
                tool_names=("wayback_lookup", "wayback_extract", "wayback_timeline"),
            )
        )
        registry.register_prompt_hint(
            PromptHint(
                source="wayback_plugin",
                section="tools_guidance",
                content=(
                    "Wayback flow: (1) wayback_lookup to find snapshots, "
                    "(2) wayback_extract to read snapshot content. "
                    "If one date fails/garbles, retry nearby dates and report the final snapshot URL. "
                    "Do not loop too many times: normally no more than 2 extract attempts."
                ),
                priority=35,
                tool_names=("wayback_lookup", "wayback_extract", "wayback_timeline"),
            )
        )

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self._timeout_seconds),
                follow_redirects=True,
                headers={
                    "User-Agent": self._ua,
                    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                    "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
                },
            )
        return self._client

    def _new_deadline(self, budget_seconds: float | None = None) -> float:
        budget = float(budget_seconds if budget_seconds is not None else self._operation_budget_seconds)
        budget = max(0.2, budget)
        return asyncio.get_running_loop().time() + budget

    def _remaining(self, deadline: float | None) -> float:
        if deadline is None:
            return self._timeout_seconds
        return deadline - asyncio.get_running_loop().time()

    async def _await_with_deadline(
        self,
        awaitable: Awaitable[_T],
        *,
        deadline: float | None,
        minimum: float = 0.05,
    ) -> _T:
        if deadline is None:
            return await awaitable
        remaining = self._remaining(deadline)
        if remaining <= 0:
            raise TimeoutError("deadline_exceeded")
        timeout = max(minimum, remaining)
        return await asyncio.wait_for(awaitable, timeout=timeout)

    async def _race_variants(
        self,
        *,
        variants: list[str],
        worker: Callable[[str], Awaitable[_T]],
        is_success: Callable[[_T], bool],
        deadline: float | None,
        errors: list[str] | None = None,
    ) -> tuple[str, _T | None]:
        if not variants:
            return "", None

        pending: dict[asyncio.Task[tuple[str, _T | None, Exception | None]], str] = {}
        next_index = 0

        async def _runner(variant: str) -> tuple[str, _T | None, Exception | None]:
            try:
                value = await worker(variant)
                return variant, value, None
            except Exception as exc:
                return variant, None, exc

        def _launch_one() -> bool:
            nonlocal next_index
            if next_index >= len(variants):
                return False
            if deadline is not None and self._remaining(deadline) <= 0:
                return False
            variant = variants[next_index]
            next_index += 1
            task = asyncio.create_task(_runner(variant))
            pending[task] = variant
            return True

        _launch_one()
        while pending:
            if deadline is not None and self._remaining(deadline) <= 0:
                break
            wait_budget = self._variant_stagger_seconds
            if deadline is not None:
                wait_budget = min(wait_budget, max(0.01, self._remaining(deadline)))
            done, _ = await asyncio.wait(
                list(pending.keys()),
                timeout=wait_budget,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not done:
                if len(pending) < self._variant_max_parallel:
                    _launch_one()
                continue

            for task in done:
                pending.pop(task, None)
                variant, value, err = await task
                if err is not None:
                    if errors is not None:
                        errors.append(f"{variant}:{err}")
                    continue
                if value is not None and is_success(value):
                    for rest in pending:
                        rest.cancel()
                    with contextlib.suppress(Exception):
                        await asyncio.gather(*pending.keys(), return_exceptions=True)
                    return variant, value

            while len(pending) < self._variant_max_parallel and next_index < len(variants):
                if not _launch_one():
                    break

        for task in pending:
            task.cancel()
        with contextlib.suppress(Exception):
            await asyncio.gather(*pending.keys(), return_exceptions=True)
        return "", None

    async def _query_cdx(
        self,
        *,
        url: str,
        from_ts: str = "",
        to_ts: str = "",
        limit: int = 8,
        include_non_200: bool = False,
        deadline: float | None = None,
    ) -> list[_Snapshot]:
        started = asyncio.get_running_loop().time()
        cache_key = f"{url}:{from_ts}:{to_ts}:{limit}:{include_non_200}"
        if cache_key in self._cache:
            cached_data, cached_time = self._cache[cache_key]
            if time.time() - cached_time < self._cache_ttl:
                _log.debug("wayback_cdx_cache_hit | url=%s", url)
                return cached_data

        client = await self._get_client()
        params: dict[str, str] = {
            "url": url,
            "output": "json",
            "fl": "timestamp,original,statuscode,mimetype,length,digest",
            "limit": str(max(1, min(5000, int(limit)))),
            "sort": "reverse",
        }
        if not include_non_200:
            params["filter"] = "statuscode:200"
        if from_ts:
            params["from"] = _normalize_timestamp(from_ts)
        if to_ts:
            params["to"] = _normalize_timestamp(to_ts)

        max_retries = self._cdx_max_retries
        retry_delay = self._cdx_retry_base_delay
        last_error: Exception | None = None
        data: Any = []

        for attempt in range(max_retries):
            try:
                resp = await self._await_with_deadline(
                    client.get(_WAYBACK_CDX_URL, params=params),
                    deadline=deadline,
                )
                resp.raise_for_status()
                data = resp.json()
                break
            except TimeoutError as exc:
                last_error = exc
                if attempt >= max_retries - 1:
                    raise
                remaining = self._remaining(deadline)
                if remaining <= 0.2:
                    raise
                pause = min(retry_delay, max(0.05, remaining - 0.1))
                await asyncio.sleep(pause)
                retry_delay *= 1.7
            except httpx.HTTPStatusError as exc:
                last_error = exc
                if exc.response.status_code == 429 and attempt < max_retries - 1:
                    remaining = self._remaining(deadline)
                    if remaining <= 0.2:
                        raise TimeoutError("cdx_rate_limit_deadline_exceeded")
                    pause = min(retry_delay, max(0.05, remaining - 0.1))
                    _log.warning(
                        "wayback_cdx_rate_limited | url=%s | attempt=%d/%d | retry_after=%.2fs | remaining=%.2fs",
                        url,
                        attempt + 1,
                        max_retries,
                        pause,
                        max(0.0, remaining),
                    )
                    await asyncio.sleep(pause)
                    retry_delay *= 1.7
                    continue
                raise
            except Exception as exc:
                last_error = exc
                raise

        if last_error is not None and not isinstance(data, list):
            raise last_error
        if not isinstance(data, list) or len(data) <= 1:
            return []

        header = data[0]
        if not isinstance(header, list):
            return []
        idx = {str(name): i for i, name in enumerate(header)}

        out: list[_Snapshot] = []
        for row in data[1:]:
            if not isinstance(row, list):
                continue
            ts = _normalize_timestamp(_cell(row, idx, "timestamp"))
            original = normalize_text(_cell(row, idx, "original"))
            if not ts or not original:
                continue
            out.append(
                _Snapshot(
                    timestamp=ts,
                    original=original,
                    statuscode=normalize_text(_cell(row, idx, "statuscode")),
                    mimetype=normalize_text(_cell(row, idx, "mimetype")),
                    length=normalize_text(_cell(row, idx, "length")),
                    digest=normalize_text(_cell(row, idx, "digest")),
                )
            )

        self._cache[cache_key] = (out, time.time())
        if len(self._cache) > 100:
            now = time.time()
            expired = [k for k, (_, t) in self._cache.items() if now - t > self._cache_ttl]
            for k in expired:
                self._cache.pop(k, None)

        _log.debug(
            "wayback_cdx_done | url=%s | rows=%d | elapsed_ms=%d",
            url,
            len(out),
            int((asyncio.get_running_loop().time() - started) * 1000),
        )

        return out

    async def _query_cdx_with_budget(
        self,
        *,
        url: str,
        from_ts: str = "",
        to_ts: str = "",
        limit: int = 8,
        include_non_200: bool = False,
        deadline: float | None = None,
    ) -> list[_Snapshot]:
        try:
            return await self._query_cdx(
                url=url,
                from_ts=from_ts,
                to_ts=to_ts,
                limit=limit,
                include_non_200=include_non_200,
                deadline=deadline,
            )
        except TypeError as exc:
            # Compatibility path for monkeypatched tests/older signatures.
            if "deadline" not in str(exc):
                raise
            return await self._query_cdx(
                url=url,
                from_ts=from_ts,
                to_ts=to_ts,
                limit=limit,
                include_non_200=include_non_200,
            )

    async def _lookup_closest_with_budget(
        self,
        *,
        url: str,
        timestamp: str = "",
        deadline: float | None = None,
    ) -> _Snapshot | None:
        try:
            return await self._lookup_closest(url=url, timestamp=timestamp, deadline=deadline)
        except TypeError as exc:
            # Compatibility path for monkeypatched tests/older signatures.
            if "deadline" not in str(exc):
                raise
            return await self._lookup_closest(url=url, timestamp=timestamp)

    async def _lookup_closest(
        self,
        *,
        url: str,
        timestamp: str = "",
        deadline: float | None = None,
    ) -> _Snapshot | None:
        client = await self._get_client()
        params = {"url": url}
        ts = _normalize_timestamp(timestamp)
        if ts:
            params["timestamp"] = ts
        resp = await self._await_with_deadline(
            client.get(_WAYBACK_AVAILABLE_URL, params=params),
            deadline=deadline,
        )
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, dict):
            return None
        snap_info = data.get("archived_snapshots", {}).get("closest", {})
        if not isinstance(snap_info, dict) or not snap_info.get("available"):
            return None
        raw_url = normalize_text(str(snap_info.get("url", "")))
        parsed = _parse_wayback_url(raw_url)
        if parsed:
            parsed.statuscode = normalize_text(str(snap_info.get("status", "")))
            return parsed
        snap_ts = _normalize_timestamp(str(snap_info.get("timestamp", "")))
        if not snap_ts:
            return None
        return _Snapshot(timestamp=snap_ts, original=url)

    async def _query_available_years(self, *, url: str, from_year: int = 0, to_year: int = 0) -> list[int]:
        _ = (url, from_year, to_year)
        return []

    async def _resolve_snapshot(
        self,
        *,
        url: str,
        year: int | None,
        timestamp: str,
        deadline: float | None = None,
    ) -> tuple[_Snapshot | None, str]:
        op_deadline = deadline if deadline is not None else self._new_deadline()
        parsed_wayback = _parse_wayback_url(url)
        if parsed_wayback:
            return parsed_wayback, "input_wayback_url"

        target_ts = _normalize_timestamp(timestamp)
        if not target_ts and year:
            target_ts = f"{max(1900, min(2099, int(year)))}0701000000"
        variants = _url_variants(url)
        if not variants:
            return None, "invalid_url"

        errors: list[str] = []

        # 1) Timestamp-oriented search.
        if target_ts:
            ts_target_int = int(target_ts)

            async def _lookup_variant(variant: str) -> _Snapshot | None:
                return await self._lookup_closest_with_budget(
                    url=variant,
                    timestamp=target_ts,
                    deadline=op_deadline,
                )

            variant, closest = await self._race_variants(
                variants=variants,
                worker=_lookup_variant,
                is_success=lambda snap: bool(snap and snap.timestamp),
                deadline=op_deadline,
                errors=errors,
            )
            if closest and closest.timestamp:
                return closest, f"available_closest:{variant}"

            win_from, win_to = _ts_window_around(target_ts, days=150)
            if win_from and win_to:
                async def _query_window(variant: str) -> list[_Snapshot]:
                    return await self._query_cdx_with_budget(
                        url=variant,
                        from_ts=win_from,
                        to_ts=win_to,
                        limit=max(self._default_limit, 24),
                        include_non_200=self._include_non_200_default,
                        deadline=op_deadline,
                    )

                variant, rows = await self._race_variants(
                    variants=variants,
                    worker=_query_window,
                    is_success=lambda items: bool(items),
                    deadline=op_deadline,
                    errors=errors,
                )
                if rows:
                    picked = min(rows, key=lambda row: abs(int(row.timestamp) - ts_target_int))
                    return picked, f"cdx_window:{variant}"

        # 2) Year-oriented search.
        if year:
            y_from, y_to = _ts_year_window(year)
            target_for_pick = target_ts or y_from
            target_int = int(target_for_pick)

            async def _query_year(variant: str) -> list[_Snapshot]:
                return await self._query_cdx_with_budget(
                    url=variant,
                    from_ts=y_from,
                    to_ts=y_to,
                    limit=max(self._default_limit, 18),
                    include_non_200=self._include_non_200_default,
                    deadline=op_deadline,
                )

            variant, rows = await self._race_variants(
                variants=variants,
                worker=_query_year,
                is_success=lambda items: bool(items),
                deadline=op_deadline,
                errors=errors,
            )
            if rows:
                picked = min(rows, key=lambda row: abs(int(row.timestamp) - target_int))
                return picked, f"cdx_year:{variant}"

            # Retry nearby years when the exact year has no usable snapshot.
            for offset in range(1, self._retry_nearby + 1):
                for yy in (year - offset, year + offset):
                    if yy < 1900 or yy > 2099:
                        continue
                    y_from2, y_to2 = _ts_year_window(yy)
                    target2 = int(target_ts or y_from2)

                    async def _query_nearby(variant: str, _from: str = y_from2, _to: str = y_to2) -> list[_Snapshot]:
                        return await self._query_cdx_with_budget(
                            url=variant,
                            from_ts=_from,
                            to_ts=_to,
                            limit=max(self._default_limit, 14),
                            include_non_200=self._include_non_200_default,
                            deadline=op_deadline,
                        )

                    variant, rows = await self._race_variants(
                        variants=variants,
                        worker=_query_nearby,
                        is_success=lambda items: bool(items),
                        deadline=op_deadline,
                        errors=errors,
                    )
                    if rows:
                        picked = min(rows, key=lambda row: abs(int(row.timestamp) - target2))
                        return picked, f"cdx_nearby_year:{yy}:{variant}"

        # If caller explicitly constrained by year/timestamp, avoid unrelated latest fallback.
        if year or target_ts:
            if errors:
                _log.debug("wayback_resolve_constraint_not_found | url=%s | errors=%s", url, clip_text("; ".join(errors), 300))
            return None, "constraint_not_found"

        # 3) Unconstrained fallback (latest snapshot).
        async def _lookup_latest(variant: str) -> _Snapshot | None:
            return await self._lookup_closest_with_budget(url=variant, deadline=op_deadline)

        variant, latest = await self._race_variants(
            variants=variants,
            worker=_lookup_latest,
            is_success=lambda snap: bool(snap and snap.timestamp),
            deadline=op_deadline,
            errors=errors,
        )
        if latest and latest.timestamp:
            return latest, f"available_latest:{variant}"

        async def _query_latest_rows(variant: str) -> list[_Snapshot]:
            return await self._query_cdx_with_budget(
                url=variant,
                limit=self._default_limit,
                include_non_200=self._include_non_200_default,
                deadline=op_deadline,
            )

        variant, rows = await self._race_variants(
            variants=variants,
            worker=_query_latest_rows,
            is_success=lambda items: bool(items),
            deadline=op_deadline,
            errors=errors,
        )
        if rows:
            rows = sorted(rows, key=lambda s: s.timestamp, reverse=True)
            return rows[0], f"cdx_fallback:{variant}"

        if errors:
            _log.debug("wayback_resolve_not_found | url=%s | errors=%s", url, clip_text("; ".join(errors), 300))
        return None, "not_found"

    async def _fetch_snapshot_text(
        self,
        snapshot: _Snapshot,
        *,
        max_chars: int,
        deadline: float | None = None,
    ) -> dict[str, Any]:
        client = await self._get_client()
        candidates = [snapshot.raw_url(), snapshot.ui_url()]
        errors: list[str] = []

        for page_url in candidates:
            try:
                resp = await self._await_with_deadline(
                    client.get(page_url),
                    deadline=deadline,
                )
            except Exception as exc:
                errors.append(f"{page_url} -> {exc}")
                continue
            if resp.status_code >= 400:
                errors.append(f"{page_url} -> status={resp.status_code}")
                continue

            html, encoding = _decode_best(
                resp.content,
                response_encoding=str(resp.encoding or ""),
                content_type=str(resp.headers.get("content-type", "")),
            )
            text = _html_to_text(html, max_chars=max_chars)
            if not text:
                errors.append(f"{page_url} -> empty_text")
                continue

            return {
                "page_url": page_url,
                "status_code": resp.status_code,
                "encoding": encoding,
                "title": _extract_title(html),
                "text": text,
            }

        raise RuntimeError("; ".join(errors) if errors else "snapshot_fetch_failed")

    async def _llm_extract(
        self,
        *,
        instruction: str,
        snapshot: _Snapshot,
        page_url: str,
        title: str,
        text: str,
    ) -> str:
        if not self._model_client:
            return ""
        prompt = (
            "Please extract requested facts from this Wayback snapshot.\n"
            "Requirements: use Simplified Chinese, do not hallucinate, say unknown when needed.\n\n"
            f"User request:\n{instruction}\n\n"
            f"Snapshot time: {_pretty_ts(snapshot.timestamp)}\n"
            f"Original URL: {snapshot.original}\n"
            f"Snapshot URL: {page_url}\n"
            f"Page title: {title or '(none)'}\n\n"
            f"Page text (truncated):\n{clip_text(text, self._llm_input_chars)}"
        )
        messages = [
            {
                "role": "system",
                "content": "You are a web archive extraction assistant. Reply in Simplified Chinese.",
            },
            {"role": "user", "content": prompt},
        ]
        try:
            out = await self._model_client.chat_text_with_retry(
                messages=messages,
                max_tokens=self._llm_max_tokens,
                retries=1,
                backoff=0.8,
            )
            return normalize_text(str(out or ""))
        except Exception as exc:
            _log.warning("wayback llm extract failed | %s", exc)
            return ""

    async def _handle_wayback_lookup(self, args: dict[str, Any], context: dict[str, Any]) -> Any:
        from core.agent_tools import ToolCallResult

        if not self._enabled:
            return ToolCallResult(ok=False, error="disabled", display="wayback plugin is disabled")
        started = asyncio.get_running_loop().time()
        deadline = self._new_deadline()

        url = _normalize_url(args.get("url", ""))
        if not url:
            return ToolCallResult(ok=False, error="invalid_url", display="invalid or missing url")
        variants = _url_variants(url)
        if not variants:
            return ToolCallResult(ok=False, error="invalid_url", display="invalid or missing url")

        year = int(args.get("year", 0) or 0)
        limit = max(1, min(20, int(args.get("limit", self._default_limit) or self._default_limit)))
        include_non_200 = bool(args.get("include_non_200", self._include_non_200_default))
        from_ts = _normalize_timestamp(args.get("from_ts", ""))
        to_ts = _normalize_timestamp(args.get("to_ts", ""))
        if year and not from_ts and not to_ts:
            from_ts, to_ts = _ts_year_window(year)

        rows_all: list[_Snapshot] = []
        errors: list[str] = []
        source_variant = ""

        async def _query_variant(variant: str) -> list[_Snapshot]:
            return await self._query_cdx_with_budget(
                url=variant,
                from_ts=from_ts,
                to_ts=to_ts,
                limit=max(limit, 12),
                include_non_200=include_non_200,
                deadline=deadline,
            )

        source_variant, first_rows = await self._race_variants(
            variants=variants,
            worker=_query_variant,
            is_success=lambda items: bool(items),
            deadline=deadline,
            errors=errors,
        )
        if first_rows:
            rows_all.extend(first_rows)

        rows = _dedupe_snapshots(rows_all)
        rows.sort(key=lambda s: s.timestamp, reverse=True)
        rows = rows[:limit]

        if not rows:
            fallback_snapshot: _Snapshot | None = None
            fallback_mode = ""
            try:
                try:
                    fallback_snapshot, fallback_mode = await self._resolve_snapshot(
                        url=url,
                        year=year or None,
                        timestamp=from_ts or to_ts,
                        deadline=deadline,
                    )
                except TypeError as exc:
                    if "deadline" not in str(exc):
                        raise
                    fallback_snapshot, fallback_mode = await self._resolve_snapshot(
                        url=url,
                        year=year or None,
                        timestamp=from_ts or to_ts,
                    )
            except Exception as exc:
                errors.append(f"resolve_snapshot:{exc}")

            if fallback_snapshot is not None:
                display_lines = [
                    f"Wayback closest snapshot for {url}:",
                    f"1. {_pretty_ts(fallback_snapshot.timestamp)} | status={fallback_snapshot.statuscode or '-'} | {clip_text(fallback_snapshot.ui_url(), 120)}",
                    f"mode: {fallback_mode}",
                ]
                _log.info(
                    "wayback_lookup_done | url=%s | count=1 | source=%s | approximate=true | elapsed_ms=%d | errors=%d",
                    url,
                    fallback_mode or "-",
                    int((asyncio.get_running_loop().time() - started) * 1000),
                    len(errors),
                )
                return ToolCallResult(
                    ok=True,
                    data={
                        "url": url,
                        "year": year or None,
                        "from_ts": from_ts,
                        "to_ts": to_ts,
                        "count": 1,
                        "approximate": True,
                        "source_variant": fallback_mode,
                        "snapshots": [_snapshot_dict(fallback_snapshot)],
                        "recommended": _snapshot_dict(fallback_snapshot),
                    },
                    display="\n".join(display_lines),
                )

            msg = f"no snapshots found for {url}"
            if year:
                msg += f" in {year}"
            if errors:
                msg += f" | variants_errors={clip_text('; '.join(errors), 220)}"
            _log.info(
                "wayback_lookup_done | url=%s | count=0 | elapsed_ms=%d | errors=%d",
                url,
                int((asyncio.get_running_loop().time() - started) * 1000),
                len(errors),
            )
            return ToolCallResult(ok=False, error="no_snapshot", display=msg)

        target = ""
        if year:
            target = f"{year}0701000000"
        elif from_ts:
            target = from_ts
        recommended = _pick_nearest(rows, target) if target else rows[0]

        lines = [f"Wayback snapshots for {url} (showing {len(rows)}):"]
        for i, s in enumerate(rows[:limit], start=1):
            lines.append(
                f"{i}. {_pretty_ts(s.timestamp)} | status={s.statuscode or '-'} | "
                f"{clip_text(s.ui_url(), 120)}"
            )
        lines.append(
            f"recommended: {_pretty_ts(recommended.timestamp)} | {clip_text(recommended.ui_url(), 140)}"
        )

        _log.info(
            "wayback_lookup_done | url=%s | count=%d | source=%s | elapsed_ms=%d | errors=%d",
            url,
            len(rows),
            source_variant or "-",
            int((asyncio.get_running_loop().time() - started) * 1000),
            len(errors),
        )
        return ToolCallResult(
            ok=True,
            data={
                "url": url,
                "year": year or None,
                "from_ts": from_ts,
                "to_ts": to_ts,
                "count": len(rows),
                "source_variant": source_variant,
                "snapshots": [_snapshot_dict(s) for s in rows],
                "recommended": _snapshot_dict(recommended),
            },
            display="\n".join(lines),
        )

    async def _handle_wayback_extract(self, args: dict[str, Any], context: dict[str, Any]) -> Any:
        from core.agent_tools import ToolCallResult

        if not self._enabled:
            return ToolCallResult(ok=False, error="disabled", display="wayback plugin is disabled")
        started = asyncio.get_running_loop().time()
        deadline = self._new_deadline()

        url = _normalize_url(args.get("url", ""))
        if not url:
            return ToolCallResult(ok=False, error="invalid_url", display="invalid or missing url")

        year = int(args.get("year", 0) or 0) or None
        timestamp = _normalize_timestamp(args.get("timestamp", ""))
        instruction = normalize_text(args.get("instruction", ""))
        max_chars = int(args.get("max_chars", self._max_text_chars) or self._max_text_chars)
        max_chars = max(1000, min(self._max_text_chars, max_chars))

        try:
            snapshot, resolve_mode = await self._resolve_snapshot(
                url=url,
                year=year,
                timestamp=timestamp,
                deadline=deadline,
            )
        except TypeError as exc:
            if "deadline" not in str(exc):
                raise
            snapshot, resolve_mode = await self._resolve_snapshot(
                url=url,
                year=year,
                timestamp=timestamp,
            )
        if not snapshot:
            return ToolCallResult(
                ok=False,
                error="snapshot_not_found",
                display=f"no usable wayback snapshot found for {url}",
            )

        try:
            page = await self._fetch_snapshot_text(snapshot, max_chars=max_chars, deadline=deadline)
        except Exception as exc:
            return ToolCallResult(ok=False, error=f"fetch_failed:{exc}", display=f"snapshot fetch failed: {exc}")

        title = normalize_text(page.get("title", ""))
        text = str(page.get("text", ""))
        page_url = str(page.get("page_url", snapshot.raw_url()))
        extracted = ""
        if instruction:
            extracted = await self._llm_extract(
                instruction=instruction,
                snapshot=snapshot,
                page_url=page_url,
                title=title,
                text=text,
            )
        if not extracted:
            extracted = _lines_excerpt(text, max_lines=14, max_chars=1800)

        display_lines = [
            f"snapshot_time: {_pretty_ts(snapshot.timestamp)}",
            f"resolve_mode: {resolve_mode}",
            f"snapshot_url: {clip_text(page_url, 180)}",
        ]
        if title:
            display_lines.append(f"title: {clip_text(title, 140)}")
        display_lines.append("")
        display_lines.append(clip_text(extracted, 2200))

        _log.info(
            "wayback_extract_done | url=%s | mode=%s | status=%s | elapsed_ms=%d",
            url,
            resolve_mode,
            page.get("status_code", 0),
            int((asyncio.get_running_loop().time() - started) * 1000),
        )
        return ToolCallResult(
            ok=True,
            data={
                "url": url,
                "snapshot": _snapshot_dict(snapshot),
                "resolve_mode": resolve_mode,
                "snapshot_url": page_url,
                "encoding": page.get("encoding", ""),
                "status_code": page.get("status_code", 0),
                "title": title,
                "text_excerpt": clip_text(text, 4000),
                "answer": extracted,
            },
            display="\n".join(display_lines),
        )

    async def _handle_wayback_timeline(self, args: dict[str, Any], context: dict[str, Any]) -> Any:
        from core.agent_tools import ToolCallResult

        if not self._enabled:
            return ToolCallResult(ok=False, error="disabled", display="wayback plugin is disabled")
        started = asyncio.get_running_loop().time()
        deadline = self._new_deadline()

        url = _normalize_url(args.get("url", ""))
        if not url:
            return ToolCallResult(ok=False, error="invalid_url", display="invalid or missing url")

        from_year = int(args.get("from_year", 0) or 0)
        to_year = int(args.get("to_year", 0) or 0)
        limit = max(100, min(5000, int(args.get("limit", 2000) or 2000)))

        from_ts = ""
        to_ts = ""
        if from_year:
            from_ts = f"{max(1900, min(2099, from_year))}0101000000"
        if to_year:
            to_ts = f"{max(1900, min(2099, to_year))}1231235959"

        try:
            rows = await self._query_cdx_with_budget(
                url=url,
                from_ts=from_ts,
                to_ts=to_ts,
                limit=limit,
                include_non_200=self._include_non_200_default,
                deadline=deadline,
            )
        except Exception as exc:
            with contextlib.suppress(Exception):
                years = await self._query_available_years(url=url, from_year=from_year, to_year=to_year)
                if years:
                    years = sorted({int(y) for y in years if int(y) >= 1900})
                    return ToolCallResult(
                        ok=True,
                        data={
                            "url": url,
                            "approximate": True,
                            "years": years,
                            "counts_by_year": {},
                            "scanned": 0,
                        },
                        display=f"available-years fallback for {url}: {', '.join(str(y) for y in years)}",
                    )
            return ToolCallResult(ok=False, error=f"timeline_failed:{exc}", display=f"timeline failed: {exc}")

        if not rows:
            return ToolCallResult(ok=False, error="no_snapshot", display=f"no snapshots for {url}")

        counts: dict[str, int] = {}
        for s in rows:
            y = s.timestamp[:4]
            if len(y) != 4:
                continue
            counts[y] = counts.get(y, 0) + 1
        years = sorted(counts.keys())

        lines = [f"Wayback timeline for {url}:"]
        for y in years:
            lines.append(f"{y}: {counts[y]}")
        if rows:
            lines.append(
                f"latest: {_pretty_ts(rows[0].timestamp)} | {clip_text(rows[0].ui_url(), 120)}"
            )

        _log.info(
            "wayback_timeline_done | url=%s | years=%d | scanned=%d | elapsed_ms=%d",
            url,
            len(years),
            len(rows),
            int((asyncio.get_running_loop().time() - started) * 1000),
        )
        return ToolCallResult(
            ok=True,
            data={
                "url": url,
                "counts_by_year": counts,
                "scanned": len(rows),
                "latest": _snapshot_dict(rows[0]),
            },
            display="\n".join(lines),
        )


def _cell(row: list[Any], idx: dict[str, int], key: str) -> str:
    i = idx.get(key)
    if i is None or i < 0 or i >= len(row):
        return ""
    return str(row[i] or "")


def _snapshot_dict(s: _Snapshot) -> dict[str, Any]:
    return {
        "timestamp": s.timestamp,
        "time": _pretty_ts(s.timestamp),
        "original": s.original,
        "statuscode": s.statuscode,
        "mimetype": s.mimetype,
        "length": s.length,
        "digest": s.digest,
        "snapshot_url": s.ui_url(),
        "raw_snapshot_url": s.raw_url(),
    }


def _dedupe_snapshots(items: list[_Snapshot]) -> list[_Snapshot]:
    seen: set[tuple[str, str]] = set()
    out: list[_Snapshot] = []
    for s in items:
        key = (s.timestamp, normalize_text(s.original).lower())
        if key in seen:
            continue
        seen.add(key)
        out.append(s)
    return out


def _pick_nearest(items: list[_Snapshot], target_ts: str) -> _Snapshot:
    if not items:
        raise ValueError("empty snapshots")
    target = _normalize_timestamp(target_ts)
    if not target:
        return items[0]
    with contextlib.suppress(ValueError):
        t = int(target)
        return min(items, key=lambda s: abs(int(s.timestamp) - t))
    return items[0]
