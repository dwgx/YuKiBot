from __future__ import annotations

import inspect
import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from core.webui_route_context import WebUIRouteContext


def build_auth_status_router(ctx: WebUIRouteContext) -> APIRouter:
    router = APIRouter()

    @router.get("/health")
    async def health():
        return {"status": "ok"}

    _auth_attempts: dict[str, list[float]] = {}
    _AUTH_MAX_ATTEMPTS = 10
    _AUTH_WINDOW_SECONDS = 300

    @router.post("/auth")
    async def auth(request: Request):
        import hmac
        client_ip = request.client.host if request.client else "unknown"
        now = time.time()

        # 速率限制：每 IP 5 分钟内最多 10 次
        attempts = _auth_attempts.setdefault(client_ip, [])
        attempts[:] = [t for t in attempts if now - t < _AUTH_WINDOW_SECONDS]
        if len(attempts) >= _AUTH_MAX_ATTEMPTS:
            raise HTTPException(429, "登录尝试过于频繁，请稍后再试")
        attempts.append(now)

        body = await request.json()
        token = str(body.get("token", ""))
        expected = ctx.get_token()

        if not expected:
            raise HTTPException(403, "WEBUI_TOKEN 未配置")

        if not hmac.compare_digest(token, expected):
            raise HTTPException(401, "Token 错误")

        response = JSONResponse({"ok": True})
        ctx.set_auth_cookie(response, request, expected)
        return response

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
