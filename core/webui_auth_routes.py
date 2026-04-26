from __future__ import annotations

import ipaddress
import inspect
import json
import os
import threading
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from core.webui_route_context import WebUIRouteContext


class _AuthAttemptStore:
    def __init__(self, path: Path, *, max_attempts: int, window_seconds: int) -> None:
        self._path = path
        self._max_attempts = max_attempts
        self._window_seconds = window_seconds
        self._lock = threading.Lock()
        self._memory_data: dict[str, list[float]] = {}
        self._warned_storage_error = False

    def _prune(self, data: dict[str, list[float]], now: float) -> dict[str, list[float]]:
        cutoff = now - float(self._window_seconds)
        pruned: dict[str, list[float]] = {}
        for key, values in data.items():
            kept = [float(item) for item in values if float(item) >= cutoff]
            if kept:
                pruned[str(key)] = kept
        return pruned

    def _load_locked(self, now: float) -> dict[str, list[float]]:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8")) if self._path.exists() else {}
        except Exception as exc:
            self._warn_storage_error("load", exc)
            return self._prune(dict(self._memory_data), now)
        data: dict[str, list[float]] = {}
        if isinstance(raw, dict):
            for key, values in raw.items():
                if not isinstance(values, list):
                    continue
                valid: list[float] = []
                for item in values:
                    try:
                        valid.append(float(item))
                    except Exception:
                        continue
                if valid:
                    data[str(key)] = valid
        pruned = self._prune(data, now)
        self._memory_data = pruned
        return pruned

    def _save_locked(self, data: dict[str, list[float]]) -> None:
        self._memory_data = {str(key): list(values) for key, values in data.items()}
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(self._path.suffix + ".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self._path)
        except OSError as exc:
            self._warn_storage_error("save", exc)

    def _warn_storage_error(self, action: str, exc: Exception) -> None:
        if self._warned_storage_error:
            return
        self._warned_storage_error = True
        print(f"[WebUI Auth] attempt store {action} failed, using memory fallback: {exc}", flush=True)

    def is_limited(self, key: str, now: float) -> bool:
        with self._lock:
            data = self._load_locked(now)
            limited = len(data.get(key, [])) >= self._max_attempts
            self._save_locked(data)
            return limited

    def record_failure(self, key: str, now: float) -> int:
        with self._lock:
            data = self._load_locked(now)
            attempts = data.setdefault(key, [])
            attempts.append(now)
            self._save_locked(data)
            return len(attempts)

    def clear(self, key: str, now: float) -> None:
        with self._lock:
            data = self._load_locked(now)
            if key in data:
                data.pop(key, None)
                self._save_locked(data)
            elif self._path.exists():
                self._save_locked(data)


def _normalize_ip(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        return str(ipaddress.ip_address(text))
    except ValueError:
        return ""


def _truthy_env(name: str) -> bool:
    return str(os.environ.get(name, "")).strip().lower() in {"1", "true", "yes", "on"}


def _extract_client_ip(request: Request) -> str:
    fallback = _normalize_ip(request.client.host if request.client else "") or "unknown"
    if not _truthy_env("WEBUI_TRUST_PROXY_HEADERS"):
        return fallback

    x_forwarded_for = str(request.headers.get("X-Forwarded-For", "")).strip()
    if x_forwarded_for:
        for part in x_forwarded_for.split(","):
            candidate = _normalize_ip(part)
            if candidate:
                return candidate

    x_real_ip = _normalize_ip(request.headers.get("X-Real-IP", ""))
    if x_real_ip:
        return x_real_ip
    return fallback


def build_auth_status_router(ctx: WebUIRouteContext) -> APIRouter:
    router = APIRouter()

    @router.get("/health")
    async def health():
        return {"status": "ok"}

    _AUTH_MAX_ATTEMPTS = 10
    _AUTH_WINDOW_SECONDS = 300
    _attempt_store = _AuthAttemptStore(
        ctx.resolve_auth_attempt_store_path(),
        max_attempts=_AUTH_MAX_ATTEMPTS,
        window_seconds=_AUTH_WINDOW_SECONDS,
    )

    @router.post("/auth")
    async def auth(request: Request):
        import hmac
        client_ip = _extract_client_ip(request)
        now = time.time()

        # 速率限制：失败登录按来源地址持久化计数，重启不清零。
        if _attempt_store.is_limited(client_ip, now):
            raise HTTPException(429, "登录尝试过于频繁，请稍后再试")

        body = await request.json()
        token = str(body.get("token", ""))
        expected = ctx.get_token()

        if not expected:
            raise HTTPException(403, "WEBUI_TOKEN 未配置")

        if not hmac.compare_digest(token, expected):
            _attempt_store.record_failure(client_ip, now)
            raise HTTPException(401, "Token 错误")

        _attempt_store.clear(client_ip, now)
        response = JSONResponse({"ok": True})
        ctx.set_auth_cookie(response, request, expected)
        return response

    @router.get("/auth/session")
    async def auth_session(request: Request):
        await ctx.check_auth(request)
        return {"ok": True}

    @router.post("/auth/logout")
    async def auth_logout():
        response = JSONResponse({"ok": True})
        ctx.clear_auth_cookie(response)
        return response

    @router.get("/status", dependencies=[Depends(ctx.check_auth)])
    async def status():
        engine = ctx.get_engine()
        if not engine:
            raise HTTPException(503, "引擎未初始化")

        admin = getattr(engine, "admin", None)
        uptime = int(time.time() - getattr(admin, "_started", ctx.get_start_time()))
        msg_count = getattr(admin, "_count", 0)
        white = list(getattr(admin, "_white", set()))

        model_client = getattr(engine, "model_client", None)
        provider = getattr(model_client, "provider", "?") if model_client else "?"
        model = getattr(model_client, "model", "?") if model_client else "?"

        agent = getattr(engine, "agent", None)
        agent_enable = getattr(agent, "enable", False) if agent else False

        safety = getattr(engine, "safety", None)
        scale = int(getattr(safety, "scale", 2)) if safety else 2

        registry = getattr(engine, "agent_tool_registry", None)
        tool_count = getattr(registry, "tool_count", 0) if registry else 0

        plugins_obj = getattr(engine, "plugins", None)
        plugin_map = getattr(plugins_obj, "plugins", {}) if plugins_obj else {}
        plugin_list = []
        if isinstance(plugin_map, dict):
            for name, obj in plugin_map.items():
                desc = getattr(obj, "description", "") or ""
                plugin_list.append({"name": name, "description": str(desc)})

        queue_cfg = engine.config.get("queue", {}) if isinstance(getattr(engine, "config", None), dict) else {}
        if not isinstance(queue_cfg, dict):
            queue_cfg = {}
        runtime_agent_rows: list[dict[str, Any]] = []
        runtime_state_provider = getattr(engine, "runtime_agent_state_provider", None)
        if callable(runtime_state_provider):
            try:
                provider_rows = runtime_state_provider(limit=200)
                if inspect.isawaitable(provider_rows):
                    provider_rows = await provider_rows
                if isinstance(provider_rows, list):
                    runtime_agent_rows = [row for row in provider_rows if isinstance(row, dict)]
            except Exception:
                runtime_agent_rows = []

        group_concurrency = max(1, int(queue_cfg.get("group_concurrency", 1) or 1))
        single_inflight = bool(queue_cfg.get("single_inflight_per_conversation", True))
        max_concurrent_total = max(0, int(queue_cfg.get("max_concurrent_total", 0) or 0))
        multi_conversation_enabled = (not single_inflight) and max_concurrent_total != 1

        return {
            "uptime_seconds": uptime,
            "message_count": msg_count,
            "whitelist_groups": white,
            "model": f"{provider}/{model}",
            "agent_enabled": agent_enable,
            "tool_count": tool_count,
            "safety_scale": scale,
            "bot_name": getattr(engine, "bot_name", "YuKiKo"),
            "plugins": plugin_list,
            "queue": {
                "group_concurrency": group_concurrency,
                "single_inflight_per_conversation": single_inflight,
                "max_concurrent_total": max_concurrent_total,
                "multi_conversation_enabled": multi_conversation_enabled,
                "active_conversations": len(runtime_agent_rows),
            },
            "napcat": {
                "registered_tools": ctx.count_registered_napcat_tools(),
                "diagnostics_path": "/api/webui/napcat/status",
            },
        }

    @router.get("/napcat/status", dependencies=[Depends(ctx.check_auth)])
    async def napcat_status(bot_id: str = Query("", description="可选，指定 OneBot bot_id")):
        return await ctx.collect_napcat_status(bot_id)

    return router
