"""NewAPI 管理插件 — 通过 Bot 操作 NewAPI 站点的核心功能。

支持功能:
  - 账号注册 / 登录 (私聊绑定)
  - 令牌管理 (创建 / 列表 / 删除 / 修改额度 / 过期时间 / 分组)
  - 模型列表 / 分组查看
  - 钱包余额 / 使用统计
  - 订阅套餐查看
  - 签到 / 邀请奖励
  - 个人设置 (绑定邮箱)

用户通过私聊 Bot 提供账号密码完成绑定，之后所有操作自动认证。
"""
from __future__ import annotations

import base64
import copy
import hashlib
import html
import json
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urljoin

import httpx

from plugins.newapi_client import NewAPIClient

_log = logging.getLogger("yukiko.plugin.newapi")

# ── 用户凭据持久化 ──────────────────────────────────────────────────────

_CREDENTIALS_FILE = Path(__file__).parent / "config" / "newapi_credentials.json"
_PAYMENT_QR_CACHE_DIR = Path(__file__).resolve().parent.parent / "storage" / "newapi_pay_qr"
_PENDING_PAYMENTS_FILE = Path(__file__).resolve().parent.parent / "storage" / "newapi_pending_payments.json"
_PAYMENT_HTTP_TIMEOUT = httpx.Timeout(18.0, connect=8.0)
_PAYMENT_HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/123.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}
_PAYMENT_FOLLOW_MAX_STEPS = 8
_PENDING_PAYMENT_TTL_SECONDS = 2 * 60 * 60

# ── 会话缓存 (避免每条命令都做一次完整 login) ─────────────────────────────
_SESSION_TTL = 600  # 10 分钟
_session_cache: dict[str, tuple[dict[str, str], int | None, float]] = {}  # key -> (cookies, user_id, expires)

_PLUGIN_RUNTIME_DEFAULTS: dict[str, Any] = {
    "display_name": "NewAPI",
    "session_ttl_seconds": 600,
    "response": {
        "force_plain_text": True,
        "strip_markdown_chars": True,
    },
    "payment": {
        "auto_require_method_selection_when_multiple": True,
        "auto_prefer_methods": ["wxpay", "alipay", "qqpay"],
        "auto_fallback_method_when_info_unavailable": "wxpay",  # wxpay/alipay/qqpay/stripe/none
        "include_epay_submit_url": True,
        "show_amount_unit_hint": True,
        "show_topup_command_hints": True,
    },
}

_PLUGIN_RUNTIME_CFG: dict[str, Any] = copy.deepcopy(_PLUGIN_RUNTIME_DEFAULTS)


def _build_runtime_config(raw_config: dict[str, Any]) -> dict[str, Any]:
    cfg = copy.deepcopy(_PLUGIN_RUNTIME_DEFAULTS)
    src = raw_config if isinstance(raw_config, dict) else {}

    display_name = str(src.get("display_name", cfg["display_name"])).strip()
    cfg["display_name"] = display_name or str(cfg["display_name"])

    ttl_raw = src.get("session_ttl_seconds", cfg["session_ttl_seconds"])
    try:
        ttl = int(ttl_raw)
    except Exception:
        ttl = int(cfg["session_ttl_seconds"])
    cfg["session_ttl_seconds"] = max(60, min(86400, ttl))

    response_raw = src.get("response", {})
    if isinstance(response_raw, dict):
        cfg["response"]["force_plain_text"] = bool(
            response_raw.get("force_plain_text", cfg["response"]["force_plain_text"]),
        )
        cfg["response"]["strip_markdown_chars"] = bool(
            response_raw.get("strip_markdown_chars", cfg["response"]["strip_markdown_chars"]),
        )

    payment_raw = src.get("payment", {})
    if isinstance(payment_raw, dict):
        cfg["payment"]["auto_require_method_selection_when_multiple"] = bool(
            payment_raw.get(
                "auto_require_method_selection_when_multiple",
                cfg["payment"]["auto_require_method_selection_when_multiple"],
            ),
        )
        cfg["payment"]["include_epay_submit_url"] = bool(
            payment_raw.get("include_epay_submit_url", cfg["payment"]["include_epay_submit_url"]),
        )
        cfg["payment"]["show_amount_unit_hint"] = bool(
            payment_raw.get("show_amount_unit_hint", cfg["payment"]["show_amount_unit_hint"]),
        )
        cfg["payment"]["show_topup_command_hints"] = bool(
            payment_raw.get("show_topup_command_hints", cfg["payment"]["show_topup_command_hints"]),
        )

        methods_raw = payment_raw.get("auto_prefer_methods", cfg["payment"]["auto_prefer_methods"])
        methods: list[str] = []
        if isinstance(methods_raw, list):
            for item in methods_raw:
                method = _normalize_pay_method(str(item))
                if method in {"wxpay", "alipay", "qqpay"} and method not in methods:
                    methods.append(method)
        if not methods:
            methods = list(cfg["payment"]["auto_prefer_methods"])
        cfg["payment"]["auto_prefer_methods"] = methods

        fallback_raw = _normalize_pay_method(
            str(payment_raw.get(
                "auto_fallback_method_when_info_unavailable",
                cfg["payment"]["auto_fallback_method_when_info_unavailable"],
            )),
        )
        if fallback_raw not in {"wxpay", "alipay", "qqpay", "stripe", "none"}:
            fallback_raw = str(cfg["payment"]["auto_fallback_method_when_info_unavailable"])
        cfg["payment"]["auto_fallback_method_when_info_unavailable"] = fallback_raw

    return cfg


def _get_runtime_cfg() -> dict[str, Any]:
    return _PLUGIN_RUNTIME_CFG if isinstance(_PLUGIN_RUNTIME_CFG, dict) else _PLUGIN_RUNTIME_DEFAULTS


def _get_plugin_display_name() -> str:
    cfg = _get_runtime_cfg()
    raw = str(cfg.get("display_name", "")).strip() if isinstance(cfg, dict) else ""
    return raw or "NewAPI"


def _get_session_ttl_seconds() -> int:
    cfg = _get_runtime_cfg()
    try:
        ttl = int(cfg.get("session_ttl_seconds", _SESSION_TTL))
    except Exception:
        ttl = _SESSION_TTL
    return max(60, min(86400, ttl))


def _load_credentials() -> dict[str, dict]:
    """加载已绑定用户的凭据。格式: {platform_user_id: {username, password, site_url}}"""
    if _CREDENTIALS_FILE.exists():
        try:
            return json.loads(_CREDENTIALS_FILE.read_text("utf-8"))
        except Exception:
            pass
    return {}


def _save_credentials(creds: dict[str, dict]):
    _CREDENTIALS_FILE.parent.mkdir(parents=True, exist_ok=True)
    _CREDENTIALS_FILE.write_text(json.dumps(creds, ensure_ascii=False, indent=2), "utf-8")


def _load_pending_payments() -> dict[str, dict[str, Any]]:
    if _PENDING_PAYMENTS_FILE.exists():
        try:
            data = json.loads(_PENDING_PAYMENTS_FILE.read_text("utf-8"))
            if isinstance(data, dict):
                return {
                    str(key): value
                    for key, value in data.items()
                    if isinstance(value, dict)
                }
        except Exception:
            _log.warning("newapi_pending_payment_load_failed", exc_info=True)
    return {}


def _pending_payment_storage_key(key: str) -> str:
    raw = str(key or "").strip()
    if not raw:
        return ""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _save_pending_payments(data: dict[str, dict[str, Any]]) -> None:
    _PENDING_PAYMENTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    _PENDING_PAYMENTS_FILE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        "utf-8",
    )


def _prune_pending_payments(
    data: dict[str, dict[str, Any]] | None = None,
) -> dict[str, dict[str, Any]]:
    raw = data if isinstance(data, dict) else _load_pending_payments()
    now = int(time.time())
    out: dict[str, dict[str, Any]] = {}
    changed = False
    for key, value in raw.items():
        if not isinstance(value, dict):
            changed = True
            continue
        created_at = _safe_int(value.get("created_at"), 0)
        if created_at > 0 and now - created_at > _PENDING_PAYMENT_TTL_SECONDS:
            changed = True
            continue
        out[str(key)] = value
    if changed:
        _save_pending_payments(out)
    return out


def _get_pending_payment(key: str) -> dict[str, Any] | None:
    data = _prune_pending_payments()
    storage_key = _pending_payment_storage_key(key)
    value = data.get(storage_key)
    if not isinstance(value, dict):
        # 兼容旧版本的明文 key，读取后立即迁移。
        legacy_value = data.get(str(key))
        if isinstance(legacy_value, dict):
            data.pop(str(key), None)
            if storage_key:
                data[storage_key] = legacy_value
            _save_pending_payments(data)
            value = legacy_value
    return value if isinstance(value, dict) else None


def _set_pending_payment(key: str, payload: dict[str, Any]) -> None:
    data = _prune_pending_payments()
    storage_key = _pending_payment_storage_key(key)
    if not storage_key:
        return
    data.pop(str(key), None)
    data[storage_key] = payload
    _save_pending_payments(data)


def _clear_pending_payment(key: str) -> None:
    data = _prune_pending_payments()
    removed = False
    storage_key = _pending_payment_storage_key(key)
    if storage_key and data.pop(storage_key, None) is not None:
        removed = True
    if data.pop(str(key), None) is not None:
        removed = True
    if removed:
        _save_pending_payments(data)


def _user_key(context: dict) -> str:
    """生成跨平台唯一用户标识。"""
    uid = str(context.get("user_id", ""))
    platform = str(context.get("platform", "qq"))
    return f"{platform}:{uid}"


def _invalidate_session(key: str) -> None:
    """清除指定用户的会话缓存。"""
    _session_cache.pop(key, None)


async def _get_client(context: dict) -> NewAPIClient | None:
    """根据已绑定凭据创建已登录的客户端。复用缓存的会话避免重复登录。"""
    key = _user_key(context)
    creds = _load_credentials()
    info = creds.get(key)
    if not info:
        return None

    client = NewAPIClient(info["site_url"])
    now = time.time()

    # 尝试复用缓存的会话
    cached = _session_cache.get(key)
    if cached:
        cookies, user_id, expires = cached
        if now < expires:
            for k, v in cookies.items():
                client._http.cookies.set(k, v)
            client._user_id = user_id
            return client
        # 过期，清除
        del _session_cache[key]

    # 完整登录
    result = await client.login(info["username"], info["password"])
    if not result.get("success"):
        await client.close()
        return None

    # 缓存会话
    _session_cache[key] = (
        {k: v for k, v in client._http.cookies.items()},
        client._user_id,
        now + _get_session_ttl_seconds(),
    )
    return client


# ── 格式化工具 ────────────────────────────────────────────────────────────

def _fmt_quota(q: int) -> str:
    """将内部额度值转为可读字符串 (NewAPI 额度单位通常是 1/500000 美元)。"""
    if q <= 0:
        return "0"
    usd = q / 500000
    if usd >= 1_000_000:
        return f"${usd/1_000_000:.1f}M"
    if usd >= 1_000:
        return f"${usd/1_000:.1f}K"
    return f"${usd:.2f}"


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        if value is None:
            return default
        if isinstance(value, bool):
            return int(value)
        return int(float(str(value).strip()))
    except Exception:
        return default


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(str(value).strip())
    except Exception:
        return default


def _fmt_time(ts: int) -> str:
    if ts <= 0 or ts == -1:
        return "永不过期"
    try:
        return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return str(ts)


def _fmt_local_time(ts: int) -> str:
    if ts <= 0:
        return "-"
    try:
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return str(ts)


def _fmt_signed_quota(delta: int | None) -> str:
    if delta is None:
        return "-"
    if delta == 0:
        return "0"
    sign = "+" if delta > 0 else "-"
    return f"{sign}{_fmt_quota(abs(delta))}"


