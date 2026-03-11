"""Agent 循环核心 — 多步推理 + 工具调用。











Agent 接收用户消息后，进入 think → act → observe 循环：





1. LLM 分析当前状态，决定调用哪个工具（或直接回复）





2. 执行工具，获取结果





3. 把结果喂回 LLM，继续循环





4. 当 LLM 调用 final_answer 时，循环结束





"""





from __future__ import annotations











import asyncio





from datetime import datetime





import json





import logging





import re





import secrets





import time





from dataclasses import dataclass, field





from types import SimpleNamespace





from typing import Any





from urllib.parse import urlsplit











from core.agent_tools import AgentToolRegistry





from core import prompt_loader as _pl





from core.prompt_policy import PromptPolicy





from services.model_client import ModelClient





from utils.text import clip_text, normalize_text











_log = logging.getLogger("yukiko.agent")











@dataclass(slots=True)





class AgentContext:





    """Agent 单次运行的上下文。"""





    conversation_id: str





    user_id: str





    user_name: str





    group_id: int





    bot_id: str





    is_private: bool





    mentioned: bool





    message_text: str





    message_id: str = ""





    reply_to_message_id: str = ""





    raw_segments: list[dict[str, Any]] = field(default_factory=list)





    reply_media_segments: list[dict[str, Any]] = field(default_factory=list)





    reply_to_user_id: str = ""





    reply_to_user_name: str = ""





    reply_to_text: str = ""





    api_call: Any = None





    admin_handler: Any = None  # async fn(text, user_id, group_id) -> str|None





    config_patch_handler: Any = None  # async fn(patch, actor_user_id, reason, dry_run) -> tuple[bool, str, dict]





    sticker_manager: Any = None  # StickerManager instance





    tool_executor: Any = None  # ToolExecutor instance (for video parsing etc.)





    crawler_hub: Any = None  # CrawlerHub instance





    knowledge_base: Any = None  # KnowledgeBase instance





    memory_engine: Any = None  # MemoryEngine instance





    trace_id: str = ""





    memory_context: list[str] = field(default_factory=list)





    related_memories: list[str] = field(default_factory=list)





    user_profile_summary: str = ""





    preferred_name: str = ""





    recent_speakers: list[tuple[str, str, str]] = field(default_factory=list)





    user_policies: dict[str, Any] = field(default_factory=dict)





    user_directives: list[str] = field(default_factory=list)





    media_summary: list[str] = field(default_factory=list)





    reply_media_summary: list[str] = field(default_factory=list)





    at_other_user_ids: list[str] = field(default_factory=list)





    at_other_user_names: dict[str, str] = field(default_factory=dict)  # {qq_id: name}





    verbosity: str = "medium"  # verbose / medium / brief / minimal





    output_style_instruction: str = ""  # 额外输出风格指令（可按群覆盖）





    sender_role: str = ""  # "owner" / "admin" / "member" — QQ群内角色





    is_whitelisted_group: bool = False  # 当前群是否在白名单中

    stream_callback: Any = None  # asyncio.Queue for real-time streaming updates

















@dataclass(slots=True)





class AgentResult:





    """Agent 循环的最终输出。"""





    reply_text: str = ""





    image_url: str = ""





    image_urls: list[str] = field(default_factory=list)





    video_url: str = ""





    audio_file: str = ""





    cover_url: str = ""





    action: str = "reply"





    reason: str = ""





    tool_calls_made: int = 0





    total_time_ms: int = 0





    steps: list[dict[str, Any]] = field(default_factory=list)

















