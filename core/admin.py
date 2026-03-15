"""Admin command center for Yukiko bot."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from difflib import get_close_matches
from pathlib import Path
from typing import Any

from utils.text import normalize_text

_log = logging.getLogger("yukiko.admin")


# ── 模糊匹配命令表 ──
_FUZZY_COMMAND_MAP: dict[str, str] = {
    "重载": "reload", "重新加载": "reload", "刷新配置": "reload", "reload": "reload",
    "ping": "ping", "在吗": "ping", "存活": "ping",
    "状态": "status", "运行状态": "status", "status": "status",
    "帮助": "help", "help": "help", "菜单": "help", "功能": "help",
    "详细": "help_detail", "detail": "help_detail", "help_detail": "help_detail", "参数": "help_detail",
    "加白": "white_add", "加白名单": "white_add", "白名单添加": "white_add",
    "拉黑": "white_rm", "移除白名单": "white_rm", "删白": "white_rm",
    "白名单": "white_list", "查白名单": "white_list",
    "尺度": "scale", "安全尺度": "scale", "scale": "scale",
    "敏感词": "sensitive", "屏蔽词": "sensitive",
    "戳": "poke", "戳一戳": "poke", "poke": "poke",
    "骰子": "dice", "dice": "dice", "扔骰子": "dice",
    "猜拳": "rps", "rps": "rps", "石头剪刀布": "rps",
    "音乐": "music_card", "点歌": "music_card", "音乐卡片": "music_card",
    "json": "json_card", "卡片": "json_card",
    "行为": "behavior", "模式": "behavior", "冷漠": "behavior_cold",
    "安静": "behavior_quiet", "活跃": "behavior_active",
    "插件": "plugins", "plugins": "plugins",
    "群信息": "group_info",
    "cookie": "cookie", "cookies": "cookie",
    "说": "say", "say": "say",
    "debug": "debug", "调试": "debug",
    "定海神针": "clear_screen", "刷屏": "clear_screen", "清屏": "clear_screen",
    "学习表情包": "learn_sticker", "表情包": "sticker_status", "扫描表情包": "scan_sticker",
    "忽略用户": "ignore_user", "别理他": "ignore_user", "不理他": "ignore_user",
    "恢复用户": "unignore_user", "取消忽略": "unignore_user", "解除忽略": "unignore_user",
    "忽略列表": "ignore_list",
    "高风险确认": "high_risk_confirm", "风险确认": "high_risk_confirm", "二次确认": "high_risk_confirm",
    "免确认": "high_risk_confirm", "不用确认": "high_risk_confirm", "直接执行": "high_risk_confirm",
    "更新": "update", "升级": "update", "update": "update", "upgrade": "update", "远程更新": "update",
}


class AdminEngine:
    _TOP = {
        "/help": "help",
        "/plugins": "plugins",
        "/ping": "ping",
        "/health": "ping",
        "/yukibot": "reload",
        "/yukiko": "reload",
    }

    _SUB = {
        "help": "help",
        "帮助": "help",
        "help_detail": "help_detail",
        "详细": "help_detail",
        "detail": "help_detail",
        "plugins": "plugins",
        "reload": "reload",
        "重载": "reload",
        "ping": "ping",
        "status": "status",
        "状态": "status",
        "say": "say",
        "说": "say",
        "加白": "white_add",
        "加白本群": "white_add",
        "拉黑": "white_rm",
        "拉黑本群": "white_rm",
        "白名单": "white_list",
        "whitelist": "white",
        "群信息": "group_info",
        "debug": "debug",
        "update": "update",
        "升级": "update",
        "更新": "update",
        "upgrade": "update",
        "远程更新": "update",
        "cookie": "cookie",
        "cookies": "cookie",
        "尺度": "scale",
        "scale": "scale",
        "敏感词": "sensitive",
        "sensitive": "sensitive",
        "戳": "poke",
        "poke": "poke",
        "骰子": "dice",
        "dice": "dice",
        "猜拳": "rps",
        "rps": "rps",
        "音乐卡片": "music_card",
        "music": "music_card",
        "json": "json_card",
        "jsoncard": "json_card",
        "行为": "behavior",
        "冷漠": "behavior_cold",
        "安静": "behavior_quiet",
        "活跃": "behavior_active",
        "定海神针": "clear_screen",
        "刷屏": "clear_screen",
        "学习表情包": "learn_sticker",
        "表情包": "sticker_status",
        "表情包状态": "sticker_status",
        "扫描表情包": "scan_sticker",
        "忽略用户": "ignore_user",
        "ignore": "ignore_user",
        "恢复用户": "unignore_user",
        "解除忽略": "unignore_user",
        "unignore": "unignore_user",
        "忽略列表": "ignore_list",
        "ignored": "ignore_list",
        "高风险确认": "high_risk_confirm",
        "风险确认": "high_risk_confirm",
        "二次确认": "high_risk_confirm",
        "high_risk_confirm": "high_risk_confirm",
        "risk_confirm": "high_risk_confirm",
    }

    _PUBLIC = {"help", "help_detail", "plugins"}
    _GROUP_ADMIN_ACTIONS = {"ignore_user", "unignore_user", "ignore_list", "high_risk_confirm"}

    def __init__(self, config: dict[str, Any], storage_dir: Path):
        self.config = config if isinstance(config, dict) else {}
        self.storage_dir = Path(storage_dir)
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        self._started = time.time()
        self._count = 0
        self._update_lock = asyncio.Lock()

        admin_cfg = self.config.get("admin", {}) if isinstance(self.config, dict) else {}
        if not isinstance(admin_cfg, dict):
            admin_cfg = {}
        self._enabled = bool(admin_cfg.get("enable", True))
        # Some QQ clients will show "发送者版本过低" for JSON card messages.
        # Keep help as plain text by default; allow opt-in via config.
        self._help_json_card = bool(admin_cfg.get("help_json_card", False))
        self.non_whitelist_mode = normalize_text(str(admin_cfg.get("non_whitelist_mode", "silent"))).lower() or "silent"
        self._super_users = {str(x).strip() for x in (admin_cfg.get("super_users", []) or []) if str(x).strip()}
        # 兼容 setup / webui 使用的单值字段：admin.super_admin_qq
        super_admin_qq = normalize_text(str(admin_cfg.get("super_admin_qq", "")))
        if super_admin_qq:
            self._super_users.add(super_admin_qq)

        self._white_path = self.storage_dir / "whitelist_groups.json"
        self._white: set[int] = set()
        for x in admin_cfg.get("whitelist_groups", []) or []:
            try:
                self._white.add(int(x))
            except Exception:
                pass
        self._load_white()

        self._ignore_path = self.storage_dir / "ignored_users.json"
        self._ignored_global: set[str] = set()
        self._ignored_group: dict[int, set[str]] = {}
        self._ignore_policy = normalize_text(str(admin_cfg.get("ignore_policy", "silent"))).lower() or "silent"
        if self._ignore_policy not in {"silent", "soft", "ai_review"}:
            self._ignore_policy = "silent"
        self._load_ignore()

        self._policy_path = self.storage_dir / "runtime_policies.json"
        self._high_risk_confirm_global: bool | None = None
        self._high_risk_confirm_group: dict[int, bool] = {}
        self._load_runtime_policy()

    @property
    def enabled(self) -> bool:
        return self._enabled

    def is_admin_command(self, text: str) -> bool:
        t = normalize_text(text)
        if not t:
            return False
        head = t.split(maxsplit=1)[0].lower()
        return head in self._TOP or head in {"/yuki", "/yuki帮助"}

    def is_super_admin(self, user_id: str) -> bool:
        if not self.enabled:
            return True
        if not self._super_users:
            return True
        return str(user_id).strip() in self._super_users

    def is_group_admin_user(self, user_id: str, group_id: int = 0, sender_role: str = "") -> bool:
        if self.is_super_admin(user_id):
            return True
        role = normalize_text(sender_role).lower()
        gid = int(group_id or 0)
        if gid <= 0 or role not in {"owner", "admin"}:
            return False
        return self.is_group_whitelisted(gid)

    def is_group_whitelisted(self, group_id: int | str) -> bool:
        if not self.enabled:
            return True
        try:
            gid = int(group_id)
        except Exception:
            return False
        if gid <= 0:
            return False
        if not self._white:
            return False
        return gid in self._white

    @property
    def ignore_policy(self) -> str:
        return self._ignore_policy

    def is_user_ignored(self, user_id: str, group_id: int = 0) -> bool:
        uid = normalize_text(str(user_id))
        if not uid:
            return False
        if uid in self._ignored_global:
            return True
        if int(group_id or 0) > 0 and uid in self._ignored_group.get(int(group_id), set()):
            return True
        return False

    def list_ignored_users(self, group_id: int = 0) -> dict[str, Any]:
        gid = int(group_id or 0)
        group_items = sorted(self._ignored_group.get(gid, set())) if gid > 0 else []
        global_items = sorted(self._ignored_global)
        return {
            "policy": self._ignore_policy,
            "group_id": gid,
            "group_users": group_items,
            "global_users": global_items,
            "group_count": len(group_items),
            "global_count": len(global_items),
        }

    def add_ignored_user(self, user_id: str, group_id: int = 0, scope: str = "group") -> tuple[bool, str]:
        uid = normalize_text(str(user_id))
        if not uid:
            return False, "目标用户不能为空"
        scope_value = normalize_text(scope).lower() or "group"
        gid = int(group_id or 0)
        if scope_value in {"global", "all", "全局"}:
            self._ignored_global.add(uid)
            self._save_ignore()
            return True, f"已忽略用户 {uid}（全局）"
        if gid <= 0:
            return False, "群聊内默认按本群忽略；私聊请使用全局 scope"
        bucket = self._ignored_group.setdefault(gid, set())
        bucket.add(uid)
        self._save_ignore()
        return True, f"已忽略用户 {uid}（本群 {gid}）"

    def remove_ignored_user(self, user_id: str, group_id: int = 0, scope: str = "group") -> tuple[bool, str]:
        uid = normalize_text(str(user_id))
        if not uid:
            return False, "目标用户不能为空"
        scope_value = normalize_text(scope).lower() or "group"
        gid = int(group_id or 0)
        changed = False
        if scope_value in {"global", "all", "全局"}:
            if uid in self._ignored_global:
                self._ignored_global.discard(uid)
                changed = True
        else:
            if gid > 0 and uid in self._ignored_group.get(gid, set()):
                self._ignored_group[gid].discard(uid)
                if not self._ignored_group[gid]:
                    self._ignored_group.pop(gid, None)
                changed = True
            elif uid in self._ignored_global:
                # 兜底：群聊解封时允许自动解除全局忽略，避免误操作后难恢复。
                self._ignored_global.discard(uid)
                changed = True
                scope_value = "global"
        if changed:
            self._save_ignore()
            if scope_value in {"global", "all", "全局"}:
                return True, f"已恢复用户 {uid}（全局）"
            return True, f"已恢复用户 {uid}（本群 {gid}）"
        return False, f"用户 {uid} 不在忽略列表"

    def increment_message_count(self) -> None:
        self._count += 1

    def get_high_risk_confirmation_policy(
        self,
        *,
        group_id: int = 0,
        default_required: bool = True,
    ) -> dict[str, Any]:
        gid = int(group_id or 0)
        if gid > 0 and gid in self._high_risk_confirm_group:
            required = bool(self._high_risk_confirm_group.get(gid, True))
            return {
                "high_risk_confirmation_required": required,
                "source": "group",
                "group_id": gid,
                "overridden": True,
            }
        if self._high_risk_confirm_global is not None:
            required = bool(self._high_risk_confirm_global)
            return {
                "high_risk_confirmation_required": required,
                "source": "global",
                "group_id": gid,
                "overridden": True,
            }
        return {
            "high_risk_confirmation_required": bool(default_required),
            "source": "default",
            "group_id": gid,
            "overridden": False,
        }

    def set_high_risk_confirmation_policy(
        self,
        *,
        required: bool | None,
        scope: str = "group",
        group_id: int = 0,
    ) -> tuple[bool, str, dict[str, Any]]:
        scope_value = normalize_text(scope).lower() or "group"
        gid = int(group_id or 0)
        payload: dict[str, Any] = {"scope": scope_value, "group_id": gid}
        if scope_value in {"global", "all", "全局"}:
            self._high_risk_confirm_global = None if required is None else bool(required)
            self._save_runtime_policy()
            if required is None:
                payload["high_risk_confirmation_required"] = None
                return True, "已恢复全局高风险确认默认策略", payload
            payload["high_risk_confirmation_required"] = bool(required)
            if required:
                return True, "已开启全局高风险二次确认", payload
            return True, "已关闭全局高风险二次确认", payload
        if gid <= 0:
            return False, "群聊内默认按本群设置；私聊请使用 global scope", payload
        if required is None:
            self._high_risk_confirm_group.pop(gid, None)
            self._save_runtime_policy()
            payload["high_risk_confirmation_required"] = None
            return True, f"已恢复本群 {gid} 的高风险确认默认策略", payload
        self._high_risk_confirm_group[gid] = bool(required)
        self._save_runtime_policy()
        payload["high_risk_confirmation_required"] = bool(required)
        if required:
            return True, f"已开启本群 {gid} 的高风险二次确认", payload
        return True, f"已关闭本群 {gid} 的高风险二次确认", payload

    async def handle_command(
        self,
        text: str,
        user_id: str,
        group_id: int,
        sender_role: str = "",
        engine: Any = None,
        api_call: Any = None,
    ) -> str | None:
        raw = normalize_text(text).strip()
        if not raw:
            return None

        parts = raw.split(maxsplit=2)
        first = parts[0].lower()

        if first in self._TOP:
            action = self._TOP[first]
            arg = parts[1].strip() if len(parts) > 1 else ""
            return await self._dispatch(action, arg, user_id, group_id, sender_role, engine, api_call)

        if first in {"/yuki", "/yuki帮助"}:
            sub = normalize_text(parts[1]).lower() if len(parts) > 1 else "help"
            if first == "/yuki帮助":
                sub = "help"
            action = self._SUB.get(sub)
            if not action:
                # 模糊匹配: 用户输错了命令
                action = self._fuzzy_match_command(sub)
            if not action:
                # 列出相似命令提示
                suggestions = self._suggest_commands(sub)
                if suggestions:
                    return f"未知命令「{sub}」，你是不是想用:\n" + "\n".join(f"  /yuki {s}" for s in suggestions)
                return "未知子命令 发送 /yuki help 查看用法"
            arg = parts[2].strip() if len(parts) > 2 else ""
            return await self._dispatch(action, arg, user_id, group_id, sender_role, engine, api_call)
        return None

    def _fuzzy_match_command(self, text: str) -> str | None:
        """模糊匹配命令名。"""
        t = text.strip().lower()
        if not t:
            return None
        # 先查全局模糊表
        if t in _FUZZY_COMMAND_MAP:
            return _FUZZY_COMMAND_MAP[t]
        # 前缀匹配
        for key, action in _FUZZY_COMMAND_MAP.items():
            if key.startswith(t) or t.startswith(key):
                return action
        # difflib 近似匹配
        candidates = list(_FUZZY_COMMAND_MAP.keys())
        matches = get_close_matches(t, candidates, n=1, cutoff=0.6)
        if matches:
            return _FUZZY_COMMAND_MAP[matches[0]]
        # 也查 _SUB 表
        sub_matches = get_close_matches(t, list(self._SUB.keys()), n=1, cutoff=0.6)
        if sub_matches:
            return self._SUB[sub_matches[0]]
        return None

    def _suggest_commands(self, text: str) -> list[str]:
        """返回最相似的命令建议。"""
        t = text.strip().lower()
        if not t:
            return []
        candidates = list(self._SUB.keys())
        matches = get_close_matches(t, candidates, n=3, cutoff=0.4)
        return matches

    async def _dispatch(
        self,
        action: str,
        arg: str,
        user_id: str,
        group_id: int,
        sender_role: str,
        engine: Any,
        api_call: Any,
    ) -> str | None:
        if action not in self._PUBLIC:
            if self.is_super_admin(user_id):
                pass
            elif action in self._GROUP_ADMIN_ACTIONS and self.is_group_admin_user(user_id, group_id, sender_role):
                pass
            else:
                if action in self._GROUP_ADMIN_ACTIONS:
                    return "权限不足，此指令需要本群管理员/群主或超级管理员权限"
                return "权限不足 仅超级管理员可使用此指令"
        handler = getattr(self, f"_act_{action}", None)
        if handler is None:
            return "命令暂未实现"
        return await handler(arg=arg, user_id=user_id, group_id=group_id, engine=engine, api_call=api_call)

    async def _act_help(self, **kwargs: Any) -> str:
        return (
            "YuKiKo 管理面板\n"
            "---------------------\n"
            "reload / ping / status / update\n"
            "加白 / 拉黑 / 白名单\n"
            "尺度 / 敏感词 / 行为\n"
            "戳 / 骰子 / 猜拳\n"
            "音乐卡片 / json / 定海神针\n"
            "表情包 / 学习表情包 / 扫描表情包\n"
            "忽略用户 / 恢复用户 / 忽略列表\n"
            "高风险确认\n"
            "---------------------\n"
            "/yuki help_detail 查看详细参数"
        )

    async def _act_help_detail(self, **kwargs: Any) -> str:
        return (
            "YuKiKo Command Reference\n"
            "---------------------\n"
            "System\n"
            "  /yuki reload\n"
            "  /yuki ping\n"
            "  /yuki status\n"
            "  /yuki update [check|run|restart]\n"
            "  /yuki plugins\n"
            "  /yuki debug <QQ>\n"
            "  /yuki cookie [platform] [browser] [force]\n"
            "---------------------\n"
            "Whitelist\n"
            "  /yuki 加白          add current group\n"
            "  /yuki 拉黑          remove current group\n"
            "  /yuki 白名单        list all groups\n"
            "  /yuki 忽略列表      list ignored users\n"
            "  /yuki 忽略用户 <QQ> [group|global]\n"
            "  /yuki 恢复用户 <QQ> [group|global]\n"
            "  /yuki 高风险确认 [on|off|default] [group|global]\n"
            "---------------------\n"
            "Safety & Behavior\n"
            "  /yuki 尺度 <0-3>    0=off 1=loose 2=standard 3=strict\n"
            "  /yuki 敏感词 添加 <word>\n"
            "  /yuki 敏感词 删除 <word>\n"
            "  /yuki 行为          show current params\n"
            "  /yuki 冷漠          only respond to @ and name\n"
            "  /yuki 安静          high threshold, rare reply\n"
            "  /yuki 活跃          active in group chat\n"
            "  /yuki 行为 默认     reset to default\n"
            "  /yuki 行为 接话门槛 <float>\n"
            "  /yuki 收藏表情      custom face status\n"
            "  /yuki 拉取收藏表情 [n]  fetch n custom faces (default 48)\n"
            "  /yuki 学习收藏表情 [n]  learn n custom faces via LLM\n"
            "  /yuki 高风险确认 off group   disable confirm for this group\n"
            "  /yuki 高风险确认 on group    enable confirm for this group\n"
            "  /yuki 高风险确认 default     reset current scope to default\n"
            "---------------------\n"
            "Interactive\n"
            "  /yuki 戳 <QQ>       poke user\n"
            "  /yuki 骰子          roll dice\n"
            "  /yuki 猜拳          rock-paper-scissors\n"
            "  /yuki say <text>    echo text\n"
            "---------------------\n"
            "Media\n"
            "  /yuki 音乐卡片 <keyword>\n"
            "  /yuki 音乐卡片 <platform> <id>\n"
            "  /yuki json <raw JSON string>\n"
            "---------------------\n"
            "Special\n"
            "  /yuki 定海神针 [lines] [segments] [delay_sec]\n"
            "    default: 3000 lines, 10 segments, 0.8s delay\n"
            "    range: lines 120-20000, segments 1-80, delay 0-30\n"
            "---------------------\n"
            "Sticker\n"
            "  /yuki 表情包          sticker system status\n"
            "  /yuki 扫描表情包      rescan QQ emoji cache\n"
            "  /yuki 学习表情包 [n]  learn n stickers via LLM (default 5)\n"
            "---------------------\n"
            "Update (Remote)\n"
            "  /yuki update check            check local/remote version\n"
            "  /yuki update run              pull latest + sync deps + build webui + auto hot-reload\n"
            "  /yuki update restart          run update and restart service\n"
            "  /yuki update run --no-webui   skip webui build\n"
            "  /yuki update run --no-python  skip pip install\n"
            "  /yuki update run --allow-dirty\n"
            "  /yuki update run --no-hot-reload\n"
            "---------------------\n"
            "Fuzzy match enabled - typos are auto-corrected"
        )

    async def _send_help_card(self, api_call: Any, group_id: int, user_id: str) -> bool:
        """尝试发送 JSON 卡片格式的帮助菜单。失败则回退纯文本。"""
        try:
            # 使用 structmsg news 格式 — 兼容性最好
            card = {
                "app": "com.tencent.structmsg",
                "desc": "YuKiKo 管理面板",
                "view": "news",
                "ver": "0.0.0.1",
                "prompt": "[YuKiKo] 管理面板",
                "meta": {
                    "news": {
                        "action": "",
                        "android_pkg_name": "",
                        "app_type": 1,
                        "appid": 100446242,
                        "desc": (
                            "⚙ reload / ping / status\n"
                            "🛡 加白 / 拉黑 / 白名单\n"
                            "🎛 尺度 / 敏感词 / 行为\n"
                            "🎮 戳 / 骰子 / 猜拳\n"
                            "🎵 音乐卡片 / json / 定海神针\n"
                            "💡 输错命令也没关系 AI会帮你猜"
                        ),
                        "jumpUrl": "",
                        "preview": "",
                        "source_icon": "",
                        "source_url": "",
                        "tag": "YuKiKo",
                        "title": "YuKiKo 管理面板",
                    }
                },
            }
            payload = [{"type": "json", "data": {"data": json.dumps(card, ensure_ascii=False)}}]
            if group_id:
                await api_call("send_group_msg", group_id=group_id, message=payload)
            else:
                await api_call("send_private_msg", user_id=int(user_id), message=payload)
            return True
        except Exception as exc:
            _log.debug("help_card_fail | %s", exc)
            return False

    async def _act_plugins(self, **kwargs: Any) -> str:
        engine = kwargs.get("engine")
        if engine is None:
            return "引擎未就绪"
        schemas = getattr(getattr(engine, "plugins", None), "schemas", None)
        if not isinstance(schemas, list):
            return "插件系统未就绪"
        if not schemas:
            return "当前没有已加载插件"
        lines = [f"已加载插件 共 {len(schemas)} 个"]
        for item in schemas:
            if not isinstance(item, dict):
                continue
            lines.append(f"- {normalize_text(str(item.get('name', 'unknown')))}: {normalize_text(str(item.get('description', '')))}")
        return "\n".join(lines)

    async def _act_ping(self, **_: Any) -> str:
        return "pong"

    async def _act_status(self, **kwargs: Any) -> str:
        engine = kwargs.get("engine")
        uptime = int(time.time() - self._started)
        hours = uptime // 3600
        minutes = (uptime % 3600) // 60
        uptime_str = f"{hours}h{minutes}m" if hours else f"{minutes}m{uptime % 60}s"

        lines = [
            "YuKiKo 运行状态",
            "---------------------",
            f"  运行时长  {uptime_str}",
            f"  消息计数  {self._count}",
            f"  白名单群  {len(self._white)} 个",
            f"  忽略用户  群内{sum(len(x) for x in self._ignored_group.values())} / 全局{len(self._ignored_global)}",
        ]
        if engine is not None:
            if getattr(engine, "safety", None) is not None:
                scale = int(getattr(engine.safety, "scale", 2))
                names = getattr(engine.safety, "SCALE_NAMES", {})
                profile = normalize_text(str(getattr(engine.safety, "profile", "")))
                profile_names = getattr(engine.safety, "PROFILE_NAMES", {})
                profile_label = profile_names.get(profile, profile or "?")
                lines.append(f"  安全尺度  {scale} ({names.get(scale, '?')}) | 档位 {profile_label}")
            if getattr(engine, "agent", None) is not None:
                agent_enable = getattr(engine.agent, "enable", False)
                lines.append(f"  Agent模式 {'开启' if agent_enable else '关闭'}")
            if getattr(engine, "model_client", None) is not None:
                provider = getattr(engine.model_client, "provider", "?")
                model = getattr(engine.model_client, "model", "?")
                lines.append(f"  AI模型    {provider}/{model}")
            if getattr(engine, "agent_tool_registry", None) is not None:
                tool_count = getattr(engine.agent_tool_registry, "tool_count", 0)
                lines.append(f"  可用工具  {tool_count} 个")
            plugin_map = getattr(getattr(engine, "plugins", None), "plugins", {})
            if isinstance(plugin_map, dict):
                abnormal_rows: list[str] = []
                for pname, pobj in plugin_map.items():
                    status_fn = getattr(pobj, "status_text", None)
                    if not callable(status_fn):
                        continue
                    try:
                        status_raw = normalize_text(str(status_fn()))
                    except Exception:
                        continue
                    if not status_raw:
                        continue
                    rows = [normalize_text(x) for x in status_raw.splitlines() if normalize_text(x)]
                    if not rows:
                        continue
                    lines.append(f"  插件状态  {pname}")
                    for row in rows[:5]:
                        lines.append(f"    {row}")
                    lower_blob = "\n".join(rows[:5]).lower()
                    if any(
                        key in lower_blob
                        for key in (
                            " fail",
                            "异常",
                            "error",
                            "not_logged_in",
                            "invalid_",
                            "timeout",
                            "unavailable",
                            "disabled",
                        )
                    ):
                        abnormal_rows.append(f"{pname}: {rows[0]}")
                if abnormal_rows:
                    lines.append("  插件异常汇总")
                    for row in abnormal_rows:
                        lines.append(f"    {row}")
        return "\n".join(lines)

    async def _act_say(self, **kwargs: Any) -> str:
        return normalize_text(str(kwargs.get("arg", "")))

    async def _act_reload(self, **kwargs: Any) -> str:
        engine = kwargs.get("engine")
        if engine is None:
            return "引擎未就绪"
        ok, msg = engine.reload_config()
        return f"重载{'成功' if ok else '失败'}: {msg}"

    async def _act_update(self, **kwargs: Any) -> str:
        arg = normalize_text(str(kwargs.get("arg", "")))
        tokens = [x for x in arg.split() if x]
        if not tokens:
            return (
                "用法:\n"
                "/yuki update check\n"
                "/yuki update run\n"
                "/yuki update restart\n"
                "/yuki update run --no-webui --no-python --allow-dirty\n"
                "/yuki update run --no-hot-reload\n"
            )

        first = normalize_text(tokens[0]).lower()
        check_alias = {"check", "status", "查看", "检查"}
        run_alias = {"run", "pull", "升级", "更新", "执行", "update", "upgrade"}
        restart_alias = {"restart", "重启"}
        force_alias = {"force", "强制"}

        cmd_args = ["update"]
        passthrough: list[str] = []
        check_only = False
        restart_mode = False
        allow_dirty = False

        for raw in tokens:
            t = normalize_text(raw).lower()
            if t in check_alias:
                check_only = True
                continue
            if t in run_alias:
                continue
            if t in restart_alias:
                restart_mode = True
                continue
            if t in force_alias:
                allow_dirty = True
                continue
            passthrough.append(raw)

        if first in check_alias:
            check_only = True
        elif first in restart_alias:
            restart_mode = True
        elif first not in run_alias and first.startswith("--"):
            # 允许直接传 flags：/yuki update --check-only ...
            pass

        if check_only:
            cmd_args.append("--check-only")
        if restart_mode:
            cmd_args.append("--restart")
        if allow_dirty:
            cmd_args.append("--allow-dirty")
        cmd_args.extend(passthrough)

        if self._update_lock.locked():
            return "已有远程更新任务在执行中，请稍后再试。"

        async with self._update_lock:
            timeout_sec = 120 if check_only else 1800
            ok, output = await self._run_manager_command(cmd_args, timeout_sec=timeout_sec)
            output = self._clip_command_output(output)
            title = "远程更新检查完成" if check_only else ("远程更新成功" if ok else "远程更新失败")
            if not output:
                return title
            return f"{title}\n{output}"

    async def _run_manager_command(self, args: list[str], timeout_sec: int = 600) -> tuple[bool, str]:
        root_dir = Path(__file__).resolve().parents[1]
        script = root_dir / "scripts" / "yukiko_manager.sh"
        if not script.exists():
            return False, f"管理脚本不存在: {script}"

        cmd = ["bash", "scripts/yukiko_manager.sh", *args]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(root_dir),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        except Exception as exc:
            return False, f"启动更新进程失败: {str(exc)[:160]}"

        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout_sec)
        except asyncio.TimeoutError:
            proc.kill()
            try:
                await proc.wait()
            except Exception:
                pass
            return False, f"更新命令超时（>{timeout_sec}s）"

        text = ""
        if stdout:
            text = stdout.decode("utf-8", errors="ignore").replace("\r\n", "\n").replace("\r", "\n").strip()
        return proc.returncode == 0, text

    @staticmethod
    def _clip_command_output(text: str, *, max_lines: int = 80, max_chars: int = 3800) -> str:
        raw = (text or "").replace("\r\n", "\n").replace("\r", "\n")
        if not raw.strip():
            return ""
        rows = [line.rstrip() for line in raw.splitlines()]
        if len(rows) > max_lines:
            rows = rows[:max_lines] + ["...(output truncated)"]
        out = "\n".join(rows)
        if len(out) > max_chars:
            out = out[:max_chars] + "\n...(output truncated)"
        return out

    async def _act_white(self, **kwargs: Any) -> str:
        sub = normalize_text(str(kwargs.get("arg", ""))).lower()
        if sub in {"add", "+", "加", "加白"}:
            return await self._act_white_add(**kwargs)
        if sub in {"rm", "remove", "del", "-", "拉黑", "删"}:
            return await self._act_white_rm(**kwargs)
        return await self._act_white_list(**kwargs)

    async def _act_white_add(self, **kwargs: Any) -> str:
        gid = int(kwargs.get("group_id", 0) or 0)
        if not gid:
            return "仅可在群聊内执行"
        self._white.add(gid)
        self._save_white()
        return f"已加白本群 {gid}"

    async def _act_white_rm(self, **kwargs: Any) -> str:
        gid = int(kwargs.get("group_id", 0) or 0)
        if not gid:
            return "仅可在群聊内执行"
        self._white.discard(gid)
        self._save_white()
        return f"已从白名单移除本群 {gid}"

    async def _act_white_list(self, **_: Any) -> str:
        if not self._white:
            return "白名单为空"
        return "白名单群\n" + "\n".join(f"- {x}" for x in sorted(self._white))

    @staticmethod
    def _parse_ignore_action_arg(arg: str, *, default_scope: str = "group") -> tuple[str, str]:
        raw = normalize_text(arg)
        if not raw:
            return "", default_scope
        parts = [normalize_text(item) for item in raw.split() if normalize_text(item)]
        if not parts:
            return "", default_scope
        user_id = parts[0]
        scope = default_scope
        for token in parts[1:]:
            lowered = token.lower()
            if lowered in {"global", "all", "全局"}:
                scope = "global"
                break
            if lowered in {"group", "本群"}:
                scope = "group"
                break
        return user_id, scope

    async def _act_ignore_user(self, **kwargs: Any) -> str:
        gid = int(kwargs.get("group_id", 0) or 0)
        user_id = normalize_text(str(kwargs.get("user_id", "")))
        arg = normalize_text(str(kwargs.get("arg", "")))
        default_scope = "group" if gid > 0 else "global"
        target_user_id, scope = self._parse_ignore_action_arg(arg, default_scope=default_scope)
        if not target_user_id:
            return "用法: /yuki 忽略用户 <QQ号> [group|global]"
        if scope == "global" and not self.is_super_admin(user_id):
            return "权限不足，只有超级管理员可以设置全局忽略"
        ok, message = self.add_ignored_user(target_user_id, group_id=gid, scope=scope)
        return message if ok else f"忽略失败: {message}"

    async def _act_unignore_user(self, **kwargs: Any) -> str:
        gid = int(kwargs.get("group_id", 0) or 0)
        user_id = normalize_text(str(kwargs.get("user_id", "")))
        arg = normalize_text(str(kwargs.get("arg", "")))
        default_scope = "group" if gid > 0 else "global"
        target_user_id, scope = self._parse_ignore_action_arg(arg, default_scope=default_scope)
        if not target_user_id:
            return "用法: /yuki 恢复用户 <QQ号> [group|global]"
        if scope == "global" and not self.is_super_admin(user_id):
            return "权限不足，只有超级管理员可以解除全局忽略"
        ok, message = self.remove_ignored_user(target_user_id, group_id=gid, scope=scope)
        return message if ok else f"恢复失败: {message}"

    async def _act_ignore_list(self, **kwargs: Any) -> str:
        gid = int(kwargs.get("group_id", 0) or 0)
        info = self.list_ignored_users(group_id=gid)
        rows = [
            f"忽略策略: {info.get('policy', 'silent')}",
            f"全局忽略({info.get('global_count', 0)}):",
        ]
        global_users = info.get("global_users", [])
        if isinstance(global_users, list) and global_users:
            rows.extend([f"- {item}" for item in global_users[:60]])
        else:
            rows.append("- (空)")
        if gid > 0:
            rows.append(f"本群忽略({info.get('group_count', 0)}):")
            group_users = info.get("group_users", [])
            if isinstance(group_users, list) and group_users:
                rows.extend([f"- {item}" for item in group_users[:60]])
            else:
                rows.append("- (空)")
        return "\n".join(rows)

    @staticmethod
    def _parse_high_risk_confirm_arg(arg: str, *, default_scope: str = "group") -> tuple[bool | None, str]:
        content = normalize_text(arg).lower()
        if not content:
            return None, default_scope
        parts = [item for item in content.split() if item]
        required: bool | None = None
        scope = default_scope
        for token in parts:
            if token in {"on", "enable", "enabled", "true", "1", "需要确认", "开启", "打开", "恢复确认"}:
                required = True
                continue
            if token in {"off", "disable", "disabled", "false", "0", "免确认", "不用确认", "不需要确认", "关闭", "直接执行"}:
                required = False
                continue
            if token in {"default", "reset", "恢复默认", "默认"}:
                required = None
                continue
            if token in {"global", "all", "全局"}:
                scope = "global"
                continue
            if token in {"group", "本群"}:
                scope = "group"
                continue
        return required, scope

    async def _act_high_risk_confirm(self, **kwargs: Any) -> str:
        gid = int(kwargs.get("group_id", 0) or 0)
        user_id = normalize_text(str(kwargs.get("user_id", "")))
        arg = normalize_text(str(kwargs.get("arg", "")))
        default_scope = "group" if gid > 0 else "global"
        if not arg:
            policy = self.get_high_risk_confirmation_policy(group_id=gid)
            required = bool(policy.get("high_risk_confirmation_required", True))
            source = normalize_text(str(policy.get("source", "default"))) or "default"
            state = "开启" if required else "关闭"
            if source == "group" and gid > 0:
                return f"当前本群高风险二次确认：{state}"
            if source == "global":
                return f"当前全局高风险二次确认：{state}"
            return f"当前高风险二次确认沿用默认策略：{'开启' if required else '关闭'}"
        required, scope = self._parse_high_risk_confirm_arg(arg, default_scope=default_scope)
        if scope == "global" and not self.is_super_admin(user_id):
            return "权限不足，只有超级管理员可以修改全局高风险确认策略"
        ok, message, _payload = self.set_high_risk_confirmation_policy(
            required=required,
            scope=scope,
            group_id=gid,
        )
        return message if ok else f"设置失败: {message}"

    async def _act_group_info(self, **kwargs: Any) -> str:
        gid = int(kwargs.get("group_id", 0) or 0)
        api_call = kwargs.get("api_call")
        if not gid or not api_call:
            return "仅可在群聊内执行"
        try:
            data = await api_call("get_group_info", group_id=gid, no_cache=True)
            return f"群信息\n- ID: {gid}\n- 名称: {normalize_text(str((data or {}).get('group_name', '未知')))}\n- 人数: {int((data or {}).get('member_count', 0) or 0)}"
        except Exception as exc:
            return f"获取群信息失败: {str(exc)[:80]}"

    async def _act_debug(self, **kwargs: Any) -> str:
        engine = kwargs.get("engine")
        arg = normalize_text(str(kwargs.get("arg", "")))
        if engine is None:
            return "引擎未就绪"
        if not arg:
            return "用法: /yuki debug <QQ号>"
        memory = getattr(engine, "memory", None)
        if memory is None:
            return "记忆系统未就绪"
        try:
            out = memory.get_user_profile_summary(arg)
            return out or "暂无画像数据"
        except Exception as exc:
            return f"读取画像失败: {str(exc)[:80]}"

    async def _act_cookie(self, **kwargs: Any) -> str:
        engine = kwargs.get("engine")
        arg = normalize_text(str(kwargs.get("arg", "")))
        if engine is None:
            return "引擎未就绪"

        platform = "all"
        browser = "edge"
        force = False
        parts = [x for x in arg.split() if x]
        if parts:
            platform = parts[0].lower()
        if len(parts) >= 2:
            browser = parts[1].lower()
        if len(parts) >= 3:
            force = parts[2].lower() in {"1", "true", "yes", "y", "force"}

        from core.cookie_auth import extract_bilibili_cookies, extract_douyin_cookie, extract_kuaishou_cookie

        ok: list[str] = []
        fail: list[str] = []

        async def refresh_one(name: str) -> None:
            try:
                if name == "bilibili":
                    data = await asyncio.to_thread(extract_bilibili_cookies, browser, force)
                    sess = normalize_text(str((data or {}).get("SESSDATA", "")))
                    jct = normalize_text(str((data or {}).get("bili_jct", "")))
                    if sess:
                        tools = getattr(engine, "tools", None)
                        if tools is not None:
                            setattr(tools, "_bilibili_sessdata", sess)
                            setattr(tools, "_bilibili_jct", jct)
                            setattr(tools, "_bilibili_cookie", f"SESSDATA={sess}; bili_jct={jct}" if jct else f"SESSDATA={sess}")
                        ok.append("bilibili")
                    else:
                        fail.append("bilibili(empty)")
                elif name == "douyin":
                    cookie = await asyncio.to_thread(extract_douyin_cookie, browser, force)
                    if cookie:
                        tools = getattr(engine, "tools", None)
                        if tools is not None:
                            setattr(tools, "_douyin_cookie", normalize_text(str(cookie)))
                        ok.append("douyin")
                    else:
                        fail.append("douyin(empty)")
                elif name == "kuaishou":
                    cookie = await asyncio.to_thread(extract_kuaishou_cookie, browser, force)
                    if cookie:
                        tools = getattr(engine, "tools", None)
                        if tools is not None:
                            setattr(tools, "_kuaishou_cookie", normalize_text(str(cookie)))
                        ok.append("kuaishou")
                    else:
                        fail.append("kuaishou(empty)")
            except Exception as exc:
                fail.append(f"{name}({str(exc)[:40]})")

        targets = ["bilibili", "douyin", "kuaishou"] if platform in {"all", "*"} else [platform]
        for one in targets:
            await refresh_one(one)
        return f"cookie 刷新完成 成功 {ok or '无'} 失败 {fail or '无'}"

    async def _act_scale(self, **kwargs: Any) -> str:
        engine = kwargs.get("engine")
        arg = normalize_text(str(kwargs.get("arg", "")))
        if engine is None or getattr(engine, "safety", None) is None:
            return "安全系统未就绪"
        if not arg:
            level = int(getattr(engine.safety, "scale", 2))
            names = getattr(engine.safety, "SCALE_NAMES", {})
            profile = normalize_text(str(getattr(engine.safety, "profile", "")))
            profile_names = getattr(engine.safety, "PROFILE_NAMES", {})
            return f"当前尺度 {level} ({names.get(level, 'unknown')})，档位 {profile_names.get(profile, profile or 'unknown')}"
        try:
            level = int(arg)
        except Exception:
            setter = getattr(engine.safety, "set_profile", None)
            if callable(setter):
                result = setter(arg)
                if "无效档位" not in normalize_text(str(result)):
                    return str(result)
            return "用法: /yuki 尺度 <0-3|保守|一般|开放|很开放>"
        return engine.safety.set_scale(level)

    async def _act_sensitive(self, **kwargs: Any) -> str:
        engine = kwargs.get("engine")
        arg = normalize_text(str(kwargs.get("arg", "")))
        if engine is None or getattr(engine, "safety", None) is None:
            return "安全系统未就绪"
        args = [x for x in arg.split() if x]
        if not args:
            rows = engine.safety.list_output_words()
            if not rows:
                return "输出敏感词为空"
            # 只返回数量，不暴露具体词汇
            return f"当前共 {len(rows)} 条输出敏感词（防封群用，具体内容不对外展示）"
        cmd = args[0].lower()
        if cmd in {"添加", "add"} and len(args) >= 2:
            word = args[1]
            repl = args[2] if len(args) >= 3 else "**"
            engine.safety.add_output_word(word, repl)
            return f"已添加敏感词 {word} -> {repl}"
        if cmd in {"删除", "del", "remove"} and len(args) >= 2:
            ok = engine.safety.remove_output_word(args[1])
            return f"已删除敏感词 {args[1]}" if ok else f"未找到敏感词 {args[1]}"
        return "用法: /yuki 敏感词 [添加|删除] ..."

    async def _act_poke(self, **kwargs: Any) -> str | None:
        api_call = kwargs.get("api_call")
        gid = int(kwargs.get("group_id", 0) or 0)
        uid = str(kwargs.get("user_id", ""))
        arg = normalize_text(str(kwargs.get("arg", "")))
        if not api_call:
            return "API 不可用"
        target = arg or uid
        try:
            if gid:
                try:
                    await api_call("group_poke", group_id=gid, user_id=int(target))
                except Exception:
                    await api_call("send_group_msg", group_id=gid, message=[{"type": "poke", "data": {"type": "1", "id": "-1"}}])
            else:
                await api_call("send_private_msg", user_id=int(target), message=[{"type": "poke", "data": {"type": "1", "id": "-1"}}])
            return None
        except Exception as exc:
            return f"戳一戳失败: {str(exc)[:80]}"

    async def _act_dice(self, **kwargs: Any) -> str | None:
        return await self._send_segment(kwargs, "dice", {})

    async def _act_rps(self, **kwargs: Any) -> str | None:
        return await self._send_segment(kwargs, "rps", {})

    async def _act_music_card(self, **kwargs: Any) -> str | None:
        api_call = kwargs.get("api_call")
        gid = int(kwargs.get("group_id", 0) or 0)
        uid = str(kwargs.get("user_id", ""))
        engine = kwargs.get("engine")
        arg = normalize_text(str(kwargs.get("arg", "")))
        if not api_call:
            return "API 不可用"
        if not arg:
            return "用法: /yuki 音乐卡片 <歌名> 或 <平台> <数字ID>"
        parts = arg.split(maxsplit=1)
        if len(parts) == 2 and parts[0].lower() in {"qq", "163", "kugou", "kuwo", "migu"} and parts[1].isdigit():
            return await self._send_segment(kwargs, "music", {"type": parts[0].lower(), "id": parts[1]})
        tools = getattr(engine, "tools", None) if engine is not None else None
        me = getattr(tools, "_music_engine", None) if tools is not None else None
        if me is None:
            return "音乐引擎未就绪"
        try:
            rows = await me.search(arg, limit=6)
        except Exception as exc:
            return f"搜索失败: {str(exc)[:80]}"
        if not rows:
            return f"没找到「{arg}」相关歌曲"
        song = rows[0]
        data = {
            "type": "custom",
            "url": f"https://music.163.com/#/song?id={song.song_id}",
            "audio": f"https://music.163.com/song/media/outer/url?id={song.song_id}.mp3",
            "title": song.name or arg,
            "content": song.artist or "未知艺人",
            "image": "https://p2.music.126.net/6y-UleORITEDbvrOLV0Q8A==/5639395138885805.jpg",
        }
        msg = [{"type": "music", "data": data}]
        try:
            if gid:
                await api_call("send_group_msg", group_id=gid, message=msg)
            else:
                await api_call("send_private_msg", user_id=int(uid), message=msg)
            return None
        except Exception as exc:
            return f"音乐卡片发送失败: {str(exc)[:80]}"

    async def _act_clear_screen(self, **kwargs: Any) -> str | None:
        """定海神针 — 分段发送大量空行刷屏，末尾附 AI 语录。"""
        arg = str(kwargs.get("arg", "")).strip()
        api_call = kwargs.get("api_call")
        group_id = int(kwargs.get("group_id", 0) or 0)
        user_id = str(kwargs.get("user_id", ""))
        engine = kwargs.get("engine")

        total_lines = 3000
        segment_count = 10
        delay_seconds = 0.8

        parts = [p for p in arg.split() if p.strip()]
        if len(parts) >= 1:
            try: total_lines = int(parts[0])
            except ValueError: pass
        if len(parts) >= 2:
            try: segment_count = int(parts[1])
            except ValueError: pass
        if len(parts) >= 3:
            try: delay_seconds = float(parts[2])
            except ValueError: pass

        total_lines = max(120, min(20000, total_lines))
        segment_count = max(1, min(80, segment_count))
        delay_seconds = max(0.0, min(30.0, delay_seconds))

        # AI 语录
        quote = await self._random_ai_quote(engine)

        if not api_call:
            lines = ["　" for _ in range(total_lines)]
            lines.append(f"「{quote}」")
            return "\n".join(lines)

        base = total_lines // segment_count
        rem = total_lines % segment_count

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
            except Exception as exc:
                return f"定海神针发送失败（第 {idx + 1}/{segment_count} 段）：{exc}"
            if idx < segment_count - 1 and delay_seconds > 0:
                await asyncio.sleep(delay_seconds)
        return None

    async def _random_ai_quote(self, engine: Any = None) -> str:
        """随机语录：优先 AI 生成，失败则本地兜底。"""
        import random
        local_pool = [
            "潮落归海，言尽于此。",
            "山高水长，后会有期。",
            "风起于青萍之末。",
            "浮生若梦，为欢几何。",
            "天地一逆旅，同悲万古尘。",
            "人生到处知何似，应似飞鸿踏雪泥。",
            "此去经年，应是良辰好景虚设。",
        ]
        if engine and hasattr(engine, "model_client"):
            try:
                client = engine.model_client
                resp = await asyncio.wait_for(
                    client.chat_text([{"role": "user", "content": "用一句古风短句作为定海神针的结尾语录，15字以内，只输出语录本身"}]),
                    timeout=5,
                )
                text = (resp or "").strip().strip("\"'「」""")
                if 2 < len(text) < 30:
                    return text
            except Exception:
                pass
        return random.choice(local_pool)

    async def _act_json_card(self, **kwargs: Any) -> str | None:
        arg = normalize_text(str(kwargs.get("arg", "")))
        if not arg:
            return "用法: /yuki json <JSON字符串>"
        try:
            json.loads(arg)
        except json.JSONDecodeError:
            return "JSON 格式错误"
        return await self._send_segment(kwargs, "json", {"data": arg})

    async def _act_behavior(self, **kwargs: Any) -> str:
        engine = kwargs.get("engine")
        arg = normalize_text(str(kwargs.get("arg", ""))).lower()
        if engine is None or not isinstance(getattr(engine, "config", None), dict):
            return "行为系统未就绪"
        if not arg:
            trigger_cfg = engine.config.get("trigger", {}) if isinstance(engine.config.get("trigger"), dict) else {}
            routing_cfg = engine.config.get("routing", {}) if isinstance(engine.config.get("routing"), dict) else {}
            return (
                "行为参数\n"
                f"- ai_listen_enable: {trigger_cfg.get('ai_listen_enable', True)}\n"
                f"- ai_listen_min_messages: {trigger_cfg.get('ai_listen_min_messages', 8)}\n"
                f"- ai_listen_min_score: {trigger_cfg.get('ai_listen_min_score', 2.2)}\n"
                f"- min_confidence: {routing_cfg.get('min_confidence', 0.58)}\n"
                f"- followup_min_confidence: {routing_cfg.get('followup_min_confidence', 0.75)}"
            )
        if arg in {"默认", "default"}:
            return self._set_behavior_mode(engine, "default")
        if arg in {"冷漠", "cold"}:
            return self._set_behavior_mode(engine, "cold")
        if arg in {"安静", "quiet"}:
            return self._set_behavior_mode(engine, "quiet")
        if arg in {"活跃", "active"}:
            return self._set_behavior_mode(engine, "active")

        # ── 单参数精细调整: /yuki 行为 <参数名> <值> ──
        _PARAM_MAP: dict[str, tuple[str, str]] = {
            # 中文别名 -> (config section, key)
            "接话门槛": ("routing", "min_confidence"),
            "min_confidence": ("routing", "min_confidence"),
            "追问门槛": ("routing", "followup_min_confidence"),
            "followup_min_confidence": ("routing", "followup_min_confidence"),
            "旁听门槛": ("routing", "non_directed_min_confidence"),
            "non_directed_min_confidence": ("routing", "non_directed_min_confidence"),
            "ai门槛": ("routing", "ai_gate_min_confidence"),
            "ai_gate_min_confidence": ("routing", "ai_gate_min_confidence"),
            "旁听开关": ("trigger", "ai_listen_enable"),
            "ai_listen_enable": ("trigger", "ai_listen_enable"),
            "旁听消息数": ("trigger", "ai_listen_min_messages"),
            "ai_listen_min_messages": ("trigger", "ai_listen_min_messages"),
            "旁听分数": ("trigger", "ai_listen_min_score"),
            "ai_listen_min_score": ("trigger", "ai_listen_min_score"),
            "追问窗口": ("trigger", "followup_reply_window_seconds"),
            "followup_reply_window_seconds": ("trigger", "followup_reply_window_seconds"),
            "追问轮数": ("trigger", "followup_max_turns"),
            "followup_max_turns": ("trigger", "followup_max_turns"),
        }
        parts = arg.split(None, 1)
        if len(parts) == 2 and parts[0] in _PARAM_MAP:
            section, key = _PARAM_MAP[parts[0]]
            raw_val = parts[1]
            cfg = engine.config.setdefault(section, {}) if isinstance(engine.config, dict) else {}
            if not isinstance(cfg, dict):
                cfg = {}
                engine.config[section] = cfg
            # 类型推断
            if raw_val in {"true", "True", "1", "开", "on"}:
                cfg[key] = True
            elif raw_val in {"false", "False", "0", "关", "off"}:
                cfg[key] = False
            else:
                try:
                    cfg[key] = int(raw_val) if raw_val.isdigit() else float(raw_val)
                except ValueError:
                    return f"参数值无效，需要数字: {raw_val}"
            return f"已设置 {key} = {cfg[key]}"

        return (
            "用法: /yuki 行为 [默认|冷漠|安静|活跃]\n"
            "或: /yuki 行为 <参数名> <值>\n"
            "可用参数: 接话门槛 追问门槛 旁听门槛 ai门槛 旁听开关 旁听消息数 旁听分数 追问窗口 追问轮数"
        )

    async def _act_behavior_cold(self, **kwargs: Any) -> str:
        engine = kwargs.get("engine")
        if engine is None:
            return "行为系统未就绪"
        return self._set_behavior_mode(engine, "cold")

    async def _act_behavior_quiet(self, **kwargs: Any) -> str:
        engine = kwargs.get("engine")
        if engine is None:
            return "行为系统未就绪"
        return self._set_behavior_mode(engine, "quiet")

    async def _act_behavior_active(self, **kwargs: Any) -> str:
        engine = kwargs.get("engine")
        if engine is None:
            return "行为系统未就绪"
        return self._set_behavior_mode(engine, "active")

    def _set_behavior_mode(self, engine: Any, mode: str) -> str:
        trigger_cfg = engine.config.setdefault("trigger", {}) if isinstance(engine.config, dict) else {}
        routing_cfg = engine.config.setdefault("routing", {}) if isinstance(engine.config, dict) else {}
        if not isinstance(trigger_cfg, dict):
            trigger_cfg = {}
            engine.config["trigger"] = trigger_cfg
        if not isinstance(routing_cfg, dict):
            routing_cfg = {}
            engine.config["routing"] = routing_cfg

        if mode == "cold":
            trigger_cfg.update({
                "ai_listen_enable": False,
                "delegate_undirected_to_ai": False,
                "followup_reply_window_seconds": 10,
                "followup_max_turns": 1,
            })
            routing_cfg.update({
                "min_confidence": 0.72,
                "followup_min_confidence": 0.88,
                "non_directed_min_confidence": 0.90,
                "ai_gate_min_confidence": 0.84,
            })
            return "已切换到冷漠模式"
        if mode == "quiet":
            trigger_cfg.update({
                "ai_listen_enable": True,
                "delegate_undirected_to_ai": True,
                "ai_listen_min_messages": 12,
                "ai_listen_min_score": 3.6,
                "followup_reply_window_seconds": 20,
                "followup_max_turns": 1,
            })
            routing_cfg.update({
                "min_confidence": 0.66,
                "followup_min_confidence": 0.84,
                "non_directed_min_confidence": 0.86,
                "ai_gate_min_confidence": 0.80,
            })
            return "已切换到安静模式"
        if mode == "active":
            trigger_cfg.update({
                "ai_listen_enable": True,
                "delegate_undirected_to_ai": True,
                "ai_listen_min_messages": 5,
                "ai_listen_min_score": 1.6,
                "followup_reply_window_seconds": 40,
                "followup_max_turns": 4,
            })
            routing_cfg.update({
                "min_confidence": 0.48,
                "followup_min_confidence": 0.62,
                "non_directed_min_confidence": 0.64,
                "ai_gate_min_confidence": 0.54,
            })
            return "已切换到活跃模式"

        trigger_cfg.update({
            "ai_listen_enable": True,
            "delegate_undirected_to_ai": True,
            "ai_listen_min_messages": 8,
            "ai_listen_min_score": 2.2,
            "followup_reply_window_seconds": 30,
            "followup_max_turns": 2,
        })
        routing_cfg.update({
            "min_confidence": 0.58,
            "followup_min_confidence": 0.75,
            "non_directed_min_confidence": 0.72,
            "ai_gate_min_confidence": 0.66,
        })
        return "已恢复默认行为模式"

    async def _send_segment(self, kwargs: dict[str, Any], seg_type: str, data: dict[str, Any]) -> str | None:
        api_call = kwargs.get("api_call")
        gid = int(kwargs.get("group_id", 0) or 0)
        uid = str(kwargs.get("user_id", ""))
        if not api_call:
            return "API 不可用"
        msg = [{"type": seg_type, "data": data}]
        try:
            if gid:
                await api_call("send_group_msg", group_id=gid, message=msg)
            else:
                await api_call("send_private_msg", user_id=int(uid), message=msg)
            return None
        except Exception as exc:
            return f"消息段发送失败: {str(exc)[:80]}"

    def _load_white(self) -> None:
        if not self._white_path.exists():
            return
        try:
            data = json.loads(self._white_path.read_text(encoding="utf-8"))
            rows = data.get("groups", []) if isinstance(data, dict) else []
            if isinstance(rows, list):
                for x in rows:
                    try:
                        self._white.add(int(x))
                    except Exception:
                        pass
        except Exception as exc:
            _log.debug("load_whitelist_fail | %s", exc)

    def _save_white(self) -> None:
        try:
            payload = {"groups": sorted(self._white)}
            self._white_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as exc:
            _log.debug("save_whitelist_fail | %s", exc)

    def _load_ignore(self) -> None:
        if not self._ignore_path.exists():
            return
        try:
            data = json.loads(self._ignore_path.read_text(encoding="utf-8"))
        except Exception as exc:
            _log.debug("load_ignore_fail | %s", exc)
            return
        if not isinstance(data, dict):
            return
        ignore_policy = normalize_text(str(data.get("ignore_policy", ""))).lower()
        if ignore_policy in {"silent", "soft", "ai_review"}:
            self._ignore_policy = ignore_policy
        global_rows = data.get("global", [])
        if isinstance(global_rows, list):
            for item in global_rows:
                uid = normalize_text(str(item))
                if uid:
                    self._ignored_global.add(uid)
        groups = data.get("groups", {})
        if isinstance(groups, dict):
            for raw_gid, rows in groups.items():
                try:
                    gid = int(raw_gid)
                except Exception:
                    continue
                if gid <= 0 or not isinstance(rows, list):
                    continue
                bucket = self._ignored_group.setdefault(gid, set())
                for item in rows:
                    uid = normalize_text(str(item))
                    if uid:
                        bucket.add(uid)

    def _save_ignore(self) -> None:
        payload = {
            "global": sorted(self._ignored_global),
            "groups": {
                str(gid): sorted(users)
                for gid, users in sorted(self._ignored_group.items(), key=lambda pair: pair[0])
                if users
            },
            "ignore_policy": self._ignore_policy,
        }
        try:
            self._ignore_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as exc:
            _log.debug("save_ignore_fail | %s", exc)

    def _load_runtime_policy(self) -> None:
        if not self._policy_path.exists():
            return
        try:
            data = json.loads(self._policy_path.read_text(encoding="utf-8"))
        except Exception as exc:
            _log.debug("load_runtime_policy_fail | %s", exc)
            return
        if not isinstance(data, dict):
            return
        global_value = data.get("high_risk_confirmation_global")
        if isinstance(global_value, bool):
            self._high_risk_confirm_global = global_value
        groups = data.get("high_risk_confirmation_groups", {})
        if isinstance(groups, dict):
            for raw_gid, raw_required in groups.items():
                try:
                    gid = int(raw_gid)
                except Exception:
                    continue
                if gid <= 0 or not isinstance(raw_required, bool):
                    continue
                self._high_risk_confirm_group[gid] = raw_required

    def _save_runtime_policy(self) -> None:
        payload = {
            "high_risk_confirmation_global": self._high_risk_confirm_global,
            "high_risk_confirmation_groups": {
                str(gid): required
                for gid, required in sorted(self._high_risk_confirm_group.items(), key=lambda pair: pair[0])
            },
        }
        try:
            self._policy_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as exc:
            _log.debug("save_runtime_policy_fail | %s", exc)

    # ── 表情包管理 ──

    async def _act_sticker_status(self, **kwargs: Any) -> str:
        engine = kwargs.get("engine")
        if not engine or not hasattr(engine, "sticker"):
            return "表情系统未初始化"
        return engine.sticker.status_text()

    async def _act_scan_sticker(self, **kwargs: Any) -> str:
        engine = kwargs.get("engine")
        if not engine or not hasattr(engine, "sticker"):
            return "表情系统未初始化"
        result = engine.sticker.scan()
        return (
            f"扫描完成\n"
            f"经典表情: {result['faces']}\n"
            f"本地表情包: {result['emojis']}\n"
            f"已学习: {engine.sticker.learned_count}"
        )

    async def _act_learn_sticker(self, **kwargs: Any) -> str:
        engine = kwargs.get("engine")
        if not engine or not hasattr(engine, "sticker"):
            return "表情系统未初始化"

        unregistered = engine.sticker.get_unregistered()
        if not unregistered:
            return f"所有表情包已注册完毕 ({engine.sticker.registered_count}/{engine.sticker.emoji_count})"

        total = len(unregistered)
        batch_size = int(kwargs.get("arg", "").strip() or "5")
        batch_size = max(1, min(batch_size, 20))

        async def _llm_vision_call(messages: list[dict]) -> str:
            client = engine.model_client
            resp = await client.chat_text(messages=messages, max_tokens=200)
            return str(resp)

        try:
            learned = await engine.sticker.learn_batch(
                llm_call=_llm_vision_call,
                batch_size=batch_size,
            )
            return (
                f"注册完成: 本次 {learned}/{batch_size}\n"
                f"总进度: {engine.sticker.registered_count}/{engine.sticker.emoji_count}\n"
                f"剩余: {total - learned}"
            )
        except Exception as e:
            return f"学习失败: {str(e)[:100]}"