def _status_text(s: int) -> str:
    return {1: "✅启用", 2: "❌禁用", 3: "⏰已过期", 4: "💸已耗尽"}.get(s, f"未知({s})")


def _normalize_topup_status(raw_status: Any) -> str:
    status = str(raw_status or "").strip().lower()
    aliases = {
        "success": "success",
        "succeeded": "success",
        "paid": "success",
        "complete": "success",
        "completed": "success",
        "done": "success",
        "finished": "success",
        "pending": "pending",
        "wait": "pending",
        "waiting": "pending",
        "processing": "pending",
        "created": "pending",
        "fail": "failed",
        "failed": "failed",
        "error": "failed",
        "cancel": "cancelled",
        "cancelled": "cancelled",
        "canceled": "cancelled",
        "refund": "refunded",
        "refunded": "refunded",
    }
    return aliases.get(status, status)


def _topup_status_text(raw_status: Any) -> str:
    status = _normalize_topup_status(raw_status)
    if status == "success":
        return "已到账"
    if status == "pending":
        return "待支付"
    if status == "failed":
        return "支付失败"
    if status == "cancelled":
        return "已取消"
    if status == "refunded":
        return "已退款"
    return str(raw_status or "未知").strip() or "未知"


def _extract_topup_items(raw_data: Any) -> list[dict[str, Any]]:
    candidates: list[Any] = []
    if isinstance(raw_data, list):
        candidates = raw_data
    elif isinstance(raw_data, dict):
        for key in ("items", "list", "rows", "records"):
            if isinstance(raw_data.get(key), list):
                candidates = raw_data.get(key, [])
                break
        if not candidates and isinstance(raw_data.get("data"), list):
            candidates = raw_data.get("data", [])
        if not candidates and isinstance(raw_data.get("data"), dict):
            nested = raw_data.get("data", {})
            for key in ("items", "list", "rows", "records"):
                if isinstance(nested.get(key), list):
                    candidates = nested.get(key, [])
                    break
    return [item for item in candidates if isinstance(item, dict)]


def _find_topup_record(
    records: list[dict[str, Any]],
    *,
    site_order_no: str = "",
    amount: int = 0,
    method: str = "",
    created_at: int = 0,
) -> dict[str, Any] | None:
    order_no = str(site_order_no or "").strip()
    if order_no:
        for item in records:
            trade_no = str(item.get("trade_no", "")).strip()
            if trade_no == order_no:
                return item
        for item in records:
            trade_no = str(item.get("trade_no", "")).strip()
            if trade_no and order_no in trade_no:
                return item

    if amount <= 0 and not method and created_at <= 0:
        return records[0] if records else None

    best: dict[str, Any] | None = None
    best_score = -1
    for item in records:
        score = 0
        item_amount = _safe_int(item.get("amount"), 0)
        item_method = _normalize_pay_method(str(item.get("payment_method", "")))
        item_create_time = _safe_int(item.get("create_time"), 0)

        if amount > 0 and item_amount == amount:
            score += 4
        if method and item_method == method:
            score += 3
        if created_at > 0 and item_create_time > 0:
            if abs(item_create_time - created_at) <= 10 * 60:
                score += 2
            elif item_create_time >= created_at - 10 * 60:
                score += 1
        if score > best_score:
            best = item
            best_score = score
    return best if best_score > 0 else (records[0] if records else None)


def _topup_record_is_success(record: dict[str, Any] | None) -> bool:
    if not isinstance(record, dict):
        return False
    status = _normalize_topup_status(record.get("status"))
    if status == "success":
        return True
    return _safe_int(record.get("complete_time"), 0) > 0 and status not in {"failed", "cancelled", "refunded"}


def _pending_payment_brief(pending: dict[str, Any]) -> str:
    order_no = str(pending.get("site_order_no", "")).strip() or "-"
    method = _normalize_pay_method(str(pending.get("method", "")))
    method_name = {"wxpay": "微信", "alipay": "支付宝", "qqpay": "QQ", "stripe": "Stripe", "creem": "Creem"}.get(method, method or "-")
    amount = _safe_int(pending.get("amount"), 0)
    created_at = _safe_int(pending.get("created_at"), 0)
    return f"订单号 {order_no}，渠道 {method_name}，充值额度 {amount}，发起时间 {_fmt_local_time(created_at)}"


def _plain_reply(text: str) -> str:
    """按配置清理 Markdown 强调符号，保持插件回复可控。"""
    if not text:
        return ""
    value = str(text)
    cfg = _get_runtime_cfg()
    response_cfg = cfg.get("response", {}) if isinstance(cfg, dict) else {}
    if not isinstance(response_cfg, dict):
        response_cfg = {}

    if not bool(response_cfg.get("force_plain_text", True)):
        return value
    if bool(response_cfg.get("strip_markdown_chars", True)):
        value = value.replace("**", "").replace("`", "")
    return value


# ── 隐私 & 数据隔离 ──────────────────────────────────────────────────────

# 必须私聊才能执行的命令 (涉及密码、密钥、个人敏感数据)
_PRIVATE_ONLY_COMMANDS: set[str] = {
    "bind", "register",       # 密码
    "token.key",              # 完整密钥
    "token.create",           # 返回完整密钥
    "token.delete",           # 令牌操作
    "token.update",           # 令牌操作
    "me",                     # 邮箱、邀请码、额度等个人信息
    "balance",                # 余额
    "stats",                  # 使用统计
    "tokens",                 # 令牌列表 (含部分密钥)
    "email",                  # 邮箱绑定
    "aff",                    # 邀请链接
    "aff.transfer",           # 邀请奖励划转
    "topup",                  # 充值信息
    "pay",                    # 支付链接
    "pay.status",             # 支付状态 / 到账情况
}

# 群聊中允许的命令 (不含个人敏感数据)
# models, groups, subs, checkin, pricing, help, unbind

_PRIVATE_ONLY_MSG = (
    "🔒 此命令涉及个人敏感信息，请私聊 Bot 使用。\n"
    "直接私信我发送相同命令即可；若私信失败，请先加我好友或开启临时会话。"
)

_PASSWORD_INPUT_COMMANDS: set[str] = {"bind", "register", "email"}

_PRIVACY_GUARD_DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "sensitive_commands": sorted(_PASSWORD_INPUT_COMMANDS),
    "only_when_password_like": True,
    "recall_message": True,
    "notify_group": True,
    "notify_private": True,
    "group_notice_template": (
        "⚠️ 检测到可能包含密码的敏感命令，已提醒改为私聊操作。"
        "请不要在群内发送账号密码。"
    ),
    "private_notice_template": (
        "你刚才在群里发送的命令可能包含密码/敏感信息。"
        "请改用私聊发送：/api {command} ..."
    ),
}


def _safe_notice_template(template: str, values: dict[str, str]) -> str:
    """安全渲染提醒模板，避免 format KeyError。"""
    text = str(template or "").strip()
    if not text:
        return ""

    class _Map(dict):
        def __missing__(self, key: str) -> str:  # type: ignore[override]
            return ""

    try:
        return text.format_map(_Map(values))
    except Exception:
        return text


def _build_privacy_guard_config(raw_config: dict[str, Any]) -> dict[str, Any]:
    cfg = dict(_PRIVACY_GUARD_DEFAULTS)
    incoming = raw_config.get("privacy_guard", {}) if isinstance(raw_config, dict) else {}
    if not isinstance(incoming, dict):
        incoming = {}
    cfg.update(incoming)

    # 兼容旧字段名
    if "group_sensitive_recall" in raw_config and isinstance(raw_config.get("group_sensitive_recall"), bool):
        cfg["enabled"] = bool(raw_config.get("group_sensitive_recall"))

    commands_raw = cfg.get("sensitive_commands", [])
    if not isinstance(commands_raw, list):
        commands_raw = []
    commands = {
        _resolve_command(str(item), "")[0]
        for item in commands_raw
        if str(item).strip()
    }
    commands.discard("")
    if not commands:
        commands = set(_PASSWORD_INPUT_COMMANDS)
    cfg["sensitive_commands"] = commands
    cfg["enabled"] = bool(cfg.get("enabled", True))
    cfg["only_when_password_like"] = bool(cfg.get("only_when_password_like", True))
    cfg["recall_message"] = bool(cfg.get("recall_message", True))
    cfg["notify_group"] = bool(cfg.get("notify_group", True))
    cfg["notify_private"] = bool(cfg.get("notify_private", True))
    cfg["group_notice_template"] = str(
        cfg.get("group_notice_template", _PRIVACY_GUARD_DEFAULTS["group_notice_template"]),
    )
    cfg["private_notice_template"] = str(
        cfg.get("private_notice_template", _PRIVACY_GUARD_DEFAULTS["private_notice_template"]),
    )
    return cfg


def _looks_like_password_input(cmd: str, cmd_args: str, raw_text: str) -> bool:
    """判断是否像“带密码输入”的命令，避免误撤回普通群消息。"""
    merged = f"{cmd} {cmd_args} {raw_text}".lower()
    if any(k in merged for k in ("密码", "password", "pwd", "pass=", "口令")):
        return True

    parts = [p for p in str(cmd_args or "").split() if p]
    if cmd in {"bind", "register"} and len(parts) >= 3:
        return True
    if cmd == "email" and len(parts) >= 2 and "@" in parts[-1]:
        return True
    return False


def _context_is_private(context: dict[str, Any]) -> bool:
    if bool(context.get("is_private", False)):
        return True
    conversation_id = str(context.get("conversation_id", "")).strip().lower()
    return conversation_id.startswith("private:")

_COMMAND_ALIASES: dict[str, str] = {
    "api": "help",
    "/api": "help",
    "commands": "help",
    "command": "help",
    "帮助": "help",
    "?": "help",
    "token.list": "tokens",
    "token.ls": "tokens",
    "list_tokens": "tokens",
    "list-token": "tokens",
    "my.tokens": "tokens",
    "my_tokens": "tokens",
    "token.del": "token.delete",
    "token.rm": "token.delete",
    "delete_token": "token.delete",
    "remove_token": "token.delete",
    "update_token": "token.update",
    "edit_token": "token.update",
    "token.edit": "token.update",
    "create_token": "token.create",
    "new_token": "token.create",
    "token.new": "token.create",
    "get_token_key": "token.key",
    "token.getkey": "token.key",
    "subscription": "subs",
    "subscriptions": "subs",
    "group": "groups",
    "model": "models",
    "price": "pricing",
    "prices": "pricing",
    "pay_status": "pay.status",
    "topup.status": "pay.status",
}

_TOKEN_SUBCOMMAND_ALIASES: dict[str, str] = {
    "list": "tokens",
    "ls": "tokens",
    "create": "token.create",
    "new": "token.create",
    "delete": "token.delete",
    "del": "token.delete",
    "rm": "token.delete",
    "update": "token.update",
    "edit": "token.update",
    "key": "token.key",
}


def _resolve_command(raw_cmd: str, raw_args: str) -> tuple[str, str]:
    """规范化命令与参数，兼容 token.list / token list 等写法。"""
    cmd = str(raw_cmd or "").strip().lower()
    args = str(raw_args or "").strip()
    cmd = _COMMAND_ALIASES.get(cmd, cmd)

    if cmd == "token" and args:
        sub_parts = args.split(None, 1)
        sub = _TOKEN_SUBCOMMAND_ALIASES.get(sub_parts[0].strip().lower(), "")
        if sub:
            cmd = sub
            args = sub_parts[1].strip() if len(sub_parts) > 1 else ""

    if cmd == "aff" and args.lower().startswith("transfer"):
        parts = args.split(None, 1)
        cmd = "aff.transfer"
        args = parts[1].strip() if len(parts) > 1 else ""

    if cmd == "pay" and args.lower().startswith("status"):
        parts = args.split(None, 1)
        cmd = "pay.status"
        args = parts[1].strip() if len(parts) > 1 else ""

    return cmd, args


