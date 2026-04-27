export type Cfg = Record<string, unknown>;
export type FieldType = "text" | "password" | "number" | "switch" | "select" | "slider" | "textarea" | "list" | "group_verbosity_map" | "group_text_map" | "text_map";
export type EnvDraftMap = Record<string, string>;
export interface FieldDef { path: string; label: string; type: FieldType; options?: { value: string; label: string }[]; min?: number; max?: number; step?: number; rows?: number; }
export interface SectionDef { key: string; label: string; fields: FieldDef[]; }
export type SectionMeta = { description: string; essentials?: string[] };

export const DEFAULT_CONFIG: Cfg = {};

export const SECTIONS: SectionDef[] = [
  { key: "control", label: "总控面板", fields: [
    { path: "control.chat_mode", label: "聊天活跃度", type: "select", options: [{ value: "quiet", label: "quiet(安静)" }, { value: "balanced", label: "balanced(均衡)" }, { value: "active", label: "active(活跃)" }] },
    { path: "control.undirected_policy", label: "非指向策略", type: "select", options: [
      { value: "mention_only", label: "mention_only(仅@/私聊/明确指向)" },
      { value: "high_confidence_only", label: "high_confidence_only(高置信旁听)" },
      { value: "off", label: "off(关闭非指向处理)" },
    ] },
    { path: "control.knowledge_learning", label: "知识学习", type: "select", options: [{ value: "aggressive", label: "aggressive(积极)" }] },
    { path: "control.memory_recall_level", label: "记忆回忆等级", type: "select", options: [{ value: "off", label: "off(关闭)" }, { value: "light", label: "light(轻量)" }, { value: "strong", label: "strong(深度)" }] },
    { path: "control.emoji_level", label: "表情强度", type: "select", options: [{ value: "off", label: "off(关闭)" }, { value: "low", label: "low(少量)" }, { value: "medium", label: "medium(中等)" }, { value: "high", label: "high(大量)" }] },
    { path: "control.split_mode", label: "分段模式", type: "select", options: [{ value: "semantic", label: "semantic(语义分段)" }] },
    { path: "control.send_rate_profile", label: "发送限频档位", type: "select", options: [{ value: "safe_qq_group", label: "safe_qq_group(安全)" }, { value: "balanced", label: "balanced(均衡)" }, { value: "active", label: "active(活跃)" }] },
    { path: "control.login_backlog_import_enable", label: "登录离线消息回填", type: "switch" },
    { path: "control.login_backlog_llm_summary_enable", label: "离线消息 LLM 摘要", type: "switch" },
    { path: "control.login_backlog_import_include_private", label: "包含私聊离线消息", type: "switch" },
    { path: "control.login_backlog_import_only_unread", label: "仅导入未读会话", type: "switch" },
    { path: "control.login_backlog_import_max_conversations", label: "离线导入会话上限", type: "number", min: 1, max: 200 },
    { path: "control.login_backlog_import_max_messages_per_conversation", label: "每会话导入消息上限", type: "number", min: 1, max: 200 },
    { path: "control.login_backlog_import_max_pages_per_conversation", label: "每会话历史翻页上限", type: "number", min: 1, max: 10 },
    { path: "control.login_backlog_import_lookback_hours", label: "首次回看时长(小时)", type: "number", min: 1, max: 720 },
  ]},
  { key: "boundary", label: "响应边界", fields: [
    { path: "bot.allow_non_to_me", label: "允许非@消息进入处理", type: "switch" },
    { path: "bot.private_chat_mode", label: "私聊响应模式", type: "select", options: [
      { value: "off", label: "off(关闭私聊对话)" },
      { value: "whitelist", label: "whitelist(仅白名单QQ)" },
      { value: "all", label: "all(全部私聊可聊)" },
    ] },
    { path: "bot.private_chat_whitelist", label: "私聊白名单QQ（逗号分隔）", type: "list" },
    { path: "trigger.ai_listen_min_unique_users", label: "旁听最少人数", type: "number", min: 1, max: 20 },
  ]},
  { key: "agent", label: "Agent 设置", fields: [
    { path: "agent.enable", label: "启用 Agent", type: "switch" },
    { path: "agent.max_steps", label: "最大步骤数", type: "number", min: 1, max: 20 },
    { path: "agent.max_tokens", label: "Agent 最大 Tokens", type: "number", min: 512, max: 32768 },
    { path: "agent.fallback_on_parse_error", label: "解析失败回退", type: "switch" },
    { path: "agent.allow_silent_on_llm_error", label: "LLM 异常静默", type: "switch" },
    { path: "agent.repeat_tool_guard_enable", label: "重复工具防护", type: "switch" },
    { path: "agent.max_same_tool_call", label: "同工具连续上限", type: "number", min: 1, max: 12 },
    { path: "agent.max_consecutive_think", label: "连续思考上限", type: "number", min: 1, max: 12 },
    { path: "agent.tool_timeout_seconds", label: "工具超时(秒)", type: "number", min: 5, max: 300 },
    { path: "agent.tool_timeout_seconds_media", label: "媒体工具超时(秒)", type: "number", min: 5, max: 600 },
    { path: "agent.llm_step_timeout_seconds", label: "LLM 单步超时(秒)", type: "number", min: 5, max: 300 },
    { path: "agent.llm_step_timeout_seconds_after_tool", label: "工具后 LLM 超时(秒)", type: "number", min: 5, max: 300 },
    { path: "agent.total_timeout_seconds", label: "总超时(0=不限制)", type: "number", min: 0, max: 900 },
    { path: "agent.queue_timeout_margin_seconds", label: "队列超时裕量(秒)", type: "number", min: 1, max: 60 },
    { path: "agent.context_retention", label: "上下文保留", type: "select", options: [
      { value: "minimal", label: "很少 (300字符)" },
      { value: "medium", label: "中等 (800字符)" },
      { value: "large", label: "很大 (1500字符)" },
    ]},
    { path: "agent.auto_compress_to_memory", label: "自动压缩上下文到记忆库", type: "switch" },
    { path: "agent.runtime_rules", label: "Agent 运行时规则注入", type: "textarea", rows: 8 },
    { path: "agent.high_risk_control.enable", label: "高风险二次确认", type: "switch" },
    { path: "agent.high_risk_control.default_require_confirmation", label: "默认需确认", type: "switch" },
  ]},
  { key: "knowledge_update", label: "知识学习规则", fields: [
    { path: "knowledge_update.llm_extractor_enable", label: "启用 LLM 抽取器", type: "switch" },
    { path: "knowledge_update.llm_timeout_seconds", label: "LLM 抽取超时(秒)", type: "number", min: 6, max: 60 },
    { path: "knowledge_update.trend_fetch_enable", label: "启用热搜抓取", type: "switch" },
    { path: "knowledge_update.trend_fetch_interval_seconds", label: "热搜抓取间隔(秒)", type: "number", min: 300, max: 7200 },
  ]},
  { key: "bot", label: "Bot 设置", fields: [
    { path: "bot.name", label: "Bot 名称", type: "text" },
    { path: "bot.nicknames", label: "昵称列表（逗号分隔）", type: "list" },
    { path: "bot.language", label: "语言", type: "text" },
    { path: "bot.reply_with_quote", label: "引用回复", type: "switch" },
    { path: "bot.reply_with_at", label: "@用户回复", type: "switch" },
    { path: "bot.allow_markdown", label: "Markdown 输出", type: "switch" },
    { path: "bot.allow_search", label: "搜索功能", type: "switch" },
    { path: "bot.allow_image", label: "图片功能", type: "switch" },

    { path: "bot.max_reply_chars", label: "回复总字数上限", type: "number", min: 120, max: 8000 },
    { path: "bot.max_reply_chars_proactive", label: "主动回复字数上限", type: "number", min: 60, max: 4000 },
    { path: "bot.multi_reply_enable", label: "启用分段发送", type: "switch" },
    { path: "bot.multi_reply_max_chunks", label: "分段最大条数", type: "number", min: 1, max: 20 },
    { path: "bot.multi_reply_max_lines", label: "每段最大行数", type: "number", min: 1, max: 12 },
    { path: "bot.multi_reply_max_chars", label: "每段最大字数(通用)", type: "number", min: 80, max: 4000 },
    { path: "bot.multi_reply_chat_max_lines", label: "每段最大行数(聊天)", type: "number", min: 1, max: 20 },
    { path: "bot.multi_reply_chat_max_chars", label: "每段最大字数(聊天)", type: "number", min: 60, max: 4000 },
    { path: "bot.multi_reply_chat_max_chunks", label: "聊天最大分段条数", type: "number", min: 1, max: 30 },
    { path: "bot.multi_reply_interval_ms", label: "分段间隔(ms)", type: "number", min: 0, max: 5000 },
    { path: "bot.multi_image_max_count", label: "多图最大发送数量", type: "number", min: 1, max: 20 },
    { path: "bot.multi_image_interval_ms", label: "多图间隔(ms)", type: "number", min: 0, max: 5000 },
    { path: "bot.voice_send_max_seconds", label: "语音最大时长(秒)", type: "number", min: 10, max: 300 },
    { path: "bot.voice_send_try_full_first", label: "优先尝试整段发送", type: "switch" },
    { path: "bot.voice_send_split_enable", label: "允许超长语音自动分段", type: "switch" },
    { path: "bot.voice_send_split_max_segments", label: "语音最大分段数", type: "number", min: 1, max: 20 },
    { path: "bot.voice_send_music_force_full", label: "点歌优先整段发送", type: "switch" },
    { path: "bot.voice_send_music_disable_split", label: "点歌禁用自动分段", type: "switch" },
    { path: "bot.video_send_strategy", label: "视频发送策略", type: "select", options: [
      { value: "direct_first", label: "direct_first(优先直链)" },
      { value: "upload_file_first", label: "upload_file_first(优先上传)" },
      { value: "upload_only", label: "upload_only(仅上传)" }
    ]},
    { path: "bot.mention_only_reply_mode", label: "仅@空消息回复模式", type: "select", options: [
      { value: "template", label: "template(模板)" },
      { value: "ai", label: "ai(模型生成)" },
      { value: "hybrid", label: "hybrid(AI失败回退模板)" }
    ]},
    { path: "bot.mention_only_reply_template", label: "仅@空消息回复模板", type: "textarea", rows: 2 },
    { path: "bot.mention_only_reply_template_with_name", label: "仅@空消息回复模板(带用户名)", type: "textarea", rows: 2 },
    { path: "bot.mention_only_ai_prompt", label: "仅@空消息 AI 提示词", type: "textarea", rows: 3 },
    { path: "bot.mention_only_ai_system_prompt", label: "仅@空消息 AI 系统提示词", type: "textarea", rows: 3 },
    { path: "bot.short_ping_phrases", label: "短口头禅触发词（逗号分隔）", type: "list" },
    { path: "bot.short_ping_require_directed", label: "短口头禅需@/私聊/别名命中", type: "switch" },
    { path: "bot.sanitize_banned_phrases", label: "回复净化黑名单短语（逗号分隔）", type: "list" },
  ]},
  { key: "api", label: "API 设置", fields: [
    { path: "api.provider", label: "API 提供商", type: "select", options: [{ value: "openai", label: "OpenAI" }, { value: "anthropic", label: "Anthropic" }, { value: "gemini", label: "Gemini" }, { value: "deepseek", label: "DeepSeek" }, { value: "newapi", label: "NEWAPI" }, { value: "openrouter", label: "OpenRouter" }, { value: "xai", label: "xAI (Grok)" }, { value: "qwen", label: "Qwen" }, { value: "moonshot", label: "Moonshot (Kimi)" }, { value: "mistral", label: "Mistral" }, { value: "zhipu", label: "Zhipu" }, { value: "siliconflow", label: "SiliconFlow" }] },
    { path: "api.endpoint_type", label: "端点类型", type: "select", options: [{ value: "openai_response", label: "OpenAI-Response" }, { value: "openai", label: "OpenAI" }, { value: "anthropic", label: "Anthropic" }, { value: "dmxapi", label: "DMXAPI" }, { value: "gemini", label: "Gemini" }, { value: "weiyi_ai", label: "唯—AI (A)" }] },
    { path: "api.model", label: "模型名称", type: "select" }, { path: "api.api_key", label: "API Key", type: "password" }, { path: "api.base_url", label: "Base URL", type: "text" },
    { path: "api.temperature", label: "Temperature", type: "number", min: 0, max: 2, step: 0.1 }, { path: "api.max_tokens", label: "Max Tokens", type: "number", min: 100, max: 32000 }, { path: "api.timeout_seconds", label: "超时秒数", type: "number", min: 5, max: 300 },
  ]},
  { key: "search", label: "搜索设置", fields: [
    { path: "search.enable", label: "启用搜索", type: "switch" },
    { path: "search.max_results", label: "文本搜索结果数", type: "number", min: 1, max: 12 },
    { path: "search.max_image_results", label: "图片搜索结果数", type: "number", min: 1, max: 12 },
    { path: "search.timeout_seconds", label: "搜索请求超时(秒)", type: "number", min: 5, max: 60 },
    { path: "search.searxng_base", label: "SearXNG 地址(可选)", type: "text" },
    { path: "search.allow_private_network", label: "允许私网搜索(谨慎)", type: "switch" },
    { path: "search.tool_interface.enable", label: "启用工具接口", type: "switch" },
    { path: "search.tool_interface.browser_enable", label: "启用通用网页抓取(含 Gitee/CSDN/论坛)", type: "switch" },
    { path: "search.tool_interface.web_fetch_timeout_seconds", label: "网页打开超时(秒)", type: "number", min: 6, max: 60 },
    { path: "search.tool_interface.web_fetch_max_chars", label: "网页摘要最大字数", type: "number", min: 280, max: 6000 },
    { path: "search.tool_interface.web_fetch_max_pages", label: "网页最多跟进页数", type: "number", min: 1, max: 3 },
    { path: "search.scrape.timeout_seconds", label: "网页抓取超时(秒)", type: "number", min: 5, max: 60 },
    { path: "search.scrape.max_text_len", label: "抓取文本最大长度", type: "number", min: 1000, max: 20000 },
  ]},
  { key: "video", label: "视频解析", fields: [
    { path: "search.video_resolver.enable", label: "启用视频解析", type: "switch" },
    { path: "search.video_resolver.download_max_mb", label: "视频下载上限(MB)", type: "number", min: 8, max: 512 },
    { path: "search.video_resolver.download_timeout_seconds", label: "视频下载超时(秒)", type: "number", min: 10, max: 600 },
    { path: "search.video_resolver.resolve_total_timeout_seconds", label: "视频总超时(秒)", type: "number", min: 20, max: 900 },
    { path: "search.video_resolver.require_audio_for_send", label: "要求视频必须有音频", type: "switch" },
    { path: "search.video_resolver.allow_silent_video_fallback", label: "允许无音频视频fallback", type: "switch" },
    { path: "search.video_resolver.validate_direct_url", label: "验证直链有效性", type: "switch" },
    { path: "search.video_resolver.search_max_duration_seconds", label: "搜索最大时长(秒)", type: "number", min: 60, max: 3600 },
    { path: "search.video_resolver.search_send_max_duration_seconds", label: "搜索发送最大时长(秒)", type: "number", min: 300, max: 3600 },
  ]},
  { key: "vision", label: "图片识别", fields: [
    { path: "search.vision.enable", label: "启用图片识别", type: "switch" },
    { path: "search.vision.timeout_seconds", label: "识别超时(秒)", type: "number", min: 5, max: 120 },
    { path: "search.vision.max_tokens", label: "最大 Tokens", type: "number", min: 200, max: 4000 },
    { path: "search.vision.temperature", label: "Temperature", type: "number", min: 0, max: 1, step: 0.1 },
  ]},
  { key: "music", label: "音乐设置", fields: [
    { path: "music.enable", label: "启用音乐功能", type: "switch" },
    { path: "music.api_base", label: "音乐 API 地址", type: "text" },
    { path: "music.timeout_seconds", label: "请求超时(秒)", type: "number", min: 5, max: 60 },
    { path: "music.max_voice_duration_seconds", label: "最大语音时长(秒,0=不限)", type: "number", min: 0, max: 600 },
    { path: "music.break_limit_enable", label: "启用破限策略", type: "switch" },
    { path: "music.trial_max_duration_ms", label: "试听阈值(ms)", type: "number", min: 0, max: 180000 },
    { path: "music.artist_guard_enable", label: "启用歌手一致性校验", type: "switch" },
    { path: "music.artist_guard_allow_mismatch_fallback", label: "歌手不一致时允许回退", type: "switch" },
    { path: "music.cache_keep_files", label: "缓存保留文件数", type: "number", min: 10, max: 200 },
    { path: "music.cache_dir", label: "缓存目录", type: "text" },
    { path: "music.local_source_enable", label: "启用本地音源匹配", type: "switch" },
    { path: "music.unblock_enable", label: "启用音源解锁", type: "switch" },
    { path: "music.unblock_api_base", label: "解锁 API 地址", type: "text" },
    { path: "music.unblock_sources", label: "解锁音源(逗号分隔)", type: "text" },
  ]},
  { key: "image_gen", label: "图片生成", fields: [
    { path: "image_gen.enable", label: "启用图片生成", type: "switch" },
    { path: "image_gen.default_model", label: "默认模型", type: "text" },
    { path: "image_gen.default_size", label: "默认尺寸", type: "select", options: [
      { value: "1024x1024", label: "1024x1024" },
      { value: "1792x1024", label: "1792x1024" },
      { value: "1024x1792", label: "1024x1792" },
    ]},
    { path: "image_gen.nsfw_filter", label: "NSFW 过滤", type: "switch" },
    { path: "image_gen.prompt_review_enable", label: "生成前提示词审查", type: "switch" },
    { path: "image_gen.prompt_review_fail_closed", label: "提示词审查失败时默认拦截", type: "switch" },
    { path: "image_gen.prompt_review_model", label: "提示词审查模型(留空=主模型)", type: "text" },
    { path: "image_gen.prompt_review_max_tokens", label: "提示词审查最大 Tokens", type: "number", min: 80, max: 600 },
    { path: "image_gen.post_review_enable", label: "生成后主模型二次审查", type: "switch" },
    { path: "image_gen.post_review_fail_closed", label: "审查失败时默认拦截(严格)", type: "switch" },
    { path: "image_gen.post_review_model", label: "二次审查模型(留空=主模型)", type: "text" },
    { path: "image_gen.post_review_max_tokens", label: "二次审查最大 Tokens", type: "number", min: 120, max: 1200 },
    { path: "image_gen.max_prompt_length", label: "提示词最大长度", type: "number", min: 100, max: 2000 },
    { path: "image_gen.custom_block_terms", label: "额外拦截词（逗号/换行分隔）", type: "list" },
    { path: "image_gen.custom_allow_terms", label: "额外放行词（仅覆盖自定义拦截词）", type: "list" },
  ]},
  { key: "affinity", label: "好感度系统", fields: [
    { path: "affinity.enable", label: "启用好感度系统", type: "switch" },
    { path: "affinity.checkin_base_reward", label: "签到基础奖励", type: "number", min: 0, max: 10, step: 0.5 },
    { path: "affinity.checkin_streak_bonus", label: "连续签到加成", type: "number", min: 0, max: 5, step: 0.1 },
    { path: "affinity.interaction_reward", label: "互动奖励", type: "number", min: 0, max: 2, step: 0.1 },
    { path: "affinity.decay_per_day", label: "每日衰减", type: "number", min: 0, max: 2, step: 0.1 },
  ]},
  { key: "emotion", label: "情绪系统", fields: [
    { path: "emotion.enable", label: "启用情绪系统", type: "switch" },
    { path: "emotion.emoji_probability", label: "表情概率", type: "number", min: 0, max: 1, step: 0.01 },
    { path: "emotion.warn_threshold", label: "警告阈值", type: "number", min: 0, max: 50 },
    { path: "emotion.strike_threshold", label: "惩罚阈值", type: "number", min: 0, max: 100 },
    { path: "emotion.warn_cooldown_seconds", label: "警告冷却(秒)", type: "number", min: 5, max: 120 },
    { path: "emotion.strike_cooldown_seconds", label: "惩罚冷却(秒)", type: "number", min: 10, max: 300 },
  ]},
  { key: "safety", label: "安全设置", fields: [
    { path: "safety.profile", label: "安全档位", type: "select", options: [
      { value: "conservative", label: "conservative(保守)" },
      { value: "normal", label: "normal(一般)" },
      { value: "open", label: "open(开放)" },
      { value: "very_open", label: "very_open(很开放)" },
    ]},
    { path: "safety.scale", label: "兼容尺度 (0宽松-3最严)", type: "slider", min: 0, max: 3, step: 1 },
    { path: "safety.custom_block_terms", label: "额外安全拦截词（逗号/换行分隔）", type: "list" },
    { path: "safety.custom_allow_terms", label: "额外安全放行词（仅覆盖自定义拦截词）", type: "list" },
    { path: "safety.group_profiles", label: "群聊安全档位覆盖（每行: 群号=profile）", type: "group_text_map", rows: 4 },
    { path: "safety.output_sensitive_words", label: "输出敏感词替换（每行: 原词=替换词）", type: "text_map", rows: 6 },
  ]},
  { key: "output", label: "输出设置", fields: [
    { path: "output.verbosity", label: "详略度", type: "select", options: [{ value: "verbose", label: "详细" }, { value: "medium", label: "中等" }, { value: "brief", label: "简洁" }, { value: "minimal", label: "极简" }] },
    { path: "output.token_saving", label: "省 Token 模式", type: "switch" },
    { path: "output.group_overrides", label: "群聊详略覆盖（每行: 群号=级别）", type: "group_verbosity_map", rows: 5 },
    { path: "output.style_instruction", label: "全局输出风格指令", type: "textarea", rows: 3 },
    { path: "output.group_style_overrides", label: "群聊输出风格覆盖（每行: 群号=指令）", type: "group_text_map", rows: 6 },
  ]},
  { key: "admin", label: "管理设置", fields: [
    { path: "admin.super_admin_qq", label: "超级管理员 QQ", type: "text" },
    { path: "admin.super_users", label: "管理员列表（逗号分隔）", type: "list" },
    { path: "admin.whitelist_groups", label: "白名单群号（逗号分隔）", type: "list" },
    { path: "admin.non_whitelist_mode", label: "非白名单群策略", type: "select", options: [
      { value: "minimal", label: "minimal(最小响应)" },
      { value: "silent", label: "silent(完全静默)" }
    ]},
  ]},
  { key: "trigger", label: "触发策略", fields: [
    { path: "trigger.ai_listen_enable", label: "旁听探测开关", type: "switch" },
    { path: "trigger.ai_listen_keyword_enable", label: "记忆关键词触发", type: "switch" },
    { path: "trigger.ai_listen_min_keyword_hits", label: "关键词命中阈值", type: "number", min: 1, max: 8 },
    { path: "trigger.delegate_undirected_to_ai", label: "非指向消息交给 AI 判定", type: "switch" },
    { path: "trigger.ai_listen_min_messages", label: "旁听最少消息数", type: "number", min: 1, max: 50 },
    { path: "trigger.ai_listen_min_score", label: "旁听最低分", type: "number", min: 0.5, max: 10, step: 0.1 },
    { path: "trigger.followup_reply_window_seconds", label: "追问窗口(秒)", type: "number", min: 5, max: 120 },
    { path: "trigger.followup_max_turns", label: "追问轮数", type: "number", min: 1, max: 10 },
  ]},
  { key: "routing", label: "路由设置", fields: [
    { path: "routing.mode", label: "路由模式", type: "select", options: [
      { value: "ai_full", label: "ai_full(完全AI判定)" }
    ]},
    { path: "routing.trust_ai_fully", label: "完全信任AI判定", type: "switch" },
    { path: "routing.followup_fast_path_enable", label: "多模态追问快速通道", type: "switch" },
    { path: "routing.min_confidence", label: "接话门槛", type: "number", min: 0, max: 1, step: 0.01 },
    { path: "routing.followup_min_confidence", label: "追问门槛", type: "number", min: 0, max: 1, step: 0.01 },
    { path: "routing.non_directed_min_confidence", label: "旁听门槛", type: "number", min: 0, max: 1, step: 0.01 },
    { path: "routing.ai_gate_min_confidence", label: "AI 门槛", type: "number", min: 0, max: 1, step: 0.01 },
    { path: "routing.zero_threshold_disables_undirected", label: "零阈值禁用非指向", type: "switch" },
  ]},
  { key: "self_check", label: "防误触护栏", fields: [
    { path: "self_check.enable", label: "启用自检护栏", type: "switch" },
    { path: "self_check.block_at_other", label: "@他人场景拦截", type: "switch" },
    { path: "self_check.listen_probe_min_confidence", label: "旁听最低置信度", type: "number", min: 0, max: 1, step: 0.01 },
    { path: "self_check.non_direct_reply_min_confidence", label: "非指向回复最低置信度", type: "number", min: 0, max: 1, step: 0.01 },
    { path: "self_check.cross_user_guard_seconds", label: "跨用户隔离窗口(秒)", type: "number", min: 5, max: 180 },
  ]},
  { key: "queue", label: "队列策略", fields: [
    { path: "queue.group_concurrency", label: "会话并发数", type: "number", min: 1, max: 8 },
    { path: "queue.single_inflight_per_conversation", label: "单会话单 inflight", type: "switch" },
    { path: "queue.cancel_previous_on_new", label: "新消息取消旧任务", type: "switch" },
    { path: "queue.cancel_previous_on_interrupt_request", label: "中断类请求优先打断旧任务", type: "switch" },
    { path: "queue.smart_interrupt_enable", label: "启用智能打断", type: "switch" },
    { path: "queue.smart_interrupt_cross_user_enable", label: "启用跨用户智能打断", type: "switch" },
    { path: "queue.cancel_previous_mode", label: "取消策略", type: "select", options: [
      { value: "interrupt", label: "interrupt(仅中断指令)" },
      { value: "high_priority", label: "high_priority(@/私聊/命令)" },
      { value: "always", label: "always(任意新消息)" }
    ]},
    { path: "queue.group_isolate_by_user", label: "群聊按用户隔离会话", type: "switch" },
  ]},
  { key: "prompt_control", label: "Prompt 管控", fields: [
    { path: "prompt_control.enable", label: "启用 Prompt 管控", type: "switch" },
    { path: "prompt_control.low_iq_mode", label: "低智商模型强化模式", type: "switch" },
    { path: "prompt_control.global_prefix", label: "全局前置注入", type: "textarea", rows: 3 },
    { path: "prompt_control.global_suffix", label: "全局后置注入", type: "textarea", rows: 3 },
    { path: "prompt_control.persona_override", label: "人设覆盖文本", type: "textarea", rows: 5 },
  ]},
];

