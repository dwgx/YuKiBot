import { useEffect, useMemo, useState, useCallback } from "react";
import {
  Card, CardBody, CardHeader, Input, Switch, Button, Select, SelectItem, Textarea,
  Spinner, Slider, Chip, Tabs, Tab,
} from "@heroui/react";
import { Save, ChevronLeft, ChevronRight } from "lucide-react";
import { motion } from "framer-motion";
import { api, type ImageGenTestResponse } from "../api/client";

type Cfg = Record<string, unknown>;
type FieldType = "text" | "password" | "number" | "switch" | "select" | "slider" | "textarea" | "list" | "group_verbosity_map" | "group_text_map";
interface FieldDef { path: string; label: string; type: FieldType; options?: { value: string; label: string }[]; min?: number; max?: number; step?: number; rows?: number; }
interface SectionDef { key: string; label: string; fields: FieldDef[]; }

const DEFAULT_CONFIG: Cfg = {};

const SECTIONS: SectionDef[] = [
  { key: "control", label: "总控面板", fields: [
    { path: "control.chat_mode", label: "聊天活跃度", type: "select", options: [{ value: "quiet", label: "quiet" }, { value: "balanced", label: "balanced" }, { value: "active", label: "active" }] },
    { path: "control.undirected_policy", label: "非指向策略", type: "select", options: [
      { value: "mention_only", label: "mention_only(仅@/私聊/明确指向)" },
      { value: "high_confidence_only", label: "high_confidence_only(高置信旁听)" },
      { value: "off", label: "off(关闭非指向处理)" },
    ] },
    { path: "control.knowledge_learning", label: "知识学习", type: "select", options: [{ value: "aggressive", label: "aggressive" }] },
    { path: "control.memory_recall_level", label: "记忆回忆等级", type: "select", options: [{ value: "off", label: "off" }, { value: "light", label: "light" }, { value: "strong", label: "strong" }] },
    { path: "control.emoji_level", label: "表情强度", type: "select", options: [{ value: "off", label: "off" }, { value: "low", label: "low" }, { value: "medium", label: "medium" }, { value: "high", label: "high" }] },
    { path: "control.split_mode", label: "分段模式", type: "select", options: [{ value: "semantic", label: "semantic" }] },
    { path: "control.send_rate_profile", label: "发送限频档位", type: "select", options: [{ value: "safe_qq_group", label: "safe_qq_group" }, { value: "balanced", label: "balanced" }, { value: "active", label: "active" }] },
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
    { path: "trigger.ai_listen_enable", label: "启用非@旁听判定", type: "switch" },
    { path: "trigger.delegate_undirected_to_ai", label: "非指向消息交给 AI 决策", type: "switch" },
    { path: "control.undirected_policy", label: "非指向响应策略", type: "select", options: [
      { value: "mention_only", label: "mention_only(仅@/私聊/明确指向)" },
      { value: "high_confidence_only", label: "high_confidence_only(高置信旁听)" },
      { value: "off", label: "off(关闭非指向处理)" },
    ] },
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
    { path: "agent.music_fast_path_enable", label: "音乐本地快速通道", type: "switch" },
    { path: "agent.runtime_rules", label: "Agent 运行时规则注入", type: "textarea", rows: 8 },
    { path: "agent.high_risk_control.enable", label: "高风险二次确认", type: "switch" },
    { path: "agent.high_risk_control.default_require_confirmation", label: "默认需确认", type: "switch" },
  ]},
  { key: "knowledge_update", label: "知识学习规则", fields: [
    { path: "knowledge_update.llm_extractor_enable", label: "启用 LLM 抽取器", type: "switch" },
    { path: "knowledge_update.llm_timeout_seconds", label: "LLM 抽取超时(秒)", type: "number", min: 6, max: 60 },
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
    { path: "bot.allow_non_to_me", label: "允许非@触发", type: "switch" },
    { path: "bot.multi_reply_enable", label: "启用分段发送", type: "switch" },
    { path: "bot.multi_reply_max_chunks", label: "分段最大条数", type: "number", min: 1, max: 20 },
    { path: "bot.multi_reply_max_lines", label: "每段最大行数", type: "number", min: 1, max: 12 },
    { path: "bot.multi_reply_max_chars", label: "每段最大字数(通用)", type: "number", min: 80, max: 500 },
    { path: "bot.multi_reply_chat_max_chars", label: "每段最大字数(聊天)", type: "number", min: 60, max: 400 },
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
    { path: "api.provider", label: "API 提供商", type: "select", options: [{ value: "skiapi", label: "SKIAPI" }, { value: "openai", label: "OpenAI" }, { value: "anthropic", label: "Anthropic" }, { value: "gemini", label: "Gemini" }, { value: "deepseek", label: "DeepSeek" }, { value: "newapi", label: "NEWAPI" }, { value: "openrouter", label: "OpenRouter" }, { value: "xai", label: "xAI (Grok)" }, { value: "qwen", label: "Qwen" }, { value: "moonshot", label: "Moonshot (Kimi)" }, { value: "mistral", label: "Mistral" }, { value: "zhipu", label: "Zhipu" }, { value: "siliconflow", label: "SiliconFlow" }] },
    { path: "api.endpoint_type", label: "端点类型", type: "select", options: [{ value: "openai_response", label: "OpenAI-Response" }, { value: "openai", label: "OpenAI" }, { value: "anthropic", label: "Anthropic" }, { value: "dmxapi", label: "DMXAPI" }, { value: "gemini", label: "Gemini" }, { value: "weiyi_ai", label: "唯—AI (A)" }] },
    { path: "api.model", label: "模型名称", type: "select" }, { path: "api.api_key", label: "API Key", type: "password" }, { path: "api.base_url", label: "Base URL", type: "text" },
    { path: "api.temperature", label: "Temperature", type: "number", min: 0, max: 2, step: 0.1 }, { path: "api.max_tokens", label: "Max Tokens", type: "number", min: 100, max: 32000 }, { path: "api.timeout_seconds", label: "超时秒数", type: "number", min: 5, max: 300 },
  ]},
  { key: "search", label: "搜索设置", fields: [
    { path: "search.enable", label: "启用搜索", type: "switch" },
    { path: "search.intent_shortcut_enable", label: "搜索意图快捷通道", type: "switch" },
    { path: "search.tool_interface.auto_method_enable", label: "工具自动方法推断", type: "switch" },
    { path: "search.tool_interface.enable", label: "启用工具接口", type: "switch" },
    { path: "search.tool_interface.browser_enable", label: "启用通用网页抓取(含 Gitee/CSDN/论坛)", type: "switch" },
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
    { path: "music.cache_keep_files", label: "缓存保留文件数", type: "number", min: 10, max: 200 },
    { path: "music.local_source_enable", label: "启用本地音源匹配", type: "switch" },
    { path: "music.unblock_enable", label: "启用音源解锁", type: "switch" },
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
    { path: "image_gen.max_prompt_length", label: "提示词最大长度", type: "number", min: 100, max: 2000 },
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
  { key: "safety", label: "安全设置", fields: [{ path: "safety.scale", label: "安全尺度 (0宽松-3最严)", type: "slider", min: 0, max: 3, step: 1 }] },
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

const INPUT_CLASSES = { label: "text-default-500 text-xs", input: "text-sm", inputWrapper: "bg-content2/55 border border-default-400/35 shadow-none transition-all duration-200 data-[hover=true]:bg-content2/70 data-[focus=true]:bg-content2/80 data-[focus=true]:border-primary/65 data-[focus=true]:shadow-[0_0_0_2px_rgba(120,120,130,0.24)]" };
const SELECT_CLASSES = { label: "text-default-500 text-xs", trigger: "bg-content2/55 border border-default-400/35 shadow-none transition-all duration-200 data-[hover=true]:bg-content2/70 data-[focus=true]:bg-content2/80 data-[focus=true]:border-primary/65 data-[focus=true]:shadow-[0_0_0_2px_rgba(120,120,130,0.24)]" };
const SHELL = "rounded-2xl border border-default-400/35 bg-content1/55 p-3 shadow-sm transition-all duration-200 hover:border-default-400/55 hover:bg-content1/75";
const IMAGE_GEN_PROMPT_PRESETS: Array<{ label: string; prompt: string }> = [
  { label: "Q版头像", prompt: "Q版动漫头像，干净背景，角色居中，头肩构图，细节清晰，高质量插画" },
  { label: "猫娘表情包", prompt: "灰白短发猫娘，猫耳+蓝色蝴蝶结，蓝绿色半睁眼，嘴巴微张o型，困倦摆烂表情，Q版头像，表情包风格" },
  { label: "二次元立绘", prompt: "二次元角色立绘，完整服装设定，线条干净，光影柔和，背景简洁，高清插画" },
  { label: "像素头像", prompt: "像素风角色头像，8-bit配色，清晰轮廓，简洁背景，游戏图标风格" },
  { label: "海报风", prompt: "角色主题海报，强对比配色，电影感构图，细节丰富，文字区域留白" },
  { label: "写实摄影", prompt: "写实摄影风人像，柔光，浅景深，肤质自然，构图干净，高清细节" },
];

const MODEL_OPTIONS: Record<string, { value: string; label: string }[]> = {
  skiapi: [
    { value: "claude-opus-4-6", label: "claude-opus-4-6" },
    { value: "claude-sonnet-4-5-20250929", label: "claude-sonnet-4-5-20250929" },
    { value: "claude-haiku-4-5-20251001", label: "claude-haiku-4-5-20251001" },
    { value: "grok-4.1-mini", label: "grok-4.1-mini" },
    { value: "grok-4", label: "grok-4" },
    { value: "grok-4.1-thinking", label: "grok-4.1-thinking" },
    { value: "grok-4.1-fast", label: "grok-4.1-fast" },
    { value: "grok-4.1-expert", label: "grok-4.1-expert" },
    { value: "grok-4.20-beta", label: "grok-4.20-beta" },
    { value: "grok-imagine-1.0", label: "grok-imagine-1.0" },
    { value: "grok-imagine-1.0-fast", label: "grok-imagine-1.0-fast" },
    { value: "grok-imagine-1.0-edit", label: "grok-imagine-1.0-edit" },
    { value: "grok-imagine-1.0-video", label: "grok-imagine-1.0-video" },
    { value: "gpt-5.3", label: "gpt-5.3" },
    { value: "gpt-5.3-codex", label: "gpt-5.3-codex" },
    { value: "gpt-5.2-codex", label: "gpt-5.2-codex" },
    { value: "gpt-5.1-codex-mini", label: "gpt-5.1-codex-mini" },
    { value: "gpt-5.1-codex-max", label: "gpt-5.1-codex-max" },
    { value: "gpt-5.1-codex", label: "gpt-5.1-codex" },
    { value: "gpt-5-codex", label: "gpt-5-codex" },
    { value: "gpt-5-codex-mini", label: "gpt-5-codex-mini" },
    { value: "codex-mini-latest", label: "codex-mini-latest" },
    { value: "gpt-5.2", label: "gpt-5.2" },
    { value: "gpt-5", label: "gpt-5" },
  ],
  openai: [
    { value: "gpt-5.3", label: "gpt-5.3" },
    { value: "gpt-5.3-codex", label: "gpt-5.3-codex" },
    { value: "gpt-5.2-codex", label: "gpt-5.2-codex" },
    { value: "gpt-5.1-codex-mini", label: "gpt-5.1-codex-mini" },
    { value: "gpt-5.1-codex-max", label: "gpt-5.1-codex-max" },
    { value: "gpt-5.1-codex", label: "gpt-5.1-codex" },
    { value: "gpt-5-codex", label: "gpt-5-codex" },
    { value: "gpt-5.2", label: "gpt-5.2" },
    { value: "gpt-5", label: "gpt-5" },
    { value: "gpt-5-mini", label: "gpt-5-mini" },
    { value: "gpt-5-nano", label: "gpt-5-nano" },
  ],
  deepseek: [
    { value: "deepseek-chat", label: "deepseek-chat" },
    { value: "deepseek-reasoner", label: "deepseek-reasoner" },
  ],
  anthropic: [
    { value: "claude-sonnet-4-5-20250929", label: "claude-sonnet-4-5-20250929" },
    { value: "claude-opus-4-1", label: "claude-opus-4-1" },
  ],
  gemini: [
    { value: "gemini-2.5-pro", label: "gemini-2.5-pro" },
    { value: "gemini-2.5-flash", label: "gemini-2.5-flash" },
  ],
  newapi: [
    { value: "gpt-5.3", label: "gpt-5.3" },
    { value: "gpt-5.3-codex", label: "gpt-5.3-codex" },
    { value: "gpt-5.2-codex", label: "gpt-5.2-codex" },
    { value: "gpt-5.1-codex-mini", label: "gpt-5.1-codex-mini" },
    { value: "gpt-5.1-codex-max", label: "gpt-5.1-codex-max" },
    { value: "gpt-5.1-codex", label: "gpt-5.1-codex" },
    { value: "gpt-5-codex", label: "gpt-5-codex" },
    { value: "gpt-5-codex-mini", label: "gpt-5-codex-mini" },
    { value: "codex-mini-latest", label: "codex-mini-latest" },
    { value: "gpt-5", label: "gpt-5" },
    { value: "gpt-5-mini", label: "gpt-5-mini" },
    { value: "gpt-5-nano", label: "gpt-5-nano" },
  ],
  openrouter: [
    { value: "openrouter/auto", label: "openrouter/auto" },
    { value: "openai/gpt-5", label: "openai/gpt-5" },
  ],
  xai: [
    { value: "grok-4.1-mini", label: "grok-4.1-mini" },
    { value: "grok-4", label: "grok-4" },
    { value: "grok-4.1-thinking", label: "grok-4.1-thinking" },
    { value: "grok-4.1-fast", label: "grok-4.1-fast" },
    { value: "grok-4.1-expert", label: "grok-4.1-expert" },
    { value: "grok-4.20-beta", label: "grok-4.20-beta" },
    { value: "grok-imagine-1.0", label: "grok-imagine-1.0" },
    { value: "grok-imagine-1.0-fast", label: "grok-imagine-1.0-fast" },
    { value: "grok-imagine-1.0-edit", label: "grok-imagine-1.0-edit" },
    { value: "grok-imagine-1.0-video", label: "grok-imagine-1.0-video" },
  ],
  qwen: [
    { value: "qwen-max-latest", label: "qwen-max-latest" },
    { value: "qwen-plus-latest", label: "qwen-plus-latest" },
  ],
  moonshot: [
    { value: "kimi-thinking-preview", label: "kimi-thinking-preview" },
    { value: "moonshot-v1-128k", label: "moonshot-v1-128k" },
  ],
  mistral: [
    { value: "mistral-medium-latest", label: "mistral-medium-latest" },
    { value: "mistral-large-latest", label: "mistral-large-latest" },
  ],
  zhipu: [
    { value: "glm-4-plus", label: "glm-4-plus" },
    { value: "glm-4-air", label: "glm-4-air" },
  ],
  siliconflow: [
    { value: "Qwen/Qwen2.5-72B-Instruct", label: "Qwen/Qwen2.5-72B-Instruct" },
    { value: "deepseek-ai/DeepSeek-V3", label: "deepseek-ai/DeepSeek-V3" },
  ],
};

function getPath(obj: Cfg, path: string): unknown { return path.split(".").reduce((o: unknown, k) => (o && typeof o === "object" ? (o as Cfg)[k] : undefined), obj); }
function setPath(obj: Cfg, path: string, value: unknown): Cfg { const keys = path.split("."); const result = { ...obj }; let node: Cfg = result; for (let i = 0; i < keys.length - 1; i++) { const k = keys[i]; node[k] = { ...(typeof node[k] === "object" && node[k] ? (node[k] as Cfg) : {}) }; node = node[k] as Cfg; } node[keys[keys.length - 1]] = value; return result; }
function parseListValue(input: string): string[] { return input.split(/[\n,，]/g).map((s) => s.trim()).filter(Boolean); }
function parseGroupVerbosityMap(input: string): Record<string, string> {
  const map: Record<string, string> = {};
  const alias: Record<string, string> = { "详细": "verbose", "中等": "medium", "简洁": "brief", "极简": "minimal" };
  const allowed = new Set(["verbose", "medium", "brief", "minimal"]);
  const lines = input.split(/\r?\n/g);
  for (const raw of lines) {
    const line = raw.trim();
    if (!line) continue;
    const m = line.match(/^(\d{5,20})\s*[:=，,\s]\s*(\S+)$/);
    if (!m) continue;
    const gid = m[1];
    const rawVerb = m[2].trim();
    const normalized = (alias[rawVerb] || rawVerb.toLowerCase()).trim();
    if (!allowed.has(normalized)) continue;
    map[gid] = normalized;
  }
  return map;
}
function formatGroupVerbosityMap(value: unknown): string {
  if (!value || typeof value !== "object" || Array.isArray(value)) return "";
  const rows = Object.entries(value as Record<string, unknown>)
    .filter(([k, v]) => !!k && !!v)
    .map(([k, v]) => [String(k), String(v).toLowerCase()] as const)
    .filter(([, v]) => ["verbose", "medium", "brief", "minimal"].includes(v))
    .sort((a, b) => Number(a[0]) - Number(b[0]));
  return rows.map(([gid, verbosity]) => `${gid}=${verbosity}`).join("\n");
}
function parseGroupTextMap(input: string): Record<string, string> {
  const map: Record<string, string> = {};
  const lines = input.split(/\r?\n/g);
  for (const raw of lines) {
    const line = raw.trim();
    if (!line || line.startsWith("#")) continue;
    let gid = "";
    let text = "";
    const pair = line.match(/^(\d{5,20})\s*(?:=|:|：|，|,)\s*(.+)$/);
    if (pair) {
      gid = pair[1];
      text = pair[2].trim();
    } else {
      const ws = line.match(/^(\d{5,20})\s+(.+)$/);
      if (!ws) continue;
      gid = ws[1];
      text = ws[2].trim();
    }
    if (!gid || !text) continue;
    map[gid] = text;
  }
  return map;
}
function formatGroupTextMap(value: unknown): string {
  if (!value || typeof value !== "object" || Array.isArray(value)) return "";
  const rows = Object.entries(value as Record<string, unknown>)
    .map(([gid, v]) => [String(gid).trim(), String(v ?? "").trim()] as const)
    .filter(([gid, text]) => /^\d{5,20}$/.test(gid) && !!text)
    .sort((a, b) => Number(a[0]) - Number(b[0]));
  return rows.map(([gid, text]) => `${gid}=${text}`).join("\n");
}
function parseNumberInput(raw: string, current: unknown, field: FieldDef): number {
  const text = raw.trim();
  const fallback = typeof current === "number"
    ? current
    : (typeof field.min === "number" ? field.min : 0);
  if (text === "" || text === "-" || text === "." || text === "-.") return fallback;
  const n = Number.parseFloat(text);
  if (!Number.isFinite(n)) return fallback;
  let next = n;
  if (typeof field.min === "number") next = Math.max(field.min, next);
  if (typeof field.max === "number") next = Math.min(field.max, next);
  return next;
}
function mergeDefaults(def: unknown, cur: unknown): unknown {
  if (Array.isArray(def)) return Array.isArray(cur) ? cur : [...def];
  if (def && typeof def === "object") {
    const base = def as Cfg; const current = (cur && typeof cur === "object" && !Array.isArray(cur)) ? (cur as Cfg) : {}; const out: Cfg = {};
    Object.keys(base).forEach((k) => { out[k] = mergeDefaults(base[k], current[k]); });
    Object.keys(current).forEach((k) => { if (!(k in out)) out[k] = current[k]; });
    return out;
  }
  return cur === undefined ? def : cur;
}
function withDefaults(raw: Cfg): Cfg { const merged = mergeDefaults(DEFAULT_CONFIG, raw); return merged && typeof merged === "object" && !Array.isArray(merged) ? (merged as Cfg) : { ...DEFAULT_CONFIG }; }

export default function ConfigPage() {
  const [config, setConfig] = useState<Cfg>({});
  const [fieldDrafts, setFieldDrafts] = useState<Record<string, string>>({});
  const [numberDrafts, setNumberDrafts] = useState<Record<string, string>>({});
  const [rawConfigText, setRawConfigText] = useState("");
  const [rawConfigError, setRawConfigError] = useState("");
  const [rawConfigDirty, setRawConfigDirty] = useState(false);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [msg, setMsg] = useState("");
  const [activeSection, setActiveSection] = useState("control");
  const [jsonMode, setJsonMode] = useState<"sections" | "raw">("sections");
  const [jsonSectionKey, setJsonSectionKey] = useState("control");
  const [jsonSectionText, setJsonSectionText] = useState("");
  const [jsonSectionError, setJsonSectionError] = useState("");
  const [imageGenTestPrompt, setImageGenTestPrompt] = useState("一只可爱的猫娘女仆，二次元插画，精致细节");
  const [imageGenTestModel, setImageGenTestModel] = useState("");
  const [imageGenTestSize, setImageGenTestSize] = useState("");
  const [imageGenTestStyle, setImageGenTestStyle] = useState("");
  const [imageGenTesting, setImageGenTesting] = useState(false);
  const [imageGenTestResult, setImageGenTestResult] = useState<ImageGenTestResponse | null>(null);
  const applyImageGenPreset = (prompt: string, mode: "replace" | "append" = "replace") => {
    if (mode === "append") {
      setImageGenTestPrompt((prev) => {
        const cur = String(prev || "").trim();
        if (!cur) return prompt;
        return `${cur}，${prompt}`;
      });
      return;
    }
    setImageGenTestPrompt(prompt);
  };
  const pickRandomImageGenPreset = () => {
    if (IMAGE_GEN_PROMPT_PRESETS.length === 0) return;
    const idx = Math.floor(Math.random() * IMAGE_GEN_PROMPT_PRESETS.length);
    applyImageGenPreset(IMAGE_GEN_PROMPT_PRESETS[idx].prompt, "replace");
  };

  const activeIndex = useMemo(() => Math.max(0, SECTIONS.findIndex((s) => s.key === activeSection)), [activeSection]);
  const active = SECTIONS[activeIndex];
  const topLevelJsonKeys = useMemo(() => {
    return Object.keys(config).filter((k) => typeof k === "string" && k.trim()).sort();
  }, [config]);

  const load = useCallback(async () => {
    try {
      const res = await api.getConfig();
      const merged = withDefaults((res.config || {}) as Cfg);
      setConfig(merged);
      setFieldDrafts({});
      setNumberDrafts({});
      setRawConfigText(JSON.stringify(merged, null, 2));
      setRawConfigError("");
      setRawConfigDirty(false);
    } catch (e: unknown) {
      setMsg(e instanceof Error ? e.message : "加载失败");
    } finally {
      setLoading(false);
    }
  }, []);
  useEffect(() => { load(); }, [load]);
  useEffect(() => {
    if (topLevelJsonKeys.length === 0) return;
    const key = topLevelJsonKeys.includes(jsonSectionKey) ? jsonSectionKey : topLevelJsonKeys[0];
    if (key !== jsonSectionKey) {
      setJsonSectionKey(key);
      return;
    }
    const sectionValue = getPath(config, key);
    setJsonSectionText(JSON.stringify(sectionValue, null, 2));
    setJsonSectionError("");
  }, [config, jsonSectionKey, topLevelJsonKeys]);

  useEffect(() => {
    if (!imageGenTestModel) {
      const fallbackModel = String(getPath(config, "image_gen.default_model") ?? "").trim();
      if (fallbackModel) setImageGenTestModel(fallbackModel);
    }
    if (!imageGenTestSize) {
      const fallbackSize = String(getPath(config, "image_gen.default_size") ?? "").trim();
      if (fallbackSize) setImageGenTestSize(fallbackSize);
    }
  }, [config, imageGenTestModel, imageGenTestSize]);

  const updateField = (path: string, value: unknown) => setConfig((prev) => {
    const next = setPath(prev, path, value);
    setFieldDrafts((drafts) => {
      if (!(path in drafts)) return drafts;
      const copied = { ...drafts };
      delete copied[path];
      return copied;
    });
    setNumberDrafts((drafts) => {
      if (!(path in drafts)) return drafts;
      const copied = { ...drafts };
      delete copied[path];
      return copied;
    });
    setRawConfigText(JSON.stringify(next, null, 2));
    setRawConfigError("");
    setRawConfigDirty(false);
    return next;
  });

  const commitMapDraft = useCallback((field: FieldDef, raw: string) => {
    if (field.type === "group_verbosity_map") {
      updateField(field.path, parseGroupVerbosityMap(raw));
      return;
    }
    if (field.type === "group_text_map") {
      updateField(field.path, parseGroupTextMap(raw));
    }
  }, []);

  const commitNumberDraft = useCallback((field: FieldDef, raw: string, current: unknown) => {
    updateField(field.path, parseNumberInput(raw, current, field));
  }, []);

  const applyPendingDrafts = useCallback((base: Cfg): Cfg => {
    let next = base;
    const fieldMap = new Map<string, FieldDef>();
    for (const section of SECTIONS) {
      for (const field of section.fields) fieldMap.set(field.path, field);
    }
    for (const [path, raw] of Object.entries(fieldDrafts)) {
      const field = fieldMap.get(path);
      if (!field) continue;
      if (field.type === "group_verbosity_map") {
        next = setPath(next, path, parseGroupVerbosityMap(raw));
      } else if (field.type === "group_text_map") {
        next = setPath(next, path, parseGroupTextMap(raw));
      }
    }
    for (const [path, raw] of Object.entries(numberDrafts)) {
      const field = fieldMap.get(path);
      if (!field || field.type !== "number") continue;
      const current = getPath(next, path);
      next = setPath(next, path, parseNumberInput(raw, current, field));
    }
    return next;
  }, [fieldDrafts, numberDrafts]);

  const resolveConfigForAction = useCallback((): Cfg => {
    let payload = applyPendingDrafts(config);
    if (rawConfigDirty) {
      const parsed = JSON.parse(rawConfigText);
      if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
        throw new Error("根节点必须是 JSON 对象");
      }
      payload = withDefaults(parsed as Cfg);
    }
    return withDefaults(payload);
  }, [applyPendingDrafts, config, rawConfigDirty, rawConfigText]);

  const applyRawConfig = () => {
    try {
      const parsed = JSON.parse(rawConfigText);
      if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) throw new Error("根节点必须是 JSON 对象");
      const merged = withDefaults(parsed as Cfg);
      setConfig(merged);
      setRawConfigText(JSON.stringify(merged, null, 2));
      setRawConfigError("");
      setRawConfigDirty(false);
      setMsg("全量配置 JSON 已应用到表单");
    } catch (e: unknown) {
      const detail = e instanceof Error ? e.message : "未知错误";
      setRawConfigError(`JSON 解析失败: ${detail}`);
      setMsg("全量配置 JSON 应用失败");
    }
  };

  const applySectionJson = () => {
    try {
      const parsed = JSON.parse(jsonSectionText);
      const next = setPath(config, jsonSectionKey, parsed);
      setConfig(next);
      setRawConfigText(JSON.stringify(next, null, 2));
      setRawConfigDirty(false);
      setJsonSectionError("");
      setMsg(`已应用片段：${jsonSectionKey}`);
    } catch (e: unknown) {
      const detail = e instanceof Error ? e.message : "未知错误";
      setJsonSectionError(`片段 JSON 解析失败: ${detail}`);
      setMsg("片段应用失败");
    }
  };

  const formatRawJson = () => {
    try {
      const parsed = JSON.parse(rawConfigText);
      const pretty = JSON.stringify(parsed, null, 2);
      setRawConfigText(pretty);
      setRawConfigError("");
    } catch (e: unknown) {
      const detail = e instanceof Error ? e.message : "未知错误";
      setRawConfigError(`JSON 格式化失败: ${detail}`);
    }
  };

  const handleSave = async () => {
    setSaving(true);
    setMsg("");
    try {
      const payload = resolveConfigForAction();
      const res = await api.updateConfig(payload);
      setMsg(res.ok ? "保存成功，已重载" : `失败: ${res.message}`);
      if (res.ok) await load();
    } catch (e: unknown) {
      const detail = e instanceof Error ? e.message : "未知错误";
      setRawConfigError(`JSON 解析失败: ${detail}`);
      setMsg("保存失败");
    } finally {
      setSaving(false);
    }
  };

  const handleTestImageGen = async () => {
    setImageGenTesting(true);
    setImageGenTestResult(null);
    setMsg("");
    try {
      const payload = resolveConfigForAction();
      const imageGenCfg = getPath(payload, "image_gen");
      const imageGenOverride = imageGenCfg && typeof imageGenCfg === "object" && !Array.isArray(imageGenCfg)
        ? (imageGenCfg as Record<string, unknown>)
        : undefined;

      const res = await api.testImageGen({
        prompt: imageGenTestPrompt.trim() || "一只可爱的猫娘女仆，二次元插画，精致细节",
        model: imageGenTestModel.trim() || undefined,
        size: imageGenTestSize.trim() || undefined,
        style: imageGenTestStyle.trim() || undefined,
        image_gen: imageGenOverride,
      });
      setImageGenTestResult(res);
      setMsg(res.ok ? "图片生成测试成功（未保存配置）" : `图片生成测试失败: ${res.message}`);
    } catch (e: unknown) {
      const detail = e instanceof Error ? e.message : "未知错误";
      setRawConfigError(`JSON 解析失败: ${detail}`);
      setMsg("图片生成测试失败");
    } finally {
      setImageGenTesting(false);
    }
  };

  const renderField = (field: FieldDef) => {
    const val = getPath(config, field.path);
    const wide = field.type === "textarea" || field.type === "group_verbosity_map" || field.type === "group_text_map";
    const cls = `${SHELL} ${wide ? "lg:col-span-2 2xl:col-span-3" : ""}`;
    const selected = val === undefined || val === null || String(val) === "" ? [] : [String(val)];

    const control = (() => {
      if (field.type === "switch") {
        return <div className="flex items-center justify-between gap-4"><div className="text-sm font-medium text-default-600">{field.label}</div><Switch isSelected={!!val} onValueChange={(v) => updateField(field.path, v)} /></div>;
      }
      if (field.type === "slider") {
        return <div className="space-y-3"><div className="text-sm font-medium text-default-600">{field.label}</div><Slider step={field.step || 1} minValue={field.min || 0} maxValue={field.max || 10} value={Number(val) || field.min || 0} onChange={(v) => updateField(field.path, Array.isArray(v) ? Number(v[0]) : Number(v))} /></div>;
      }
      if (field.type === "select") {
        const providerValue = String(getPath(config, "api.provider") ?? "");
        const options = field.path === "api.model" ? (MODEL_OPTIONS[providerValue] || []) : (field.options || []);
        return (
          <Select
            label={field.label}
            labelPlacement="outside"
            selectedKeys={selected}
            onSelectionChange={(keys) => {
              const arr = Array.from(keys);
              if (arr.length <= 0) return;
              const next = String(arr[0]);
              if (field.path === "api.provider") {
                updateField(field.path, next);
                const models = MODEL_OPTIONS[next] || [];
                if (models.length > 0) {
                  updateField("api.model", models[0].value);
                }
                return;
              }
              updateField(field.path, next);
            }}
            classNames={SELECT_CLASSES}
          >
            {options.map((o) => <SelectItem key={o.value}>{o.label}</SelectItem>)}
          </Select>
        );
      }
      if (field.type === "textarea") {
        return <Textarea label={field.label} labelPlacement="outside" minRows={field.rows || 2} maxRows={8} value={String(val ?? "")} onValueChange={(v) => updateField(field.path, v)} classNames={INPUT_CLASSES} />;
      }
      if (field.type === "password") {
        return <Input label={field.label} labelPlacement="outside" type="password" value={String(val ?? "")} onValueChange={(v) => updateField(field.path, v)} description={val === "***" ? "已加密，留空不修改" : undefined} classNames={INPUT_CLASSES} />;
      }
      if (field.type === "number") {
        const rawValue = numberDrafts[field.path];
        const inputValue = rawValue === undefined ? String(val ?? "") : rawValue;
        return (
          <Input
            label={field.label}
            labelPlacement="outside"
            type="number"
            value={inputValue}
            min={field.min}
            max={field.max}
            step={field.step}
            onValueChange={(v) => setNumberDrafts((prev) => ({ ...prev, [field.path]: v }))}
            onBlur={() => commitNumberDraft(field, inputValue, val)}
            onKeyDown={(e) => {
              if (e.key === "Enter") {
                commitNumberDraft(field, inputValue, val);
              }
            }}
            classNames={INPUT_CLASSES}
          />
        );
      }
      if (field.type === "list") {
        const list = Array.isArray(val) ? val.map((x) => String(x)) : [];
        return <Input label={field.label} labelPlacement="outside" value={list.join(", ")} onValueChange={(v) => updateField(field.path, parseListValue(v))} classNames={INPUT_CLASSES} />;
      }
      if (field.type === "group_verbosity_map" || field.type === "group_text_map") {
        const formatted = field.type === "group_verbosity_map" ? formatGroupVerbosityMap(val) : formatGroupTextMap(val);
        const draft = fieldDrafts[field.path];
        const textValue = draft === undefined ? formatted : draft;
        const description = field.type === "group_verbosity_map"
          ? "每行格式: 群号=verbose|medium|brief|minimal（支持中文别名：详细/中等/简洁/极简）"
          : "每行格式: 群号=输出指令，例如 ***REMOVED***=多用口语、最多两段";
        return (
          <Textarea
            label={field.label}
            labelPlacement="outside"
            minRows={field.rows || 4}
            maxRows={12}
            value={textValue}
            onValueChange={(v) => setFieldDrafts((prev) => ({ ...prev, [field.path]: v }))}
            onBlur={() => commitMapDraft(field, fieldDrafts[field.path] ?? textValue)}
            description={description}
            classNames={INPUT_CLASSES}
          />
        );
      }
      return <Input label={field.label} labelPlacement="outside" value={String(val ?? "")} onValueChange={(v) => updateField(field.path, v)} classNames={INPUT_CLASSES} />;
    })();

    return <motion.div key={field.path} className={cls} initial={{ opacity: 0, y: 4 }} animate={{ opacity: 1, y: 0 }} whileHover={{ y: -1 }} transition={{ duration: 0.16 }}>{control}</motion.div>;
  };

  if (loading) return <div className="flex justify-center py-20"><Spinner size="lg" /></div>;

  return (
    <div className="space-y-4 max-w-none">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-2"><h2 className="text-xl font-bold">配置编辑</h2><Chip size="sm" variant="flat" color="primary">{active.label}</Chip></div>
        <Button color="primary" startContent={<Save size={16} />} isLoading={saving} onPress={handleSave}>保存并重载</Button>
      </div>
      {msg && <p className={msg.includes("成功") ? "text-success" : "text-danger"}>{msg}</p>}

      <div className="sticky top-0 z-20 rounded-xl border border-default-400/35 bg-background/85 backdrop-blur-md p-2">
        <div className="flex flex-wrap items-center gap-2">
          {SECTIONS.map((section) => <Button key={section.key} size="sm" radius="full" variant={activeSection === section.key ? "solid" : "flat"} color={activeSection === section.key ? "primary" : "default"} onPress={() => setActiveSection(section.key)}>{section.label}</Button>)}
        </div>
      </div>

      <Card className="border border-default-400/35 bg-content1/40 backdrop-blur-md">
        <CardHeader className="flex items-center justify-between gap-3">
          <div className="font-semibold">{active.label}</div>
          <div className="flex items-center gap-2">
            <Button size="sm" variant="flat" startContent={<ChevronLeft size={14} />} isDisabled={activeIndex <= 0} onPress={() => setActiveSection(SECTIONS[Math.max(0, activeIndex - 1)].key)}>上一段</Button>
            <Button size="sm" variant="flat" endContent={<ChevronRight size={14} />} isDisabled={activeIndex >= SECTIONS.length - 1} onPress={() => setActiveSection(SECTIONS[Math.min(SECTIONS.length - 1, activeIndex + 1)].key)}>下一段</Button>
          </div>
        </CardHeader>
        <CardBody>
          <div className="grid grid-cols-2 gap-4">{active.fields.map(renderField)}</div>
        </CardBody>
      </Card>

      {active.key === "image_gen" && (
        <Card className="border border-default-400/35 bg-content1/35 backdrop-blur-sm">
          <CardHeader className="flex items-center justify-between gap-2">
            <div className="font-semibold">测试生成（不保存配置）</div>
            <Chip size="sm" variant="flat" color="primary">开箱即用验证</Chip>
          </CardHeader>
          <CardBody className="space-y-3">
            <p className="text-xs text-default-500">
              会使用你当前页面里的配置（包含未保存修改）进行一次真实图片生成测试。
            </p>
            <div className="space-y-2">
              <div className="text-xs text-default-500">提示词模板（点击快速填充）</div>
              <div className="flex flex-wrap items-center gap-2">
                {IMAGE_GEN_PROMPT_PRESETS.map((preset) => (
                  <Chip
                    key={preset.label}
                    variant="flat"
                    color="default"
                    className="cursor-pointer"
                    onClick={() => applyImageGenPreset(preset.prompt, "replace")}
                  >
                    {preset.label}
                  </Chip>
                ))}
                <Button size="sm" variant="flat" onPress={pickRandomImageGenPreset}>
                  随机模板
                </Button>
                <Button
                  size="sm"
                  variant="flat"
                  onPress={() => applyImageGenPreset("高质量，构图清晰，细节丰富，避免畸形手部和错位五官", "append")}
                >
                  追加质量词
                </Button>
              </div>
            </div>
            <div className="grid grid-cols-1 lg:grid-cols-2 gap-3">
              <Textarea
                label="测试提示词"
                labelPlacement="outside"
                minRows={3}
                maxRows={6}
                value={imageGenTestPrompt}
                onValueChange={setImageGenTestPrompt}
                classNames={INPUT_CLASSES}
              />
              <div className="space-y-3">
                <Input
                  label="测试模型（可留空=走默认模型）"
                  labelPlacement="outside"
                  value={imageGenTestModel}
                  onValueChange={setImageGenTestModel}
                  classNames={INPUT_CLASSES}
                />
                <Select
                  label="测试尺寸"
                  labelPlacement="outside"
                  selectedKeys={imageGenTestSize ? [imageGenTestSize] : []}
                  onSelectionChange={(keys) => {
                    const arr = Array.from(keys);
                    setImageGenTestSize(arr.length > 0 ? String(arr[0]) : "");
                  }}
                  classNames={SELECT_CLASSES}
                >
                  {["1024x1024", "1792x1024", "1024x1792"].map((size) => (
                    <SelectItem key={size}>{size}</SelectItem>
                  ))}
                </Select>
                <Input
                  label="测试风格（可选）"
                  labelPlacement="outside"
                  value={imageGenTestStyle}
                  onValueChange={setImageGenTestStyle}
                  classNames={INPUT_CLASSES}
                />
              </div>
            </div>

            <div className="flex flex-wrap items-center gap-2">
              <Button color="primary" isLoading={imageGenTesting} onPress={handleTestImageGen}>
                开始测试生成
              </Button>
            </div>

            {imageGenTestResult && (
              <div className="rounded-xl border border-default-400/35 bg-content2/35 p-3 space-y-2">
                <p className={imageGenTestResult.ok ? "text-success text-sm" : "text-danger text-sm"}>
                  {imageGenTestResult.message}
                </p>
                <div className="text-xs text-default-500 flex flex-wrap gap-3">
                  <span>请求模型: {imageGenTestResult.requested_model || "(默认)"}</span>
                  <span>实际模型: {imageGenTestResult.model_used || "-"}</span>
                  <span>默认模型: {imageGenTestResult.default_model || "-"}</span>
                  <span>已配模型数: {Number(imageGenTestResult.configured_models ?? 0)}</span>
                </div>
                {imageGenTestResult.revised_prompt && (
                  <p className="text-xs text-default-500 break-all">
                    revised_prompt: {imageGenTestResult.revised_prompt}
                  </p>
                )}
                {imageGenTestResult.image_url && (
                  <div className="pt-1">
                    <img
                      src={imageGenTestResult.image_url}
                      alt="image-gen-test"
                      className="max-h-80 rounded-lg border border-default-300/40"
                    />
                  </div>
                )}
              </div>
            )}
          </CardBody>
        </Card>
      )}

      <Card className="border border-default-400/35 bg-content1/35 backdrop-blur-sm">
        <CardHeader className="flex flex-wrap items-center justify-between gap-2">
          <div className="font-semibold">JSON编辑区</div>
          <Tabs
            selectedKey={jsonMode}
            onSelectionChange={(key) => setJsonMode(String(key) as "sections" | "raw")}
            size="sm"
            color="primary"
            variant="bordered"
            className="max-w-full"
          >
            <Tab key="sections" title="结构浏览/片段编辑" />
            <Tab key="raw" title="全量原始 JSON" />
          </Tabs>
        </CardHeader>
        <CardBody>
          {jsonMode === "sections" ? (
            <div className="grid grid-cols-1 lg:grid-cols-[240px_1fr] gap-3">
              <div className="rounded-xl border border-default-400/30 bg-content2/40 p-2 max-h-[420px] overflow-auto">
                <div className="text-xs text-default-500 px-2 py-1">顶级 JSON 段</div>
                <div className="flex flex-col gap-1">
                  {topLevelJsonKeys.map((key) => (
                    <Button
                      key={key}
                      size="sm"
                      variant={jsonSectionKey === key ? "flat" : "light"}
                      color={jsonSectionKey === key ? "primary" : "default"}
                      className="justify-start"
                      onPress={() => setJsonSectionKey(key)}
                    >
                      {key}
                    </Button>
                  ))}
                </div>
              </div>
              <div className="space-y-3">
                <Textarea
                  label={`JSON 片段：${jsonSectionKey}`}
                  labelPlacement="outside"
                  minRows={12}
                  maxRows={22}
                  value={jsonSectionText}
                  onValueChange={setJsonSectionText}
                  description={jsonSectionError || "仅修改当前片段，应用后会同步到表单和全量 JSON"}
                  color={jsonSectionError ? "danger" : "default"}
                  classNames={INPUT_CLASSES}
                />
                <div className="flex flex-wrap gap-2">
                  <Button variant="flat" onPress={applySectionJson}>应用当前片段</Button>
                  <Button
                    variant="light"
                    onPress={() => navigator.clipboard.writeText(jsonSectionText).catch(() => {})}
                  >
                    复制片段
                  </Button>
                </div>
              </div>
            </div>
          ) : (
            <div className="space-y-3">
              <Textarea
                label="全量配置 JSON（可编辑）"
                labelPlacement="outside"
                minRows={12}
                maxRows={30}
                value={rawConfigText}
                onValueChange={(v) => { setRawConfigText(v); setRawConfigDirty(true); }}
                description={rawConfigError || (rawConfigDirty ? "有未应用的 JSON 修改，保存时会优先用这里的内容" : undefined)}
                color={rawConfigError ? "danger" : "default"}
                classNames={INPUT_CLASSES}
              />
              <div className="flex flex-wrap gap-2">
                <Button variant="flat" onPress={applyRawConfig}>应用 JSON 到表单</Button>
                <Button variant="light" onPress={formatRawJson}>格式化 JSON</Button>
              </div>
            </div>
          )}
        </CardBody>
      </Card>
    </div>
  );
}