def _extract_token_items(raw_data: Any) -> list[dict[str, Any]]:
    """兼容不同返回格式，提取 token 列表。"""
    candidates: list[Any] = []
    if isinstance(raw_data, list):
        candidates = raw_data
    elif isinstance(raw_data, dict):
        if isinstance(raw_data.get("items"), list):
            candidates = raw_data.get("items", [])
        elif isinstance(raw_data.get("tokens"), list):
            candidates = raw_data.get("tokens", [])
        elif isinstance(raw_data.get("data"), list):
            candidates = raw_data.get("data", [])
    return [item for item in candidates if isinstance(item, dict)]


def _parse_expired_time(raw_value: str) -> int | None:
    """解析过期值，支持时间戳/天数/明天等自然写法。"""
    value = str(raw_value or "").strip().lower()
    if not value:
        return None
    if value in {"-1", "never", "none", "永久", "永不过期", "永久不过期"}:
        return -1
    if value in {"tomorrow", "明天"}:
        return int((datetime.now(timezone.utc) + timedelta(days=1)).timestamp())
    if value in {"today", "今天"}:
        return int(datetime.now(timezone.utc).timestamp())
    if value.endswith("d") and value[:-1].isdigit():
        days = int(value[:-1])
        return int((datetime.now(timezone.utc) + timedelta(days=days)).timestamp()) if days > 0 else -1
    if value.isdigit():
        num = int(value)
        if num > 1_000_000_000:
            return num
        return int((datetime.now(timezone.utc) + timedelta(days=num)).timestamp()) if num > 0 else -1
    return None


def _resp_success(resp: Any) -> bool:
    if not isinstance(resp, dict):
        return False
    msg = str(resp.get("message", "")).strip().lower()
    if msg == "success":
        return True
    return bool(resp.get("success", False))


def _resp_error_text(resp: Any, default: str = "请求失败") -> str:
    if not isinstance(resp, dict):
        return default

    data = resp.get("data")
    if isinstance(data, str) and data.strip():
        return data.strip()
    if isinstance(data, dict):
        for key in ("message", "msg", "error", "detail"):
            value = str(data.get(key, "")).strip()
            if value:
                return value

    err = resp.get("error")
    if isinstance(err, dict):
        for key in ("message", "msg", "detail"):
            value = str(err.get(key, "")).strip()
            if value:
                return value
    elif isinstance(err, str) and err.strip():
        return err.strip()

    msg = str(resp.get("message", "")).strip()
    if msg and msg.lower() not in {"success", "ok"}:
        return msg

    return default


def _extract_pay_url(resp: Any) -> str:
    if not isinstance(resp, dict):
        return ""

    direct = str(resp.get("url", "")).strip()
    if direct.startswith("http"):
        return direct

    data = resp.get("data")
    if isinstance(data, str) and data.strip().startswith("http"):
        return data.strip()
    if isinstance(data, dict):
        for key in ("pay_link", "checkout_url", "url", "pay_url", "link"):
            value = str(data.get(key, "")).strip()
            if value.startswith("http"):
                return value
    return ""


def _build_epay_submit_url(resp: Any) -> str:
    """构造 EPay 可直接访问的完整支付链接（含签名参数）。"""
    if not isinstance(resp, dict):
        return ""
    base_url = str(resp.get("url", "")).strip()
    if not base_url.startswith("http"):
        return ""
    data = resp.get("data")
    if not isinstance(data, dict):
        return base_url

    keys = (
        "pid",
        "type",
        "out_trade_no",
        "notify_url",
        "return_url",
        "name",
        "money",
        "sign",
        "sign_type",
        "device",
    )
    params: dict[str, str] = {}
    for key in keys:
        value = data.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if not text:
            continue
        params[key] = text

    if not params:
        return base_url
    sep = "&" if "?" in base_url else "?"
    return f"{base_url}{sep}{urlencode(params)}"


def _build_file_uri(path_like: Path | str) -> str:
    source = str(path_like).strip()
    if not source:
        return ""
    lower = source.lower()
    if lower.startswith(("file://", "http://", "https://", "base64://")):
        return source
    try:
        return Path(source).expanduser().resolve().as_uri()
    except Exception:
        normalized = source.replace("\\", "/")
        if not normalized.startswith("/"):
            normalized = f"/{normalized}"
        return f"file://{normalized}"


def _sanitize_filename_part(raw_value: str, default: str = "pay") -> str:
    value = re.sub(r"[^0-9A-Za-z._-]+", "_", str(raw_value or "").strip()).strip("._")
    return value or default


def _parse_auto_submit_form(body: str) -> tuple[str, dict[str, str]] | None:
    if not body:
        return None
    form_match = re.search(r'<form[^>]+action="([^"]+)"[^>]+method="post"', body, flags=re.I)
    if not form_match:
        return None
    action = html.unescape(form_match.group(1)).strip()
    if not action:
        return None
    fields = {
        name: html.unescape(value)
        for name, value in re.findall(
            r'<input[^>]+type="hidden"[^>]+name="([^"]+)"[^>]+value="([^"]*)"',
            body,
            flags=re.I,
        )
    }
    if not fields:
        return None
    return action, fields


def _parse_js_redirect(body: str) -> str:
    if not body:
        return ""
    patterns = (
        r'window\.location\.replace\(["\']([^"\']+)["\']\)',
        r'window\.location\.href\s*=\s*["\']([^"\']+)["\']',
        r'location\.href\s*=\s*["\']([^"\']+)["\']',
    )
    for pattern in patterns:
        match = re.search(pattern, body, flags=re.I)
        if match:
            return html.unescape(match.group(1)).strip()
    return ""


def _extract_payment_page_meta(body: str, page_url: str) -> dict[str, str]:
    meta: dict[str, str] = {
        "landing_url": str(page_url or "").strip(),
        "qr_image_url": "",
        "qr_text": "",
        "verify_code": "",
        "provider_trade_no": "",
    }
    if not body:
        return meta

    qr_text_patterns = (
        r"""(?:var|const|let)\s+(?:code_url|qr_url|qrcode_url|pay_url|native_url)\s*=\s*['"]([^'"]+)['"]""",
        r'''"(?:code_url|qr_url|qrcode_url|pay_url|native_url)"\s*:\s*"([^"]+)"''',
    )
    for pattern in qr_text_patterns:
        match = re.search(pattern, body, flags=re.I)
        if match:
            candidate = html.unescape(match.group(1)).strip()
            if candidate:
                meta["qr_text"] = urljoin(page_url, candidate)
                break

    image_patterns = (
        r"""data:image/[A-Za-z0-9.+-]+;base64,[A-Za-z0-9+/=\s]+""",
        r"""<img[^>]+src=['"]([^'"]+)['"][^>]*>""",
        r"""(?:src|href)=["']([^"']+\.(?:png|jpg|jpeg|gif|webp)(?:\?[^"']*)?)["']""",
    )
    for pattern in image_patterns:
        for match in re.finditer(pattern, body, flags=re.I):
            candidate = match.group(0) if pattern.startswith("data:image") else match.group(1)
            candidate = html.unescape(str(candidate or "").strip())
            if not candidate:
                continue
            if candidate.startswith("data:image/"):
                meta["qr_image_url"] = candidate
                return meta
            normalized = urljoin(page_url, candidate)
            lowered = normalized.lower()
            if re.search(r"(qrcode|qr[_/-]?code|/qr/|/qrcode/)", lowered):
                meta["qr_image_url"] = normalized
                return meta

    verify_patterns = (
        r"""(?:验证码|校验码|支付码|口令)[^0-9A-Za-z]{0,6}([0-9A-Za-z-]{4,12})""",
        r"""(?:verify(?:_code)?|captcha|auth(?:_code)?)\s*[:=]\s*['"]?([0-9A-Za-z-]{4,12})""",
    )
    for pattern in verify_patterns:
        match = re.search(pattern, body, flags=re.I)
        if match:
            meta["verify_code"] = match.group(1).strip()
            break

    order_patterns = (
        r"""(?:trade_no|out_trade_no)\s*[:=]\s*['"]([0-9A-Za-z_-]{8,64})['"]""",
        r"""name=["'](?:trade_no|out_trade_no)["']\s+value=["']([0-9A-Za-z_-]{8,64})["']""",
    )
    for pattern in order_patterns:
        match = re.search(pattern, body, flags=re.I)
        if match:
            meta["provider_trade_no"] = match.group(1).strip()
            break

    return meta


async def _resolve_payment_page_meta(pay_url: str) -> dict[str, str]:
    target = str(pay_url or "").strip()
    if not target.startswith("http"):
        return {}

    try:
        async with httpx.AsyncClient(
            timeout=_PAYMENT_HTTP_TIMEOUT,
            follow_redirects=True,
            headers=dict(_PAYMENT_HTTP_HEADERS),
            verify=False,
        ) as http:
            response = await http.get(target)
            page_url = str(response.url)
            body = response.text if "html" in response.headers.get("content-type", "").lower() else ""

            for _ in range(_PAYMENT_FOLLOW_MAX_STEPS):
                content_type = response.headers.get("content-type", "").lower()
                if content_type.startswith("image/"):
                    return {
                        "landing_url": page_url,
                        "qr_image_url": page_url,
                        "qr_text": "",
                        "verify_code": "",
                        "provider_trade_no": "",
                    }

                form = _parse_auto_submit_form(body)
                if form is not None:
                    action, fields = form
                    response = await http.post(urljoin(page_url, action), data=fields)
                    page_url = str(response.url)
                    body = response.text if "html" in response.headers.get("content-type", "").lower() else ""
                    continue

                redirect_url = _parse_js_redirect(body)
                if redirect_url:
                    response = await http.get(urljoin(page_url, redirect_url))
                    page_url = str(response.url)
                    body = response.text if "html" in response.headers.get("content-type", "").lower() else ""
                    continue
                break

            return _extract_payment_page_meta(body, page_url)
    except Exception:
        _log.warning("newapi_payment_page_probe_failed | url=%s", pay_url[:160], exc_info=True)
        return {}


def _write_qr_png_sync(qr_text: str, output_path: Path) -> None:
    import qrcode

    output_path.parent.mkdir(parents=True, exist_ok=True)
    qr = qrcode.QRCode(border=2, box_size=9)
    qr.add_data(qr_text)
    qr.make(fit=True)
    image = qr.make_image(fill_color="black", back_color="white")
    image.save(output_path)


async def _materialize_payment_qr_image(
    *,
    qr_image_url: str,
    qr_text: str,
    file_stem: str,
) -> Path | None:
    output_name = f"{_sanitize_filename_part(file_stem)}.png"
    output_path = _PAYMENT_QR_CACHE_DIR / output_name
    output_path.parent.mkdir(parents=True, exist_ok=True)

    data_url = str(qr_image_url or "").strip()
    if data_url.startswith("data:image/"):
        match = re.match(r"^data:image/[^;]+;base64,(.+)$", data_url, flags=re.I | re.S)
        if not match:
            return None
        try:
            output_path.write_bytes(base64.b64decode(match.group(1)))
            return output_path
        except Exception:
            return None

    image_url = str(qr_image_url or "").strip()
    text = str(qr_text or "").strip()
    prefer_text_render = bool(text) and (
        not image_url or not re.search(r"(qrcode|qr[_/-]?code|/qr/|/qrcode/)", image_url.lower())
    )
    if prefer_text_render:
        try:
            _write_qr_png_sync(text, output_path)
            return output_path
        except Exception:
            _log.warning("newapi_payment_qr_render_failed | text_len=%d", len(text), exc_info=True)
            return None

    if image_url.startswith("http"):
        try:
            async with httpx.AsyncClient(
                timeout=_PAYMENT_HTTP_TIMEOUT,
                headers=dict(_PAYMENT_HTTP_HEADERS),
                verify=False,
            ) as http:
                response = await http.get(image_url)
            if response.status_code == 200 and response.content:
                output_path.write_bytes(response.content)
                return output_path
        except Exception:
            _log.warning("newapi_payment_qr_download_failed | url=%s", image_url, exc_info=True)

    if not text:
        return None
    try:
        _write_qr_png_sync(text, output_path)
        return output_path
    except Exception:
        _log.warning("newapi_payment_qr_render_failed | text_len=%d", len(text), exc_info=True)
        return None