export const SECTION_META: Record<string, SectionMeta> = {
  control: {
    description: "这里决定机器人的整体性格、活跃度和默认行为，适合先改这里。",
    essentials: [
      "control.chat_mode",
      "control.undirected_policy",
      "control.knowledge_learning",
      "control.memory_recall_level",
      "control.emoji_level",
      "control.send_rate_profile",
    ],
  },
  boundary: {
    description: "控制机器人在群聊和私聊里什么场景该说话、什么场景保持安静。旁听参数和非指向策略在「触发策略」和「总控面板」中配置。",
    essentials: [
      "bot.allow_non_to_me",
      "bot.private_chat_mode",
      "bot.private_chat_whitelist",
      "trigger.ai_listen_min_unique_users",
    ],
  },
  agent: {
    description: "这是 Agent 的核心运行参数，日常只需关心开关、步数和上下文保留。",
    essentials: [
      "agent.enable",
      "agent.max_steps",
      "agent.max_tokens",
      "agent.context_retention",
      "agent.high_risk_control.enable",
    ],
  },
  knowledge_update: {
    description: "控制自学习与热搜更新的节奏；不折腾自学习时通常只改开关。",
    essentials: [
      "knowledge_update.llm_extractor_enable",
      "knowledge_update.trend_fetch_enable",
      "knowledge_update.trend_fetch_interval_seconds",
    ],
  },
  bot: {
    description: "这里是最像“机器人定义”的部分：名字、可用能力、回复长度和输出习惯。",
    essentials: [
      "bot.name",
      "bot.nicknames",
      "bot.language",
      "bot.allow_markdown",
      "bot.allow_search",
      "bot.allow_image",
      "bot.reply_with_quote",
      "bot.max_reply_chars",
      "bot.multi_reply_enable",
    ],
  },
  api: {
    description: "主模型通道配置。一般只需提供商、模型、Key；其余属于高级兼容项。",
    essentials: [
      "api.provider",
      "api.endpoint_type",
      "api.model",
      "api.api_key",
      "api.base_url",
    ],
  },
  search: {
    description: "控制联网搜索与网页抓取。日常先看启用状态、结果数和超时。",
    essentials: [
      "search.enable",
      "search.max_results",
      "search.max_image_results",
      "search.timeout_seconds",
    ],
  },
  video: {
    description: "短视频解析和发送策略，通常只需要限制大小和时长。",
    essentials: [
      "search.video_resolver.enable",
      "search.video_resolver.download_max_mb",
      "search.video_resolver.search_send_max_duration_seconds",
    ],
  },
  vision: {
    description: "图片识图模型配置，建议只保留开关、超时和 token 上限。",
    essentials: [
      "search.vision.enable",
      "search.vision.timeout_seconds",
      "search.vision.max_tokens",
    ],
  },
  music: {
    description: "点歌与语音播放相关设置，普通使用主要改 API 地址和时长限制。",
    essentials: [
      "music.enable",
      "music.api_base",
      "music.max_voice_duration_seconds",
      "music.break_limit_enable",
    ],
  },
  image_gen: {
    description: "图片生成主配置。默认模型、尺寸和审核开关属于常用，其它高级规则可按需展开。",
    essentials: [
      "image_gen.enable",
      "image_gen.default_model",
      "image_gen.default_size",
      "image_gen.nsfw_filter",
      "image_gen.prompt_review_enable",
      "image_gen.post_review_enable",
      "image_gen.max_prompt_length",
    ],
  },
  affinity: {
    description: "签到和互动带来的好感度变化，通常只需要总开关和奖励倍率。",
    essentials: [
      "affinity.enable",
      "affinity.checkin_base_reward",
      "affinity.interaction_reward",
    ],
  },
  emotion: {
    description: "机器人的情绪和惩罚阈值设定，建议先保守调整。",
    essentials: [
      "emotion.enable",
      "emotion.emoji_probability",
      "emotion.warn_threshold",
      "emotion.strike_threshold",
    ],
  },
  safety: {
    description: "全局安全尺度和敏感词替换，和平台底线直接相关。",
    essentials: [
      "safety.profile",
      "safety.scale",
      "safety.custom_block_terms",
      "safety.output_sensitive_words",
    ],
  },
  output: {
    description: "统一控制输出长短和写作风格，群聊覆盖规则放在高级里。",
    essentials: [
      "output.verbosity",
      "output.token_saving",
      "output.style_instruction",
    ],
  },
  admin: {
    description: "权限和群白名单配置。一般只需超级管理员和白名单群。",
    essentials: [
      "admin.super_admin_qq",
      "admin.super_users",
      "admin.whitelist_groups",
      "admin.non_whitelist_mode",
    ],
  },
  trigger: {
    description: "决定机器人什么时候接话、什么时候追问。",
    essentials: [
      "trigger.ai_listen_enable",
      "trigger.ai_listen_keyword_enable",
      "trigger.ai_listen_min_keyword_hits",
      "trigger.followup_reply_window_seconds",
    ],
  },
  routing: {
    description: "AI 判定接话门槛；多数情况下只调整模式和主阈值即可。",
    essentials: [
      "routing.mode",
      "routing.trust_ai_fully",
      "routing.min_confidence",
      "routing.followup_min_confidence",
    ],
  },
  self_check: {
    description: "防止误触发和串台的最后一道护栏，建议保持开启。",
    essentials: [
      "self_check.enable",
      "self_check.block_at_other",
      "self_check.non_direct_reply_min_confidence",
    ],
  },
  queue: {
    description: "并发、打断和会话隔离策略；普通场景只需要少量核心开关。",
    essentials: [
      "queue.group_concurrency",
      "queue.single_inflight_per_conversation",
      "queue.cancel_previous_on_new",
      "queue.smart_interrupt_enable",
    ],
  },
  prompt_control: {
    description: "Prompt 注入和人设覆盖。默认仅保留开关和人设覆盖入口。",
    essentials: [
      "prompt_control.enable",
      "prompt_control.persona_override",
    ],
  },
};

