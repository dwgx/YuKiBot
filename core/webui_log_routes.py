from __future__ import annotations

import asyncio
import contextlib
import json

from fastapi import APIRouter, Depends, Query, WebSocket, WebSocketDisconnect

from core.webui_route_context import WebUIRouteContext


def build_log_router(ctx: WebUIRouteContext) -> APIRouter:
    router = APIRouter()

    @router.get("/logs", dependencies=[Depends(ctx.check_auth)])
    async def get_logs(lines: int = Query(100, ge=1, le=10000)):
        log_file = ctx.resolve_log_file_path()
        log_lines = ctx.read_log_tail(log_file, lines)
        return {"lines": log_lines}

    @router.websocket("/logs/stream")
    async def ws_log_stream(ws: WebSocket):
        if not await ctx.check_ws_auth(ws):
            return

        await ws.accept()

        log_file = ctx.resolve_log_file_path()
        last_file = log_file
        last_size = log_file.stat().st_size if log_file.exists() else 0

        try:
            while True:
                await asyncio.sleep(1)
                log_file = ctx.resolve_log_file_path()
                if log_file != last_file:
                    last_file = log_file
                    last_size = 0

                if not log_file.exists():
                    continue

                current_size = log_file.stat().st_size
                if current_size > last_size:
                    with open(log_file, "r", encoding="utf-8", errors="replace") as f:
                        f.seek(last_size)
                        new_content = f.read()
                        if new_content:
                            for raw_line in new_content.splitlines():
                                for line in ctx.split_log_chunks(raw_line):
                                    await ws.send_text(json.dumps({"line": line}, ensure_ascii=False))
                    last_size = current_size
                elif current_size < last_size:
                    last_size = 0

        except WebSocketDisconnect:
            ctx.logger.debug("WebSocket 日志流断开")
        except RuntimeError as exc:
            message = str(exc).lower()
            if "websocket" in message and ("disconnect" in message or "close" in message):
                ctx.logger.debug("WebSocket 日志流关闭: %s", exc)
            else:
                ctx.logger.error("WebSocket 日志流错误: %s", exc)
        except Exception as exc:
            ctx.logger.error("WebSocket 日志流错误: %s", exc)
        finally:
            with contextlib.suppress(Exception):
                await ws.close()

    return router