async def _send_context_message(
    context: dict[str, Any],
    *,
    message: str,
) -> bool:
    api_call = context.get("api_call")
    if not callable(api_call):
        return False
    user_id = str(context.get("user_id", "")).strip()
    group_id_raw = str(context.get("group_id", "")).strip()
    try:
        if _context_is_private(context) and user_id.isdigit():
            await api_call("send_private_msg", user_id=int(user_id), message=message)
            return True
        if group_id_raw.isdigit():
            await api_call("send_group_msg", group_id=int(group_id_raw), message=message)
            return True
    except Exception:
        _log.warning("newapi_context_send_failed | private=%s | user=%s | group=%s", _context_is_private(context), user_id or "-", group_id_raw or "-", exc_info=True)
    return False


async def _deliver_payment_materials(
    *,
    context: dict[str, Any],
    method_name: str,
    amount: int,
    pay_money: str,
    site_order_no: str,
    pay_url: str,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "sent": False,
        "summary": "",
        "verify_code": "",
        "provider_trade_no": "",
        "landing_url": "",
    }
    if not callable(context.get("api_call")):
        return result

    page_meta = await _resolve_payment_page_meta(pay_url)
    qr_image_path = await _materialize_payment_qr_image(
        qr_image_url=str(page_meta.get("qr_image_url", "")).strip(),
        qr_text=str(page_meta.get("qr_text", "")).strip() or pay_url,
        file_stem=f"{site_order_no or amount}_{method_name}",
    )

    result["verify_code"] = str(page_meta.get("verify_code", "")).strip()
    result["provider_trade_no"] = str(page_meta.get("provider_trade_no", "")).strip()
    result["landing_url"] = str(page_meta.get("landing_url", "")).strip()

    sent_any = False
    if qr_image_path is not None:
        sent_any = await _send_context_message(
            context,
            message=f"[CQ:image,file={_build_file_uri(qr_image_path)}]",
        )

    detail_lines = [f"{method_name}支付信息", f"充值额度: {amount}"]
    if pay_money:
        detail_lines.append(f"实付金额: ¥{pay_money}")
    if site_order_no:
        detail_lines.append(f"站内订单号: {site_order_no}")
    if result["provider_trade_no"]:
        detail_lines.append(f"支付单号: {result['provider_trade_no']}")
    if result["verify_code"]:
        detail_lines.append(f"验证码: {result['verify_code']}")
    elif not sent_any:
        detail_lines.append("未从支付页解析到验证码，请直接打开备用链接继续支付。")
    if not sent_any and pay_url:
        detail_lines.append(f"备用链接: {pay_url}")

    detail_sent = await _send_context_message(context, message="\n".join(detail_lines))
    result["sent"] = bool(sent_any or detail_sent)

    summary_parts = [f"{method_name}支付二维码已私发。"] if sent_any else [f"{method_name}支付信息已私发。"]
    if result["verify_code"]:
        summary_parts.append(f"验证码: {result['verify_code']}。")
    if site_order_no:
        summary_parts.append(f"站内订单号: {site_order_no}。")
    summary_parts.append("支付完成后可发送 /api pay.status 查询是否到账。")
    if not result["sent"]:
        summary_parts = []
    result["summary"] = "".join(summary_parts)
    return result


def _normalize_pay_method(raw_method: str) -> str:
    method = str(raw_method or "").strip().lower()
    aliases = {
        "auto": "auto",
        "wx": "wxpay",
        "wechat": "wxpay",
        "weixin": "wxpay",
        "微信": "wxpay",
        "wxpay": "wxpay",
        "ali": "alipay",
        "alipay": "alipay",
        "zfb": "alipay",
        "支付宝": "alipay",
        "qq": "qqpay",
        "qq支付": "qqpay",
        "qqpay": "qqpay",
        "stripe": "stripe",
        "creem": "creem",
    }
    return aliases.get(method, method)


def _remember_pending_payment(
    *,
    context: dict[str, Any],
    amount: int,
    method: str,
    site_order_no: str,
    balance_before_quota: int | None,
) -> None:
    key = _user_key(context)
    _set_pending_payment(
        key,
        {
            "site_order_no": str(site_order_no or "").strip(),
            "method": _normalize_pay_method(method),
            "amount": int(amount),
            "balance_before_quota": balance_before_quota,
            "created_at": int(time.time()),
        },
    )

# ── 命令处理函数 ──────────────────────────────────────────────────────────

async def _cmd_bind(args: str, context: dict) -> str:
    """绑定账号: /api bind <站点URL> <用户名> <密码>"""
    parts = args.split(None, 2)
    if len(parts) < 3:
        return "用法: /api bind <站点URL> <用户名> <密码>\n例: /api bind https://skiapi.dev myuser mypass\n⚠️ 请在私聊中使用此命令!"
    site_url, username, password = parts
    if not site_url.startswith("http"):
        site_url = "https://" + site_url

    client = NewAPIClient(site_url)
    try:
        result = await client.login(username, password)
        if not result.get("success"):
            msg = result.get("message", "登录失败")
            return f"❌ 绑定失败: {msg}"
        # 保存凭据 & 缓存会话
        key = _user_key(context)
        _invalidate_session(key)
        creds = _load_credentials()
        creds[key] = {"site_url": site_url, "username": username, "password": password}
        _save_credentials(creds)
        _clear_pending_payment(key)
        _session_cache[key] = (
            {k: v for k, v in client._http.cookies.items()},
            client._user_id,
            time.time() + _get_session_ttl_seconds(),
        )
        return f"✅ 绑定成功! 站点: {site_url}\n用户: {username}\n之后可直接使用 /api 命令操作。"
    finally:
        await client.close()


async def _cmd_register(args: str, context: dict) -> str:
    """注册账号: /api register <站点URL> <用户名> <密码> [邮箱] [邀请码]"""
    parts = args.split()
    if len(parts) < 3:
        return "用法: /api register <站点URL> <用户名> <密码> [邮箱] [邀请码]"
    site_url = parts[0] if parts[0].startswith("http") else "https://" + parts[0]
    username, password = parts[1], parts[2]
    email = parts[3] if len(parts) > 3 else ""
    aff = parts[4] if len(parts) > 4 else ""

    client = NewAPIClient(site_url)
    try:
        result = await client.register(username, password, email, aff)
        if not result.get("success"):
            return f"❌ 注册失败: {result.get('message', '未知错误')}"
        # 自动绑定
        key = _user_key(context)
        _invalidate_session(key)
        creds = _load_credentials()
        creds[key] = {"site_url": site_url, "username": username, "password": password}
        _save_credentials(creds)
        _clear_pending_payment(key)
        login_result = await client.login(username, password)
        if login_result.get("success"):
            _session_cache[key] = (
                {k: v for k, v in client._http.cookies.items()},
                client._user_id,
                time.time() + _get_session_ttl_seconds(),
            )
        return f"✅ 注册成功并已自动绑定!\n站点: {site_url}\n用户: {username}"
    finally:
        await client.close()


async def _cmd_unbind(args: str, context: dict) -> str:
    """解绑账号: /api unbind"""
    key = _user_key(context)
    _invalidate_session(key)
    _clear_pending_payment(key)
    creds = _load_credentials()
    if key in creds:
        del creds[key]
        _save_credentials(creds)
        return "✅ 已解绑。"
    return "你还没有绑定任何账号。"


async def _cmd_me(args: str, context: dict) -> str:
    """查看个人信息: /api me"""
    client = await _get_client(context)
    if not client:
        return "❌ 未绑定账号，请先 /api bind"
    try:
        r = await client.get_self()
        if not r.get("success"):
            return f"❌ {r.get('message', '获取失败')}"
        d = r.get("data", {})
        lines = [
            f"👤 {d.get('display_name') or d.get('username')}",
            f"用户名: {d.get('username')}",
            f"邮箱: {d.get('email') or '未绑定'}",
            f"分组: {d.get('group', 'default')}",
            f"余额: {_fmt_quota(d.get('quota', 0))}",
            f"已用: {_fmt_quota(d.get('used_quota', 0))}",
            f"请求次数: {d.get('request_count', 0)}",
            f"邀请码: {d.get('aff_code', '无')}",
        ]
        return "\n".join(lines)
    finally:
        await client.close()

async def _cmd_tokens(args: str, context: dict) -> str:
    """列出令牌: /api tokens"""
    client = await _get_client(context)
    if not client:
        return "❌ 未绑定账号，请先 /api bind"
    try:
        r = await client.list_tokens(page=0, size=20)
        if not r.get("success"):
            return f"❌ {r.get('message', '获取失败')}"
        tokens = _extract_token_items(r.get("data", {}))
        if not tokens:
            return "暂无令牌。使用 /api token.create <名称> 创建。"
        lines = ["📋 令牌列表:"]
        for t in tokens:
            key_masked = t.get("key", "")
            if len(key_masked) > 10:
                key_masked = key_masked[:6] + "xxxx" + key_masked[-4:]
            elif key_masked:
                key_masked = key_masked[:3] + "xxx"
            else:
                key_masked = "-"
            quota = "♾️无限" if t.get("unlimited_quota") else _fmt_quota(t.get("remain_quota", 0))
            lines.append(
                f"  [{t.get('id')}] {t.get('name', '?')} | {_status_text(t.get('status', 1))} | "
                f"额度: {quota} | 过期: {_fmt_time(t.get('expired_time', -1))} | "
                f"分组: {t.get('group') or 'default'} | Key: {key_masked}"
            )
        if len(lines) == 1:
            return "暂无令牌。使用 /api token.create <名称> 创建。"
        return "\n".join(lines)
    finally:
        await client.close()


async def _cmd_token_create(args: str, context: dict) -> str:
    """创建令牌: /api token.create <名称> [额度] [过期天数] [分组]
    额度为 0 且加 --unlimited 则无限额度。"""
    client = await _get_client(context)
    if not client:
        return "❌ 未绑定账号，请先 /api bind"
    try:
        parts = args.split()
        if not parts:
            return "用法: /api token.create <名称> [额度] [过期天数] [分组] [--unlimited]"
        name = parts[0]
        unlimited = "--unlimited" in parts
        clean = [p for p in parts[1:] if p != "--unlimited"]
        try:
            quota = int(clean[0]) * 500000 if len(clean) > 0 else 0
            expire_days = int(clean[1]) if len(clean) > 1 else -1
        except ValueError:
            return "用法: /api token.create <名称> [额度] [过期天数] [分组] [--unlimited]\n示例: /api token.create my-key 10 30 default"
        group = clean[2] if len(clean) > 2 else ""

        expired_time = -1
        if expire_days > 0:
            expired_time = int(time.time()) + expire_days * 86400

        r = await client.create_token(
            name=name,
            remain_quota=quota,
            unlimited_quota=unlimited,
            expired_time=expired_time,
            group=group,
        )
        if not r.get("success"):
            return f"❌ 创建失败: {r.get('message', '未知错误')}"

        # 尝试获取完整 key
        data = r.get("data", {})
        key_display = data.get("key", "创建成功，请在令牌列表中查看")
        return f"✅ 令牌已创建!\n名称: {name}\n密钥: {key_display}\n额度: {'♾️无限' if unlimited else _fmt_quota(quota)}\n过期: {_fmt_time(expired_time)}"
    finally:
        await client.close()


