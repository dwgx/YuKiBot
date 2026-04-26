from __future__ import annotations

import os
import platform
import re
import shutil
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping

NAPCAT_ID_KEYS = frozenset({
    "bot_id",
    "group_id",
    "group_openid",
    "message_id",
    "operator_id",
    "peer_id",
    "qq",
    "self_id",
    "target_id",
    "target_user_id",
    "user_id",
    "user_openid",
})
NAPCAT_API_ALIASES: dict[str, str] = {
    "send_group_message": "send_group_msg",
    "send_private_message": "send_private_msg",
    "get_user_info": "get_stranger_info",
    "get_message": "get_msg",
    "delete_message": "delete_msg",
    "get_group_notice": "_get_group_notice",
    "send_group_notice": "_send_group_notice",
    "delete_group_notice": "_del_group_notice",
    "set_group_sign": "send_group_sign",
    # NapCat 统一戳一戳 API（group_poke / friend_poke → send_poke）
    # 注意: 旧版 group_poke / friend_poke 仍然可用，但推荐用 send_poke
}
_VERSION_PART_RE = re.compile(r"\d+")
_STRING_ID_VERSION_FLOOR = (4, 8, 115)


def _clean_text(value: Any) -> str:
    return str(value or "").strip()


def normalize_napcat_id(value: Any) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if value.is_integer():
            return str(int(value))
        return None
    text = _clean_text(value)
    return text or None


def _normalize_value(value: Any, *, parent_key: str = "") -> Any:
    if isinstance(value, dict):
        normalized: dict[Any, Any] = {}
        for raw_key, item in value.items():
            key = _clean_text(raw_key)
            if key in NAPCAT_ID_KEYS:
                normalized_id = normalize_napcat_id(item)
                normalized[raw_key] = normalized_id if normalized_id is not None else item
                continue
            normalized[raw_key] = _normalize_value(item, parent_key=key)
        return normalized
    if isinstance(value, list):
        return [_normalize_value(item, parent_key=parent_key) for item in value]
    if parent_key in NAPCAT_ID_KEYS:
        normalized_id = normalize_napcat_id(value)
        if normalized_id is not None:
            return normalized_id
    return value


def normalize_napcat_api_kwargs(api: str, kwargs: Mapping[str, Any] | None) -> dict[str, Any]:
    _ = api
    source = dict(kwargs or {})
    return _normalize_value(source)


def resolve_napcat_api_name(api: str) -> str:
    text = _clean_text(api)
    if not text:
        return ""
    return NAPCAT_API_ALIASES.get(text, text)


async def call_napcat_api(
    api_call: Callable[..., Awaitable[Any]],
    api: str,
    **kwargs: Any,
) -> Any:
    resolved_api = resolve_napcat_api_name(api)
    return await api_call(resolved_api, **normalize_napcat_api_kwargs(resolved_api, kwargs))


async def call_napcat_bot_api(bot: Any, api: str, **kwargs: Any) -> Any:
    return await call_napcat_api(bot.call_api, api, **kwargs)


def extract_napcat_version_info(payload: Any) -> dict[str, str]:
    version = payload if isinstance(payload, dict) else {}
    data = version.get("data")
    if isinstance(data, dict):
        version = data

    def pick(*keys: str) -> str:
        for key in keys:
            text = _clean_text(version.get(key, ""))
            if text:
                return text
        return ""

    return {
        "app_name": pick("app_name", "name"),
        "app_version": pick("app_version", "version", "napcat_version", "plugin_version"),
        "protocol_version": pick("protocol_version", "protocol", "onebot_version"),
    }


def parse_napcat_version(value: Any) -> tuple[int, ...]:
    text = _clean_text(value)
    if not text:
        return ()
    parts = _VERSION_PART_RE.findall(text)
    if not parts:
        return ()
    return tuple(int(part) for part in parts[:4])


def napcat_prefers_string_ids(version_payload: Any) -> bool | None:
    meta = extract_napcat_version_info(version_payload)
    version_tuple = parse_napcat_version(meta.get("app_version", ""))
    if not version_tuple:
        return None
    return version_tuple >= _STRING_ID_VERSION_FLOOR


def collect_linux_runtime_diagnostics() -> dict[str, Any]:
    system_name = platform.system().lower() or os.name.lower()
    home = Path.home()
    shell_paths = [
        Path("/opt/QQ/resources/app/app_launcher/napcat/napcat.mjs"),
        Path("/opt/QQ/qq"),
        Path("/usr/bin/napcat"),
        Path("/usr/local/bin/napcat"),
        home / "NapCat.Shell" / "napcat" / "napcat.mjs",
        home / "NapCat.Shell" / "napcat.mjs",
    ]
    service_units = [
        Path("/etc/systemd/system/napcat.service"),
        Path("/usr/lib/systemd/system/napcat.service"),
        Path("/lib/systemd/system/napcat.service"),
    ]
    binaries = {
        "napcat": shutil.which("napcat") or "",
        "qq": shutil.which("qq") or "",
        "ffmpeg": shutil.which("ffmpeg") or "",
        "ffprobe": shutil.which("ffprobe") or "",
        "node": shutil.which("node") or "",
        "npm": shutil.which("npm") or "",
    }
    existing_shell_paths = [str(path) for path in shell_paths if path.exists()]
    existing_service_units = [str(path) for path in service_units if path.exists()]
    return {
        "platform": system_name,
        "is_linux": system_name == "linux",
        "binaries": binaries,
        "ffmpeg_ready": bool(binaries["ffmpeg"]),
        "ffprobe_ready": bool(binaries["ffprobe"]),
        "media_stack_ready": bool(binaries["ffmpeg"] and binaries["ffprobe"]),
        "napcat_command_ready": bool(binaries["napcat"]),
        "qq_command_ready": bool(binaries["qq"]),
        "shell_install_paths": existing_shell_paths,
        "service_units": existing_service_units,
        "shell_install_detected": bool(existing_shell_paths or binaries["napcat"]),
    }


def build_napcat_diagnostics(
    *,
    status_payload: Any,
    version_payload: Any,
    bot_self_id: str = "",
    bot_id: str = "",
) -> dict[str, Any]:
    status = status_payload if isinstance(status_payload, dict) else {}
    version_meta = extract_napcat_version_info(version_payload)
    linux = collect_linux_runtime_diagnostics()
    return {
        "bot": {
            "bot_id": _clean_text(bot_id),
            "self_id": _clean_text(bot_self_id),
        },
        "runtime": {
            "online": bool(status.get("online", False)),
            "good": bool(status.get("good", False)),
            "status": status,
        },
        "version": version_meta,
        "compatibility": {
            "normalized_id_keys": sorted(NAPCAT_ID_KEYS),
            "string_id_normalization_active": True,
            "string_id_preferred_by_version": napcat_prefers_string_ids(version_payload),
        },
        "linux": linux,
    }
