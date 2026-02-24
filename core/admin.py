"""管理员指令系统 — 超级管理员鉴权、群白名单、热重载、群信息查询、行为调参。

指令格式（仅超级管理员可用，/help 除外）：
  /yuki help | /help          功能帮助
  /yuki reload | /yukibot     热重载配置
  /yuki ping | /ping          存活检测
  /yuki status                运行状态
  /yuki say <text>            复读
  /yuki 加白 | /whitelist add 当前群加白
  /yuki 拉黑 | /whitelist rm  当前群移除白名单
  /yuki 白名单                列出白名单
  /yuki 群信息                当前群基本信息
  /yuki 群成员                群成员列表
  /yuki 管理员                群管理员列表
  /yuki debug <user_id>       查看用户画像
  /yuki cookie [平台] [浏览器] [force] 刷新平台Cookie
  /yuki 行为                  查看行为参数
  /yuki 冷漠|安静|活跃        切换行为预设
  /yuki 行为 <参数名> <值>    单独调参
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import time
from pathlib import Path
from typing import Any, TYPE_CHECKING

from core.system_prompts import SystemPromptRelay
from utils.text import normalize_text

if TYPE_CHECKING:
    pass  # 避免循环导入

_log = logging.getLogger("yukiko.admin")


class AdminEngine:
    """管理员指令解析 + 群白名单管理 + 群信息查询。"""

    # ── 顶级指令（第一个词直接匹配）──
    _TOP_COMMANDS: dict[str, str] = {
        "/yukibot": "_cmd_reload",
        "/yukiko": "_cmd_reload",
        "/ping": "_cmd_ping",
        "/health": "_cmd_health",
        "/help": "_cmd_help",
        "/plugins": "_cmd_plugins",
    }

    # ── /yuki 子命令映射 ──
    _YUKI_SUBS: dict[str, str] = {
        "help": "_cmd_help",
        "帮助": "_cmd_help",
        "reload": "_cmd_reload",
        "重载": "_cmd_reload",
        "热重载": "_cmd_reload",
        "ping": "_cmd_ping",
        "status": "_cmd_status",
        "状态": "_cmd_status",
        "health": "_cmd_health",
        "say": "_cmd_say",
        "说": "_cmd_say",
        "加白": "_cmd_whitelist_add",
        "加白本群": "_cmd_whitelist_add",
        "whitelist": "_cmd_whitelist",
        "拉黑": "_cmd_whitelist_remove",
        "拉黑本群": "_cmd_whitelist_remove",
        "移除白名单": "_cmd_whitelist_remove",
        "白名单": "_cmd_whitelist_list",
        "群信息": "_cmd_group_info",
        "群成员": "_cmd_group_members",
        "群管理员": "_cmd_group_admins",
        "管理员": "_cmd_group_admins",
        "debug": "_cmd_debug",
        "调试": "_cmd_debug",
        "cookie": "_cmd_cookie_refresh",
        "cookies": "_cmd_cookie_refresh",
        "plugins": "_cmd_plugins",
        "刷新cookie": "_cmd_cookie_refresh",
        "行为": "_cmd_behavior",
        "behavior": "_cmd_behavior",
        "冷漠": "_cmd_behavior_cold",
        "活跃": "_cmd_behavior_active",
        "安静": "_cmd_behavior_quiet",
        "定海神针": "_cmd_clear_screen",
        "清屏": "_cmd_clear_screen",
    }

    def __init__(self, config: dict[str, Any], storage_dir: Path):
        admin_cfg = config.get("admin", {}) or {}
        self.super_admin_qq: str = str(admin_cfg.get("super_admin_qq", "")).strip()
        self.non_whitelist_mode: str = str(admin_cfg.get("non_whitelist_mode", "minimal")).strip()
        self._whitelist_file = storage_dir / "admin_state.json"
        self._whitelisted_groups: set[int] = self._load_whitelist()
        self._start_time = time.time()
        self._message_count = 0

    # ── 公共接口 ──────────────────────────────────────────────
    @property
    def enabled(self) -> bool:
        return bool(self.super_admin_qq)

    def is_admin_command(self, text: str) -> bool:
        """判断消息是否为管理员指令。"""
        stripped = text.strip()
        if not stripped:
            return False
        parts = stripped.split()
        first = parts[0].lower()
        # 顶级指令
        if first in self._TOP_COMMANDS:
            return True
        # /yuki 前缀
        if first in ("/yuki", "/yuki帮助"):
            return True
        return False

    def is_super_admin(self, user_id: str) -> bool:
        return self.enabled and str(user_id) == self.super_admin_qq

    def is_group_whitelisted(self, group_id: int | str) -> bool:
        """群是否在白名单中。权限系统未启用时所有群都算白名单。"""
        if not self.enabled:
            return True
        return int(group_id) in self._whitelisted_groups if group_id else True

    def increment_message_count(self) -> None:
        self._message_count += 1

    async def handle_command(
        self,
        text: str,
        user_id: str,
        group_id: int,
        engine: Any = None,
        api_call: Any = None,
    ) -> str | None:
        """处理管理员指令。返回回复文本，非指令返回 None。"""
        stripped = text.strip()
        if not stripped:
            return None
        parts = stripped.split(maxsplit=2)
        first = parts[0].lower()

        # 顶级指令
        if first in self._TOP_COMMANDS:
            method_name = self._TOP_COMMANDS[first]
            # /help 不需要鉴权
            if method_name in {"_cmd_help", "_cmd_plugins"}:
                handler = getattr(self, method_name)
                return await handler(arg="", group_id=group_id, engine=engine, api_call=api_call)
            if not self.is_super_admin(user_id):
                return "权限不足，仅超级管理员可使用此指令。"
            handler = getattr(self, method_name)
            arg = parts[1].strip() if len(parts) > 1 else ""
            return await handler(arg=arg, user_id=user_id, group_id=group_id, engine=engine, api_call=api_call)

        # /yuki 前缀
        if first in ("/yuki", "/yuki帮助"):
            sub = parts[1].lower() if len(parts) > 1 else "help"
            if first == "/yuki帮助":
                sub = "help"
            method_name = self._YUKI_SUBS.get(sub)
            if not method_name:
                return f"未知子命令: {sub}\n发 /yuki help 查看可用指令。"
            # /help 不需要鉴权
            if method_name in {"_cmd_help", "_cmd_plugins"}:
                handler = getattr(self, method_name)
                return await handler(arg="", group_id=group_id, engine=engine, api_call=api_call)
            if not self.is_super_admin(user_id):
                return "权限不足，仅超级管理员可使用此指令。"
            handler = getattr(self, method_name)
            arg = parts[2].strip() if len(parts) > 2 else ""
            return await handler(arg=arg, user_id=user_id, group_id=group_id, engine=engine, api_call=api_call)

        return None

    # ── 指令实现 ──────────────────────────────────────────────
    async def _cmd_help(self, **_: Any) -> str:
        return (
            "YuKiKo Bot 指令列表\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "功能:\n"
            "  @我 或叫 雪/yukiko/yuki 触发对话\n"
            "  发送视频链接 → 自动解析（B站/抖音/快手）\n"
            "  网络搜索、AI 画图、GitHub 搜索\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "管理指令（仅管理员）:\n"
            "  /yuki reload  热重载配置\n"
            "  /yuki ping    存活检测\n"
            "  /yuki status  运行状态\n"
            "  /yuki say <文本>  复读\n"
            "  /yuki 加白    当前群加白名单\n"
            "  /yuki 拉黑    当前群移除白名单\n"
            "  /yuki 白名单  列出白名单\n"
            "  /yuki 群信息  当前群基本信息\n"
            "  /yuki debug <QQ号>  查看用户画像\n"
            "  /yuki cookie [平台] [浏览器] [force]  刷新Cookie\n"
            "  /plugins 或 /yuki plugins  列出已加载插件和 rules\n"
            "  /yuki 定海神针 [总行数] [分段数] [段间延迟秒]\n"
            "      例: /yuki 定海神针 3000 10 5\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "行为调参:\n"
            "  /yuki 行为          查看当前参数\n"
            "  /yuki 冷漠          只回@和叫名字的\n"
            "  /yuki 安静          偶尔接话，门槛高\n"
            "  /yuki 活跃          积极参与群聊\n"
            "  /yuki 行为 默认     恢复出厂设置\n"
            "  /yuki 行为 接话门槛 3.0  单独调参"
        )

    async def _cmd_plugins(self, engine: Any = None, **_: Any) -> str:
        if engine is None:
            return "引擎未就绪。"
        registry = getattr(engine, "plugins", None)
        schemas = getattr(registry, "schemas", None) if registry is not None else None
        if not isinstance(schemas, list):
            return "插件系统未就绪。"
        if not schemas:
            return "当前没有已加载插件。"

        lines: list[str] = [f"已加载插件（共 {len(schemas)} 个）:"]
        for item in schemas:
            if not isinstance(item, dict):
                continue
            name = normalize_text(str(item.get("name", ""))) or "unknown"
            desc = normalize_text(str(item.get("description", ""))) or "无描述"
            lines.append(f"- {name}: {desc}")

            intents_raw = item.get("intent_examples", [])
            intents = [normalize_text(str(x)) for x in intents_raw if normalize_text(str(x))]
            if intents:
                lines.append(f"  intents: {' | '.join(intents[:2])}")

            rules_raw = item.get("rules", [])
            rules: list[str] = []
            if isinstance(rules_raw, str):
                one = normalize_text(rules_raw)
                if one:
                    rules.append(one)
            elif isinstance(rules_raw, list):
                rules = [normalize_text(str(x)) for x in rules_raw if normalize_text(str(x))]
            elif isinstance(rules_raw, dict):
                for key, value in rules_raw.items():
                    left = normalize_text(str(key))
                    right = normalize_text(str(value))
                    if left and right:
                        rules.append(f"{left}: {right}")
                    elif left:
                        rules.append(left)
            if rules:
                lines.append(f"  rules: {' ; '.join(rules[:3])}")
        return "\n".join(lines)

    async def _cmd_ping(self, **_: Any) -> str:
        return "pong"

    async def _cmd_health(self, **_: Any) -> str:
        uptime = int(time.time() - self._start_time)
        h, m, s = uptime // 3600, (uptime % 3600) // 60, uptime % 60
        pid = os.getpid()
        return f"运行中 | PID {pid} | 已处理 {self._message_count} 条 | 运行 {h}h{m}m{s}s"

    async def _cmd_say(self, arg: str = "", **_: Any) -> str:
        return arg or "（空消息）"

    async def _cmd_reload(self, engine: Any = None, **_: Any) -> str:
        if engine is None:
            return "引擎未就绪，无法重载。"
        reload_fn = getattr(engine, "reload_config", None)
        if not reload_fn:
            return "引擎不支持热重载。"
        ok, msg = reload_fn()
        return f"重载{'成功' if ok else '失败'}: {msg}"

    async def _cmd_whitelist(self, arg: str = "", group_id: int = 0, **_: Any) -> str:
        sub = arg.lower().split()[0] if arg else ""
        if sub in ("add", "加白"):
            return await self._cmd_whitelist_add(group_id=group_id)
        if sub in ("remove", "rm", "拉黑", "移除"):
            return await self._cmd_whitelist_remove(group_id=group_id)
        if sub in ("list", "列表", ""):
            return await self._cmd_whitelist_list()
        return "用法: /whitelist add | remove | list"

    async def _cmd_whitelist_add(self, group_id: int = 0, **_: Any) -> str:
        if not group_id:
            return "请在群聊中使用此指令。"
        self._whitelisted_groups.add(group_id)
        self._save_whitelist()
        return f"群 {group_id} 已加入白名单。"

    async def _cmd_whitelist_remove(self, group_id: int = 0, **_: Any) -> str:
        if not group_id:
            return "请在群聊中使用此指令。"
        self._whitelisted_groups.discard(group_id)
        self._save_whitelist()
        return f"群 {group_id} 已移出白名单。"

    async def _cmd_whitelist_list(self, **_: Any) -> str:
        if not self._whitelisted_groups:
            return "白名单为空。"
        items = ", ".join(str(g) for g in sorted(self._whitelisted_groups))
        return f"白名单群: {items}"

    async def _cmd_status(self, engine: Any = None, **_: Any) -> str:
        lines = [await self._cmd_health()]
        lines.append(f"白名单群数: {len(self._whitelisted_groups)}")
        if engine:
            mem = getattr(engine, "memory", None)
            if mem:
                profiles = getattr(mem, "_profiles", {})
                lines.append(f"用户画像数: {len(profiles)}")
        return "\n".join(lines)

    async def _cmd_debug(self, arg: str = "", engine: Any = None, **_: Any) -> str:
        if not arg:
            return "用法: /yuki debug <QQ号>"
        if not engine:
            return "引擎未就绪。"
        mem = getattr(engine, "memory", None)
        if not mem:
            return "记忆系统未就绪。"
        summary = mem.get_user_profile_summary(arg)
        return summary or f"未找到用户 {arg} 的画像。"

    async def _cmd_cookie_refresh(self, arg: str = "", engine: Any = None, **_: Any) -> str:
        """刷新平台 Cookie（通过浏览器提取）。
        用法: /yuki cookie [bilibili|douyin|kuaishou] [浏览器] [force]
        """
        parts_raw = arg.split() if arg else []
        force_tokens = {"force", "auto", "close"}
        auto_close = any(normalize_text(str(p)).lower() in force_tokens for p in parts_raw)
        parts = [p for p in parts_raw if normalize_text(str(p)).lower() not in force_tokens]

        platform = normalize_text(parts[0]).lower() if parts else "all"
        browser = normalize_text(parts[1]).lower() if len(parts) > 1 else ("edge" if os.name == "nt" else "chrome")
        results: list[str] = []
        running_hint_needed = False

        try:
            from core.cookie_auth import (
                extract_bilibili_cookies,
                extract_douyin_cookie,
                extract_kuaishou_cookie,
                is_browser_running,
            )
        except ImportError:
            return "cookie_auth 模块不可用。"

        if platform in ("all", "bilibili", "bili", "b站"):
            bili = extract_bilibili_cookies(browser, auto_close=auto_close)
            if bili.get("sessdata"):
                results.append(f"B站: 提取成功 (SESSDATA={bili['sessdata'][:8]}...)")
                if engine:
                    self._update_engine_cookie(engine, "bilibili", bili)
            else:
                results.append(f"B站: 提取失败（检查 {browser}）")
                running_hint_needed = running_hint_needed or (not auto_close)

        if platform in ("all", "douyin", "抖音"):
            cookie = extract_douyin_cookie(browser, auto_close=auto_close)
            if cookie:
                results.append(f"抖音: 提取成功 ({len(cookie)} 字节)")
                if engine:
                    self._update_engine_cookie(engine, "douyin", {"cookie": cookie})
            else:
                results.append(f"抖音: 提取失败（检查 {browser}）")
                running_hint_needed = running_hint_needed or (not auto_close)

        if platform in ("all", "kuaishou", "快手"):
            cookie = extract_kuaishou_cookie(browser, auto_close=auto_close)
            if cookie:
                results.append(f"快手: 提取成功 ({len(cookie)} 字节)")
                if engine:
                    self._update_engine_cookie(engine, "kuaishou", {"cookie": cookie})
            else:
                results.append(f"快手: 提取失败（检查 {browser}）")
                running_hint_needed = running_hint_needed or (not auto_close)

        if not results:
            return f"未知平台: {platform}\n用法: /yuki cookie [bilibili|douyin|kuaishou] [浏览器] [force]"

        if running_hint_needed and browser in {"edge", "chrome", "brave"}:
            try:
                if is_browser_running(browser):
                    results.append(
                        f"提示: 检测到 {browser} 正在运行，试试 `/yuki cookie {platform} {browser} force` 强制提取"
                    )
            except Exception:
                pass

        return "Cookie 刷新结果:\n" + "\n".join(results)

    @staticmethod
    def _update_engine_cookie(engine: Any, platform: str, data: dict[str, str]) -> None:
        """热更新 engine/tools 中的 cookie（不写入配置文件，重启后失效）。"""
        tools = getattr(engine, "tools", None)
        if not tools:
            return

        va = getattr(tools, "_video_analyzer", None)

        if platform == "bilibili":
            sessdata = data.get("sessdata", "")
            bili_jct = data.get("bili_jct", "")
            if va:
                va._bili_sessdata = sessdata
                va._bili_jct = bili_jct
        elif platform == "douyin":
            cookie = data.get("cookie", "")
            tools._douyin_cookie = cookie
            if va:
                va._douyin_cookie = cookie
        elif platform == "kuaishou":
            cookie = data.get("cookie", "")
            tools._kuaishou_cookie = cookie
            if va:
                va._kuaishou_cookie = cookie

    # ── 行为调参 ──────────────────────────────────────────────

    # 预设模板：(trigger 覆盖, routing 覆盖, self_check 覆盖, 描述)
    _BEHAVIOR_PRESETS: dict[str, tuple[dict, dict, dict, str]] = {
        "默认": (
            {"ai_listen_enable": True, "delegate_undirected_to_ai": True,
             "ai_listen_min_messages": 8, "ai_listen_min_score": 2.2,
             "followup_reply_window_seconds": 30, "followup_max_turns": 2},
            {"min_confidence": 0.55, "followup_min_confidence": 0.75,
             "non_directed_min_confidence": 0.72, "ai_gate_min_confidence": 0.66},
            {"listen_probe_min_confidence": 0.86, "non_direct_reply_min_confidence": 0.78},
            "出厂默认，适度主动",
        ),
        "冷漠": (
            {"ai_listen_enable": False, "delegate_undirected_to_ai": False,
             "followup_reply_window_seconds": 10, "followup_max_turns": 1},
            {"min_confidence": 0.70, "followup_min_confidence": 0.88,
             "non_directed_min_confidence": 0.90, "ai_gate_min_confidence": 0.85},
            {"listen_probe_min_confidence": 0.98, "non_direct_reply_min_confidence": 0.92},
            "只在被 @ 或叫名字时回复，几乎不主动接话",
        ),
        "安静": (
            {"ai_listen_enable": True, "delegate_undirected_to_ai": True,
             "ai_listen_min_messages": 15, "ai_listen_min_score": 3.5,
             "followup_reply_window_seconds": 15, "followup_max_turns": 1},
            {"min_confidence": 0.65, "followup_min_confidence": 0.82,
             "non_directed_min_confidence": 0.82, "ai_gate_min_confidence": 0.78},
            {"listen_probe_min_confidence": 0.92, "non_direct_reply_min_confidence": 0.85},
            "偶尔接话，但门槛较高",
        ),
        "活跃": (
            {"ai_listen_enable": True, "delegate_undirected_to_ai": True,
             "ai_listen_min_messages": 4, "ai_listen_min_score": 1.5,
             "followup_reply_window_seconds": 45, "followup_max_turns": 3},
            {"min_confidence": 0.45, "followup_min_confidence": 0.60,
             "non_directed_min_confidence": 0.58, "ai_gate_min_confidence": 0.50},
            {"listen_probe_min_confidence": 0.75, "non_direct_reply_min_confidence": 0.65},
            "积极参与群聊，主动接话频率高",
        ),
    }

    # 可单独调的参数 → (config section, key, 类型, 说明)
    _BEHAVIOR_PARAMS: dict[str, tuple[str, str, type, str]] = {
        "主动接话": ("trigger", "ai_listen_enable", bool, "是否主动加入群聊讨论"),
        "接话门槛": ("trigger", "ai_listen_min_score", float, "主动接话热度门槛 (越高越不容易触发)"),
        "接话消息数": ("trigger", "ai_listen_min_messages", int, "触发主动接话的最少消息数"),
        "跟随窗口": ("trigger", "followup_reply_window_seconds", int, "跟随对话窗口(秒)"),
        "跟随轮数": ("trigger", "followup_max_turns", int, "跟随对话最大轮数"),
        "非指名置信度": ("routing", "non_directed_min_confidence", float, "非指名回复最低置信度"),
        "跟随置信度": ("routing", "followup_min_confidence", float, "跟随对话最低置信度"),
        "基础置信度": ("routing", "min_confidence", float, "基础最低置信度"),
        "旁听置信度": ("self_check", "listen_probe_min_confidence", float, "旁听模式最低置信度"),
    }

    async def _cmd_behavior(self, arg: str = "", engine: Any = None, **_: Any) -> str:
        """行为调参主命令。"""
        if not arg:
            return self._behavior_status(engine)

        parts = arg.split(maxsplit=1)
        sub = parts[0]

        # 预设切换
        if sub in self._BEHAVIOR_PRESETS:
            return self._apply_behavior_preset(sub, engine)

        # 单参数调整: /yuki 行为 参数名 值
        if sub in self._BEHAVIOR_PARAMS:
            if len(parts) < 2:
                section, key, typ, desc = self._BEHAVIOR_PARAMS[sub]
                current = self._get_config_value(engine, section, key)
                return f"{sub}: {current} ({desc})"
            return self._set_behavior_param(sub, parts[1].strip(), engine)

        # 列出可用预设和参数
        presets = " | ".join(self._BEHAVIOR_PRESETS.keys())
        params = "\n".join(f"  {name}: {desc}" for name, (_, _, _, desc) in self._BEHAVIOR_PARAMS.items())
        return (
            f"未知参数: {sub}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"预设: {presets}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"可调参数:\n{params}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"用法:\n"
            f"  /yuki 行为 冷漠\n"
            f"  /yuki 行为 接话门槛 3.0"
        )

    async def _cmd_behavior_cold(self, engine: Any = None, **_: Any) -> str:
        return self._apply_behavior_preset("冷漠", engine)

    async def _cmd_behavior_active(self, engine: Any = None, **_: Any) -> str:
        return self._apply_behavior_preset("活跃", engine)

    async def _cmd_behavior_quiet(self, engine: Any = None, **_: Any) -> str:
        return self._apply_behavior_preset("安静", engine)

    async def _cmd_clear_screen(
        self,
        arg: str = "",
        group_id: int = 0,
        user_id: str = "",
        api_call: Any = None,
        engine: Any = None,
        **_: Any,
    ) -> str | None:
        """定海神针 — 分段多次发送，可自定义总行数/分段数/延迟。"""
        # 用法: /yuki 定海神针 3000 10 5
        total_lines = 3000
        segment_count = 10
        delay_seconds = 0.8

        parts = [p for p in (arg or "").split() if p.strip()]
        if len(parts) >= 1:
            try:
                total_lines = int(parts[0])
            except ValueError:
                pass
        if len(parts) >= 2:
            try:
                segment_count = int(parts[1])
            except ValueError:
                pass
        if len(parts) >= 3:
            try:
                delay_seconds = float(parts[2])
            except ValueError:
                pass

        total_lines = max(120, min(20000, total_lines))
        segment_count = max(1, min(80, segment_count))
        delay_seconds = max(0.0, min(30.0, delay_seconds))

        quote = await self._random_ai_quote(engine)

        # 无 api_call 时回退为单条文本（兼容单测/离线场景）。
        if not api_call:
            lines = ["　" for _ in range(total_lines)]
            lines.append(f"「{quote}」")
            return "\n".join(lines)

        base = total_lines // segment_count
        rem = total_lines % segment_count
        sent_any = False

        for idx in range(segment_count):
            n = base + (1 if idx < rem else 0)
            if n <= 0:
                continue
            lines = ["　" for _ in range(n)]
            if idx == segment_count - 1:
                lines.append(f"「{quote}」")
            msg = "\n".join(lines)

            try:
                if group_id:
                    await api_call("send_group_msg", group_id=int(group_id), message=msg)
                else:
                    await api_call("send_private_msg", user_id=int(user_id), message=msg)
                sent_any = True
            except Exception as exc:
                return f"定海神针发送失败（第 {idx + 1}/{segment_count} 段）：{exc}"

            if idx < segment_count - 1 and delay_seconds > 0:
                await asyncio.sleep(delay_seconds)

        if not sent_any:
            return "定海神针未发送：缺少有效会话目标。"
        # 已在命令内部发送完毕，不再让上层重复发一条回执。
        return None

    async def _random_ai_quote(self, engine: Any = None) -> str:
        """随机语录：优先 AI 生成，失败则本地兜底。"""
        local_pool = [
            "潮落归海，言尽于此。",
            "风过无痕，事了拂衣。",
            "山高路远，步履不停。",
            "心有定锚，万浪自平。",
            "言有尽，而意无穷。",
            "尘嚣退场，星河入席。",
        ]

        try:
            model_client = getattr(engine, "model_client", None)
            if model_client and bool(getattr(model_client, "enabled", False)):
                messages = [
                    {
                        "role": "system",
                        "content": SystemPromptRelay.admin_quote_system_prompt(),
                    },
                    {
                        "role": "user",
                        "content": "随机来一句有气势、适合收尾的语录。",
                    },
                ]
                text = str(await model_client.chat_text(messages, max_tokens=48)).strip()
                text = " ".join(text.split())
                if text:
                    # 避免模型输出多段，取第一句并裁剪长度。
                    text = text.split("\n", 1)[0].strip("。.!！?？ ")
                    if text:
                        return text[:24]
        except Exception:
            pass

        return random.choice(local_pool)

    def _behavior_status(self, engine: Any = None) -> str:
        """显示当前行为参数。"""
        lines = ["当前行为参数:"]
        for name, (section, key, typ, desc) in self._BEHAVIOR_PARAMS.items():
            val = self._get_config_value(engine, section, key)
            lines.append(f"  {name}: {val}")

        # 判断当前最接近哪个预设
        closest = self._detect_closest_preset(engine)
        if closest:
            lines.append(f"\n当前模式: {closest}")

        presets = " | ".join(self._BEHAVIOR_PRESETS.keys())
        lines.append(f"\n可用预设: {presets}")
        lines.append("用法: /yuki 行为 <预设名|参数名> [值]")
        return "\n".join(lines)

    def _apply_behavior_preset(self, preset_name: str, engine: Any = None) -> str:
        """应用行为预设。"""
        preset = self._BEHAVIOR_PRESETS.get(preset_name)
        if not preset:
            return f"未知预设: {preset_name}"

        trigger_overrides, routing_overrides, self_check_overrides, desc = preset

        if not engine:
            return "引擎未就绪，无法调参。"

        config = getattr(engine, "config", None)
        if not isinstance(config, dict):
            return "配置不可用。"

        # 合并到 config
        if "trigger" not in config:
            config["trigger"] = {}
        config["trigger"].update(trigger_overrides)

        if "routing" not in config:
            config["routing"] = {}
        config["routing"].update(routing_overrides)

        if "self_check" not in config:
            config["self_check"] = {}
        config["self_check"].update(self_check_overrides)

        # 触发引擎重新读取参数
        init_fn = getattr(engine, "_init_from_config", None)
        if init_fn:
            init_fn()

        # 重建 trigger engine
        trigger = getattr(engine, "trigger", None)
        if trigger:
            trigger.__init__(config.get("trigger", {}), config.get("bot", {}))

        # 持久化到 config.yml
        self._save_config_to_file(engine)

        return f"已切换到「{preset_name}」模式: {desc}"

    def _set_behavior_param(self, param_name: str, value_str: str, engine: Any = None) -> str:
        """设置单个行为参数。"""
        if not engine:
            return "引擎未就绪。"

        info = self._BEHAVIOR_PARAMS.get(param_name)
        if not info:
            return f"未知参数: {param_name}"

        section, key, typ, desc = info

        try:
            if typ is bool:
                value = value_str.lower() in ("true", "1", "yes", "on", "开", "是")
            elif typ is float:
                value = float(value_str)
            elif typ is int:
                value = int(value_str)
            else:
                value = value_str
        except (ValueError, TypeError):
            return f"参数值格式错误: {value_str} (期望 {typ.__name__})"

        config = getattr(engine, "config", None)
        if not isinstance(config, dict):
            return "配置不可用。"

        if section not in config:
            config[section] = {}
        config[section][key] = value

        # 重新加载
        init_fn = getattr(engine, "_init_from_config", None)
        if init_fn:
            init_fn()

        trigger = getattr(engine, "trigger", None)
        if trigger and section == "trigger":
            trigger.__init__(config.get("trigger", {}), config.get("bot", {}))

        self._save_config_to_file(engine)

        return f"{param_name} 已设为 {value} ({desc})"

    def _get_config_value(self, engine: Any, section: str, key: str) -> Any:
        """从 engine.config 读取参数值。"""
        if not engine:
            return "N/A"
        config = getattr(engine, "config", None)
        if not isinstance(config, dict):
            return "N/A"
        return config.get(section, {}).get(key, "未设置")

    def _detect_closest_preset(self, engine: Any = None) -> str:
        """检测当前参数最接近哪个预设。"""
        if not engine:
            return ""
        config = getattr(engine, "config", None)
        if not isinstance(config, dict):
            return ""

        best_name = ""
        best_match = 0
        for name, (trigger_ov, routing_ov, sc_ov, _) in self._BEHAVIOR_PRESETS.items():
            match_count = 0
            total = 0
            for k, v in trigger_ov.items():
                total += 1
                if config.get("trigger", {}).get(k) == v:
                    match_count += 1
            for k, v in routing_ov.items():
                total += 1
                if config.get("routing", {}).get(k) == v:
                    match_count += 1
            for k, v in sc_ov.items():
                total += 1
                if config.get("self_check", {}).get(k) == v:
                    match_count += 1
            if total > 0 and match_count / total > best_match:
                best_match = match_count / total
                best_name = name

        return f"{best_name} ({int(best_match * 100)}% 匹配)" if best_match > 0.5 else ""

    @staticmethod
    def _save_config_to_file(engine: Any) -> None:
        """将当前 config 持久化到 config.yml。"""
        try:
            import yaml
            config_manager = getattr(engine, "config_manager", None)
            if config_manager:
                config_path = getattr(config_manager, "_config_file", None)
                if config_path and Path(config_path).exists():
                    config = getattr(engine, "config", None)
                    if isinstance(config, dict):
                        Path(config_path).write_text(
                            yaml.dump(config, allow_unicode=True, default_flow_style=False, sort_keys=False),
                            encoding="utf-8",
                        )
                        _log.info("行为参数已保存到 %s", config_path)
                        return
        except Exception as exc:
            _log.warning("保存行为参数失败: %s", exc)

    # ── 群信息查询（通过 OneBot API）─────────────────────────
    async def _cmd_group_info(self, group_id: int = 0, api_call: Any = None, **_: Any) -> str:
        if not group_id:
            return "请在群聊中使用此指令。"
        if not api_call:
            return "API 不可用。"
        try:
            info = await api_call("get_group_info", group_id=group_id)
            name = info.get("group_name", "未知")
            count = info.get("member_count", "?")
            max_count = info.get("max_member_count", "?")
            return f"群名: {name}\n群号: {group_id}\n成员: {count}/{max_count}"
        except Exception as exc:
            return f"获取群信息失败: {str(exc)[:100]}"

    async def _cmd_group_members(self, group_id: int = 0, api_call: Any = None, **_: Any) -> str:
        if not group_id:
            return "请在群聊中使用此指令。"
        if not api_call:
            return "API 不可用。"
        try:
            members = await api_call("get_group_member_list", group_id=group_id)
            if not members:
                return "获取群成员列表为空。"
            total = len(members)
            # 只显示前 30 个
            lines = [f"群 {group_id} 成员列表（共 {total} 人）:"]
            for m in members[:30]:
                nick = m.get("card") or m.get("nickname") or str(m.get("user_id", ""))
                role = m.get("role", "member")
                tag = " [管理]" if role == "admin" else " [群主]" if role == "owner" else ""
                lines.append(f"  {nick} ({m.get('user_id', '?')}){tag}")
            if total > 30:
                lines.append(f"  ... 还有 {total - 30} 人")
            return "\n".join(lines)
        except Exception as exc:
            return f"获取群成员失败: {str(exc)[:100]}"

    async def _cmd_group_admins(self, group_id: int = 0, api_call: Any = None, **_: Any) -> str:
        if not group_id:
            return "请在群聊中使用此指令。"
        if not api_call:
            return "API 不可用。"
        try:
            members = await api_call("get_group_member_list", group_id=group_id)
            if not members:
                return "获取群成员列表为空。"
            admins = [m for m in members if m.get("role") in ("admin", "owner")]
            if not admins:
                return "未找到管理员信息。"
            lines = [f"群 {group_id} 管理员列表:"]
            for m in admins:
                nick = m.get("card") or m.get("nickname") or str(m.get("user_id", ""))
                role = "群主" if m.get("role") == "owner" else "管理员"
                lines.append(f"  [{role}] {nick} ({m.get('user_id', '?')})")
            return "\n".join(lines)
        except Exception as exc:
            return f"获取管理员列表失败: {str(exc)[:100]}"

    # ── 白名单持久化 ─────────────────────────────────────────
    def _load_whitelist(self) -> set[int]:
        if not self._whitelist_file.exists():
            return set()
        try:
            data = json.loads(self._whitelist_file.read_text(encoding="utf-8"))
            groups = data.get("whitelisted_groups", [])
            return {int(g) for g in groups if str(g).isdigit()}
        except Exception as exc:
            _log.warning("加载白名单失败: %s", exc)
            return set()

    def _save_whitelist(self) -> None:
        try:
            self._whitelist_file.parent.mkdir(parents=True, exist_ok=True)
            data = {"whitelisted_groups": sorted(self._whitelisted_groups)}
            self._whitelist_file.write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as exc:
            _log.error("保存白名单失败: %s", exc)