async def _cmd_token_delete(args: str, context: dict) -> str:
    """删除令牌: /api token.delete <ID>"""
    client = await _get_client(context)
    if not client:
        return "❌ 未绑定账号，请先 /api bind"
    try:
        tid = int(args.strip())
        r = await client.delete_token(tid)
        if not r.get("success"):
            return f"❌ 删除失败: {r.get('message', '未知错误')}"
        return f"✅ 令牌 {tid} 已删除。"
    except ValueError:
        return "用法: /api token.delete <令牌ID>"
    finally:
        await client.close()


async def _cmd_token_update(args: str, context: dict) -> str:
    """修改令牌: /api token.update <ID> <字段>=<值> ...
    支持: name, quota, unlimited, expire_days, group, status"""
    client = await _get_client(context)
    if not client:
        return "❌ 未绑定账号，请先 /api bind"
    try:
        parts = args.split()
        if not parts:
            return "用法: /api token.update <ID> name=xxx quota=100 unlimited=true expire_days=30 group=vip status=1"
        tid: int | None = None
        payload_parts = parts
        if parts[0].isdigit():
            tid = int(parts[0])
            payload_parts = parts[1:]
        else:
            # 仅当用户只有一个令牌时，允许省略 ID
            token_list = await client.list_tokens(page=0, size=20)
            items = _extract_token_items(token_list.get("data", {}))
            if len(items) == 1:
                tid = int(items[0].get("id", 0))
            elif len(items) > 1:
                return "检测到你有多个令牌，请指定 ID：/api token.update <ID> ...\n可先用 /api tokens 查看。"
            else:
                return "未找到可更新的令牌，请先创建令牌或确认绑定账号。"
        if not tid:
            return "未能确定令牌 ID，请使用 /api tokens 查看后指定。"

        kwargs: dict[str, Any] = {}
        for kv in payload_parts:
            if "=" not in kv:
                continue
            k, v = kv.split("=", 1)
            k = k.strip().lower()
            v = v.strip()
            if k == "name":
                kwargs["name"] = v
            elif k == "quota":
                kwargs["remain_quota"] = int(v) * 500000
            elif k == "unlimited":
                kwargs["unlimited_quota"] = v.lower() in ("true", "1", "yes")
            elif k == "expire_days":
                days = int(v)
                kwargs["expired_time"] = int(time.time()) + days * 86400 if days > 0 else -1
            elif k in {"expire_time", "expired_time", "expire", "expiry", "expires"}:
                parsed = _parse_expired_time(v)
                if parsed is None:
                    return "expire_time/expired_time 不合法。支持: 时间戳、天数、tomorrow、明天、never、永久"
                kwargs["expired_time"] = parsed
            elif k == "group":
                kwargs["group"] = v
            elif k == "status":
                kwargs["status"] = int(v)
        # 自然语言兜底（尽量少写死，只补高频）
        raw_text = args.strip().lower()
        if "expired_time" not in kwargs:
            if "明天" in raw_text or "tomorrow" in raw_text:
                kwargs["expired_time"] = int((datetime.now(timezone.utc) + timedelta(days=1)).timestamp())
            elif "永久" in raw_text or "永不过期" in raw_text or "never" in raw_text:
                kwargs["expired_time"] = -1
        if not kwargs:
            return "用法: /api token.update <ID> name=xxx quota=100 unlimited=true expire_days=30 group=vip status=1"
        r = await client.update_token(tid, **kwargs)
        if not r.get("success"):
            return f"❌ 更新失败: {r.get('message', '未知错误')}"
        return f"✅ 令牌 {tid} 已更新。"
    except ValueError:
        return "参数格式错误。示例: /api token.update 123 expire_days=1 或 /api token.update 123 expire_time=tomorrow"
    finally:
        await client.close()


async def _cmd_token_key(args: str, context: dict) -> str:
    """获取令牌完整密钥: /api token.key <ID>"""
    client = await _get_client(context)
    if not client:
        return "❌ 未绑定账号，请先 /api bind"
    try:
        tid = int(args.strip())
        r = await client.get_token_key(tid)
        if not r.get("success"):
            return f"❌ {r.get('message', '获取失败')}"
        return f"🔑 令牌密钥:\n{r.get('data', {}).get('key', '未知')}"
    except ValueError:
        return "用法: /api token.key <令牌ID>"
    finally:
        await client.close()

async def _cmd_balance(args: str, context: dict) -> str:
    """查看余额: /api balance"""
    client = await _get_client(context)
    if not client:
        return "❌ 未绑定账号，请先 /api bind"
    try:
        r = await client.get_self()
        if not r.get("success"):
            return f"❌ {r.get('message', '获取失败')}"
        d = r.get("data", {})
        lines = [
            "💰 钱包信息",
            f"当前余额: {_fmt_quota(d.get('quota', 0))}",
            f"已消耗: {_fmt_quota(d.get('used_quota', 0))}",
            f"请求次数: {d.get('request_count', 0)}",
        ]
        pending = _get_pending_payment(_user_key(context))
        if pending:
            lines.append(f"最近待确认订单: {_pending_payment_brief(pending)}")
            lines.append("如已支付，可发送 /api pay.status 确认是否到账。")
        return "\n".join(lines)
    finally:
        await client.close()


async def _cmd_stats(args: str, context: dict) -> str:
    """使用统计: /api stats"""
    client = await _get_client(context)
    if not client:
        return "❌ 未绑定账号，请先 /api bind"
    try:
        r = await client.get_log_stat()
        if not r.get("success"):
            return f"❌ {r.get('message', '获取失败')}"
        data = r.get("data", {})
        if isinstance(data, list) and data:
            lines = ["📊 使用统计:"]
            for item in data[:15]:
                if not isinstance(item, dict):
                    continue
                model = item.get("model_name") or item.get("model", "?")
                count = item.get("request_count", item.get("count", 0))
                quota = _fmt_quota(item.get("quota", 0))
                lines.append(f"  {model}: {count}次, {quota}")
            return "\n".join(lines)
        return f"📊 统计数据: {json.dumps(data, ensure_ascii=False)[:800]}"
    finally:
        await client.close()


async def _cmd_models(args: str, context: dict) -> str:
    """查看可用模型: /api models"""
    client = await _get_client(context)
    if not client:
        return "❌ 未绑定账号，请先 /api bind"
    try:
        r = await client.get_user_models()
        if not r.get("success"):
            # fallback to /api/models
            r = await client.get_models()
        if not r.get("success"):
            return f"❌ {r.get('message', '获取失败')}"
        data = r.get("data", [])
        if isinstance(data, list):
            models = [m.get("id", m) if isinstance(m, dict) else str(m) for m in data]
            if len(models) > 50:
                return f"📦 可用模型 ({len(models)}个):\n" + ", ".join(models[:50]) + f"\n...等共 {len(models)} 个"
            return f"📦 可用模型 ({len(models)}个):\n" + ", ".join(models)
        return f"📦 模型数据: {str(data)[:800]}"
    finally:
        await client.close()


async def _cmd_groups(args: str, context: dict) -> str:
    """查看分组: /api groups"""
    client = await _get_client(context)
    if not client:
        return "❌ 未绑定账号，请先 /api bind"
    try:
        r = await client.get_user_groups()
        if not r.get("success"):
            r = await client.get_groups_public()
        if not r.get("success"):
            return f"❌ {r.get('message', '获取失败')}"
        data = r.get("data", [])
        if isinstance(data, list):
            return "📂 可用分组:\n" + "\n".join(f"  • {g}" for g in data)
        if isinstance(data, dict):
            lines = ["📂 分组信息:"]
            for k, v in data.items():
                lines.append(f"  • {k}: {v}")
            return "\n".join(lines)
        return f"📂 分组: {data}"
    finally:
        await client.close()


async def _cmd_subscriptions(args: str, context: dict) -> str:
    """查看订阅套餐: /api subs"""
    client = await _get_client(context)
    if not client:
        return "❌ 未绑定账号，请先 /api bind"
    try:
        r = await client.get_subscription_plans()
        if not r.get("success"):
            return f"❌ {r.get('message', '获取失败')}"
        plans = r.get("data", [])
        if not plans:
            return "暂无可用订阅套餐。"
        lines = ["🎫 订阅套餐:"]
        for p in plans:
            if not isinstance(p, dict):
                continue
            lines.append(
                f"  [{p.get('id')}] {p.get('title', '?')} - "
                f"${p.get('price_amount', 0)} / {p.get('duration_value', '?')}{p.get('duration_unit', '')}\n"
                f"      {p.get('description', '')}"
            )
        return "\n".join(lines)
    finally:
        await client.close()


async def _cmd_checkin(args: str, context: dict) -> str:
    """签到: /api checkin"""
    client = await _get_client(context)
    if not client:
        return "❌ 未绑定账号，请先 /api bind"
    try:
        r = await client.checkin()
        if not r.get("success"):
            return f"❌ {r.get('message', '签到失败')}"
        return f"✅ 签到成功! {r.get('message', '')}"
    finally:
        await client.close()


async def _cmd_email(args: str, context: dict) -> str:
    """绑定邮箱: /api email <原密码> <邮箱地址>"""
    parts = args.split(None, 1)
    if len(parts) < 2:
        return "用法: /api email <原密码> <邮箱地址>\n例: /api email your_old_password you@example.com"
    original_password = parts[0].strip()
    email = parts[1].strip()
    if "@" not in email:
        return "邮箱格式不正确。用法: /api email <原密码> <邮箱地址>"

    client = await _get_client(context)
    if not client:
        return "❌ 未绑定账号，请先 /api bind"
    try:
        r = await client.update_self(email=email, original_password=original_password)
        if not _resp_success(r):
            return f"❌ {_resp_error_text(r, '绑定失败')}"
        return f"✅ 邮箱已更新为: {email}"
    finally:
        await client.close()


async def _cmd_topup(args: str, context: dict) -> str:
    """充值: /api topup [兌換碼]"""
    runtime_cfg = _get_runtime_cfg()
    payment_cfg = runtime_cfg.get("payment", {}) if isinstance(runtime_cfg, dict) else {}
    if not isinstance(payment_cfg, dict):
        payment_cfg = {}
    show_amount_unit_hint = bool(payment_cfg.get("show_amount_unit_hint", True))
    show_topup_command_hints = bool(payment_cfg.get("show_topup_command_hints", True))

    client = await _get_client(context)
    if not client:
        return "❌ 未绑定账号，请先 /api bind"
    try:
        code = args.strip()
        if code:
            r = await client.redeem_topup(code)
            if not _resp_success(r):
                return f"❌ {_resp_error_text(r, '充值失败')}"
            message = str(r.get("message", "")).strip()
            if message and message.lower() not in {"success", "ok"}:
                return f"✅ 充值成功: {message}"
            return "✅ 充值成功。"

        r = await client.get_topup_info()
        if not _resp_success(r):
            return f"❌ {_resp_error_text(r, '获取失败')}"
        data = r.get("data", {})
        lines = ["💳 充值信息:"]
        if isinstance(data, dict):
            if data.get("enable_online_topup"):
                lines.append("  在线支付: ✅ 已启用")
            if data.get("enable_stripe_topup"):
                lines.append("  Stripe: ✅ 已启用")
            if data.get("enable_creem_topup"):
                lines.append("  Creem: ✅ 已启用")

            methods = data.get("pay_methods", [])
            if methods:
                method_names = [m.get("name", m.get("type", "?")) for m in methods if isinstance(m, dict)]
                if method_names:
                    lines.append(f"  支付方式: {', '.join(method_names)}")

            min_topup = data.get("min_topup", 0)
            if min_topup:
                lines.append(f"  最低充值: {min_topup}")
            options = data.get("amount_options", [])
            if options:
                lines.append(f"  充值选项: {', '.join(str(o) for o in options)}")
                try:
                    numeric_options = [int(o) for o in options]
                except Exception:
                    numeric_options = []
                if show_amount_unit_hint and numeric_options and max(numeric_options) >= 1_000_000:
                    lines.append("  提示: 当前站点按“充值数量/额度单位”计价，请优先使用上述档位。")
            discount = data.get("discount", [])
            if discount:
                lines.append("  优惠:")
                for d in discount:
                    if isinstance(d, dict):
                        lines.append(f"    {d.get('amount', '?')}$ → 实付 {d.get('pay', '?')}$")

            creem_products = data.get("creem_products")
            if isinstance(creem_products, str) and creem_products.strip():
                try:
                    parsed = json.loads(creem_products)
                    if isinstance(parsed, list) and parsed:
                        lines.append("  Creem 产品:")
                        for item in parsed[:8]:
                            if not isinstance(item, dict):
                                continue
                            pid = item.get("productId", "?")
                            name = item.get("name", "?")
                            price = item.get("price", "?")
                            currency = item.get("currency", "")
                            lines.append(f"    {pid} - {name} ({price} {currency})")
                except Exception:
                    pass

        if show_topup_command_hints:
            lines.append("")
            lines.append("使用 /api pay <金额> [auto/wxpay/alipay/qqpay/stripe] 发起在线支付")
            lines.append("支付完成后可用 /api pay.status 查询是否到账")
            lines.append("使用 /api pay creem <product_id> 发起 Creem 支付")
            lines.append("使用 /api topup <兌換碼> 使用兌換碼充值")
        return "\n".join(lines) if len(lines) > 3 else "💳 充值信息: 请访问站点控制台进行充值。"
    finally:
        await client.close()