class AgentLoop:





    """核心 Agent 循环引擎。











    流程:





    1. 构建 system prompt（含工具列表）





    2. 发送用户消息给 LLM





    3. LLM 返回 tool_call JSON → 执行工具 → 结果追加到对话





    4. 重复直到 LLM 调用 final_answer 或达到 max_steps





    """











    # 有副作用的发送工具（避免 final_answer 重复发送）





    _SIDE_EFFECT_SEND_TOOLS = frozenset({





        "send_group_message", "send_private_message", "send_emoji", "send_sticker",





        "learn_sticker",





    })





    _EXTERNAL_FACT_TOOLS = frozenset({





        "web_search",





        "fetch_webpage",





        "github_search",





        "github_readme",





        "search_web_media",





        "search_download_resources",





        "douyin_search",





        "scrape_extract",





        "extract_structured",





        "extract_links_and_content",





    })





    _FALLBACK_RAW_DISPLAY_SKIP_TOOLS = frozenset({





        "scrape_extract",





        "scrape_summarize",





        "scrape_structured",





        "extract_structured",





        "extract_links_and_content",





        "fetch_webpage",





    })











    def __init__(





        self,





        model_client: ModelClient,





        tool_registry: AgentToolRegistry,





        config: dict[str, Any],





    ):





        self.model_client = model_client





        self.tool_registry = tool_registry





        self.config: dict[str, Any] = {}





        self.max_steps = 8





        self.max_tokens = 4096





        self.enable = True





        self.fallback_on_parse_error = True





        self.allow_silent_on_llm_error = False





        self.repeat_tool_guard_enable = True





        self.max_same_tool_call = 3





        self.max_consecutive_think = 3





        self.tool_timeout_seconds = 28





        self.tool_timeout_seconds_media = 45





        self.media_resolver_timeout_seconds = 45





        self.llm_step_timeout_seconds = 22





        self.llm_step_timeout_seconds_after_tool = 32





        self.total_timeout_seconds = 0





        self.queue_timeout_margin_seconds = 8





        self.local_cue_inference_enable = False





        self.prompt_policy = PromptPolicy.from_config({})





        self._admin_ids: set[str] = set()





        self._pending_high_risk_actions: dict[str, dict[str, Any]] = {}





        self.high_risk_control_enable = True





        self.high_risk_default_require_confirmation = True





        self.high_risk_categories: set[str] = {"admin"}





        self.high_risk_pending_ttl_seconds = 180





        self.high_risk_name_patterns: tuple[re.Pattern[str], ...] = ()





        self.high_risk_description_patterns: tuple[re.Pattern[str], ...] = ()





        self.high_risk_user_enable_patterns: tuple[re.Pattern[str], ...] = ()





        self.high_risk_user_disable_patterns: tuple[re.Pattern[str], ...] = ()





        self.high_risk_use_confirm_token = False





        self.high_risk_confirm_cues: tuple[str, ...] = ()





        self.high_risk_cancel_cues: tuple[str, ...] = ()





        self.search_followup_resend_media_cues: tuple[str, ...] = ()





        self.token_saving_mode = False





        self.tool_result_max_items = 20





        self.tool_result_max_string_length = 1000





        self.tool_result_max_depth = 3





        self.tool_result_max_dict_items = 40





        self.context_memory_lines_limit = 8





        self.context_related_memory_limit = 5





        self.context_speaker_limit = 8





        self.context_directive_limit = 5





        self.context_profile_max_chars = 300











        # 安全: 需要管理员权限的工具 (与 AgentToolRegistry 保持同步)





        self._super_admin_tools = {





            "set_group_leave", "delete_friend", "cli_invoke", "config_update",





            "admin_command", "clean_cache", "set_qq_avatar", "set_online_status",





            "set_self_longnick",





        }





        self._group_admin_tools = {





            "set_group_ban", "set_group_kick", "set_group_whole_ban",





            "set_group_admin", "set_group_name", "send_group_notice",





            "delete_message", "set_group_special_title", "set_essence_msg",





            "delete_essence_msg", "recall_recent_messages", "set_group_card", "set_group_portrait",





            "delete_group_file", "create_group_file_folder", "del_group_notice",





        }





        self._admin_only_tools = self._super_admin_tools | self._group_admin_tools





        self.refresh_runtime_config(config)











    def refresh_runtime_config(self, config: dict[str, Any]) -> None:





        """热更新 Agent 的运行参数和管理员权限集合。"""





        self.config = config if isinstance(config, dict) else {}





        agent_cfg = self.config.get("agent", {}) if isinstance(self.config, dict) else {}





        if not isinstance(agent_cfg, dict):





            agent_cfg = {}





        self.max_steps = max(1, min(15, int(agent_cfg.get("max_steps", 8))))





        self.max_tokens = max(512, int(agent_cfg.get("max_tokens", 4096)))





        self.enable = bool(agent_cfg.get("enable", True))





        self.fallback_on_parse_error = bool(agent_cfg.get("fallback_on_parse_error", True))





        self.allow_silent_on_llm_error = bool(agent_cfg.get("allow_silent_on_llm_error", False))





        self.repeat_tool_guard_enable = bool(agent_cfg.get("repeat_tool_guard_enable", True))





        self.max_same_tool_call = max(2, min(8, int(agent_cfg.get("max_same_tool_call", 3))))





        self.max_consecutive_think = max(2, min(8, int(agent_cfg.get("max_consecutive_think", 3))))





        self.tool_timeout_seconds = max(8, min(120, int(agent_cfg.get("tool_timeout_seconds", 28))))





        self.tool_timeout_seconds_media = max(





            self.tool_timeout_seconds,





            min(180, int(agent_cfg.get("tool_timeout_seconds_media", 45))),





        )





        self.llm_step_timeout_seconds = max(6, min(120, int(agent_cfg.get("llm_step_timeout_seconds", 30))))





        self.llm_step_timeout_seconds_after_tool = max(





            self.llm_step_timeout_seconds,





            min(





                120,





                int(





                    agent_cfg.get(





                        "llm_step_timeout_seconds_after_tool",





                        max(32, self.llm_step_timeout_seconds),





                    )





                ),





            ),





        )





        self.total_timeout_seconds = max(0, int(agent_cfg.get("total_timeout_seconds", 0)))





        self.queue_timeout_margin_seconds = max(1, min(30, int(agent_cfg.get("queue_timeout_margin_seconds", 8))))





        # 上下文保留策略
        self.context_retention = str(agent_cfg.get("context_retention", "medium")).lower()
        if self.context_retention not in ("minimal", "medium", "large"):
            self.context_retention = "medium"

        # 自动压缩上下文到记忆库
        self.auto_compress_to_memory = bool(agent_cfg.get("auto_compress_to_memory", True))

        search_cfg = self.config.get("search", {}) if isinstance(self.config, dict) else {}





        if not isinstance(search_cfg, dict):





            search_cfg = {}





        video_resolver_cfg = search_cfg.get("video_resolver", {}) if isinstance(search_cfg, dict) else {}





        if not isinstance(video_resolver_cfg, dict):





            video_resolver_cfg = {}





        resolver_total_timeout = max(0, int(video_resolver_cfg.get("resolve_total_timeout_seconds", 0) or 0))





        self.media_resolver_timeout_seconds = max(





            self.tool_timeout_seconds_media,





            min(300, resolver_total_timeout or self.tool_timeout_seconds_media),





        )





        control_cfg = self.config.get("control", {}) if isinstance(self.config, dict) else {}





        if not isinstance(control_cfg, dict):





            control_cfg = {}





        # 强制关闭本地关键词/规则推断，统一交给模型按 Prompt 语义决策。





        _ = control_cfg





        self.local_cue_inference_enable = False





        self.prompt_policy = PromptPolicy.from_config(self.config)





        self._refresh_high_risk_control(agent_cfg)





        followup_cfg = self.config.get("search_followup", {}) if isinstance(self.config, dict) else {}





        if not isinstance(followup_cfg, dict):





            followup_cfg = {}





        resend_media_cues_raw = followup_cfg.get("resend_media_cues", [])





        if not isinstance(resend_media_cues_raw, list):





            resend_media_cues_raw = []





        resend_media_cues = [





            normalize_text(str(item)).lower()





            for item in resend_media_cues_raw





            if normalize_text(str(item))





        ]





        self.search_followup_resend_media_cues = tuple(dict.fromkeys(resend_media_cues))











        output_cfg = self.config.get("output", {}) if isinstance(self.config, dict) else {}





        if not isinstance(output_cfg, dict):





            output_cfg = {}





        self.token_saving_mode = bool(output_cfg.get("token_saving", False))











        compact_cfg = agent_cfg.get("compact_data", {}) if isinstance(agent_cfg, dict) else {}





        if not isinstance(compact_cfg, dict):





            compact_cfg = {}





        default_items = 12 if self.token_saving_mode else 20





        default_str_len = 420 if self.token_saving_mode else 1000





        default_depth = 2 if self.token_saving_mode else 3





        default_dict_items = 20 if self.token_saving_mode else 40





        self.tool_result_max_items = max(3, min(80, int(compact_cfg.get("max_items", default_items))))





        self.tool_result_max_string_length = max(120, min(4000, int(compact_cfg.get("max_string_length", default_str_len))))





        self.tool_result_max_depth = max(1, min(6, int(compact_cfg.get("max_depth", default_depth))))





        self.tool_result_max_dict_items = max(8, min(120, int(compact_cfg.get("max_dict_items", default_dict_items))))











        self.context_memory_lines_limit = 5 if self.token_saving_mode else 8





        self.context_related_memory_limit = 3 if self.token_saving_mode else 5





        self.context_speaker_limit = 5 if self.token_saving_mode else 8





        self.context_directive_limit = 3 if self.token_saving_mode else 5





        self.context_profile_max_chars = 180 if self.token_saving_mode else 300











        admin_cfg = self.config.get("admin", {}) if isinstance(self.config, dict) else {}





        if not isinstance(admin_cfg, dict):





            admin_cfg = {}





        self._admin_ids = set()





        for key in ("admin_ids", "super_users"):





            rows = admin_cfg.get(key, [])





            if isinstance(rows, list):





                for item in rows:





                    uid = str(item).strip()





                    if uid:





                        self._admin_ids.add(uid)





        sq = str(admin_cfg.get("super_admin_qq", "")).strip()





        if sq:





            self._admin_ids.add(sq)











        # 加白群集合 (从 admin 配置读取)





        self._whitelisted_groups: set[int] = set()





        for x in admin_cfg.get("whitelist_groups", []) or []:





            try:





                self._whitelisted_groups.add(int(x))





            except (ValueError, TypeError):





                pass











    async def _push_stream_event(self, ctx: AgentContext, event: dict[str, Any]) -> None:
        """推送实时流式事件到前端（如果有 stream_callback）。"""
        if ctx.stream_callback is not None:
            try:
                await ctx.stream_callback.put(event)
            except Exception:
                pass  # 静默失败，不影响主流程

    def _resolve_permission_level(self, ctx: "AgentContext") -> str:





        """根据用户身份和群角色计算权限等级。











        返回: "super_admin" / "group_admin" / "user"





        - super_admin: 在 _admin_ids 中的超级管理员，凌驾一切





        - group_admin: 加白群中的群主或管理员





        - user: 普通用户





        """





        uid = str(ctx.user_id).strip()





        if uid in self._admin_ids:





            return "super_admin"





        # 群管理员: 必须在加白群 + 群角色是 owner 或 admin





        if not ctx.is_private and ctx.group_id:





            role = (ctx.sender_role or "").lower()





            if ctx.is_whitelisted_group and role in ("owner", "admin"):





                return "group_admin"





        return "user"











    @staticmethod





    def _compile_regex_patterns(values: Any) -> tuple[re.Pattern[str], ...]:





        if isinstance(values, str):





            values = [values]





        if not isinstance(values, list):





            return ()





        patterns: list[re.Pattern[str]] = []





        for item in values:





            raw = normalize_text(str(item))





            if not raw:





                continue





            try:





                patterns.append(re.compile(raw, re.IGNORECASE))





            except re.error:





                continue





        return tuple(patterns)











    @staticmethod





    def _normalize_word_tuple(values: Any, default: tuple[str, ...]) -> tuple[str, ...]:





        if isinstance(values, str):





            values = [values]





        if not isinstance(values, list):





            values = list(default)





        rows = [normalize_text(str(item)).lower() for item in values if normalize_text(str(item))]





        return tuple(rows) if rows else default











    def _refresh_high_risk_control(self, agent_cfg: dict[str, Any]) -> None:





        control = agent_cfg.get("high_risk_control", {}) if isinstance(agent_cfg, dict) else {}





        if not isinstance(control, dict):





            control = {}





        default_name_patterns = [





            "^set_group_",





            "^delete_",





            "^ban_",





            "^kick_",





            "^config_update$",





            "^admin_command$",





            "^cli_invoke$",





            "^upload_group_file$",





            "^smart_download$",





        ]





        default_description_patterns = ["不可逆", "踢出群", "删除", "封禁", "禁言", "管理员权限", "可执行文件"]





        self.high_risk_control_enable = bool(control.get("enable", True))





        self.high_risk_default_require_confirmation = bool(control.get("default_require_confirmation", True))





        categories_raw = control.get("categories", ["admin"])





        if isinstance(categories_raw, str):





            categories_raw = [categories_raw]





        if isinstance(categories_raw, list):





            self.high_risk_categories = {





                normalize_text(str(item)).lower()





                for item in categories_raw





                if normalize_text(str(item))





            } or {"admin"}





        else:





            self.high_risk_categories = {"admin"}





        self.high_risk_pending_ttl_seconds = max(30, int(control.get("pending_ttl_seconds", 180)))





        self.high_risk_name_patterns = self._compile_regex_patterns(





            control.get("tool_name_patterns", default_name_patterns)





        )





        self.high_risk_description_patterns = self._compile_regex_patterns(





            control.get("description_patterns", default_description_patterns)





        )





        self.high_risk_user_enable_patterns = self._compile_regex_patterns(





            control.get("user_enable_patterns", [])





        )





        self.high_risk_user_disable_patterns = self._compile_regex_patterns(





            control.get("user_disable_patterns", [])





        )





        self.high_risk_use_confirm_token = bool(control.get("use_confirm_token", False))





        self.high_risk_confirm_cues = self._normalize_word_tuple(





            control.get("confirm_cues"),





            ("确认", "确认执行", "继续执行", "确定执行", "yes"),





        )





        self.high_risk_cancel_cues = self._normalize_word_tuple(





            control.get("cancel_cues"),





            ("取消", "算了", "停止", "不执行", "撤销"),





        )





        self._cleanup_pending_high_risk(force=True)











    def _pending_high_risk_key(self, ctx: AgentContext) -> str:





        return f"{ctx.conversation_id}:{ctx.user_id}"











    def _cleanup_pending_high_risk(self, force: bool = False) -> None:





        if not self._pending_high_risk_actions:





            return





        if force:





            self._pending_high_risk_actions.clear()





            return





        now = time.time()





        stale: list[str] = []





        for key, payload in self._pending_high_risk_actions.items():





            expires_at = float(payload.get("expires_at", 0))





            if expires_at <= 0 or expires_at < now:





                stale.append(key)





        for key in stale:





            self._pending_high_risk_actions.pop(key, None)











    @staticmethod





    def _build_args_signature(args: dict[str, Any]) -> str:





        try:





            return json.dumps(args or {}, ensure_ascii=False, sort_keys=True)





        except Exception:





            return str(args or {})











    def _is_confirmation_text(self, text: str, pending: dict[str, Any] | None = None) -> bool:





        content = normalize_text(text).lower()





        if not content:





            return False





        if isinstance(pending, dict):





            token = normalize_text(str(pending.get("confirm_token", ""))).lower()





            if token and token in content:





                return True





        return any(cue in content for cue in self.high_risk_confirm_cues)











    def _is_cancellation_text(self, text: str, pending: dict[str, Any] | None = None) -> bool:





        content = normalize_text(text).lower()





        if not content:





            return False





        if isinstance(pending, dict):





            token = normalize_text(str(pending.get("cancel_token", ""))).lower()





            if token and token in content:





                return True





        return any(cue in content for cue in self.high_risk_cancel_cues)











    def _tool_is_high_risk(self, tool_name: str) -> bool:





        schema = self.tool_registry.get_schema(tool_name)





        category = normalize_text(getattr(schema, "category", "")).lower() if schema else ""





        description = normalize_text(getattr(schema, "description", "")) if schema else ""





        if category and category in self.high_risk_categories:





            return True





        if any(pattern.search(tool_name) for pattern in self.high_risk_name_patterns):





            return True





        if description and any(pattern.search(description) for pattern in self.high_risk_description_patterns):





            return True





        return False











    def _require_high_risk_confirmation_for_user(self, ctx: AgentContext) -> bool:





        policies = ctx.user_policies if isinstance(ctx.user_policies, dict) else {}





        if "high_risk_confirmation_required" in policies:





            return bool(policies.get("high_risk_confirmation_required"))





        directives = [normalize_text(str(item)) for item in (ctx.user_directives or []) if normalize_text(str(item))]





        for row in directives:





            if any(pattern.search(row) for pattern in self.high_risk_user_disable_patterns):





                return False





        for row in directives:





            if any(pattern.search(row) for pattern in self.high_risk_user_enable_patterns):





                return True





        return self.high_risk_default_require_confirmation











    def _build_high_risk_confirm_prompt(





        self,





        tool_name: str,





        tool_args: dict[str, Any],





    ) -> tuple[str, str, str]:





        target = ""





        if isinstance(tool_args, dict):





            for key in ("user_id", "target_user_id", "group_id"):





                value = normalize_text(str(tool_args.get(key, "")))





                if value:





                    target = f"{key}={value}"





                    break





        detail = f"（{target}）" if target else ""





        confirm_token = ""





        cancel_token = ""





        if bool(getattr(self, "high_risk_use_confirm_token", False)):





            short = secrets.token_hex(2).lower()





            confirm_token = f"confirm-{short}"





            cancel_token = f"cancel-{short}"





            prompt = (





                f"这是高风险操作：{tool_name}{detail}。"





                f"请回复“{confirm_token}”确认执行，或回复“{cancel_token}”取消。"





            )





            return prompt, confirm_token, cancel_token





        return (





            f"这是高风险操作：{tool_name}{detail}。"





            "请二次确认后我才会执行。"





            "请回复“确认执行”，或回复“取消”。"





        ), confirm_token, cancel_token











    def _guard_high_risk_tool_call(self, ctx: AgentContext, tool_name: str, tool_args: dict[str, Any]) -> str:





        if not self.high_risk_control_enable:





            return ""





        if not self._tool_is_high_risk(tool_name):





            return ""





        if not self._require_high_risk_confirmation_for_user(ctx):





            return ""











        self._cleanup_pending_high_risk(force=False)





        key = self._pending_high_risk_key(ctx)





        current_args_sig = self._build_args_signature(tool_args)





        pending = self._pending_high_risk_actions.get(key)





        msg_text = normalize_text(ctx.message_text)











        if pending and self._is_cancellation_text(msg_text, pending):





            self._pending_high_risk_actions.pop(key, None)





            return "已取消上一条高风险操作，不会执行。"











        if pending:





            pending_tool = normalize_text(str(pending.get("tool_name", "")))





            pending_sig = normalize_text(str(pending.get("args_sig", "")))





            if self._is_confirmation_text(msg_text, pending) and pending_tool == tool_name and pending_sig == current_args_sig:





                self._pending_high_risk_actions.pop(key, None)





                return ""





            if pending_tool == tool_name and pending_sig == current_args_sig:





                return normalize_text(str(pending.get("prompt", ""))) or self._build_high_risk_confirm_prompt(tool_name, tool_args)[0]





            # 用户在同会话发起了新的高风险操作，覆盖旧待确认项





            self._pending_high_risk_actions.pop(key, None)











        prompt, confirm_token, cancel_token = self._build_high_risk_confirm_prompt(tool_name, tool_args)





        self._pending_high_risk_actions[key] = {





            "tool_name": tool_name,





            "args_sig": current_args_sig,





            "created_at": time.time(),





            "expires_at": time.time() + self.high_risk_pending_ttl_seconds,





            "prompt": prompt,





            "confirm_token": confirm_token,





            "cancel_token": cancel_token,





        }





        return prompt











    async def run(self, ctx: AgentContext) -> AgentResult:





        """执行 Agent 循环，返回最终结果。"""





        t0 = time.monotonic()





        steps: list[dict[str, Any]] = []





        force_tool_first = self._should_force_tool_first(ctx) if self.local_cue_inference_enable else False





        # 结构化多模态信号仍然保留：带图提问时优先拉起图片分析工具。





        force_image_tool_first = self._should_force_image_tool_first(ctx)











        system_prompt = self._build_system_prompt(ctx)





        messages: list[dict[str, str]] = [





            {"role": "system", "content": system_prompt},





            {"role": "user", "content": self._build_user_message(ctx)},





        ]











        tool_calls_made = 0





        missing_arg_counts: dict[str, int] = {}





        successful_external_fact_tools = 0





        seen_external_fact_signatures: set[str] = set()





        repeated_tool_counts: dict[str, int] = {}





        consecutive_think_count = 0





        # 追踪工具已发送的媒体（避免 final_answer 重复发送）





        tool_sent_media: set[str] = set()





        # 含媒体时给更多时间；总预算自动对齐 queue 超时，避免队列先把任务打断。





        has_media = bool(ctx.media_summary) or bool(ctx.reply_media_summary)





        total_timeout = self._resolve_total_timeout_seconds(ctx, has_media)





        deadline_ts = t0 + total_timeout











        for step_idx in range(self.max_steps):





            # 总超时保护





            elapsed = time.monotonic() - t0





            remaining = deadline_ts - time.monotonic()





            if remaining <= 3:





                _log.warning(





                    "agent_total_timeout | trace=%s | elapsed=%.1fs | limit=%.1fs",





                    ctx.trace_id,





                    elapsed,





                    total_timeout,





                )





                return await self._build_fallback_result(ctx, steps, tool_calls_made, t0, "total_timeout")





            # 调用 LLM（带重试，agent loop 是关键路径）





            llm_budget = float(self.llm_step_timeout_seconds)





            if tool_calls_made > 0:





                llm_budget = max(llm_budget, float(self.llm_step_timeout_seconds_after_tool))





            llm_timeout = min(llm_budget, max(6.0, remaining - 1.5))





            # 推送"AI 正在思考"事件
            await self._push_stream_event(ctx, {
                "type": "thinking",
                "step": step_idx,
                "message": "AI 正在思考..."
            })

            try:





                raw_response = await asyncio.wait_for(





                    self.model_client.chat_text_with_retry(





                        messages, max_tokens=self.max_tokens, retries=1, backoff=1.0,





                    ),





                    timeout=llm_timeout,





                )





            except asyncio.TimeoutError:





                _log.warning(





                    "agent_llm_timeout | trace=%s | step=%d | timeout=%.1fs",





                    ctx.trace_id,





                    step_idx,





                    llm_timeout,





                )





                if steps:





                    return await self._build_fallback_result(ctx, steps, tool_calls_made, t0, "llm_timeout")





                fallback = _pl.get_message(





                    "llm_timeout_fallback",





                    "我这边处理超时了。你可以把问题再精简一点，我马上继续。",





                )





                return AgentResult(





                    reply_text=fallback,





                    action="reply",





                    reason="agent_llm_timeout",





                    total_time_ms=self._elapsed(t0),





                )





            except Exception as exc:





                _log.warning("agent_llm_error | trace=%s | step=%d | %s", ctx.trace_id, step_idx, exc)





                if steps:





                    # 有之前的步骤结果，用最后一步的信息兜底





                    return await self._build_fallback_result(ctx, steps, tool_calls_made, t0, "llm_error")





                # undirected 场景可按配置静默，默认不静默，避免用户感知“装死”。





                if self.allow_silent_on_llm_error and not ctx.mentioned and not ctx.is_private:





                    return AgentResult(





                        reply_text="", action="reply", reason="agent_llm_error_silent",





                        total_time_ms=self._elapsed(t0),





                    )





                fallback = _pl.get_message(





                    "llm_error_fallback",





                    _pl.get_message("generic_error", "我这边接口抖了，稍等我再试一次。"),





                )





                return AgentResult(





                    reply_text=fallback,





                    action="reply", reason="agent_llm_error",





                    total_time_ms=self._elapsed(t0),





                )











            response_text = normalize_text(raw_response)





            if not response_text:





                break











            # 解析 LLM 输出: 期望 JSON tool_call 或纯文本回复





            parsed = self._parse_llm_output(response_text)











            if parsed is None:





                # 无法解析为 tool_call





                # 安全检查：如果内容看起来像 JSON，不要当作回复发出去





                if response_text.strip().startswith("{"):





                    _log.warning("agent_unparseable_json | trace=%s | step=%d", ctx.trace_id, step_idx)





                    break





                _log.info("agent_direct_reply | trace=%s | step=%d", ctx.trace_id, step_idx)





                return AgentResult(





                    reply_text=response_text,





                    action="reply", reason="agent_direct_reply",





                    tool_calls_made=tool_calls_made,





                    total_time_ms=self._elapsed(t0),





                    steps=steps,





                )











            tool_name = parsed.get("tool", "")





            tool_args = parsed.get("args", {})





            if not isinstance(tool_args, dict):





                tool_args = {}





            tool_args = self._normalize_tool_args(tool_name, tool_args, ctx)





            if force_image_tool_first and tool_calls_made == 0 and tool_name not in {"analyze_image", "think"}:





                _log.info(





                    "agent_force_image_tool_first | trace=%s | step=%d | from=%s",





                    ctx.trace_id,





                    step_idx,





                    tool_name or "unknown",





                )





                forced_args: dict[str, Any] = {}





                first_url = self._extract_first_url(ctx.message_text)





                if first_url and re.search(r"\.(?:jpg|jpeg|png|gif|webp|bmp)(?:\?|$)", first_url, re.IGNORECASE):





                    forced_args["url"] = first_url





                question = normalize_text(ctx.message_text)





                if question:





                    forced_args["question"] = question





                tool_name = "analyze_image"





                tool_args = forced_args





            missing_args = self._missing_required_tool_args(tool_name, tool_args)











            _log.info(





                "agent_tool_call | trace=%s | step=%d | tool=%s | args=%s",





                ctx.trace_id, step_idx, tool_name, json.dumps(tool_args, ensure_ascii=False)[:200],





            )











            if missing_args:





                miss_text = ", ".join(missing_args)





                miss_key = f"{tool_name}:{'|'.join(sorted(missing_args))}"





                missing_arg_counts[miss_key] = missing_arg_counts.get(miss_key, 0) + 1





                steps.append({





                    "step": step_idx,





                    "tool": tool_name,





                    "ok": False,





                    "error": f"missing_required_args:{miss_text}",





                })





                if missing_arg_counts[miss_key] >= 3:





                    fallback_text = await self._ai_fallback_reply(





                        ctx,





                        f"工具 {tool_name} 连续缺少参数({miss_text})，无法继续执行",





                    )





                    return AgentResult(





                        reply_text=fallback_text or "我先停一下，当前这步参数一直不完整。你补一句更具体的目标，我立刻继续。",





                        action="reply",





                        reason="agent_missing_args_loop_break",





                        tool_calls_made=tool_calls_made,





                        total_time_ms=self._elapsed(t0),





                        steps=steps,





                    )





                messages.append({"role": "assistant", "content": response_text})





                messages.append({





                    "role": "user",





                    "content": json.dumps(





                        {





                            "tool_result": {





                                "tool": tool_name,





                                "ok": False,





                                "error": f"工具 {tool_name} 缺少必填参数: {miss_text}",





                                "display": f"{tool_name} 缺少参数({miss_text})，请补全后重试。",





                            }





                        },





                        ensure_ascii=False,





                    ),





                })





                continue











            # final_answer 特殊处理 — 直接返回





            if tool_name == "final_answer":





                text = str(tool_args.get("text", "")).strip()





                image_url = str(tool_args.get("image_url", "")).strip()





                video_url = str(tool_args.get("video_url", "")).strip()





                audio_file = str(tool_args.get("audio_file", "")).strip()





                if audio_file.lower().endswith(".silk"):





                    preferred_audio = self._last_success_audio_file(steps, prefer_non_silk=True)





                    if preferred_audio:





                        _log.info(





                            "agent_audio_file_override | trace=%s | step=%d | from=%s | to=%s",





                            ctx.trace_id,





                            step_idx,





                            clip_text(audio_file, 120),





                            clip_text(preferred_audio, 120),





                        )





                        audio_file = preferred_audio





                if not audio_file:





                    audio_file = self._last_success_audio_file(steps)





                # 防止工具 JSON 泄漏给用户





                if text.startswith("{") and text.endswith("}"):





                    try:





                        maybe_json = json.loads(text)





                        if isinstance(maybe_json, dict):





                            text = _pl.get_message(





                                "tool_payload_leaked",





                                "检测到模型输出了工具调用格式，我已自动重试处理。",





                            )





                    except (json.JSONDecodeError, ValueError):





                        pass





                # 提取 image_urls（多图）





                raw_image_urls = tool_args.get("image_urls", [])





                image_urls: list[str] = []





                if isinstance(raw_image_urls, list):





                    image_urls = [str(u).strip() for u in raw_image_urls if str(u).strip()]





                if image_url and image_url not in image_urls:





                    image_urls.insert(0, image_url)





                if image_urls and not image_url:





                    image_url = image_urls[0]





                # final_answer 漏带媒体时，从最近成功工具结果自动回填，避免“说画好了但没图”。





                if not image_url and not image_urls:





                    fallback_images = self._last_success_image_urls(steps)





                    if fallback_images:





                        image_urls = fallback_images





                        image_url = fallback_images[0]





                        _log.info(





                            "agent_image_url_fallback | trace=%s | step=%d | count=%d",





                            ctx.trace_id,





                            step_idx,





                            len(fallback_images),





                        )





                if not video_url:





                    fallback_video = self._last_success_video_url(steps)





                    if fallback_video:





                        video_url = fallback_video





                        _log.info(





                            "agent_video_url_fallback | trace=%s | step=%d",





                            ctx.trace_id,





                            step_idx,





                        )





                # 禁止占位/伪造媒体链接直接落地，强制模型回到工具链拿真实可发送 URL。





                invalid_media_urls: list[str] = []





                for candidate in [image_url, *image_urls, video_url, audio_file]:





                    if self._is_placeholder_media_url(candidate):





                        invalid_media_urls.append(candidate)





                if invalid_media_urls:





                    steps.append(





                        {





                            "step": step_idx,





                            "tool": "policy_guard",





                            "error": "invalid_media_url_placeholder",





                        }





                    )





                    messages.append({"role": "assistant", "content": response_text})





                    messages.append(





                        {





                            "role": "user",





                            "content": json.dumps(





                                {





                                    "tool_result": {





                                        "tool": "policy_guard",





                                        "ok": False,





                                        "error": "final_answer 里出现了占位媒体链接（如 example.com）。请先调用工具获取真实 URL 再 final_answer。",





                                    }





                                },





                                ensure_ascii=False,





                            ),





                        }





                    )





                    continue





                media_candidates = [





                    normalize_text(url) for url in [image_url, *image_urls, video_url, audio_file] if normalize_text(url)





                ]





                if media_candidates:





                    known_media_urls = self._collect_known_media_urls(steps=steps, ctx=ctx)





                    known_local_media_paths = self._collect_known_local_media_paths(steps=steps, ctx=ctx)





                    out_of_chain_urls: list[str] = []





                    for candidate in media_candidates:





                        if self._is_local_media_path(candidate):





                            local_norm = self._normalize_local_media_path(candidate)





                            if not local_norm or local_norm not in known_local_media_paths:





                                out_of_chain_urls.append(candidate)





                            continue





                        if not self._url_matches_known_media(candidate, known_media_urls):





                            out_of_chain_urls.append(candidate)





                    if out_of_chain_urls:





                        dropped = {normalize_text(item) for item in out_of_chain_urls if normalize_text(item)}





                        if dropped:





                            if image_url and normalize_text(image_url) in dropped:





                                image_url = ""





                            image_urls = [u for u in image_urls if normalize_text(u) not in dropped]





                            if video_url and normalize_text(video_url) in dropped:





                                video_url = ""





                            if audio_file and normalize_text(audio_file) in dropped:





                                audio_file = ""





                            if image_urls and not image_url:





                                image_url = image_urls[0]





                        if text or image_url or image_urls or video_url or audio_file:





                            _log.info(





                                "agent_strip_out_of_chain_media | trace=%s | step=%d | dropped=%d",





                                ctx.trace_id,





                                step_idx,





                                len(out_of_chain_urls),





                            )





                        else:





                            steps.append(





                                {





                                    "step": step_idx,





                                    "tool": "policy_guard",





                                    "error": "media_url_not_from_tool_chain",





                                }





                            )





                            messages.append({"role": "assistant", "content": response_text})





                            messages.append(





                                {





                                    "role": "user",





                                    "content": json.dumps(





                                        {





                                            "tool_result": {





                                                "tool": "policy_guard",





                                                "ok": False,





                                                "error": "final_answer 的媒体链接必须来自本轮工具结果或用户原始消息。请先调用工具获取真实可发送链接，再 final_answer。",





                                            }





                                        },





                                        ensure_ascii=False,





                                    ),





                                }





                            )





                            continue





                # 工具型任务保护：明显应先调工具的请求，禁止 0 工具直接 final_answer。





                # 但如果 bot 已经用 think 推理过并决定不回复（空 text），允许通过。





                has_thought = any(s.get("tool") == "think" for s in steps)





                user_msg_clean = normalize_text(ctx.message_text)





                intentional_silence = (





                    has_thought





                    and not text





                    and not image_url





                    and not video_url





                    and not audio_file





                    and not ctx.mentioned





                    and not ctx.is_private





                    and len(user_msg_clean) <= 4





                )





                if force_tool_first and tool_calls_made == 0 and not intentional_silence:





                    _log.info(





                        "agent_force_tool_first | trace=%s | step=%d | text=%s",





                        ctx.trace_id,





                        step_idx,





                        clip_text(ctx.message_text, 120),





                    )





                    steps.append({"step": step_idx, "tool": "policy_guard", "error": "tool_required_before_final"})





                    messages.append({"role": "assistant", "content": response_text})





                    messages.append(





                        {





                            "role": "user",





                            "content": json.dumps(





                                {





                                    "tool_result": {





                                        "tool": "policy_guard",





                                        "ok": False,





                                        "error": "这是工具型请求，必须先调用最合适的工具，再输出 final_answer。",





                                    }





                                },





                                ensure_ascii=False,





                            ),





                        }





                    )





                    continue





                # 某些模型会把真正的工具调用 JSON 包在 final_answer.text 里，尝试恢复。





                recovered = None





                if text and not image_url and not video_url and not audio_file:





                    recovered = self._extract_embedded_tool_call_from_text(text)





                if recovered:





                    recovered_tool = str(recovered.get("tool", "")).strip()





                    recovered_args = recovered.get("args", {})





                    if recovered_tool == "final_answer":





                        recovered_text = ""





                        if isinstance(recovered_args, dict):





                            recovered_text = normalize_text(str(recovered_args.get("text", "")))





                        if recovered_text:





                            _log.info(





                                "agent_final_answer_embedded_final_unwrapped | trace=%s | step=%d",





                                ctx.trace_id,





                                step_idx,





                            )





                            text = recovered_text





                    elif recovered_tool and self.tool_registry.has_tool(recovered_tool):





                        _log.warning(





                            "agent_final_answer_embedded_tool_recovered | trace=%s | step=%d | tool=%s",





                            ctx.trace_id, step_idx, recovered_tool,





                        )





                        tool_name = recovered_tool





                        tool_args = recovered_args if isinstance(recovered_args, dict) else {}





                    else:





                        text = _pl.get_message(





                            "tool_payload_leaked",





                            "检测到模型输出了工具调用格式，我已自动重试处理。",





                        )





                if tool_name == "final_answer" and not text and not image_url and not video_url and not audio_file:





                    # bot 用 think 推理后决定不回复 → 保持空文本（intentional silence）





                    # 其他情况（没 think 过就空 final_answer）→ AI 生成兜底





                    if not intentional_silence:





                        text = self._last_success_display(steps)





                        if not text:





                            text = await self._ai_fallback_reply(ctx, "处理完了但没有拿到有效结果")





                        if not text:





                            text = _pl.get_message("no_result", "")





                steps.append({"step": step_idx, "tool": "final_answer", "result": "done"})





                if tool_name == "final_answer":





                    user_media_refs = self._extract_media_refs_from_segments(ctx.raw_segments)





                    reply_media_refs = self._extract_media_refs_from_segments(ctx.reply_media_segments)





                    _log.info(





                        "agent_final_answer_media_source | trace=%s | step=%d | image=%s | image_count=%d | video=%s | user_media=%d | reply_media=%d",





                        ctx.trace_id,





                        step_idx,





                        bool(image_url),





                        len(image_urls),





                        bool(video_url),





                        len(user_media_refs),





                        len(reply_media_refs),





                    )





                    # 去重：如果工具已经发送了媒体（副作用），final_answer 不再重复携带





                    if tool_sent_media:





                        if image_url and normalize_text(image_url) in tool_sent_media:





                            _log.info("agent_dedup_media | trace=%s | stripped image_url (already sent by tool)", ctx.trace_id)





                            image_url = ""





                        image_urls = [u for u in image_urls if normalize_text(u) not in tool_sent_media]





                        if video_url and normalize_text(video_url) in tool_sent_media:





                            _log.info("agent_dedup_media | trace=%s | stripped video_url (already sent by tool)", ctx.trace_id)





                            video_url = ""





                    return AgentResult(





                        reply_text=text,





                        image_url=image_url,





                        image_urls=image_urls if image_urls else ([image_url] if image_url else []),





                        video_url=video_url,





                        audio_file=audio_file,





                        action="reply", reason="agent_final_answer",





                        tool_calls_made=tool_calls_made,





                        total_time_ms=self._elapsed(t0),





                        steps=steps,





                    )











            # think 工具 — 不算真正的工具调用





            if tool_name == "think":





                consecutive_think_count += 1





                if consecutive_think_count > self.max_consecutive_think:





                    steps.append(





                        {





                            "step": step_idx,





                            "tool": "think",





                            "ok": False,





                            "error": "too_many_consecutive_think",





                        }





                    )





                    if consecutive_think_count >= self.max_consecutive_think + 2:





                        fallback_text = await self._ai_fallback_reply(





                            ctx,





                            "连续思考次数过多，没有执行有效工具",





                        )





                        return AgentResult(





                            reply_text=fallback_text or "我不绕圈了：你再说得具体一点，我直接执行。",





                            action="reply",





                            reason="agent_think_loop_break",





                            tool_calls_made=tool_calls_made,





                            total_time_ms=self._elapsed(t0),





                            steps=steps,





                        )





                    messages.append({"role": "assistant", "content": response_text})





                    messages.append(





                        {





                            "role": "user",





                            "content": json.dumps(





                                {





                                    "tool_result": {





                                        "tool": "think",





                                        "ok": False,





                                        "error": "think 连续过多，请直接调用具体工具或 final_answer。",





                                    }





                                },





                                ensure_ascii=False,





                            ),





                        }





                    )





                    continue





                thought = str(tool_args.get("thought", ""))





                steps.append({"step": step_idx, "tool": "think", "thought": clip_text(thought, 200)})





                messages.append({"role": "assistant", "content": response_text})





                messages.append({"role": "user", "content": json.dumps(





                    {"tool_result": {"tool": "think", "ok": True, "display": _pl.get_message("think_done", "思考完成，请继续")}},





                    ensure_ascii=False,





                )})





                continue





            else:





                consecutive_think_count = 0











            # 安全检查: 三级权限





            perm_level = self._resolve_permission_level(ctx)





            if tool_name in self._super_admin_tools and perm_level != "super_admin":





                steps.append({"step": step_idx, "tool": tool_name, "blocked": "need_super_admin"})





                messages.append({"role": "assistant", "content": response_text})





                messages.append({"role": "user", "content": json.dumps(





                    {"tool_result": {"tool": tool_name, "ok": False, "error": "权限不足，该操作仅超级管理员可执行"}},





                    ensure_ascii=False,





                )})





                continue





            if tool_name in self._group_admin_tools and perm_level not in ("super_admin", "group_admin"):





                steps.append({"step": step_idx, "tool": tool_name, "blocked": "need_group_admin"})





                messages.append({"role": "assistant", "content": response_text})





                messages.append({"role": "user", "content": json.dumps(





                    {"tool_result": {"tool": tool_name, "ok": False, "error": "权限不足，该操作需要群管理员或超级管理员权限"}},





                    ensure_ascii=False,





                )})





                continue











            # 检查工具是否存在





            if not self.tool_registry.has_tool(tool_name):





                steps.append({"step": step_idx, "tool": tool_name, "error": "unknown_tool"})





                messages.append({"role": "assistant", "content": response_text})





                messages.append({"role": "user", "content": json.dumps(





                    {"tool_result": {"tool": tool_name, "ok": False, "error": f"工具 {tool_name} 不存在，请检查工具名"}},





                    ensure_ascii=False,





                )})





                continue











            high_risk_guard_reply = self._guard_high_risk_tool_call(





                ctx=ctx,





                tool_name=tool_name,





                tool_args=tool_args,





            )





            if high_risk_guard_reply:





                steps.append(





                    {





                        "step": step_idx,





                        "tool": tool_name,





                        "blocked": "high_risk_confirmation_required",





                    }





                )





                return AgentResult(





                    reply_text=high_risk_guard_reply,





                    action="reply",





                    reason="agent_high_risk_guard",





                    tool_calls_made=tool_calls_made,





                    total_time_ms=self._elapsed(t0),





                    steps=steps,





                )











            # 自动补全缺失参数





            tool_args = self._normalize_tool_args(tool_name, tool_args, ctx)





            tool_signature = f"{tool_name}|{self._build_args_signature(tool_args)}"





            if self.repeat_tool_guard_enable:





                repeated_tool_counts[tool_signature] = repeated_tool_counts.get(tool_signature, 0) + 1





                repeat_count = repeated_tool_counts[tool_signature]





                if repeat_count > self.max_same_tool_call:





                    steps.append(





                        {





                            "step": step_idx,





                            "tool": tool_name,





                            "ok": False,





                            "error": f"repeated_tool_call:{repeat_count}",





                        }





                    )





                    messages.append({"role": "assistant", "content": response_text})





                    messages.append(





                        {





                            "role": "user",





                            "content": json.dumps(





                                {





                                    "tool_result": {





                                        "tool": tool_name,





                                        "ok": False,





                                        "error": "同一工具和参数重复过多，请换工具策略或直接 final_answer。",





                                    }





                                },





                                ensure_ascii=False,





                            ),





                        }





                    )





                    if repeat_count >= self.max_same_tool_call + 2:





                        return await self._build_fallback_result(ctx, steps, tool_calls_made, t0, "repeated_tool_call")





                    continue











            ext_sig = ""





            if tool_name in self._EXTERNAL_FACT_TOOLS:





                ext_sig = self._build_external_fact_signature(tool_name, tool_args)





                if ext_sig and ext_sig in seen_external_fact_signatures:





                    steps.append(





                        {





                            "step": step_idx,





                            "tool": tool_name,





                            "ok": False,





                            "error": "duplicate_external_fact_query",





                        }





                    )





                    messages.append({"role": "assistant", "content": response_text})





                    messages.append(





                        {





                            "role": "user",





                            "content": json.dumps(





                                {





                                    "tool_result": {





                                        "tool": tool_name,





                                        "ok": False,





                                        "error": "这个外部查询之前已经成功执行过，请基于已有结果继续。",





                                    }





                                },





                                ensure_ascii=False,





                            ),





                        }





                    )





                    continue











            # 执行工具





            tool_context = {





                "api_call": ctx.api_call,





                "admin_handler": ctx.admin_handler,





                "config_patch_handler": ctx.config_patch_handler,





                "sticker_manager": ctx.sticker_manager,





                "tool_executor": ctx.tool_executor,





                "crawler_hub": ctx.crawler_hub,





                "knowledge_base": ctx.knowledge_base,





                "memory_engine": ctx.memory_engine,





                "conversation_id": ctx.conversation_id,





                "user_id": ctx.user_id,





                "user_name": ctx.user_name,





                "group_id": ctx.group_id,





                "bot_id": ctx.bot_id,





                "is_private": ctx.is_private,





                "trace_id": ctx.trace_id,





                "message_text": ctx.message_text,





                "message_id": ctx.message_id,





                "raw_segments": ctx.raw_segments,





                "reply_media_segments": ctx.reply_media_segments,





                "reply_to_message_id": ctx.reply_to_message_id,





                "reply_to_user_id": ctx.reply_to_user_id,





                "reply_to_user_name": ctx.reply_to_user_name,





                "reply_to_text": ctx.reply_to_text,





                "user_policies": ctx.user_policies,





                "user_directives": ctx.user_directives,





                "is_admin_user": perm_level in ("super_admin", "group_admin"),





                "permission_level": perm_level,





                "config": self.config,





            }





            remaining_for_tool = deadline_ts - time.monotonic()





            if remaining_for_tool <= 3:





                return await self._build_fallback_result(ctx, steps, tool_calls_made, t0, "total_timeout")





            tool_timeout = min(





                self._resolve_tool_timeout_seconds(tool_name, has_media),





                max(4.0, remaining_for_tool - 1.0),





            )





            # 推送"工具调用开始"事件
            await self._push_stream_event(ctx, {
                "type": "tool_call",
                "step": step_idx,
                "data": {
                    "tool": tool_name,
                    "args": tool_args
                }
            })

            try:





                result = await asyncio.wait_for(





                    self.tool_registry.call(tool_name, tool_args, tool_context),





                    timeout=tool_timeout,





                )





            except asyncio.TimeoutError:





                result = SimpleNamespace(





                    ok=False,





                    display=f"{tool_name} 执行超时（>{int(tool_timeout)}s）",





                    error=f"tool_timeout:{tool_name}",





                    data={},





                )





            tool_calls_made += 1





            # 推送"工具结果"事件
            await self._push_stream_event(ctx, {
                "type": "tool_result",
                "step": step_idx,
                "data": {
                    "tool": tool_name,
                    "ok": result.ok,
                    "display": result.display if hasattr(result, "display") else "",
                    "error": result.error if hasattr(result, "error") else ""
                }
            })

            if not result.display and result.error:





                result.display = f"{tool_name} 失败: {result.error}"





            result_tool_name = tool_name











            if not result.ok:





                fallback = self._fallback_tool_on_failure(tool_name, tool_args, result.error)





                if fallback:





                    fb_tool_name, fb_tool_args = fallback





                    _log.info(





                        "agent_tool_fallback_try | trace=%s | step=%d | from=%s | to=%s | args=%s",





                        ctx.trace_id,





                        step_idx,





                        tool_name,





                        fb_tool_name,





                        json.dumps(fb_tool_args, ensure_ascii=False)[:200],





                    )





                    remaining_for_fallback = deadline_ts - time.monotonic()





                    if remaining_for_fallback <= 3:





                        return await self._build_fallback_result(ctx, steps, tool_calls_made, t0, "total_timeout")





                    fb_timeout = min(





                        self._resolve_tool_timeout_seconds(fb_tool_name, has_media),





                        max(4.0, remaining_for_fallback - 1.0),





                    )





                    try:





                        fb_result = await asyncio.wait_for(





                            self.tool_registry.call(fb_tool_name, fb_tool_args, tool_context),





                            timeout=fb_timeout,





                        )





                    except asyncio.TimeoutError:





                        fb_result = SimpleNamespace(





                            ok=False,





                            display=f"{fb_tool_name} 执行超时（>{int(fb_timeout)}s）",





                            error=f"tool_timeout:{fb_tool_name}",





                            data={},





                        )





                    tool_calls_made += 1





                    if not fb_result.display and fb_result.error:





                        fb_result.display = f"{fb_tool_name} 失败: {fb_result.error}"





                    if fb_result.ok:





                        result = fb_result





                        result_tool_name = fb_tool_name





            if result.ok and ext_sig:





                seen_external_fact_signatures.add(ext_sig)





                successful_external_fact_tools += 1











            # 记录 side-effect 发送工具已发送的媒体 URL





            if result.ok and result_tool_name in self._SIDE_EFFECT_SEND_TOOLS:





                # 从工具返回的 data 中提取媒体 URL





                if result.data and isinstance(result.data, dict):





                    for key in ["image_url", "video_url", "audio_url", "url"]:





                        url = normalize_text(str(result.data.get(key, "")))





                        if url:





                            tool_sent_media.add(url)





                    # 处理 image_urls 列表





                    image_urls_list = result.data.get("image_urls", [])





                    if isinstance(image_urls_list, list):





                        for url in image_urls_list:





                            url = normalize_text(str(url))





                            if url:





                                tool_sent_media.add(url)











            compact_data: dict[str, Any] = {}





            if isinstance(result.data, dict) and result.data:





                compact_data = self._compact_data(result.data)











            step_payload = {





                "step": step_idx,





                "tool": result_tool_name,





                "ok": result.ok,





                "display": clip_text(result.display, 300),





                "error": result.error,





            }





            if compact_data:





                step_payload["data"] = compact_data





            steps.append(step_payload)











            _log.info(





                "agent_tool_result | trace=%s | step=%d | tool=%s | ok=%s | display=%s",





                ctx.trace_id, step_idx, result_tool_name, result.ok, clip_text(result.display, 100),





            )











            # 把工具结果喂回 LLM





            tool_result_msg = {





                "tool_result": {





                    "tool": result_tool_name,





                    "ok": result.ok,





                    "display": clip_text(result.display, 800),





                }





            }





            if result.error:





                tool_result_msg["tool_result"]["error"] = result.error





            if compact_data:





                tool_result_msg["tool_result"]["data"] = compact_data











            messages.append({"role": "assistant", "content": response_text})





            messages.append({"role": "user", "content": json.dumps(tool_result_msg, ensure_ascii=False)})











        # 达到 max_steps，用最后的信息兜底





        _log.warning(





            "agent_max_steps | trace=%s | steps=%d | external_fact_ok=%d",





            ctx.trace_id,





            self.max_steps,





            successful_external_fact_tools,





        )





        return await self._build_fallback_result(ctx, steps, tool_calls_made, t0, "max_steps_reached")











    # ── 系统提示词构建 ──











    def _build_sticker_hint(self, ctx: AgentContext) -> str:





        """构建表情包使用提示。"""





        if not ctx.sticker_manager:





            return ""





        face_count = ctx.sticker_manager.face_count





        emoji_count = ctx.sticker_manager.emoji_count





        if face_count == 0 and emoji_count == 0:





            return ""





        hint_parts = []





        if face_count > 0:





            faces = ctx.sticker_manager.face_list_for_prompt()





            if faces:





                hint_parts.append(f"\n\n可用 QQ 经典表情 ({face_count} 个): {faces}")





        if emoji_count > 0:





            hint_parts.append(f"\n可用自定义表情包: {emoji_count} 个 (使用 send_emoji 工具，兼容别名 send_sticker)")





        return "".join(hint_parts) if hint_parts else ""











    def _build_system_prompt(self, ctx: AgentContext) -> str:





        """构建 Agent 系统提示词。"""





        template = _pl.get_dict("agent")











        identity_text = template.get("identity", "")





        output_format_text = template.get("output_format", "")





        rules_text = template.get("rules", "")





        reply_style_text = template.get("reply_style", "")





        tool_usage_text = template.get("tool_usage", "")





        context_rules_text = template.get("context_rules", "")





        network_flow_text = template.get("network_flow", "")











        # 智能工具过滤: 根据用户意图选择相关工具子集





        perm_level = self._resolve_permission_level(ctx)





        selected_tools = self.tool_registry.select_tools_for_intent(





            ctx.message_text, perm_level,





        )





        tool_docs = self.tool_registry.get_schemas_for_prompt_filtered(selected_tools)





        total_tools = self.tool_registry.tool_count





        if len(selected_tools) < total_tools:





            tool_docs += f"\n\n(已根据意图筛选 {len(selected_tools)}/{total_tools} 个工具，如需其他工具请说明)"





        sticker_hint = ""





        if hasattr(ctx, "sticker_manager") and ctx.sticker_manager:





            sticker_hint = self._build_sticker_hint(ctx)











        context_parts = []





        if ctx.memory_context:





            context_parts.append(





                "最近对话:\n"





                + "\n".join(f"- {m}" for m in ctx.memory_context[-self.context_memory_lines_limit:])





            )





        if ctx.related_memories:





            context_parts.append(





                "相关记忆:\n"





                + "\n".join(f"- {m}" for m in ctx.related_memories[:self.context_related_memory_limit])





            )





        if ctx.user_profile_summary:





            context_parts.append(f"用户画像: {clip_text(ctx.user_profile_summary, self.context_profile_max_chars)}")





        if ctx.preferred_name:





            context_parts.append(f"用户偏好称呼: {ctx.preferred_name}")





        if ctx.recent_speakers:





            speaker_rows: list[str] = []





            for uid, name, preview in ctx.recent_speakers[: self.context_speaker_limit]:





                user_label = normalize_text(name)





                if not user_label:





                    user_label = f"用户{uid[-4:]}" if uid else "某人"





                tail = f" 最近说: {clip_text(normalize_text(preview), 60)}" if normalize_text(preview) else ""





                speaker_rows.append(f"- {user_label}{tail}")





            if speaker_rows:





                context_parts.append("最近活跃用户:\n" + "\n".join(speaker_rows))





                context_parts.append(





                    "多人对话规则: 先判断用户在回复谁；出现“他/她/这个人”等指代时，优先结合 @对象、回复锚点和最近活跃用户再作答。"





                )





        if ctx.user_directives:





            context_parts.append("用户专属指令:\n" + "\n".join(f"- {d}" for d in ctx.user_directives[: self.context_directive_limit]))





        context_block = "\n\n".join(context_parts) if context_parts else "(无额外上下文)"











        prompt = (





            f"## 身份\n{identity_text}\n\n"





            f"## 输出格式\n{output_format_text}\n\n"





            f"## 规则\n{rules_text}\n"





        )





        if network_flow_text:





            prompt += f"## 联网任务流程（必须遵守）\n{network_flow_text}\n"





        prompt += (





            f"## 回复风格（极其重要）\n{reply_style_text}\n\n"





            f"## 工具使用\n{tool_usage_text}{sticker_hint}"





            f"## 上下文关联（极其重要）\n{context_rules_text}"





        )





        # 插件注入的规则





        plugin_rules = self.tool_registry.get_prompt_hints_text("rules")





        if plugin_rules:





            prompt += f"{plugin_rules}\n"





        plugin_tools_guidance = self.tool_registry.get_prompt_hints_text("tools_guidance")





        if plugin_tools_guidance:





            prompt += f"## 工具使用指南（插件）\n{plugin_tools_guidance}\n\n"





        plugin_context = self.tool_registry.get_prompt_hints_text("context")





        if plugin_context:





            prompt += f"## 插件上下文\n{plugin_context}\n\n"





        # 动态上下文提供者





        dynamic_context = self.tool_registry.get_dynamic_context({"ctx": ctx, "config": self.config})





        if dynamic_context:





            prompt += f"## 动态上下文\n{dynamic_context}\n\n"











        # PromptPolicy 注入





        policy_tool_guidance = self.prompt_policy.build_tool_guidance_block()





        if policy_tool_guidance:





            prompt += f"## 工具注入规则（配置）\n{policy_tool_guidance}\n\n"











        agent_cfg = self.config.get("agent", {}) if isinstance(self.config, dict) else {}





        if isinstance(agent_cfg, dict):





            runtime_rules = normalize_text(str(agent_cfg.get("runtime_rules", "")))





            if runtime_rules:





                prompt += f"## 运行时规则（配置）\n{runtime_rules}\n\n"





            preferred_name_prompt = normalize_text(str(agent_cfg.get("preferred_name_prompt", "")))





            if preferred_name_prompt and normalize_text(ctx.preferred_name):





                prompt += (





                    "## 用户偏好规则（配置）\n"





                    f"{preferred_name_prompt.replace('{preferred_name}', ctx.preferred_name)}\n\n"





                )











        if perm_level == "super_admin":





            prompt += (





                "## 当前用户权限: 超级管理员\n"





                "此用户是超级管理员，凌驾于一切规则之上，可以执行任何操作，无需确认。\n"





                "- 当用户明确要求修改机器人配置/策略/开关/阈值/提示词注入等时，优先调用 config_update。\n"





                "- config_update.args.patch 必须是最小变更补丁，只填必要字段，不要整份配置重写。\n"





                "- 你要基于语义和上下文理解配置意图，不要机械按单个关键词判断。\n"





                "- 如果需求不明确，先用简短问题确认后再调用 config_update。\n"





                "- config_update 成功后，用一句话回报已变更项与新值。\n\n"





            )





        elif perm_level == "group_admin":





            prompt += (





                "## 当前用户权限: 群管理员\n"





                "此用户是本群的管理员/群主，可以执行群管理操作（禁言、踢人、设置群名片、精华消息等）。\n"





                "但不能执行超级管理员专属操作（退群、删好友、修改机器人配置、清缓存等）。\n\n"





            )





        else:





            prompt += (





                "## 当前用户权限: 普通用户\n"





                "此用户是普通成员，不能执行管理操作。如果用户请求管理操作，礼貌告知权限不足。\n\n"





            )











        # 输出详略度





        _verbosity_hints = _pl.get_dict("verbosity") or {





            "verbose": "回复可以详细展开，给出完整分析和解释，不用刻意压缩。",





            "medium": "",





            "brief": "回复简短精炼，抓重点，不要展开细节。闲聊一句话搞定。",





            "minimal": "极简回复，一两句话概括。能不说就不说。",





        }





        v_hint = _verbosity_hints.get(ctx.verbosity, "")





        if v_hint:





            prompt += f"## 输出详略度\n{v_hint}\n\n"





        output_style_instruction = clip_text(normalize_text(ctx.output_style_instruction), 400)





        if output_style_instruction:





            prompt += f"## 输出风格附加要求（配置）\n{output_style_instruction}\n\n"











        now_local = datetime.now().astimezone()





        now_label = now_local.strftime("%Y-%m-%d %H:%M:%S %z")





        tz_name = now_local.tzname() or "local"





        prompt += (





            f"## 环境\n"





            f"{'私聊' if ctx.is_private else f'群聊 {ctx.group_id}'} | "





            f"用户: {ctx.user_name}(QQ:{ctx.user_id}) | @我: {ctx.mentioned} | 当前时间: {now_label} ({tz_name})\n\n"





            f"## 上下文\n{context_block}\n\n"





            f"## 可用工具\n{tool_docs}"





        )





        return self.prompt_policy.compose_prompt(channel="agent", base_prompt=prompt)











    @staticmethod





    def _render_runtime_tpl(template_text: str, values: dict[str, Any]) -> str:





        """安全渲染模板：缺失占位符不抛错，保留原样。"""





        class _SafeMap(dict):





            def __missing__(self, key: str) -> str:  # type: ignore[override]





                return "{" + key + "}"











        text = str(template_text or "")





        if not text:





            return ""





        try:





            return text.format_map(_SafeMap(values))





        except Exception:





            return text











    @staticmethod





    def _runtime_tpl(runtime_templates: dict[str, str], key: str, default: str) -> str:





        """读取 agent_runtime 模板；若用户显式配置空字符串则视为关闭该行。"""





        if key in runtime_templates:





            return str(runtime_templates.get(key, ""))





        return default











    def _build_user_message(self, ctx: AgentContext) -> str:





        """构建用户消息。"""

        # 根据 context_retention 动态调整 reply_to_text 长度
        reply_text_max_len = {
            "large": 1500,     # 很大：保留最多上下文
            "medium": 800,     # 中等：平衡
            "minimal": 300,    # 很少：最少上下文
        }.get(self.context_retention, 800)

        runtime_templates = _pl.get_dict("agent_runtime")





        rebuilt_query = self._rebuild_query_with_context(ctx.message_text, ctx)





        force_image_tool_first = self._should_force_image_tool_first(ctx)





        parts = [ctx.message_text]





        if rebuilt_query and rebuilt_query != normalize_text(ctx.message_text):





            parts.append(f"[语境补全: {rebuilt_query}]")











        # @提及的其他用户（非 bot 自身）





        if ctx.at_other_user_ids:





            at_descs = []





            for uid in ctx.at_other_user_ids:





                name = ctx.at_other_user_names.get(uid, "")





                at_descs.append(f"{name}(QQ:{uid})" if name else f"QQ:{uid}")





            parts.append(f"[用户@了: {', '.join(at_descs)}]")











        # 引用/回复消息上下文





        reply_mid = normalize_text(ctx.reply_to_message_id)





        reply_uid = normalize_text(str(ctx.reply_to_user_id))





        reply_name = normalize_text(ctx.reply_to_user_name)





        reply_text = normalize_text(ctx.reply_to_text)





        if reply_mid or reply_uid or reply_text:





            is_reply_to_bot = bool(reply_uid and reply_uid == str(ctx.bot_id))





            anchor_lines = [





                self._runtime_tpl(runtime_templates, "reply_anchor_header", "[引用锚点]"),





                self._render_runtime_tpl(





                    self._runtime_tpl(runtime_templates, "reply_anchor_line_message_id", "reply_to_message_id={reply_to_message_id}"),





                    {"reply_to_message_id": reply_mid or "-"},





                ),





                self._render_runtime_tpl(





                    self._runtime_tpl(runtime_templates, "reply_anchor_line_user_id", "reply_to_user_id={reply_to_user_id}"),





                    {"reply_to_user_id": reply_uid or "-"},





                ),





                self._render_runtime_tpl(





                    self._runtime_tpl(runtime_templates, "reply_anchor_line_user_name", "reply_to_user_name={reply_to_user_name}"),





                    {"reply_to_user_name": reply_name or "-"},





                ),





                self._render_runtime_tpl(





                    self._runtime_tpl(runtime_templates, "reply_anchor_line_is_reply_to_bot", "is_reply_to_bot={is_reply_to_bot}"),





                    {"is_reply_to_bot": "true" if is_reply_to_bot else "false"},





                ),





            ]





            if reply_text:





                line = self._render_runtime_tpl(





                    self._runtime_tpl(runtime_templates, "reply_anchor_line_text", "reply_to_text={reply_to_text}"),





                    {"reply_to_text": clip_text(reply_text, 240)},





                )





                if normalize_text(line):





                    anchor_lines.append(line)





            if ctx.reply_media_summary:





                line = self._render_runtime_tpl(





                    self._runtime_tpl(runtime_templates, "reply_anchor_line_media", "reply_to_media={reply_to_media}"),





                    {"reply_to_media": ", ".join(ctx.reply_media_summary[:5])},





                )





                if normalize_text(line):





                    anchor_lines.append(line)





            anchor_lines = [line for line in anchor_lines if normalize_text(line)]





            if anchor_lines:





                parts.append("\n".join(anchor_lines))











        if normalize_text(ctx.reply_to_text):





            reply_from = normalize_text(ctx.reply_to_user_name) or normalize_text(ctx.reply_to_user_id) or "未知用户"





            is_reply_to_bot = reply_uid == str(ctx.bot_id)





            if is_reply_to_bot:





                line = self._render_runtime_tpl(





                    self._runtime_tpl(





                        runtime_templates,





                        "reply_context_to_bot",





                        "[用户在回复bot之前的消息 | bot原文: {reply_to_text}]",





                    ),





                    {"reply_to_text": clip_text(normalize_text(ctx.reply_to_text), reply_text_max_len)},





                )





            else:





                line = self._render_runtime_tpl(





                    self._runtime_tpl(





                        runtime_templates,





                        "reply_context_to_user",





                        "[用户在回复: {reply_from}(QQ:{reply_to_user_id}) | 原文: {reply_to_text}]",





                    ),





                    {





                        "reply_from": reply_from,





                        "reply_to_user_id": reply_uid or "-",





                        "reply_to_text": clip_text(normalize_text(ctx.reply_to_text), 220),





                    },





                )





            if normalize_text(line):





                parts.append(line)











        if ctx.media_summary:





            image_count = sum(1 for m in ctx.media_summary if m.startswith("image:"))





            video_count = sum(1 for m in ctx.media_summary if m.startswith("video:"))





            voice_count = sum(1 for m in ctx.media_summary if m.startswith("record") or m.startswith("audio"))





            media_desc = ", ".join(ctx.media_summary[:5])





            media_line = self._render_runtime_tpl(





                self._runtime_tpl(runtime_templates, "attached_media_line", "[附带媒体: {media_desc}]"),





                {





                    "media_desc": media_desc,





                    "image_count": image_count,





                    "video_count": video_count,





                    "voice_count": voice_count,





                },





            )





            if normalize_text(media_line):





                parts.append(media_line)





            if image_count and force_image_tool_first:





                line = self._render_runtime_tpl(





                    self._runtime_tpl(





                        runtime_templates,





                        "hint_user_images",





                        "[提示: 用户发了{image_count}张图片并提问，请用 analyze_image 工具分析]",





                    ),





                    {"image_count": image_count},





                )





                if normalize_text(line):





                    parts.append(line)





            if voice_count:





                line = self._render_runtime_tpl(





                    self._runtime_tpl(





                        runtime_templates,





                        "hint_user_voice",





                        "[提示: 用户发了语音消息，请用 analyze_voice 工具转录]",





                    ),





                    {"voice_count": voice_count},





                )





                if normalize_text(line):





                    parts.append(line)





        if ctx.reply_media_summary:





            reply_image_count = sum(1 for m in ctx.reply_media_summary if m.startswith("image:"))





            reply_voice_count = sum(1 for m in ctx.reply_media_summary if m.startswith("record") or m.startswith("audio"))





            reply_media_line = self._render_runtime_tpl(





                self._runtime_tpl(runtime_templates, "reply_media_line", "[引用消息中的媒体: {reply_media_desc}]"),





                {





                    "reply_media_desc": ", ".join(ctx.reply_media_summary[:5]),





                    "reply_image_count": reply_image_count,





                    "reply_voice_count": reply_voice_count,





                },





            )





            if normalize_text(reply_media_line):





                parts.append(reply_media_line)





            if reply_image_count and force_image_tool_first:





                line = self._render_runtime_tpl(





                    self._runtime_tpl(





                        runtime_templates,





                        "hint_reply_images",





                        "[提示: 用户回复了一条含{reply_image_count}张图片的消息并提问，请用 analyze_image 工具分析]",





                    ),





                    {"reply_image_count": reply_image_count},





                )





                if normalize_text(line):





                    parts.append(line)





            if reply_voice_count:





                line = self._render_runtime_tpl(





                    self._runtime_tpl(





                        runtime_templates,





                        "hint_reply_voice",





                        "[提示: 引用消息含语音，请用 analyze_voice 工具转录]",





                    ),





                    {"reply_voice_count": reply_voice_count},





                )





                if normalize_text(line):





                    parts.append(line)





        return "\n".join(parts)











    def _normalize_tool_args(self, tool_name: str, args: dict[str, Any], ctx: AgentContext) -> dict[str, Any]:





        """?????????????? args={} ???????"""





        fixed = dict(args or {})





        # ?????????? cue ?????? AI ??????????





        if not tool_name:





            return fixed





        text = normalize_text(ctx.message_text)





        if not text and not normalize_text(ctx.reply_to_text) and not ctx.memory_context:





            return fixed





        contextual_query = self._rebuild_query_with_context(text, ctx)





        first_url = self._extract_first_url(text)





        reply_url = self._extract_first_url(normalize_text(ctx.reply_to_text))





        candidate_url = first_url or reply_url





        if not candidate_url and self._looks_like_reference_to_previous_link(text):





            candidate_url = self._extract_recent_url(ctx)





        qq_id = self._extract_candidate_qq_id(ctx)











        def _set_if_empty(key: str, value: Any) -> None:





            if value is None:





                return





            cur = fixed.get(key)





            if cur is None:





                fixed[key] = value





                return





            if isinstance(cur, str) and not normalize_text(cur):





                fixed[key] = value





                return





            if isinstance(cur, (int, float)) and cur == 0:





                fixed[key] = value











        if tool_name == "web_search":





            _set_if_empty("query", contextual_query or text)





            mode = normalize_text(str(fixed.get("mode", ""))).lower()





            if not mode:





                _set_if_empty("mode", self._infer_search_mode(contextual_query or text))





        elif tool_name in {"lookup_wiki"}:





            _set_if_empty("keyword", self._infer_lookup_keyword(contextual_query or text))





        elif tool_name == "split_video":





            _set_if_empty("url", candidate_url)





            inferred_mode = self._infer_split_video_mode(contextual_query or text)





            if inferred_mode:





                _set_if_empty("mode", inferred_mode)





            time_hints = self._infer_video_time_hints(contextual_query or text)





            mode_now = normalize_text(str(fixed.get("mode", inferred_mode))).lower()





            if mode_now in {"clip", "audio"}:





                if time_hints.get("start") is not None:





                    _set_if_empty("start_seconds", time_hints.get("start"))





                if time_hints.get("end") is not None:





                    _set_if_empty("end_seconds", time_hints.get("end"))





            elif mode_now == "cover":





                if time_hints.get("point") is not None:





                    _set_if_empty("frame_time_seconds", time_hints.get("point"))





            elif mode_now == "frames":





                frame_hint = self._infer_frame_count_hint(contextual_query or text)





                if frame_hint > 0:





                    _set_if_empty("max_frames", frame_hint)





        elif tool_name in {"parse_video", "analyze_video", "fetch_webpage", "download_file", "smart_download"}:





            _set_if_empty("url", candidate_url)





            if tool_name in {"download_file", "smart_download"}:





                _set_if_empty("query", contextual_query or text)





                _set_if_empty("kind", "auto")





                if self._looks_like_file_send_request(contextual_query or text):





                    _set_if_empty("upload", True)





                    _set_if_empty("group_id", int(ctx.group_id or 0))





                inferred_ext = self._infer_resource_file_type(contextual_query or text)





                if inferred_ext:





                    _set_if_empty("prefer_ext", inferred_ext)





        elif tool_name in {"github_search", "douyin_search", "search_knowledge"}:





            _set_if_empty("query", contextual_query or text)





        elif tool_name == "search_web_media":





            _set_if_empty("query", contextual_query or text)





            _set_if_empty("media_type", self._infer_media_type(contextual_query or text))





        elif tool_name == "search_download_resources":





            _set_if_empty("query", contextual_query or text)





            _set_if_empty("file_type", self._infer_resource_file_type(contextual_query or text))





        elif tool_name == "cli_invoke":





            _set_if_empty("prompt", text)





        elif tool_name == "get_user_info":





            if qq_id:





                existing = self._to_safe_int(fixed.get("user_id"))





                if existing and existing != qq_id:





                    _log.info(





                        "agent_tool_arg_override | trace=%s | tool=%s | field=user_id | old=%s | new=%s",





                        ctx.trace_id,





                        tool_name,





                        existing,





                        qq_id,





                    )





                fixed["user_id"] = qq_id





            else:





                _set_if_empty("user_id", qq_id)





        elif tool_name == "get_message":





            reply_mid = self._to_safe_int(ctx.reply_to_message_id)





            if reply_mid:





                _set_if_empty("message_id", reply_mid)





        elif tool_name == "recall_recent_messages":





            _set_if_empty("group_id", int(ctx.group_id or 0))





            if qq_id:





                _set_if_empty("user_id", qq_id)





        elif tool_name == "get_qq_avatar":





            if qq_id:





                _set_if_empty("qq", str(qq_id))





        elif tool_name in {"get_qzone_profile", "get_qzone_moods", "get_qzone_albums", "analyze_qzone", "get_qzone_photos"}:





            if qq_id:





                _set_if_empty("qq_number", str(qq_id))





        elif tool_name in {"send_emoji", "send_sticker"}:





            _set_if_empty("query", self._infer_emoji_query(contextual_query or text))











        return fixed











    @staticmethod





    def _missing_required_tool_args(tool_name: str, args: dict[str, Any]) -> list[str]:





        """仅对高频失败工具做必填校验，避免空调用。"""





        required: dict[str, list[str]] = {





            "web_search": ["query"],





            "lookup_wiki": ["keyword"],





            "parse_video": ["url"],





            "analyze_video": ["url"],





            "fetch_webpage": ["url"],





            "download_file": ["url"],





            "smart_download": ["url"],





            "github_search": ["query"],





            "douyin_search": ["query"],





            "search_knowledge": ["query"],





            "search_web_media": ["query"],





            "search_download_resources": ["query"],





            "cli_invoke": ["prompt"],





            "config_update": ["patch"],





            "get_user_info": ["user_id"],





            "get_message": ["message_id"],





            "recall_recent_messages": ["group_id", "user_id", "within_minutes"],





            "get_qzone_profile": ["qq_number"],





            "get_qzone_moods": ["qq_number"],





            "get_qzone_albums": ["qq_number"],





            "analyze_qzone": ["qq_number"],





            "get_qzone_photos": ["qq_number", "album_id"],





        }





        fields = required.get(tool_name, [])





        missing: list[str] = []





        for field in fields:





            val = args.get(field)





            if val is None:





                missing.append(field)





                continue





            if isinstance(val, str) and not normalize_text(val):





                missing.append(field)





                continue





            if isinstance(val, (int, float)) and val == 0:





                missing.append(field)





                continue





            if isinstance(val, (list, dict)) and not val:





                missing.append(field)





        return missing











    @staticmethod





    def _extract_first_url(text: str) -> str:





        m = re.search(r"https?://[^\s<>\"]+", text or "", flags=re.IGNORECASE)





        if not m:





            return ""





        return m.group(0).strip().rstrip(").,，。!?！？")











    @staticmethod





    @staticmethod



    @staticmethod

    def _looks_like_reference_to_previous_link(text: str) -> bool:

        content = normalize_text(text).lower()

        if not content or len(content) > 64:

            return False

        if re.search(r"https?://|\b(?:bv[a-z0-9]{10}|av\d{4,})\b", content, flags=re.IGNORECASE):

            return False

        if re.fullmatch(r"[\u003f\uff1f\u0021\uff01\u3002\u002e\u002c\uff0c\u003a\uff1a\u003b\uff1b\u007e\uff5e]+", content):

            return True

        if re.search(r"(?<![a-z0-9_])/(?:source|sources|open|lookup|readme|retry)(?![a-z0-9_])", content):

            return True

        return bool(re.search(r"\b(?:mode|type|target)\s*=\s*(?:source|url|link|readme|lookup)\b", content))



    def _extract_recent_url(self, ctx: AgentContext) -> str:





        candidates = [normalize_text(ctx.reply_to_text)]





        candidates.extend(normalize_text(line) for line in reversed(ctx.memory_context[-16:]))





        candidates.extend(normalize_text(line) for line in reversed(ctx.related_memories[-8:]))





        for line in candidates:





            url = self._extract_first_url(line)





            if url:





                return url





        return ""











    @staticmethod





    def _to_safe_int(value: Any) -> int:





        if isinstance(value, bool):





            return 0





        if isinstance(value, int):





            return value





        text = normalize_text(str(value))





        if not text or not re.fullmatch(r"-?\d+", text):





            return 0





        try:





            return int(text)





        except ValueError:





            return 0











    def _extract_candidate_qq_id(self, ctx: AgentContext) -> int:





        # 1) ??????? @ ??????? bot ???





        for seg in ctx.raw_segments:





            if not isinstance(seg, dict):





                continue





            if normalize_text(str(seg.get("type", ""))).lower() != "at":





                continue





            data = seg.get("data", {})





            if not isinstance(data, dict):





                continue





            qq = normalize_text(str(data.get("qq", "")))





            if not qq or qq == str(ctx.bot_id):





                continue





            if re.fullmatch(r"[1-9]\d{5,11}", qq):





                return int(qq)











        # 2) ??? reply ????????





        reply_uid = normalize_text(str(ctx.reply_to_user_id))





        if reply_uid and reply_uid != str(ctx.bot_id) and re.fullmatch(r"[1-9]\d{5,11}", reply_uid):





            return int(reply_uid)











        # 3) ????????????????????





        text = normalize_text(ctx.message_text)





        m = re.search(r"(?<!\d)([1-9]\d{5,11})(?!\d)", text)





        if m:





            try:





                return int(m.group(1))





            except ValueError:





                pass





        return 0











    @staticmethod





    def _infer_lookup_keyword(text: str) -> str:





        return normalize_text(text)[:80]











    @staticmethod





    @staticmethod



    @staticmethod

    def _infer_search_mode(text: str) -> str:

        content = normalize_text(text).lower()

        if not content:

            return ""

        if re.search(r"(?<![a-z0-9_])/(?:image|img)(?![a-z0-9_])", content) or re.search(r"(?:mode|type|media)\s*=\s*image", content):

            return "image"

        if re.search(r"(?<![a-z0-9_])/(?:video)(?![a-z0-9_])", content) or re.search(r"(?:mode|type|media)\s*=\s*video", content):

            return "video"

        if re.search(r"https?://[^\s]+", content):

            return "video"

        if re.search(r"\b(?:bv[a-z0-9]{10}|av\d{4,})\b", content, flags=re.IGNORECASE):

            return "video"

        if re.search(r"\.(?:png|jpe?g|gif|webp|bmp)\b", content, flags=re.IGNORECASE):

            return "image"

        if re.search(r"\.(?:mp4|webm|mov|m4v)\b", content, flags=re.IGNORECASE):

            return "video"

        return "text"



    @staticmethod

    @staticmethod
    def _infer_media_type(text: str) -> str:
        content = normalize_text(text).lower()
        if not content:
            return ""
        if re.search(r"(?<![a-z0-9_])/(?:gif)(?![a-z0-9_])", content) or re.search(r"\b(?:type|media)\s*=\s*gif\b", content) or re.search(r"\.gif\b", content):
            return "gif"
        search_mode = AgentLoop._infer_search_mode(content)
        if search_mode in {"video", "image"}:
            return search_mode
        return ""

    def _infer_resource_file_type(text: str) -> str:

        content = normalize_text(text).lower()

        if not content:

            return ""

        explicit = re.search(r"\b(?:prefer_ext|file_type|type)\s*=\s*([a-z0-9]{2,8})\b", content)

        if explicit:

            return explicit.group(1)

        for ext in ("apk", "ipa", "exe", "msi", "zip", "7z", "rar", "pdf", "docx", "xlsx", "pptx", "txt", "sh", "bat", "mp3", "mp4"):

            if re.search(rf"\.{ext}\b|\b{ext}\b", content, flags=re.IGNORECASE):

                return ext

        return ""



    @staticmethod

    def _infer_split_video_mode(text: str) -> str:

        content = normalize_text(text).lower()

        if not content:

            return ""

        explicit = re.search(r"\bmode\s*=\s*(audio|cover|frames|clip)\b", content)

        if explicit:

            return explicit.group(1)

        slash = re.search(r"(?<![a-z0-9_])/(audio|cover|frames|clip)(?![a-z0-9_])", content)

        if slash:

            return slash.group(1)

        if re.search(r"(?:\d{1,2}:\d{1,2}(?::\d{1,2})?|\d+(?:\.\d+)?\s*s?)\s*(?:-|~|to)\s*(?:\d{1,2}:\d{1,2}(?::\d{1,2})?|\d+(?:\.\d+)?\s*s?)", content, flags=re.IGNORECASE):

            return "clip"

        if re.search(r"\b(?:frames?|screenshots?)\b", content, flags=re.IGNORECASE):

            return "frames"

        return ""



    def _parse_time_token_to_seconds(token: str) -> float | None:





        raw = normalize_text(token).lower()





        if not raw:





            return None





        clock = re.fullmatch(r"(\d{1,2}):(\d{1,2})(?::(\d{1,2}))?", raw)





        if clock:





            h_or_m = int(clock.group(1))





            m_or_s = int(clock.group(2))





            sec_part = clock.group(3)





            if sec_part is None:





                return float(max(0, h_or_m * 60 + m_or_s))





            return float(max(0, h_or_m * 3600 + m_or_s * 60 + int(sec_part)))





        second = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(?:s|\u79d2)?", raw)





        if second:





            try:





                return max(0.0, float(second.group(1)))





            except ValueError:





                return None





        return None











    @classmethod





    @classmethod



    @classmethod

    def _infer_video_time_hints(cls, text: str) -> dict[str, float]:

        content = normalize_text(text).lower()

        if not content:

            return {}

        matched = re.search(

            r"(\d{1,2}:\d{1,2}(?::\d{1,2})?|\d+(?:\.\d+)?\s*s?)\s*(?:-|~|to)\s*(\d{1,2}:\d{1,2}(?::\d{1,2})?|\d+(?:\.\d+)?\s*s?)",

            content,

            flags=re.IGNORECASE,

        )

        if matched:

            start = cls._parse_time_token_to_seconds(matched.group(1))

            end = cls._parse_time_token_to_seconds(matched.group(2))

            if start is not None and end is not None and end >= start:

                return {"start": start, "end": end}

        for matched in re.finditer(r"\d{1,2}:\d{1,2}(?::\d{1,2})?|\d+(?:\.\d+)?\s*s?", content, flags=re.IGNORECASE):

            point = cls._parse_time_token_to_seconds(matched.group(0))

            if point is not None:

                return {"point": point}

        return {}



    @staticmethod

    def _infer_frame_count_hint(text: str) -> int:

        content = normalize_text(text).lower()

        if not content:

            return 0

        explicit = re.search(r"\b(?:frame_count|max_frames)\s*=\s*(\d{1,2})\b", content)

        if explicit:

            return max(1, min(36, int(explicit.group(1))))

        matched = re.search(r"(?<!\d)(\d{1,2})\s*(?:frames?|screenshots?)\b", content, flags=re.IGNORECASE)

        if not matched:

            return 0

        try:

            return max(1, min(36, int(matched.group(1))))

        except ValueError:

            return 0



    def _rebuild_query_with_context(self, text: str, ctx: AgentContext) -> str:





        content = normalize_text(text)





        if not content:





            return self._extract_recent_topic(ctx, "")





        rebuilt = content





        if self._looks_like_reference_to_previous_link(content):





            recent_url = self._extract_first_url(normalize_text(ctx.reply_to_text)) or self._extract_recent_url(ctx)





            if recent_url and recent_url not in rebuilt:





                rebuilt = f"{rebuilt} {recent_url}".strip()





        if not self._is_context_continuation_phrase(content):





            return rebuilt





        topic = self._extract_recent_topic(ctx, content)





        stripped = self._strip_continuation_prefix(content)





        if topic and stripped and topic not in stripped:





            return f"{topic} {stripped}".strip()





        return topic or stripped or rebuilt











    @staticmethod





    @staticmethod



    @staticmethod

    def _is_context_continuation_phrase(text: str) -> bool:

        content = normalize_text(text).lower()

        if not content or len(content) > 48:

            return False

        compact = re.sub(r"\s+", "", content)

        if not compact:

            return False

        if re.fullmatch(r"[\u003f\uff1f\u0021\uff01\u3002\u002e\u002c\uff0c\u003a\uff1a\u003b\uff1b\u007e\uff5e\-_=/\\+]+", compact):

            return True

        if re.search(r"(?<![a-z0-9_])/(?:next|continue|more|retry|again)(?![a-z0-9_])", content):

            return True

        return bool(re.search(r"\b(?:mode|step|page)\s*=\s*(?:next|continue|more|retry|again)\b", content))



    @staticmethod

    def _strip_continuation_prefix(text: str) -> str:

        content = normalize_text(text)

        if not content:

            return ""

        cleaned = re.sub(

            r"^(?:/(?:next|continue|more|retry|again)\b\s*|(?:mode|step|page)\s*=\s*(?:next|continue|more|retry|again)\b\s*)",

            "",

            content,

            flags=re.IGNORECASE,

        ).strip()

        return cleaned or content



    def _extract_recent_topic(self, ctx: AgentContext, current_text: str) -> str:





        current_norm = normalize_text(current_text).lower()





        candidates = [normalize_text(ctx.reply_to_text)]





        candidates.extend(normalize_text(line) for line in reversed(ctx.memory_context[-12:]))





        candidates.extend(normalize_text(line) for line in reversed(ctx.related_memories[-6:]))





        want_url = self._looks_like_reference_to_previous_link(current_norm)





        for item in candidates:





            if not item:





                continue





            if want_url:





                url = self._extract_first_url(item)





                if url:





                    return url





            topic = re.sub(r"https?://[^\s<>\"]+", " ", item, flags=re.IGNORECASE)





            topic = re.sub(r"\s+", " ", topic).strip(" -:|[]()")





            topic = self._extract_topic_from_reply_text(topic)





            if not topic:





                continue





            if current_norm and topic.lower() == current_norm:





                continue





            return topic





        return ""











    @staticmethod





    def _extract_topic_from_reply_text(reply_text: str) -> str:





        return clip_text(normalize_text(reply_text), 100)











    @staticmethod





    @staticmethod



    def _fallback_tool_on_failure(tool_name: str, args: dict[str, Any], error: str = "") -> tuple[str, dict[str, Any]] | None:



        query = normalize_text(str(args.get("query", "")))



        keyword = normalize_text(str(args.get("keyword", "")))



        title = normalize_text(str(args.get("title", "")))



        artist = normalize_text(str(args.get("artist", "")))



        song_name = normalize_text(str(args.get("song_name", "")))



        err = normalize_text(error).lower()



        music_query = query or keyword or f"{title} {artist}".strip() or f"{song_name} {artist}".strip()



        if tool_name in {"music_play", "music_play_by_id"} and music_query:



            recoverable_music_errors = (



                "tool_timeout:",



                "artist_mismatch",



                "preview_only",



                "no_url",



                "download_failed",



                "voice_prepare_failed",



                "play_failed",



            )



            if any(token in err for token in recoverable_music_errors):



                return "bilibili_audio_extract", {"keyword": music_query}



        if tool_name in {"smart_download", "download_file"} and err.startswith("download_untrusted_source"):



            if query:



                return "web_search", {"query": query, "mode": "text"}



            url = normalize_text(str(args.get("url", "")))



            if url:



                return "web_search", {"query": url, "mode": "text"}



        return None







    def _resolve_tool_timeout_seconds(self, tool_name: str, has_media: bool) -> float:





        resolver_budget_tools = {





            "download_file",





            "smart_download",





            "music_play",





            "music_play_by_id",





            "bilibili_audio_extract",





        }





        if tool_name in resolver_budget_tools:





            return float(self.media_resolver_timeout_seconds)





        heavy_tools = {





            "parse_video",





            "analyze_video",





            "split_video",





            "fetch_webpage",





            "download_file",





            "smart_download",





            "analyze_image",





            "scrape_extract",





            "extract_structured",





            "extract_links_and_content",





            "music_play",





            "music_play_by_id",





            "bilibili_audio_extract",





        }





        if has_media or tool_name in heavy_tools:





            return float(self.tool_timeout_seconds_media)





        return float(self.tool_timeout_seconds)











    def estimate_total_timeout_seconds(self, ctx: AgentContext, has_media: bool) -> float:





        """公开给外层编排器使用的超时预算估算。"""





        return self._resolve_total_timeout_seconds(ctx, has_media)











    def _resolve_total_timeout_seconds(self, ctx: AgentContext, has_media: bool) -> float:





        per_step_timeout = 35 if has_media else 30





        total_timeout = float(max(12, self.max_steps * per_step_timeout))





        if self.total_timeout_seconds > 0:





            total_timeout = min(total_timeout, float(self.total_timeout_seconds))











        queue_cfg = self.config.get("queue", {}) if isinstance(self.config, dict) else {}





        if isinstance(queue_cfg, dict):





            queue_timeout = self._to_safe_int(queue_cfg.get("process_timeout_seconds"))





            video_override = self._to_safe_int(queue_cfg.get("video_process_timeout_seconds"))





            download_override = self._to_safe_int(queue_cfg.get("download_process_timeout_seconds"))





            if has_media:





                queue_timeout = max(queue_timeout, video_override)





            if download_override > 0:





                queue_timeout = max(queue_timeout, download_override)











            if queue_timeout > 0:





                queue_budget = max(15, queue_timeout - self.queue_timeout_margin_seconds)





                total_timeout = min(total_timeout, float(queue_budget))











        return max(12.0, total_timeout)











    @staticmethod





    def _build_external_fact_signature(tool_name: str, args: dict[str, Any]) -> str:





        if not isinstance(args, dict):





            return ""





        fields = ["query", "url", "repo", "instruction", "schema_desc", "mode", "keyword", "media_type"]





        parts = [tool_name]





        for key in fields:





            value = normalize_text(str(args.get(key, ""))).lower()





            if value:





                parts.append(f"{key}={clip_text(value, 180)}")





        return "|".join(parts) if len(parts) > 1 else ""











    @staticmethod





    def _infer_emoji_query(text: str) -> str:





        t = normalize_text(text)





        if not t:





            return "随机"





        return t[:40]











    @staticmethod





    @staticmethod



    @staticmethod

    def _is_explicit_emoji_request(text: str) -> bool:

        content = normalize_text(text).lower()

        if not content:

            return False

        if re.search(r"(?<![a-z0-9_])/(?:emoji|sticker|meme)(?![a-z0-9_])", content):

            return True

        return bool(re.search(r"\b(?:mode|type|output)\s*=\s*(?:emoji|sticker|meme)\b", content))



    def _looks_like_choice_followup(self, text: str) -> bool:





        content = normalize_text(text).lower()





        if not content:





            return False





        if re.search(r"^(?:?)?\s*[1-9]\d?\s*(?:?|?|?|?|?|?|?|?|?)?$", content):





            return True





        return bool(re.search(r"(?<![a-z0-9_])/(?:next|prev|resend|retry)(?![a-z0-9_])", content))











    @staticmethod





    def _looks_like_file_send_request(text: str) -> bool:





        content = normalize_text(text).lower()





        if not content:





            return False





        has_ext = bool(re.search(r"\.(?:zip|7z|rar|exe|apk|ipa|msi|pdf|docx?|xlsx?|pptx?|txt|mp3|mp4)\b", content, flags=re.IGNORECASE))





        has_send_token = bool(





            re.search(r"(?<![a-z0-9_])/(?:upload|sendfile)(?![a-z0-9_])", content)





            or re.search(r"\b(?:mode|output|upload)\s*=\s*(?:send|file|true|1)\b", content)





        )





        return has_send_token and has_ext











    def _looks_like_profile_analysis_request(self, text: str) -> bool:





        content = normalize_text(text).lower()





        if not content:





            return False





        if re.search(r"(?<![a-z0-9_])/(?:profile|avatar|qzone)(?![a-z0-9_])", content):





            return True





        if re.search(r"\b(?:mode|type|target)\s*=\s*(?:profile|avatar|qzone)\b", content):





            return True





        return "?" in content or "?" in content











    def _should_force_tool_first(self, ctx: AgentContext) -> bool:





        """??????????????????"""





        text = normalize_text(ctx.message_text).lower()





        if not text and not ctx.media_summary and not ctx.reply_media_summary:





            return False











        if re.search(r"https?://", text):





            return True











        if (ctx.media_summary or ctx.reply_media_summary) and self._looks_like_image_question(text):





            return True











        if re.search(r"^[!/](?:search|lookup|download|readme|github|avatar)\b", text, flags=re.IGNORECASE):





            return True





        if re.search(r"\b(?:mode|type|output|platform|source)\s*=\s*[a-z0-9_.:-]+", text, flags=re.IGNORECASE):





            return True











        target_entity_exists = bool(self._extract_candidate_qq_id(ctx)) or bool(ctx.at_other_user_ids) or bool(





            normalize_text(str(ctx.reply_to_user_id))





        )





        if target_entity_exists and self._looks_like_profile_analysis_request(text):





            return True











        if re.search(r"\b(?:bv[a-z0-9]{10}|av\d{4,})\b", text, flags=re.IGNORECASE):





            return True





        return False











    def _should_force_image_tool_first(self, ctx: AgentContext) -> bool:





        text = normalize_text(ctx.message_text).lower()





        has_image = (





            any(item.startswith("image:") for item in (ctx.media_summary or []))





            or any(item.startswith("image:") for item in (ctx.reply_media_summary or []))





            or any(





                normalize_text(str((seg or {}).get("type", ""))).lower() == "image"





                for seg in (ctx.raw_segments or [])





                if isinstance(seg, dict)





            )





            or any(





                normalize_text(str((seg or {}).get("type", ""))).lower() == "image"





                for seg in (ctx.reply_media_segments or [])





                if isinstance(seg, dict)





            )





        )





        if not has_image or not text:





            return False





        return self._looks_like_image_question(text)











    @staticmethod





    def _looks_like_image_question(text: str) -> bool:





        content = normalize_text(text).lower()





        if not content:





            return False





        if re.search(r"(?<![a-z0-9_])/(?:analyze|image|img|ocr)(?![a-z0-9_])", content):





            return True





        if re.search(r"\b(?:mode|type|output)\s*=\s*(?:analyze|image|ocr)\b", content):





            return True





        return "?" in content or "?" in content











    def _parse_llm_output(self, text: str) -> dict[str, Any] | None:





        """解析 LLM 输出为 tool_call dict，失败返回 None。"""





        clean = text.strip()











        # 先剥离 <thinking>...</thinking> 块（LLM 可能在 tool call 前输出思考）





        clean = re.sub(r"<thinking>.*?</thinking>", "", clean, flags=re.DOTALL | re.IGNORECASE)





        clean = re.sub(r"</?thinking>", "", clean, flags=re.IGNORECASE)





        # 剥离 <tool_call>...</tool_call> 包裹（保留内部 JSON）





        clean = re.sub(r"</?tool_call>", "", clean, flags=re.IGNORECASE)











        # 兼容 <tool_use> tool_name {"arg":"val"} </tool_use> 格式





        tool_use_match = re.search(





            r"<tool_use>\s*(\w+)\s*(\{.*?\})\s*</tool_use>",





            clean, flags=re.DOTALL | re.IGNORECASE,





        )





        if tool_use_match:





            tool_name = tool_use_match.group(1).strip()





            try:





                tool_args = json.loads(tool_use_match.group(2))





                if isinstance(tool_args, dict) and tool_name:





                    return {"tool": tool_name, "args": tool_args}





            except (json.JSONDecodeError, ValueError):





                pass





        # 剥离残留的 <tool_use> 标签





        clean = re.sub(r"</?tool_use>", "", clean, flags=re.IGNORECASE)











        # 兼容 [tool_use: tool_name] key: value 格式





        bracket_match = re.search(





            r"\[tool_use:\s*(\w+)\]\s*(.*)",





            clean, flags=re.DOTALL | re.IGNORECASE,





        )





        if bracket_match:





            tool_name = bracket_match.group(1).strip()





            rest = bracket_match.group(2).strip()





            if tool_name:





                # 尝试解析 key: value 对





                args: dict[str, Any] = {}





                for kv_match in re.finditer(r"(\w+)\s*[:=]\s*(\S+)", rest):





                    args[kv_match.group(1)] = kv_match.group(2)





                return {"tool": tool_name, "args": args}











        # 兼容 [tool_call(tool_name, key="value")] 格式





        call_match = re.search(





            r"\[tool_call\(\s*(\w+)\s*,\s*(.*?)\)\]",





            clean, flags=re.DOTALL | re.IGNORECASE,





        )





        if call_match:





            tool_name = call_match.group(1).strip()





            params_str = call_match.group(2).strip()





            if tool_name:





                args = {}





                for kv_match in re.finditer(r'(\w+)\s*=\s*"([^"]*)"', params_str):





                    args[kv_match.group(1)] = kv_match.group(2)





                return {"tool": tool_name, "args": args}











        clean = clean.strip()











        # 尝试直接 JSON 解析





        try:





            data = json.loads(clean)





            if isinstance(data, dict) and "tool" in data:





                return data





            # 兼容 OpenAI function calling 格式: {"name": "tool", "arguments": {...}}





            if isinstance(data, dict) and "name" in data:





                return {"tool": data["name"], "args": data.get("arguments", data.get("args", {}))}





        except (json.JSONDecodeError, ValueError):





            pass











        # 检测多个 JSON 对象拼接: {"tool":"think",...} {"tool":"xxx",...}





        # 用括号计数找到第一个完整 JSON 对象的结束位置





        if clean.startswith("{") and clean.count("{") > clean.count("}"):





            pass  # 不完整 JSON，跳过





        elif clean.startswith("{"):





            end = self._find_json_end(clean)





            if end is not None and end < len(clean) - 1:





                first_json = clean[:end + 1]





                try:





                    data = json.loads(first_json)





                    norm = self._normalize_tool_call(data)





                    if norm:





                        _log.debug("parse_multi_json | picked first of concatenated objects")





                        return norm





                except (json.JSONDecodeError, ValueError):





                    pass











        # 尝试从 markdown code block 中提取





        code_match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", clean, re.DOTALL)





        if code_match:





            code_content = code_match.group(1).strip()





            try:





                data = json.loads(code_content)





                norm = self._normalize_tool_call(data)





                if norm:





                    return norm





            except (json.JSONDecodeError, ValueError):





                # code block 内 JSON 解析失败，尝试恢复（中文引号等）





                recovered = self._try_recover_tool_call(code_content)





                if recovered:





                    return recovered











        # 尝试找到第一个 { 和最后一个 }





        first_brace = clean.find("{")





        last_brace = clean.rfind("}")





        if first_brace >= 0 and last_brace > first_brace:





            candidate = clean[first_brace:last_brace + 1]





            try:





                data = json.loads(candidate)





                norm = self._normalize_tool_call(data)





                if norm:





                    return norm





            except (json.JSONDecodeError, ValueError):





                # 花括号提取的 JSON 解析失败，尝试恢复





                recovered = self._try_recover_tool_call(candidate)





                if recovered:





                    return recovered











        # 如果 fallback 开启，把纯文本当作 final_answer





        if self.fallback_on_parse_error and clean:





            # 如果内容看起来像 JSON tool_call 但解析失败了





            if clean.startswith("{") and ('"tool"' in clean or '"name"' in clean):





                # 尝试修复常见 JSON 问题 (中文引号、未转义引号、截断)





                recovered = self._try_recover_tool_call(clean)





                if recovered:





                    return recovered





                _log.warning("agent_parse_fail_json_like | content=%s", clean[:200])





                return {"tool": "think", "args": {"thought": "我的上一次输出格式有误，让我重新组织回复"}}





            if not clean.startswith("{"):





                return {"tool": "final_answer", "args": {"text": clean}}











        return None











    @staticmethod





    def _normalize_tool_call(data: Any) -> dict[str, Any] | None:





        """将不同格式的 tool call 统一为 {"tool": ..., "args": ...}。"""





        if not isinstance(data, dict):





            return None





        if "tool" in data:





            return data





        # OpenAI function calling 格式: {"name": "tool", "arguments": {...}}





        if "name" in data:





            return {"tool": data["name"], "args": data.get("arguments", data.get("args", {}))}





        return None











    @staticmethod





    def _find_json_end(text: str) -> int | None:





        """找到第一个完整 JSON 对象的结束位置 (括号匹配)。"""





        depth = 0





        in_string = False





        escape = False





        for i, ch in enumerate(text):





            if escape:





                escape = False





                continue





            if ch == '\\' and in_string:





                escape = True





                continue





            if ch == '"' and not escape:





                in_string = not in_string





                continue





            if in_string:





                continue





            if ch == '{':





                depth += 1





            elif ch == '}':





                depth -= 1





                if depth == 0:





                    return i





        return None











    @classmethod





    def _trim_recovered_final_answer_text(cls, content: str) -> str:





        """清理 final_answer.text 的恢复候选，避免把后续字段名拼进正文。"""





        candidate = str(content or "")





        if not candidate:





            return ""











        # 截断掉常见的后续字段开头（例如: ","image_url":）





        field_tail = re.search(





            r'(?:(?<!\\)"\s*,\s*"(?:image_url|image_urls|video_url|audio_file|cover_url|record_b64|pre_ack|action|reason)"\s*:|\\",\\\"(?:image_url|image_urls|video_url|audio_file|cover_url|record_b64|pre_ack|action|reason)\\\"\s*:)',





            candidate,





            flags=re.IGNORECASE,





        )





        if field_tail:





            candidate = candidate[: field_tail.start()]











        # 去掉尾部闭合残片与空白





        candidate = re.sub(r'"\s*\}\s*\}\s*$', "", candidate)





        candidate = candidate.rstrip('"}\n\r\t ')











        # 优先按 JSON 字符串反转义，失败再做最小替换





        try:





            candidate = str(json.loads(f'"{candidate}"'))





        except Exception:





            candidate = candidate.replace('\\"', '"').replace("\\n", "\n").replace("\\t", "\t")











        return normalize_text(candidate)











    def _try_recover_tool_call(self, text: str) -> dict[str, Any] | None:





        """尝试从格式有误的 JSON 中恢复 tool_call。











        常见问题:





        - LLM 输出被截断 (不完整的 JSON)





        - text 值中包含未转义的引号





        - 中文引号 \u201c\u201d 混入 JSON 结构





        """





        # 1. 替换中文引号为英文引号后重试





        fixed = text.replace("\u201c", '"').replace("\u201d", '"')





        fixed = fixed.replace("\u2018", "'").replace("\u2019", "'")





        try:





            data = json.loads(fixed)





            if isinstance(data, dict) and "tool" in data:





                return data





        except (json.JSONDecodeError, ValueError):





            pass











        # 2. 对 final_answer 用正则提取 text 内容





        m = re.search(r'"tool"\s*:\s*"final_answer"', text)





        if m:





            # 找到 "text" : " 之后的所有内容，去掉尾部的 "}} 等





            tm = re.search(r'"text"\s*:\s*"', text)





            if tm:





                start = tm.end()





                content = self._trim_recovered_final_answer_text(text[start:])





                if content:





                    return {"tool": "final_answer", "args": {"text": content}}











        # 3. 对其他工具，尝试截断修复 (补全 } )





        first_brace = text.find("{")





        if first_brace >= 0:





            candidate = text[first_brace:]





            open_count = candidate.count("{") - candidate.count("}")





            if open_count > 0:





                candidate += "}" * open_count





                try:





                    data = json.loads(candidate)





                    if isinstance(data, dict) and "tool" in data:





                        return data





                except (json.JSONDecodeError, ValueError):





                    pass











        return None











    def _compact_data(self, data: dict[str, Any], max_items: int = 20) -> dict[str, Any]:





        """压缩工具返回数据，避免 token 爆炸。"""





        if not isinstance(data, dict):





            return {}











        item_limit = max(3, int(max_items or self.tool_result_max_items))





        str_limit = max(120, int(self.tool_result_max_string_length))





        depth_limit = max(1, int(self.tool_result_max_depth))





        dict_item_limit = max(8, int(self.tool_result_max_dict_items))











        def _compact_value(value: Any, depth: int) -> Any:





            if isinstance(value, str):





                if len(value) <= str_limit:





                    return value





                return value[:str_limit] + "..."











            if depth >= depth_limit:





                if isinstance(value, dict):





                    return {"_omitted": f"dict({len(value)})"}





                if isinstance(value, (list, tuple, set)):





                    return {"_omitted": f"list({len(value)})"}





                return value











            if isinstance(value, dict):





                result_dict: dict[str, Any] = {}





                keys = list(value.keys())





                for key in keys[:dict_item_limit]:





                    key_name = normalize_text(str(key)) or str(key)





                    result_dict[key_name] = _compact_value(value.get(key), depth + 1)





                if len(keys) > dict_item_limit:





                    result_dict["_truncated_fields"] = len(keys) - dict_item_limit





                return result_dict











            if isinstance(value, (list, tuple, set)):





                rows = list(value)





                compact_rows = [_compact_value(item, depth + 1) for item in rows[:item_limit]]





                if len(rows) > item_limit:





                    compact_rows.append({"_truncated_items": len(rows) - item_limit})





                return compact_rows











            return value











        return _compact_value(data, 0) if isinstance(data, dict) else {}











    @staticmethod





    def _last_success_display(steps: list[dict[str, Any]]) -> str:





        for step in reversed(steps):





            if not bool(step.get("ok")):





                continue





            display = normalize_text(str(step.get("display", "")))





            if display:





                return display





        return ""











    @staticmethod





    def _last_success_audio_file(steps: list[dict[str, Any]], prefer_non_silk: bool = False) -> str:





        for step in reversed(steps):





            if not bool(step.get("ok")):





                continue





            data = step.get("data", {})





            if not isinstance(data, dict):





                continue





            for key in ("audio_file", "audio_path", "audio_file_silk", "silk_path"):





                path = normalize_text(str(data.get(key, "")))





                if path:





                    if prefer_non_silk and path.lower().endswith(".silk"):





                        continue





                    return path





        return ""











    @classmethod





    def _looks_like_image_url(cls, value: str) -> bool:





        text = normalize_text(value).lower()





        if not text:





            return False





        if text.startswith("data:image/"):





            return True





        if cls._is_local_media_path(text):





            return bool(re.search(r"\.(?:png|jpe?g|gif|webp|bmp|svg)$", text))





        if re.search(r"\.(?:png|jpe?g|gif|webp|bmp|svg)(?:\?|$)", text):





            return True





        return any(





            token in text





            for token in ("/files/image/", "/image/", "/images/", "grok-image")





        )











    @classmethod





    def _looks_like_video_url(cls, value: str) -> bool:





        text = normalize_text(value).lower()





        if not text:





            return False





        if cls._is_local_media_path(text):





            return bool(re.search(r"\.(?:mp4|mov|mkv|webm|avi|m4v)$", text))





        if re.search(r"\.(?:mp4|mov|mkv|webm|avi|m4v)(?:\?|$)", text):





            return True





        return any(token in text for token in ("/video/", "/videos/", "/files/video/"))











    @classmethod





    def _last_success_image_urls(cls, steps: list[dict[str, Any]]) -> list[str]:





        for step in reversed(steps):





            if not bool(step.get("ok")):





                continue





            data = step.get("data", {})





            if not isinstance(data, dict):





                continue





            tool = normalize_text(str(step.get("tool", ""))).lower()





            candidates: list[str] = []











            raw_urls = data.get("image_urls", [])





            if isinstance(raw_urls, list):





                for item in raw_urls:





                    value = ""





                    if isinstance(item, dict):





                        value = normalize_text(str(item.get("url", "") or item.get("image_url", "")))





                    else:





                        value = normalize_text(str(item))





                    if value:





                        candidates.append(value)











            image_url = normalize_text(str(data.get("image_url", "")))





            if image_url:





                candidates.append(image_url)











            raw_url = normalize_text(str(data.get("url", "")))





            if raw_url and ("image" in tool or cls._looks_like_image_url(raw_url)):





                candidates.append(raw_url)











            filtered: list[str] = []





            seen: set[str] = set()





            for candidate in candidates:





                if not cls._looks_like_image_url(candidate):





                    continue





                if candidate in seen:





                    continue





                seen.add(candidate)





                filtered.append(candidate)





            if filtered:





                return filtered





        return []











    @classmethod





    def _last_success_video_url(cls, steps: list[dict[str, Any]]) -> str:





        for step in reversed(steps):





            if not bool(step.get("ok")):





                continue





            data = step.get("data", {})





            if not isinstance(data, dict):





                continue





            tool = normalize_text(str(step.get("tool", ""))).lower()





            candidates: list[str] = [





                normalize_text(str(data.get("video_url", ""))),





                normalize_text(str(data.get("video", ""))),





            ]





            raw_url = normalize_text(str(data.get("url", "")))





            if raw_url and ("video" in tool or cls._looks_like_video_url(raw_url)):





                candidates.append(raw_url)





            for candidate in candidates:





                if candidate and cls._looks_like_video_url(candidate):





                    return candidate





        return ""











    def _extract_embedded_tool_call_from_text(self, text: str) -> dict[str, Any] | None:





        """从 final_answer 文本中恢复误包裹的工具调用 JSON。"""





        clean = normalize_text(text)





        if not clean:





            return None











        candidates = [clean]





        for block in re.findall(r"```(?:json)?\s*(.*?)```", clean, flags=re.DOTALL | re.IGNORECASE):





            block_clean = normalize_text(block)





            if block_clean:





                candidates.append(block_clean)











        for candidate in candidates:





            xml_parsed = self._parse_embedded_invoke_payload(candidate)





            if xml_parsed:





                return xml_parsed





            parsed = self._parse_embedded_tool_payload(candidate)





            if parsed:





                return parsed





            first_brace = candidate.find("{")





            last_brace = candidate.rfind("}")





            if first_brace >= 0 and last_brace > first_brace:





                parsed = self._parse_embedded_tool_payload(candidate[first_brace:last_brace + 1])





                if parsed:





                    return parsed





        return None











    @staticmethod





    def _normalize_embedded_tool_name(name: str) -> str:





        value = normalize_text(name)





        if not value:





            return ""





        lowered = value.lower()





        alias_map = {





            "search_web": "web_search",





            "web.search": "web_search",





            "websearch": "web_search",





            "analyzeimage": "analyze_image",





            "fetchurl": "fetch_url",





        }





        return alias_map.get(lowered, value)











    def _parse_embedded_invoke_payload(self, payload: str) -> dict[str, Any] | None:





        """兼容模型输出的 XML 风格函数调用:





        <function_calls><invoke name="web_search"><parameter name="query">...</parameter></invoke></function_calls>





        """





        text = normalize_text(payload)





        if not text:





            return None





        invoke_match = re.search(





            r"<invoke\s+name=[\"'](?P<tool>[a-zA-Z0-9_.-]+)[\"'][^>]*>(?P<body>.*?)</invoke>",





            text,





            flags=re.DOTALL | re.IGNORECASE,





        )





        if not invoke_match:





            return None





        tool_name = self._normalize_embedded_tool_name(invoke_match.group("tool"))





        if not tool_name:





            return None





        body = invoke_match.group("body") or ""





        args: dict[str, Any] = {}





        for param in re.finditer(





            r"<parameter\s+name=[\"'](?P<key>[^\"']+)[\"'][^>]*>(?P<value>.*?)</parameter>",





            body,





            flags=re.DOTALL | re.IGNORECASE,





        ):





            key = normalize_text(param.group("key"))





            value_raw = param.group("value") or ""





            value = normalize_text(re.sub(r"<[^>]+>", "", value_raw))





            if key and value:





                args[key] = value





        return {"tool": tool_name, "args": args}











    def _parse_embedded_tool_payload(self, payload: str) -> dict[str, Any] | None:





        try:





            data = json.loads(payload)





        except (json.JSONDecodeError, ValueError):





            return None





        if not isinstance(data, dict):





            return None











        # 兼容 {"tool_uses":[{"tool_name":"...","tool_arguments":{...}}]}





        tool_uses = data.get("tool_uses")





        if isinstance(tool_uses, list) and tool_uses:





            first = tool_uses[0]





            if isinstance(first, dict):





                name = first.get("tool_name") or first.get("name") or first.get("tool")





                args = first.get("tool_arguments", first.get("arguments", first.get("args", {})))





                if isinstance(name, str) and name.strip():





                    return {"tool": name.strip(), "args": args if isinstance(args, dict) else {}}











        # 兼容 {"tool_name":"...","tool_arguments":{...}}





        name = data.get("tool_name") or data.get("name") or data.get("tool")





        if isinstance(name, str) and name.strip():





            args = data.get("tool_arguments", data.get("arguments", data.get("args", {})))





            return {"tool": name.strip(), "args": args if isinstance(args, dict) else {}}











        return None











    async def _build_fallback_result(





        self,





        ctx: AgentContext,





        steps: list[dict[str, Any]],





        tool_calls_made: int,





        t0: float,





        reason: str,





    ) -> AgentResult:





        """从已有步骤中提取最佳回复作为兜底。"""





        # 找最后一个可直接面向用户展示的步骤。





        for step in reversed(steps):





            display = normalize_text(str(step.get("display", "")))





            if not display or not bool(step.get("ok")):





                continue





            tool_name = normalize_text(str(step.get("tool", ""))).lower()





            if self._skip_raw_tool_display_in_fallback(tool_name, display):





                continue





            if len(display) > 280:





                display = clip_text(display, 280)





            if display:





                return AgentResult(





                    reply_text=display,





                    action="reply", reason=f"agent_fallback_{reason}",





                    tool_calls_made=tool_calls_made,





                    total_time_ms=self._elapsed(t0),





                    steps=steps,





                )





        # 没有可用的步骤结果 → 用 AI 生成自然回复





        failed_tools = [





            f"{step.get('tool')}:{step.get('error')}"





            for step in steps





            if isinstance(step, dict) and step.get("tool") and step.get("ok") is False





        ]





        fail_hint = ", ".join(failed_tools[:4]) if failed_tools else reason





        ai_reply = await self._ai_fallback_reply(ctx, f"处理过程中失败({fail_hint})，没拿到最终结果")





        fallback_text = ai_reply or _pl.get_message(





            "no_result",





            "我这边工具刚刚没跑通，你换个说法或稍后再试，我继续处理。",





        )





        return AgentResult(





            reply_text=fallback_text,





            action="reply", reason=f"agent_fallback_{reason}",





            tool_calls_made=tool_calls_made,





            total_time_ms=self._elapsed(t0),





            steps=steps,





        )











    @classmethod





    def _skip_raw_tool_display_in_fallback(cls, tool_name: str, text: str) -> bool:





        tool = normalize_text(tool_name).lower()





        content = normalize_text(text)





        if not content:





            return True





        if tool in cls._FALLBACK_RAW_DISPLAY_SKIP_TOOLS:





            return True





        # 中间提取结果经常是英文长段，直接透传会污染群聊体验。





        letters = len(re.findall(r"[A-Za-z]", content))





        cjk = len(re.findall(r"[\u4e00-\u9fff]", content))





        if letters >= 40 and cjk <= 6:





            return True





        lower = content.lower()





        if lower.startswith("based on the webpage content"):





            return True





        if "from the webpage content" in lower and "no direct" in lower:





            return True





        return False











    @staticmethod





    def _is_placeholder_media_url(url: str) -> bool:





        value = normalize_text(url).lower()





        if not value:





            return False





        if not (value.startswith("http://") or value.startswith("https://")):





            return False





        blocked_tokens = (





            "example.com",





            "example.org",





            "example.net",





            "localhost",





            "127.0.0.1",





            "0.0.0.0",





            ".invalid/",





        )





        return any(token in value for token in blocked_tokens)











    @staticmethod





    def _is_local_media_path(url: str) -> bool:





        value = normalize_text(url)





        if not value:





            return False





        return not value.lower().startswith(("http://", "https://"))











    @staticmethod





    def _normalize_media_url(url: str) -> str:





        value = normalize_text(url).strip()





        if not value:





            return ""





        try:





            parsed = urlsplit(value)





            if parsed.scheme.lower() not in {"http", "https"}:





                return ""





            host = parsed.netloc.lower()





            path = parsed.path or ""





            query = parsed.query or ""





            # 去掉 fragment；query 保留，避免同路径不同资源被误合并。





            return f"{parsed.scheme.lower()}://{host}{path}" + (f"?{query}" if query else "")





        except Exception:





            return ""











    @classmethod





    def _url_matches_known_media(cls, candidate: str, known_urls: set[str]) -> bool:





        target = cls._normalize_media_url(candidate)





        if not target:





            return False





        if target in known_urls:





            return True





        for known in known_urls:





            if not known:





                continue





            if target.startswith(known) or known.startswith(target):





                return True





        return False











    @classmethod





    def _collect_urls_from_payload(cls, payload: Any, out: set[str]) -> None:





        if isinstance(payload, dict):





            for key, value in payload.items():





                key_norm = normalize_text(str(key)).lower()





                if isinstance(value, str):





                    if "url" in key_norm or key_norm in {"source", "link", "image", "video"}:





                        norm = cls._normalize_media_url(value)





                        if norm:





                            out.add(norm)





                    continue





                if isinstance(value, list):





                    for item in value:





                        cls._collect_urls_from_payload(item, out)





                    continue





                if isinstance(value, dict):





                    cls._collect_urls_from_payload(value, out)





            return





        if isinstance(payload, list):





            for item in payload:





                cls._collect_urls_from_payload(item, out)





            return





        if isinstance(payload, str):





            norm = cls._normalize_media_url(payload)





            if norm:





                out.add(norm)











    def _collect_known_media_urls(self, steps: list[dict[str, Any]], ctx: AgentContext) -> set[str]:





        known: set[str] = set()





        for raw_text in (ctx.message_text, ctx.reply_to_text):





            if not raw_text:





                continue





            for found in re.findall(r"https?://[^\s<>\"]+", raw_text, flags=re.IGNORECASE):





                norm = self._normalize_media_url(found)





                if norm:





                    known.add(norm)





        for step in steps:





            if not isinstance(step, dict):





                continue





            step_data = step.get("data", {})





            if isinstance(step_data, dict) and step_data:





                self._collect_urls_from_payload(step_data, known)





        return known











    @staticmethod





    def _normalize_local_media_path(path: str) -> str:





        value = normalize_text(path).strip()





        if not value:





            return ""





        if value.lower().startswith(("http://", "https://")):





            return ""





        return value.replace("\\", "/").lower()











    @classmethod





    def _collect_local_paths_from_payload(cls, payload: Any, out: set[str]) -> None:





        if isinstance(payload, dict):





            for key, value in payload.items():





                key_norm = normalize_text(str(key)).lower()





                if isinstance(value, str):





                    if any(token in key_norm for token in ("path", "file", "url", "image", "video")):





                        local = cls._normalize_local_media_path(value)





                        if local:





                            out.add(local)





                    continue





                if isinstance(value, list):





                    for item in value:





                        cls._collect_local_paths_from_payload(item, out)





                    continue





                if isinstance(value, dict):





                    cls._collect_local_paths_from_payload(value, out)





            return





        if isinstance(payload, list):





            for item in payload:





                cls._collect_local_paths_from_payload(item, out)





            return





        if isinstance(payload, str):





            local = cls._normalize_local_media_path(payload)





            if local:





                out.add(local)











    @staticmethod





    def _extract_media_refs_from_segments(segments: list[dict[str, Any]]) -> list[str]:





        refs: list[str] = []





        for seg in segments or []:





            if not isinstance(seg, dict):





                continue





            data = seg.get("data", {}) or {}





            if not isinstance(data, dict):





                continue





            for key in ("url", "file", "path"):





                value = normalize_text(str(data.get(key, "")))





                if value:





                    refs.append(value)





        return refs











    def _collect_known_local_media_paths(self, steps: list[dict[str, Any]], ctx: AgentContext) -> set[str]:





        known: set[str] = set()





        for step in steps:





            if not isinstance(step, dict):





                continue





            step_data = step.get("data", {})





            if isinstance(step_data, dict) and step_data:





                self._collect_local_paths_from_payload(step_data, known)





        for item in self._extract_media_refs_from_segments(ctx.raw_segments) + self._extract_media_refs_from_segments(





            ctx.reply_media_segments





        ):





            local = self._normalize_local_media_path(item)





            if local:





                known.add(local)





        return known











    @staticmethod





    def _sanitize_profile_summary(summary: str) -> str:





        content = normalize_text(summary)





        if not content:





            return ""





        # 避免把可识别画像统计直接喂给模型，降低隐私泄露概率。





        content = re.sub(





            r"(?:QQ号|qq号|消息数|发言数|发了\d+条消息|凌晨\d+点(?:左右)?活跃|活跃时段|作息规律)[^。；;\n]*[。；;]?",





            "",





            content,





            flags=re.IGNORECASE,





        )





        content = re.sub(r"\s{2,}", " ", content).strip()





        return content











    @staticmethod





    def _elapsed(t0: float) -> int:





        return int((time.monotonic() - t0) * 1000)











    async def _ai_fallback_reply(self, ctx: AgentContext, error_hint: str) -> str:





        """用一次快速 LLM 调用生成错误场景的自然回复，失败返回空字符串。"""





        try:





            system = (





                "你是 YuKiKo。YuKiKo 在 SKIAPI 上班。"





                "现在你在处理用户请求时遇到了问题，需要用简短自然的语气回复用户。"





                "不要用'抱歉'开头，不要太正式，像朋友聊天一样说。一句话就够了。"





                "必须使用简体中文，不要输出英文段落。"





                "禁止说自己是 IDE 助手或说无法扮演当前角色。"





            )





            memory_lines = [f"- {clip_text(normalize_text(item), 80)}" for item in ctx.memory_context[-5:] if normalize_text(item)]





            memory_block = "\n".join(memory_lines) if memory_lines else "(无)"





            user_msg = (





                f"用户说：{clip_text(ctx.message_text, 200)}\n"





                f"是否私聊：{ctx.is_private}\n"





                f"是否@机器人：{ctx.mentioned}\n"





                f"最近上下文：\n{memory_block}\n\n"





                f"情况：{error_hint}\n\n"





                "请结合上下文用一句简短的话回复用户。"





            )





            raw = await asyncio.wait_for(





                self.model_client.chat_text_with_retry(





                    [{"role": "system", "content": system}, {"role": "user", "content": user_msg}],





                    max_tokens=100, retries=1, backoff=0.5,





                ),





                timeout=8,





            )





            return normalize_text(raw).strip()





        except Exception:





            return ""