export const INPUT_CLASSES = {
  label: "text-default-500 text-xs",
  base: "bg-transparent",
  mainWrapper: "bg-transparent",
  innerWrapper: "bg-transparent",
  input: "text-sm !bg-transparent",
  inputWrapper:
    "!bg-content2/55 border border-default-400/35 shadow-none transition-all duration-200 data-[hover=true]:!bg-content2/70 data-[focus=true]:!bg-content2/80 data-[focus=true]:border-primary/65 data-[focus=true]:shadow-[0_0_0_2px_rgba(120,120,130,0.24)] before:!bg-transparent after:!bg-transparent",
};
export const SELECT_CLASSES = { label: "text-default-500 text-xs", trigger: "bg-content2/55 border border-default-400/35 shadow-none transition-all duration-200 data-[hover=true]:bg-content2/70 data-[focus=true]:bg-content2/80 data-[focus=true]:border-primary/65 data-[focus=true]:shadow-[0_0_0_2px_rgba(120,120,130,0.24)]" };
export const SHELL = "rounded-2xl border border-default-400/35 bg-content1/55 p-3 shadow-sm transition-all duration-200 hover:border-default-400/55 hover:bg-content1/75";
export const IMAGE_GEN_PROMPT_PRESETS: Array<{ label: string; prompt: string }> = [
  { label: "Q版头像", prompt: "Q版动漫头像，干净背景，角色居中，头肩构图，细节清晰，高质量插画" },
  { label: "猫娘表情包", prompt: "灰白短发猫娘，猫耳+蓝色蝴蝶结，蓝绿色半睁眼，嘴巴微张o型，困倦摆烂表情，Q版头像，表情包风格" },
  { label: "二次元立绘", prompt: "二次元角色立绘，完整服装设定，线条干净，光影柔和，背景简洁，高清插画" },
  { label: "像素头像", prompt: "像素风角色头像，8-bit配色，清晰轮廓，简洁背景，游戏图标风格" },
  { label: "海报风", prompt: "角色主题海报，强对比配色，电影感构图，细节丰富，文字区域留白" },
  { label: "写实摄影", prompt: "写实摄影风人像，柔光，浅景深，肤质自然，构图干净，高清细节" },
];

export { allModelOptions, IMAGE_MODEL_OPTIONS, MODEL_OPTIONS, uniqueModelOptions } from "../../shared/model-options";