async def _cmd_pay(args: str, context: dict) -> str:
    """发起支付: /api pay <金额> [auto/wxpay/alipay/qqpay/stripe] | /api pay creem <product_id>"""
    runtime_cfg = _get_runtime_cfg()
    payment_cfg = runtime_cfg.get("payment", {}) if isinstance(runtime_cfg, dict) else {}
    if not isinstance(payment_cfg, dict):
        payment_cfg = {}
    require_method_selection = bool(payment_cfg.get("auto_require_method_selection_when_multiple", True))
    prefer_methods_raw = payment_cfg.get("auto_prefer_methods", ["wxpay", "alipay", "qqpay"])
    prefer_methods = [m for m in prefer_methods_raw if m in {"wxpay", "alipay", "qqpay"}] if isinstance(prefer_methods_raw, list) else ["wxpay", "alipay", "qqpay"]
    if not prefer_methods:
        prefer_methods = ["wxpay", "alipay", "qqpay"]
    fallback_method = _normalize_pay_method(str(payment_cfg.get("auto_fallback_method_when_info_unavailable", "wxpay")))
    include_epay_submit_url = bool(payment_cfg.get("include_epay_submit_url", True))
    show_amount_unit_hint = bool(payment_cfg.get("show_amount_unit_hint", True))

    parts = args.split()
    if not parts:
        return (
            "用法:\n"
            "/api pay <金额> [auto/wxpay/alipay/qqpay/stripe]\n"
            "/api pay creem <product_id>\n"
            "示例: /api pay 100 auto"
        )

    client = await _get_client(context)
    if not client:
        return "❌ 未绑定账号，请先 /api bind"
    try:
        balance_before_quota: int | None = None
        r_self_before = await client.get_self()
        if _resp_success(r_self_before):
            data_before = r_self_before.get("data", {})
            if isinstance(data_before, dict):
                balance_before_quota = _safe_int(data_before.get("quota"), 0)

        # Creem: 按产品发起支付，不走金额
        if parts[0].lower() == "creem":
            if len(parts) < 2:
                return "用法: /api pay creem <product_id>"
            product_id = parts[1].strip()
            r = await client.request_creem_pay(product_id)
            if not _resp_success(r):
                return f"❌ Creem 支付失败: {_resp_error_text(r, '支付失败')}"
            pay_url = _extract_pay_url(r)
            if not pay_url:
                return "❌ Creem 未返回支付链接。请检查站点支付配置。"
            return f"💳 Creem 支付\n产品: {product_id}\n支付链接: {pay_url}"

        try:
            amount = int(parts[0])
        except ValueError:
            return "金额必须是整数。"
        if amount <= 0:
            return "金额必须大于 0。"

        method = _normalize_pay_method(parts[1] if len(parts) > 1 else "auto")
        topup_info: Any = {}
        info_data: Any = {}
        epay_methods: list[str] = []

        if method == "auto":
            topup_info = await client.get_topup_info()
            info_data = topup_info.get("data", {}) if isinstance(topup_info, dict) else {}
            if isinstance(info_data, dict):
                pay_methods = info_data.get("pay_methods", [])
                if isinstance(pay_methods, list):
                    for m in pay_methods:
                        if not isinstance(m, dict):
                            continue
                        t = _normalize_pay_method(m.get("type", ""))
                        if t in {"wxpay", "alipay", "qqpay"}:
                            epay_methods.append(t)

            unique_methods = list(dict.fromkeys(epay_methods))
            if require_method_selection and len(unique_methods) > 1:
                labels = {
                    "wxpay": "微信",
                    "alipay": "支付宝",
                    "qqpay": "QQ",
                }
                human = "、".join(f"{labels.get(m, m)}({m})" for m in unique_methods)
                suggest_lines = [f"检测到可用支付方式: {human}"]
                for m in unique_methods:
                    suggest_lines.append(f"/api pay {amount} {m}")
                return "请先选择支付方式后再发起支付:\n" + "\n".join(suggest_lines)

            if isinstance(info_data, dict) and info_data.get("enable_online_topup") and epay_methods:
                for preferred in prefer_methods:
                    if preferred in epay_methods:
                        method = preferred
                        break
                if method == "auto":
                    method = epay_methods[0]
            elif isinstance(info_data, dict) and info_data.get("enable_stripe_topup"):
                method = "stripe"
            elif isinstance(info_data, dict) and info_data.get("enable_creem_topup"):
                return "❌ 当前站点仅检测到 Creem 支付，请使用 /api pay creem <product_id>。"
            else:
                # topup/info 不可用时，按配置选择回退策略
                if fallback_method in {"wxpay", "alipay", "qqpay", "stripe"}:
                    method = fallback_method
                else:
                    return "❌ 当前无法自动判断支付方式，请手动指定：/api pay <金额> wxpay 或 alipay。"

        if method in {"wxpay", "alipay", "qqpay"}:
            # 显式选择支付方式时不强依赖 topup/info；仅在有信息时做金额提示
            if not info_data:
                topup_info = await client.get_topup_info()
                info_data = topup_info.get("data", {}) if isinstance(topup_info, dict) else {}

            if isinstance(info_data, dict):
                min_topup = info_data.get("min_topup")
                try:
                    min_amount = int(min_topup) if min_topup is not None else 0
                except Exception:
                    min_amount = 0
                if min_amount > 0 and amount < min_amount:
                    return f"❌ 金额过小：最低充值金额为 {min_amount}。"

                options = info_data.get("amount_options", [])
                option_values: list[int] = []
                if isinstance(options, list):
                    for item in options:
                        try:
                            option_values.append(int(item))
                        except Exception:
                            pass
                if option_values and amount not in option_values:
                    preview = ", ".join(str(x) for x in option_values[:12])
                    suffix = "\n提示: 该站点可能按“充值数量/额度单位”计价。" if show_amount_unit_hint else ""
                    return f"❌ 金额不在可选档位内。可选金额: {preview}{suffix}"

            pay_money = ""
            r_amount = await client.request_amount(amount)
            if not _resp_success(r_amount):
                return f"❌ 金额校验失败: {_resp_error_text(r_amount, '金额不合法')}"
            if _resp_success(r_amount):
                pay_money = str(r_amount.get("data", "")).strip()

            r = await client.request_epay(amount, method)
            if not _resp_success(r):
                return f"❌ 支付失败({method}): {_resp_error_text(r, '支付失败')}"
            pay_url = _extract_pay_url(r)
            full_pay_url = _build_epay_submit_url(r) if include_epay_submit_url else ""
            if not pay_url and not full_pay_url:
                return "❌ 未获取到支付链接，请检查站点支付配置。"
            method_name = {"wxpay": "微信", "alipay": "支付宝", "qqpay": "QQ"}.get(method, method)
            site_order_no = ""
            if isinstance(r.get("data"), dict):
                site_order_no = str(r.get("data", {}).get("out_trade_no", "")).strip()
            delivery = await _deliver_payment_materials(
                context=context,
                method_name=method_name,
                amount=amount,
                pay_money=pay_money,
                site_order_no=site_order_no,
                pay_url=full_pay_url or pay_url,
            )
            _remember_pending_payment(
                context=context,
                amount=amount,
                method=method,
                site_order_no=site_order_no,
                balance_before_quota=balance_before_quota,
            )
            if delivery.get("summary"):
                return str(delivery.get("summary", "")).strip()

            lines = [f"💳 {method_name}支付", f"充值额度: {amount}"]
            if pay_money:
                lines.append(f"实付金额: ¥{pay_money}")
            if site_order_no:
                lines.append(f"站内订单号: {site_order_no}")
            lines.append(f"支付链接: {full_pay_url or pay_url}")
            lines.append("请点击链接完成支付。")
            return "\n".join(lines)

        if method == "stripe":
            pay_money = ""
            r_amount = await client.request_stripe_amount(amount)
            if not _resp_success(r_amount):
                return f"❌ Stripe 金额校验失败: {_resp_error_text(r_amount, '金额不合法')}"
            if _resp_success(r_amount):
                pay_money = str(r_amount.get("data", "")).strip()

            r = await client.request_stripe_pay(amount)
            if not _resp_success(r):
                return f"❌ Stripe 支付失败: {_resp_error_text(r, '支付失败')}"
            pay_url = _extract_pay_url(r)
            if not pay_url:
                return "❌ Stripe 未返回支付链接。请检查站点支付配置。"
            _remember_pending_payment(
                context=context,
                amount=amount,
                method=method,
                site_order_no="",
                balance_before_quota=balance_before_quota,
            )
            lines = ["💳 Stripe 支付", f"充值额度: {amount}"]
            if pay_money:
                lines.append(f"实付金额: {pay_money}")
            lines.append(f"支付链接: {pay_url}")
            return "\n".join(lines)

        return f"不支持的支付方式: {method}\n可选: auto, wxpay, alipay, qqpay, stripe, creem"
    finally:
        await client.close()


