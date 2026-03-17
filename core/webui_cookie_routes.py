from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import yaml
from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse

from core.webui_route_context import WebUIRouteContext
from utils.text import normalize_text

_ROOT_DIR = Path(__file__).resolve().parents[1]


def build_cookie_router(ctx: WebUIRouteContext) -> APIRouter:
    router = APIRouter()

    def _cookie_error(
        *,
        status_code: int,
        code: str,
        message: str,
        hint: str = "",
        detail: str = "",
    ) -> JSONResponse:
        payload: dict[str, Any] = {"ok": False, "error_code": code, "message": message}
        if hint:
            payload["hint"] = hint
        if detail:
            payload["detail"] = detail
        return JSONResponse(payload, status_code=status_code)

    @router.get("/cookies/capabilities", dependencies=[Depends(ctx.check_auth)])
    async def cookies_capabilities():
        return {"ok": True, "data": ctx.cookie_capabilities_payload()}

    @router.post("/cookies/bilibili-qr/start", dependencies=[Depends(ctx.check_auth)])
    async def cookies_bilibili_qr_start():
        result = await ctx.start_bilibili_qr_session()
        if not result.get("ok"):
            return JSONResponse(result, status_code=503)
        return result

    @router.get("/cookies/bilibili-qr/status", dependencies=[Depends(ctx.check_auth)])
    async def cookies_bilibili_qr_status(session_id: str = Query("")):
        sid = normalize_text(session_id)
        if not sid:
            return JSONResponse({"ok": False, "status": "error", "message": "缺少 session_id"}, status_code=400)
        result = await ctx.bilibili_qr_status(sid)
        if not result.get("ok") and str(result.get("status", "") or "") in {"expired", "error"}:
            return JSONResponse(result, status_code=410 if result.get("status") == "expired" else 400)
        return result

    @router.post("/cookies/bilibili-qr/cancel", dependencies=[Depends(ctx.check_auth)])
    async def cookies_bilibili_qr_cancel(request: Request):
        body = await request.json()
        sid = normalize_text(str(body.get("session_id", "")))
        if not sid:
            return JSONResponse({"ok": False, "message": "缺少 session_id"}, status_code=400)
        return ctx.cancel_bilibili_qr_session(sid)

    @router.post("/cookies/extract", dependencies=[Depends(ctx.check_auth)])
    async def extract_cookie(request: Request):
        try:
            body = await request.json()
            platform = normalize_text(str(body.get("platform", "bilibili"))).lower() or "bilibili"
            if platform == "qq":
                platform = "qzone"
            browser = normalize_text(str(body.get("browser", "edge"))).lower() or "edge"
            allow_close = bool(body.get("allow_close", False))
            loop = asyncio.get_running_loop()

            from core.cookie_auth import (
                extract_bilibili_cookies,
                extract_douyin_cookie,
                extract_kuaishou_cookie,
                extract_qzone_cookies,
            )

            if platform == "bilibili":
                result = await loop.run_in_executor(
                    None,
                    lambda: extract_bilibili_cookies(browser=browser, auto_close=allow_close),
                )
                if result and isinstance(result, dict):
                    sessdata = normalize_text(str(result.get("sessdata", "") or result.get("SESSDATA", "")))
                    bili_jct = normalize_text(str(result.get("bili_jct", "") or result.get("BILI_JCT", "")))
                    if sessdata:
                        cookie_dict = {"SESSDATA": sessdata}
                        if bili_jct:
                            cookie_dict["bili_jct"] = bili_jct
                        return JSONResponse(
                            {
                                "ok": True,
                                "cookie": json.dumps(cookie_dict, ensure_ascii=False),
                                "message": "B站 Cookie 提取成功（浏览器）",
                                "sessdata": sessdata,
                                "bili_jct": bili_jct,
                            }
                        )
                return _cookie_error(
                    status_code=400,
                    code="bilibili_extract_failed",
                    message="B站 Cookie 提取失败",
                    hint="请先在浏览器登录 B站，或改用“B站扫码登录”。",
                )

            if platform == "douyin":
                cookie = await loop.run_in_executor(
                    None,
                    lambda: extract_douyin_cookie(browser=browser, auto_close=allow_close),
                )
                if cookie:
                    return JSONResponse({"ok": True, "cookie": cookie, "message": "抖音 Cookie 提取成功"})
                return _cookie_error(
                    status_code=400,
                    code="douyin_extract_failed",
                    message="抖音 Cookie 提取失败",
                    hint="请确认已在当前浏览器登录抖音账号后重试。",
                )

            if platform == "kuaishou":
                cookie = await loop.run_in_executor(
                    None,
                    lambda: extract_kuaishou_cookie(browser=browser, auto_close=allow_close),
                )
                if cookie:
                    return JSONResponse({"ok": True, "cookie": cookie, "message": "快手 Cookie 提取成功"})
                return _cookie_error(
                    status_code=400,
                    code="kuaishou_extract_failed",
                    message="快手 Cookie 提取失败",
                    hint="请确认已在当前浏览器登录快手账号后重试。",
                )

            if platform == "qzone":
                cookie = await loop.run_in_executor(
                    None,
                    lambda: extract_qzone_cookies(browser=browser, auto_close=allow_close),
                )
                if cookie:
                    return JSONResponse({"ok": True, "cookie": cookie, "message": "QQ空间 Cookie 提取成功"})
                return _cookie_error(
                    status_code=400,
                    code="qzone_extract_failed",
                    message="QQ空间 Cookie 提取失败",
                    hint="请先登录 qzone.qq.com / user.qzone.qq.com，再重试提取。",
                )

            return _cookie_error(
                status_code=400,
                code="unsupported_platform",
                message="不支持的平台",
            )

        except Exception as exc:
            ctx.logger.error("Cookie 提取失败: %s", exc, exc_info=True)
            return _cookie_error(
                status_code=500,
                code="internal_error",
                message="Cookie 提取失败（内部错误）",
                hint="请查看日志并检查浏览器登录状态后重试。",
                detail=str(exc),
            )

    @router.post("/cookies/save", dependencies=[Depends(ctx.check_auth)])
    async def save_cookie(request: Request):
        try:
            body = await request.json()
            platform = normalize_text(str(body.get("platform", "bilibili"))).lower() or "bilibili"
            if platform == "qq":
                platform = "qzone"
            cookie = body.get("cookie", "")

            if not cookie:
                return JSONResponse({"error": "Cookie 不能为空"}, status_code=400)

            config_file = _ROOT_DIR / "config" / "config.yml"
            if not config_file.exists():
                return JSONResponse({"error": "配置文件不存在"}, status_code=404)

            config = yaml.safe_load(config_file.read_text(encoding="utf-8")) or {}
            if not isinstance(config, dict):
                config = {}

            if platform == "bilibili":
                try:
                    cookie_dict: dict[str, Any]
                    if isinstance(cookie, dict):
                        cookie_dict = cookie
                    else:
                        cookie_text = normalize_text(str(cookie))
                        if cookie_text.startswith("{") and cookie_text.endswith("}"):
                            parsed = json.loads(cookie_text)
                            cookie_dict = parsed if isinstance(parsed, dict) else {}
                        else:
                            cookie_dict = {}
                            for part in cookie_text.split(";"):
                                if "=" not in part:
                                    continue
                                key, value = part.split("=", 1)
                                key = normalize_text(key)
                                value = normalize_text(value)
                                if key:
                                    cookie_dict[key] = value
                    sessdata = normalize_text(str(cookie_dict.get("SESSDATA", "") or cookie_dict.get("sessdata", "")))
                    bili_jct = normalize_text(str(cookie_dict.get("bili_jct", "") or cookie_dict.get("BILI_JCT", "")))
                    if not sessdata:
                        return JSONResponse({"error": "B站 Cookie 缺少 SESSDATA"}, status_code=400)

                    if "video_analysis" not in config:
                        config["video_analysis"] = {}
                    if "bilibili" not in config["video_analysis"]:
                        config["video_analysis"]["bilibili"] = {}

                    config["video_analysis"]["bilibili"]["sessdata"] = sessdata
                    config["video_analysis"]["bilibili"]["bili_jct"] = bili_jct
                except Exception:
                    return JSONResponse({"error": "B站 Cookie 格式错误"}, status_code=400)

            elif platform in ["douyin", "kuaishou", "qzone"]:
                if "video_analysis" not in config:
                    config["video_analysis"] = {}
                if platform not in config["video_analysis"]:
                    config["video_analysis"][platform] = {}

                config["video_analysis"][platform]["cookie"] = cookie
            else:
                return JSONResponse({"error": "不支持的平台"}, status_code=400)

            config_file.write_text(
                yaml.safe_dump(config, allow_unicode=True, default_flow_style=False),
                encoding="utf-8",
            )

            return JSONResponse({"message": f"{platform} Cookie 保存成功"})

        except Exception as exc:
            ctx.logger.error("Cookie 保存失败: %s", exc)
            return JSONResponse({"error": str(exc)}, status_code=500)

    return router
