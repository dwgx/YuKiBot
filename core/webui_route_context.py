from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

from fastapi import Request, Response, WebSocket


@dataclass(frozen=True)
class WebUIRouteContext:
    get_engine: Callable[[], Any]
    get_start_time: Callable[[], float]
    get_token: Callable[[], str]
    check_auth: Callable[[Request], Awaitable[None]]
    check_ws_auth: Callable[[WebSocket], Awaitable[bool]]
    set_auth_cookie: Callable[[Response, Request, str], None]
    clear_auth_cookie: Callable[[Response], None]
    count_registered_napcat_tools: Callable[[], int]
    collect_napcat_status: Callable[[str], Awaitable[dict[str, Any]]]
    resolve_log_file_path: Callable[[], Path]
    read_log_tail: Callable[[Path, int], list[str]]
    split_log_chunks: Callable[[str], list[str]]
    cookie_capabilities_payload: Callable[[], dict[str, Any]]
    start_bilibili_qr_session: Callable[[], Awaitable[dict[str, Any]]]
    bilibili_qr_status: Callable[[str], Awaitable[dict[str, Any]]]
    cancel_bilibili_qr_session: Callable[[str], dict[str, Any]]
    logger: Any