async def _cmd_pay_status(args: str, context: dict) -> str:
    """查询最近支付状态: /api pay.status [站内订单号]"""
    client = await _get_client(context)
    if not client:
        return "❌ 未绑定账号，请先 /api bind"

    key = _user_key(context)
    pending = _get_pending_payment(key)
    target_order_no = str(args or "").strip() or str((pending or {}).get("site_order_no", "")).strip()

    try:
        current_quota: int | None = None
        current_balance_text = "-"
        r_self = await client.get_self()
        if _resp_success(r_self):
            data = r_self.get("data", {})
            if isinstance(data, dict):
                current_quota = _safe_int(data.get("quota"), 0)
                current_balance_text = _fmt_quota(current_quota)

        history_size = 8 if target_order_no else 20
        r_history = await client.get_topup_history(page=0, size=history_size, keyword=target_order_no)
        if not _resp_success(r_history):
            return f"❌ 充值记录查询失败: {_resp_error_text(r_history, '查询失败')}"

        history_items = _extract_topup_items(r_history.get("data"))
        has_reference = bool(target_order_no or pending)
        matched = _find_topup_record(
            history_items,
            site_order_no=target_order_no,
            amount=_safe_int((pending or {}).get("amount"), 0),
            method=str((pending or {}).get("method", "")),
            created_at=_safe_int((pending or {}).get("created_at"), 0),
        )

        balance_before = None
        delta: int | None = None
        if pending:
            raw_before = pending.get("balance_before_quota")
            if raw_before is not None and current_quota is not None:
                balance_before = _safe_int(raw_before, 0)
                delta = current_quota - balance_before

        confirmed = _topup_record_is_success(matched)
        likely_by_balance = bool(pending and delta is not None and delta > 0)

        lines: list[str] = []
        if confirmed:
            lines.append("✅ 这笔充值已经到账。" if has_reference else "✅ 最近一笔充值已经到账。")
        elif likely_by_balance:
            lines.append("✅ 当前余额比发起支付前增加了，基本可以判定已经到账。")
        elif matched:
            prefix = "这笔充值" if has_reference else "最近一笔充值"
            lines.append(f"⏳ {prefix}当前状态: {_topup_status_text(matched.get('status'))}。")
        elif pending:
            lines.append("⏳ 还没查到最近这笔订单的到账记录。")
        elif history_items:
            lines.append("未找到待确认订单，以下是最近一笔充值记录。")
        else:
            lines.append("未找到最近待确认订单，也没有查询到充值记录。")

        if target_order_no:
            lines.append(f"站内订单号: {target_order_no}")
        elif pending:
            lines.append(f"最近待确认订单: {_pending_payment_brief(pending)}")

        if matched:
            lines.append(f"记录状态: {_topup_status_text(matched.get('status'))}")
            trade_no = str(matched.get("trade_no", "")).strip()
            if trade_no and trade_no != target_order_no:
                lines.append(f"匹配订单号: {trade_no}")
            matched_amount = _safe_int(matched.get("amount"), 0)
            if matched_amount > 0:
                lines.append(f"充值额度: {matched_amount}")
            money = _safe_float(matched.get("money"), 0.0)
            if money > 0:
                lines.append(f"实付金额: ¥{money:.2f}")
            payment_method = _normalize_pay_method(str(matched.get("payment_method", "")))
            if payment_method:
                method_name = {"wxpay": "微信", "alipay": "支付宝", "qqpay": "QQ", "stripe": "Stripe", "creem": "Creem"}.get(payment_method, payment_method)
                lines.append(f"支付方式: {method_name}")
            create_time = _safe_int(matched.get("create_time"), 0)
            if create_time > 0:
                lines.append(f"下单时间: {_fmt_local_time(create_time)}")
            complete_time = _safe_int(matched.get("complete_time"), 0)
            if complete_time > 0:
                lines.append(f"完成时间: {_fmt_local_time(complete_time)}")

        if current_quota is not None:
            lines.append(f"当前余额: {current_balance_text}")
        if balance_before is not None:
            lines.append(f"下单前余额: {_fmt_quota(balance_before)}")
            lines.append(f"余额变化: {_fmt_signed_quota(delta)}")

        if confirmed:
            _clear_pending_payment(key)
        elif pending and matched and _normalize_topup_status(matched.get("status")) in {"failed", "cancelled", "refunded"}:
            _clear_pending_payment(key)
        elif pending and not matched and target_order_no:
            lines.append("如果你刚完成支付，等几秒后再发一次 /api pay.status。")

        return "\n".join(lines)
    finally:
        await client.close()


async def _cmd_aff(args: str, context: dict) -> str:
    """邀请信息: /api aff"""
    client = await _get_client(context)
    if not client:
        return "❌ 未绑定账号，请先 /api bind"
    try:
        r_self = await client.get_self()
        r_aff = await client.get_aff_info()
        d = r_self.get("data", {}) if r_self.get("success") else {}
        creds = _load_credentials().get(_user_key(context), {})
        site = creds.get("site_url", "")
        aff_code = d.get("aff_code", "")
        lines = [
            "🎁 邀请奖励",
            f"邀请链接: {site}/register?aff={aff_code}" if aff_code else "邀请码: 无",
            f"邀请人数: {d.get('aff_count', 0)}",
            f"待划转奖励: {_fmt_quota(d.get('aff_quota', 0))}",
            f"历史奖励: {_fmt_quota(d.get('aff_history_quota', 0))}",
        ]
        return "\n".join(lines)
    finally:
        await client.close()


async def _cmd_aff_transfer(args: str, context: dict) -> str:
    """划转邀请奖励: /api aff.transfer"""
    client = await _get_client(context)
    if not client:
        return "❌ 未绑定账号，请先 /api bind"
    try:
        r = await client.transfer_aff_quota()
        if not r.get("success"):
            return f"❌ {r.get('message', '划转失败')}"
        return f"✅ 邀请奖励已划转到余额! {r.get('message', '')}"
    finally:
        await client.close()


async def _cmd_pricing(args: str, context: dict) -> str:
    """模型定价: /api pricing"""
    client = await _get_client(context)
    if not client:
        return "❌ 未绑定账号，请先 /api bind"
    try:
        r = await client.get_pricing()
        if not r.get("success"):
            return f"❌ {r.get('message', '获取失败')}"
        data = r.get("data", {})
        if isinstance(data, dict):
            lines = ["💲 模型定价 (倍率):"]
            items = list(data.items())[:30]
            for model, ratio in items:
                lines.append(f"  {model}: {ratio}")
            if len(data) > 30:
                lines.append(f"  ...共 {len(data)} 个模型")
            return "\n".join(lines)
        return f"💲 定价: {str(data)[:800]}"
    finally:
        await client.close()


async def _cmd_help(args: str, context: dict) -> str:
    return _build_help()

# ── 命令路由 ──────────────────────────────────────────────────────────────

_COMMANDS: dict[str, tuple[Any, str]] = {
    "bind":         (_cmd_bind,         "绑定账号 (私聊)"),
    "register":     (_cmd_register,     "注册新账号"),
    "unbind":       (_cmd_unbind,       "解绑账号"),
    "me":           (_cmd_me,           "查看个人信息"),
    "tokens":       (_cmd_tokens,       "列出所有令牌"),
    "token.create": (_cmd_token_create, "创建令牌"),
    "token.delete": (_cmd_token_delete, "删除令牌"),
    "token.update": (_cmd_token_update, "修改令牌"),
    "token.key":    (_cmd_token_key,    "获取令牌密钥 (私聊)"),
    "balance":      (_cmd_balance,      "查看余额"),
    "stats":        (_cmd_stats,        "使用统计"),
    "models":       (_cmd_models,       "可用模型列表"),
    "groups":       (_cmd_groups,       "查看分组"),
    "subs":         (_cmd_subscriptions,"订阅套餐"),
    "checkin":      (_cmd_checkin,      "每日签到"),
    "email":        (_cmd_email,        "绑定邮箱"),
    "topup":        (_cmd_topup,        "充值信息/兌換碼"),
    "pay":          (_cmd_pay,          "发起支付 (支持 auto/stripe/creem)"),
    "pay.status":   (_cmd_pay_status,   "查询最近支付状态/是否到账"),
    "aff":          (_cmd_aff,          "邀请信息"),
    "aff.transfer": (_cmd_aff_transfer, "划转邀请奖励"),
    "pricing":      (_cmd_pricing,      "模型定价"),
    "help":         (_cmd_help,         "查看帮助"),
}


def _build_help() -> str:
    lines = [f"🔧 {_get_plugin_display_name()} 管理命令:", ""]
    # 分类
    cats = {
        "账号": ["bind", "register", "unbind", "me", "email"],
        "令牌": ["tokens", "token.create", "token.delete", "token.update", "token.key"],
        "钱包": ["balance", "topup", "pay", "pay.status", "stats", "pricing"],
        "模型 & 分组": ["models", "groups"],
        "订阅 & 签到": ["subs", "checkin"],
        "邀请": ["aff", "aff.transfer"],
    }
    for cat, cmds in cats.items():
        lines.append(f"【{cat}】")
        for c in cmds:
            if c in _COMMANDS:
                lines.append(f"  /api {c} — {_COMMANDS[c][1]}")
        lines.append("")
    lines.append("💡 私聊 Bot 使用 /api bind 绑定账号后即可操作。")
    lines.append("🔒 涉及个人数据的命令仅限私聊使用，群聊中会被拒绝。")
    return "\n".join(lines)


# ── Plugin 类 ─────────────────────────────────────────────────────────────

class Plugin:
    """NewAPI 站点管理插件 — 通过 Bot 操作 NewAPI 的核心功能。"""

    name = "newapi"
    description = "管理 NewAPI 站点: 账号绑定、令牌管理、余额查看、模型列表、签到等。"

    intent_examples = [
        "/api help",
        "/api bind https://example.com username password",
        "/api me",
        "/api tokens",
        "/api token.create",
        "/api token.update 123 expire_days=1",
        "/api balance",
        "/api pay.status",
        "/api models",
        "/api checkin",
        "列出我的令牌",
        "把我的令牌改成明天过期",
        "给我发支付二维码",
        "我刚支付成功了，帮我查下到账没",
        "查看我的API余额",
        "创建一个新令牌",
        "帮我签到",
    ]

    rules = [
        "涉及个人数据的命令 (me/balance/tokens/stats/aff/email/topup/pay/pay.status/token.create/token.delete/token.update/token.key) 只能在私聊中使用，群聊一律拒绝。",
        "绝不在群聊中展示任何用户的密钥、密码、邮箱、余额、令牌列表等敏感信息。",
        "每个用户只能查看和操作自己绑定的账号数据，不能跨用户访问。",
        "如果用户在群聊中发送了疑似密码命令，优先执行安全处理（撤回并提醒），再引导私聊 Bot。",
        "若用户刚完成支付、发送支付成功截图、提到余额增加或询问是否到账，优先核对本地待确认订单与 NewAPI 充值记录，不要只根据截图直接下结论。",
        "回复使用纯文本，不使用 Markdown 强调符号（如 ** 和 `）。",
    ]

    args_schema = {
        "message": "string，用户的原始消息文本",
    }

    config_schema = {
        "type": "object",
        "properties": {
            "enabled": {"type": "boolean", "description": "启用插件"},
            "display_name": {
                "type": "string",
                "description": "插件显示名称（用于 WebUI 和帮助文案）",
                "default": "NewAPI",
            },
            "session_ttl_seconds": {
                "type": "integer",
                "description": "登录会话缓存 TTL（秒）",
                "default": 600,
                "minimum": 60,
                "maximum": 86400,
            },
            "response.force_plain_text": {
                "type": "boolean",
                "description": "回复强制纯文本（去除 Markdown 风格）",
                "default": True,
            },
            "response.strip_markdown_chars": {
                "type": "boolean",
                "description": "纯文本模式下去除 ** 和 反引号",
                "default": True,
            },
            "payment.auto_require_method_selection_when_multiple": {
                "type": "boolean",
                "description": "auto 模式遇到多个渠道时先让用户选择",
                "default": True,
            },
            "payment.auto_prefer_methods": {
                "type": "array",
                "description": "auto 模式优先顺序（wxpay,alipay,qqpay）",
                "default": ["wxpay", "alipay", "qqpay"],
            },
            "payment.auto_fallback_method_when_info_unavailable": {
                "type": "string",
                "description": "topup info 不可用时 auto 回退方式",
                "enum": ["wxpay", "alipay", "qqpay", "stripe", "none"],
                "default": "wxpay",
            },
            "payment.include_epay_submit_url": {
                "type": "boolean",
                "description": "返回 EPay 完整签名链接（submit.php + 参数）",
                "default": True,
            },
            "payment.show_amount_unit_hint": {
                "type": "boolean",
                "description": "显示大额档位“数量单位”提示",
                "default": True,
            },
            "payment.show_topup_command_hints": {
                "type": "boolean",
                "description": "在 /api topup 末尾显示命令示例",
                "default": True,
            },
            "privacy_guard.enabled": {"type": "boolean", "description": "群聊敏感命令保护开关", "default": True},
            "privacy_guard.sensitive_commands": {"type": "array", "description": "触发敏感保护的命令列表"},
            "privacy_guard.only_when_password_like": {"type": "boolean", "description": "仅疑似密码输入时触发", "default": True},
            "privacy_guard.recall_message": {"type": "boolean", "description": "检测到敏感命令后尝试撤回", "default": True},
            "privacy_guard.notify_group": {"type": "boolean", "description": "在群里提示", "default": True},
            "privacy_guard.notify_private": {"type": "boolean", "description": "私聊提醒发送者", "default": True},
            "privacy_guard.group_notice_template": {"type": "string", "description": "群提醒模板"},
            "privacy_guard.private_notice_template": {"type": "string", "description": "私聊提醒模板"},
        },
    }

    def __init__(self) -> None:
        self._config: dict[str, Any] = {}
        self._privacy_guard_cfg: dict[str, Any] = _build_privacy_guard_config({})
        self._runtime_cfg: dict[str, Any] = copy.deepcopy(_PLUGIN_RUNTIME_DEFAULTS)
        self.display_name = _get_plugin_display_name()
        self.description = f"管理 {self.display_name} 站点: 账号绑定、令牌管理、余额查看、模型列表、签到等。"

    async def setup(self, config: dict[str, Any], context: Any) -> None:
        global _PLUGIN_RUNTIME_CFG
        self._config = config if isinstance(config, dict) else {}
        self._privacy_guard_cfg = _build_privacy_guard_config(self._config)
        self._runtime_cfg = _build_runtime_config(self._config)
        _PLUGIN_RUNTIME_CFG = self._runtime_cfg
        self.display_name = _get_plugin_display_name()
        self.description = f"管理 {self.display_name} 站点: 账号绑定、令牌管理、余额查看、模型列表、签到等。"
        _log.info(
            "newapi plugin setup | config_keys=%s | runtime=%s | privacy_guard=%s",
            list(self._config.keys()),
            {
                "display_name": self.display_name,
                "session_ttl_seconds": self._runtime_cfg.get("session_ttl_seconds"),
                "force_plain_text": self._runtime_cfg.get("response", {}).get("force_plain_text"),
                "auto_require_method_selection": self._runtime_cfg.get("payment", {}).get("auto_require_method_selection_when_multiple"),
                "fallback_method": self._runtime_cfg.get("payment", {}).get("auto_fallback_method_when_info_unavailable"),
                "include_epay_submit_url": self._runtime_cfg.get("payment", {}).get("include_epay_submit_url"),
            },
            {
                "enabled": self._privacy_guard_cfg.get("enabled", True),
                "commands": sorted(self._privacy_guard_cfg.get("sensitive_commands", set())),
                "recall": self._privacy_guard_cfg.get("recall_message", True),
                "group_notice": self._privacy_guard_cfg.get("notify_group", True),
                "private_notice": self._privacy_guard_cfg.get("notify_private", True),
            },
        )

        registry = getattr(context, "agent_tool_registry", None)
        if registry:
            self._register_agent_tools(registry)

    async def _maybe_handle_group_password_leak(self, cmd: str, cmd_args: str, context: dict[str, Any]) -> None:
        """群聊中遇到疑似密码输入时，按配置尝试撤回并提醒。"""
        if _context_is_private(context):
            return
        cfg = self._privacy_guard_cfg
        if not bool(cfg.get("enabled", True)):
            return
        sensitive_commands = cfg.get("sensitive_commands", set())
        if cmd not in sensitive_commands:
            return

        raw_text = str(context.get("message_text", "")).strip()
        if bool(cfg.get("only_when_password_like", True)) and not _looks_like_password_input(cmd, cmd_args, raw_text):
            return

        api_call = context.get("api_call")
        if not callable(api_call):
            return

        user_id = str(context.get("user_id", "")).strip()
        group_id_raw = str(context.get("group_id", "")).strip()
        message_id = str(context.get("message_id", "")).strip()

        recalled = False
        if bool(cfg.get("recall_message", True)) and message_id:
            try:
                message_arg: Any = int(message_id) if message_id.isdigit() else message_id
                await api_call("delete_msg", message_id=message_arg)
                recalled = True
            except Exception:
                _log.warning(
                    "group_sensitive_recall_failed | user=%s | group=%s | message_id=%s | cmd=%s",
                    user_id or "-",
                    group_id_raw or "-",
                    message_id or "-",
                    cmd,
                    exc_info=True,
                )

        group_id = int(group_id_raw) if group_id_raw.isdigit() else 0
        if bool(cfg.get("notify_group", True)) and group_id > 0:
            notice = _safe_notice_template(
                str(cfg.get("group_notice_template", "")),
                {
                    "command": cmd,
                    "user_id": user_id,
                    "group_id": str(group_id),
                    "recalled": "1" if recalled else "0",
                },
            )
            if notice:
                try:
                    await api_call("send_group_msg", group_id=group_id, message=notice)
                except Exception:
                    _log.warning(
                        "group_sensitive_notice_failed | user=%s | group=%s | cmd=%s",
                        user_id or "-",
                        group_id or "-",
                        cmd,
                        exc_info=True,
                    )

        if bool(cfg.get("notify_private", True)) and user_id.isdigit():
            private_notice = _safe_notice_template(
                str(cfg.get("private_notice_template", "")),
                {
                    "command": cmd,
                    "user_id": user_id,
                    "group_id": str(group_id),
                    "recalled": "1" if recalled else "0",
                },
            )
            if private_notice:
                try:
                    await api_call("send_private_msg", user_id=int(user_id), message=private_notice)
                except Exception:
                    _log.warning(
                        "group_sensitive_private_notice_failed | user=%s | cmd=%s",
                        user_id,
                        cmd,
                        exc_info=True,
                    )

    async def handle(self, message: str, context: dict) -> str:
        text = (message or "").strip()

        # 支持 /api <cmd> 和直接 <cmd> 两种格式
        if text.lower().startswith("/api"):
            text = text[4:].strip()

        if not text or text.lower() in ("help", "帮助", "?"):
            return _plain_reply(_build_help())

        # 解析子命令
        parts = text.split(None, 1)
        cmd, args = _resolve_command(parts[0], parts[1] if len(parts) > 1 else "")

        # 隐私保护: 敏感命令必须私聊
        if cmd in _PRIVATE_ONLY_COMMANDS and not _context_is_private(context):
            await self._maybe_handle_group_password_leak(cmd=cmd, cmd_args=args, context=context)
            return _PRIVATE_ONLY_MSG

        handler = _COMMANDS.get(cmd)
        if handler:
            try:
                return _plain_reply(await handler[0](args, context))
            except Exception as e:
                _log.exception("newapi command error: %s", cmd)
                return f"❌ 命令执行出错: {e}"

        return f"未知命令: {cmd}\n输入 /api help 查看帮助。"

    async def teardown(self) -> None:
        _log.info("newapi plugin teardown")

    # ── Agent 工具注册 ────────────────────────────────────────────────────

    def _register_agent_tools(self, registry: Any) -> None:
        from core.agent_tools import ToolSchema, PromptHint
        display_name = getattr(self, "display_name", "") or _get_plugin_display_name()

        # 注册一个通用的 NewAPI 操作工具
        registry.register(
            ToolSchema(
                name="newapi_manage",
                description=(
                    f"管理 {display_name} 站点。可执行标准子命令："
                    "bind/register/unbind/me/tokens/token.create/token.delete/token.update/token.key/"
                    "balance/stats/models/groups/subs/checkin/email/topup/pay/pay.status/aff/aff.transfer/pricing/help。"
                    "敏感命令在群聊会被拒绝；若群聊命令疑似包含密码会触发安全提醒。"
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "command": {
                            "type": "string",
                            "description": f"要执行的 {display_name} 子命令。",
                            "enum": sorted(_COMMANDS.keys()),
                        },
                        "args": {
                            "type": "string",
                            "description": "命令参数字符串，按 /api 命令格式传入。",
                            "default": "",
                        },
                    },
                    "required": ["command"],
                },
                category="general",
                group="utility",
            ),
            self._agent_handle,
        )

        registry.register_context_provider(
            "newapi_binding",
            self._agent_context_summary,
            priority=35,
        )

        registry.register_prompt_hint(PromptHint(
            source="newapi",
            section="tools_guidance",
            content=(
                f"newapi_manage 用于管理用户绑定的 {display_name} 账号。"
                "先看 newapi_binding 上下文；未绑定时优先引导 /api bind。"
                "当用户只发 '/api'、'帮助'、'?' 时，优先调用 help。"
                "对敏感命令（me/balance/tokens/stats/aff/email/topup/pay/pay.status/token.*）必须在私聊执行，群聊拒绝。"
                "群聊出现 bind/register/email 且参数疑似密码时，也要调用 newapi_manage 对应命令，让插件触发撤回+提醒策略。"
                "用户要求充值但未指定渠道时，先让用户在微信/支付宝等可用方式中二选一，不要直接默认渠道。"
                "如果用户刚发起过 pay，随后又说已支付、支付成功、到账了吗、余额增加了，或者发送支付成功/余额截图，优先调用 pay.status 核对最近订单和当前余额，不要只根据截图直接回答。"
                "当 newapi_binding 上下文里出现 recent_pending_payment 时，遇到支付相关追问必须先查 pay.status，再决定是否回复到账。"
                "command 参数必须使用 enum 中的标准命令，不要自造 token.list 之类别名。"
                "newapi 相关 final_answer 必须使用纯文本，禁止使用 Markdown 粗体和反引号。"
            ),
            priority=30,
        ))

    async def _agent_handle(self, args: dict[str, Any], context: dict[str, Any]) -> Any:
        from core.agent_tools import ToolCallResult
        raw_cmd = str(args.get("command", "")).strip()
        raw_args = str(args.get("args", "")).strip()
        cmd, cmd_args = _resolve_command(raw_cmd, raw_args)
        if not cmd:
            return ToolCallResult(ok=False, error="missing_command", display="请指定命令")
        # 隐私保护: 敏感命令在群聊中拒绝执行
        if cmd in _PRIVATE_ONLY_COMMANDS and not _context_is_private(context):
            await self._maybe_handle_group_password_leak(cmd=cmd, cmd_args=cmd_args, context=context)
            return ToolCallResult(ok=False, error="private_only", display=_PRIVATE_ONLY_MSG)
        handler = _COMMANDS.get(cmd)
        if not handler:
            return ToolCallResult(ok=False, error="unknown_command", display=f"未知命令: {cmd}")
        try:
            result = await handler[0](cmd_args, context)
            display = _plain_reply(str(result or ""))
            failed = display.strip().startswith("❌")
            return ToolCallResult(
                ok=not failed,
                data={"command": cmd},
                error="command_failed" if failed else "",
                display=display,
            )
        except Exception as e:
            return ToolCallResult(ok=False, error=str(e), display=f"执行出错: {e}")

    @staticmethod
    def _agent_context_summary(info: dict[str, Any]) -> str:
        display_name = _get_plugin_display_name()
        ctx = info.get("ctx")
        is_private = bool(getattr(ctx, "is_private", False))
        platform = str(getattr(ctx, "platform", "") or info.get("platform", "qq"))
        user_id = str(getattr(ctx, "user_id", "") or info.get("user_id", ""))
        key = f"{platform}:{user_id}"
        creds = _load_credentials().get(key)
        if not creds:
            return f"newapi_binding: 当前用户未绑定 {display_name}。敏感操作前需先私聊执行 /api bind <站点URL> <用户名> <密码>。"
        if not is_private:
            return f"newapi_binding: 当前用户已绑定 {display_name}。余额、订单号、支付状态等敏感信息只能在私聊上下文中查看或操作。"
        site_url = str(creds.get("site_url", "")).strip() or "-"
        username = str(creds.get("username", "")).strip() or "-"
        pending = _get_pending_payment(key)
        if not pending:
            return f"newapi_binding: 当前用户已绑定 {display_name} 站点 {site_url}，账号 {username}。"
        return (
            f"newapi_binding: 当前用户已绑定 {display_name} 站点 {site_url}，账号 {username}。"
            f" recent_pending_payment: {_pending_payment_brief(pending)}。"
            " 如果用户提到支付成功、已付款、余额增加、到账了吗，或发送支付成功/余额截图，应先调用 newapi_manage 的 pay.status。"
        )
