"""Prompt Navigator configuration and section switching helpers."""
from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Any
from urllib.parse import urlparse

from utils.text import clip_text, normalize_text

_URL_RE = re.compile(r"https?://[^\s<>\"]+", re.IGNORECASE)
_BARE_WEB_HOST_RE = re.compile(
    r"(?<![@A-Za-z0-9_.-])"
    r"((?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+"
    r"(?:com|net|org|dev|io|ai|app|site|xyz|me|co|cn|jp|tv|gg|cc|info|wiki|top)"
    r"(?::\d{2,5})?(?:/[A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=%-]*)?)",
    re.IGNORECASE,
)
_DOWNLOAD_EXT_RE = re.compile(r"\.(?:apk|exe|msi|zip|7z|rar|ipa|dmg)(?:[?#]|$)", re.IGNORECASE)
_VIDEO_EXT_RE = re.compile(r"\.(?:mp4|webm|mov|m4v|mkv)(?:[?#]|$)", re.IGNORECASE)
_VIDEO_DOMAINS = (
    "bilibili.com",
    "b23.tv",
    "douyin.com",
    "iesdouyin.com",
    "kuaishou.com",
    "acfun.cn",
    "ixigua.com",
    "iqiyi.com",
    "qiyi.com",
    "iq.com",
    "youtube.com",
    "youtu.be",
    "tiktok.com",
    "v.qq.com",
    "m.v.qq.com",
)

NAVIGATE_SECTION_TOOL = "navigate_section"
CONTROL_TOOLS = ("think", "final_answer", NAVIGATE_SECTION_TOOL)


@dataclass(slots=True)
class PromptSection:
    id: str
    name: str = ""
    when_to_use: str = ""
    tools: list[str] = field(default_factory=list)
    instructions: str = ""
    fallback_sections: list[str] = field(default_factory=list)
    failure_policy: str = ""


@dataclass(slots=True)
class PromptNavigatorConfig:
    enable: bool = True
    mode: str = "local_prefilter_llm_review"
    strict_tool_routing: bool = True
    default_section: str = "general_chat"
    max_switches: int = 3
    root_prompt: str = ""
    sections: dict[str, PromptSection] = field(default_factory=dict)


@dataclass(slots=True)
class NavigatorState:
    active_section: str
    candidate_sections: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    visible_tools: list[str] = field(default_factory=list)
    switch_count: int = 0
    visited_sections: list[str] = field(default_factory=list)


def _as_bool(value: Any, default: bool = True) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        text = normalize_text(value).lower()
        if text in {"1", "true", "yes", "on", "enabled"}:
            return True
        if text in {"0", "false", "no", "off", "disabled"}:
            return False
    return default


def _as_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [normalize_text(str(item)) for item in value if normalize_text(str(item))]
    if isinstance(value, tuple):
        return [normalize_text(str(item)) for item in value if normalize_text(str(item))]
    if isinstance(value, str):
        parts: list[str] = []
        for line in value.replace(",", "\n").splitlines():
            text = normalize_text(line)
            if text:
                parts.append(text)
        return parts
    return []


def default_prompt_navigator_payload() -> dict[str, Any]:
    """Default editable Prompt Navigator graph."""
    return {
        "enable": True,
        "mode": "local_prefilter_llm_review",
        "strict_tool_routing": True,
        "default_section": "general_chat",
        "max_switches": 3,
        "root_prompt": (
            "你现在使用 Prompt Navigator。先阅读分区目录，只在当前分区工具足够时执行；"
            "如果当前分区缺少工具、说明不匹配、或任务需要跨能力，先调用 navigate_section(section_id, reason) "
            "切到更合适的分区。切换后按新分区提示和新工具范围继续，不要臆造工具结果。"
        ),
        "sections": {
            "general_chat": {
                "name": "普通对话与澄清",
                "when_to_use": "没有明确结构化媒体、链接、下载、管理或记忆任务时，从这里开始。",
                "tools": ["think", "final_answer", "navigate_section"],
                "instructions": (
                    "先判断是否能直接自然回复。若用户实际在要联网、看图、解析链接、下载、点歌、记忆或管理，"
                    "不要硬答，调用 navigate_section 切到对应分区。"
                ),
                "fallback_sections": [
                    "web_research",
                    "multimodal_media",
                    "memory_knowledge",
                    "fallback_debug",
                ],
                "failure_policy": "不确定任务类型时，用一句话澄清；发现缺工具时切分区。",
            },
            "multimodal_media": {
                "name": "图片语音本地视频",
                "when_to_use": "当前消息或引用消息携带图片、语音、视频文件等媒体结构。",
                "tools": [
                    "analyze_image",
                    "ocr_image",
                    "analyze_voice",
                    "analyze_local_video",
                    "split_video",
                    "think",
                    "final_answer",
                    "navigate_section",
                ],
                "instructions": (
                    "优先针对用户当前附带媒体或引用媒体。图片理解用 analyze_image，提取文字用 ocr_image，"
                    "语音用 analyze_voice，本地视频内容理解用 analyze_local_video，切片/抽音频/封面/关键帧用 split_video。"
                ),
                "fallback_sections": ["video_url", "creative_generation", "fallback_debug"],
                "failure_policy": "媒体缺失或无法读取时，说明缺少哪类媒体并请求用户重发或改发直链。",
            },
            "video_url": {
                "name": "视频链接解析与分析",
                "when_to_use": "消息中有视频平台链接或视频文件 URL，需要解析、提取音频/封面/直链或总结内容。",
                "tools": [
                    "parse_video",
                    "analyze_video",
                    "split_video",
                    "fetch_webpage",
                    "think",
                    "final_answer",
                    "navigate_section",
                ],
                "instructions": (
                    "拿可发送视频直链优先 parse_video；理解/总结视频内容优先 analyze_video；"
                    "抽音频、切片、封面、关键帧用 split_video。链接后带说明文字时，仍以提取出的 URL 为目标。"
                ),
                "fallback_sections": ["web_research", "download_resources", "fallback_debug"],
                "failure_policy": "解析失败时换 analyze_video 或 fetch_webpage 验证页面；仍失败则回报平台限制和下一步。",
            },
            "web_research": {
                "name": "网页检索与阅读",
                "when_to_use": "用户要查外部事实、打开网页、读链接、搜资料、查 GitHub 或需要最新信息。",
                "tools": [
                    "web_search",
                    "fetch_webpage",
                    "scrape_extract",
                    "scrape_summarize",
                    "scrape_structured",
                    "scrape_follow_links",
                    "github_search",
                    "github_readme",
                    "douyin_search",
                    "think",
                    "final_answer",
                    "navigate_section",
                ],
                "instructions": (
                    "外部事实先工具验证。搜索 query 要具体；打开已有 URL 用 fetch_webpage；"
                    "网页内容复杂时再用 scrape_extract/summarize/structured/follow_links。"
                ),
                "fallback_sections": ["video_url", "download_resources", "memory_knowledge", "fallback_debug"],
                "failure_policy": "搜索无结果时换一个更具体查询；仍失败就说明查不到的范围和原因。",
            },
            "media_search": {
                "name": "图片视频检索与推送",
                "when_to_use": "用户想看、找、发某个主题的视频、图片、壁纸、头像、GIF，但没有给出具体可解析链接。",
                "tools": [
                    "search_media",
                    "search_web_media",
                    "web_search",
                    "parse_video",
                    "think",
                    "final_answer",
                    "navigate_section",
                ],
                "instructions": (
                    "用户要看某主题视频/图片时，先 search_media；media_type 按需求填 video/image/gif。"
                    "search_media 若返回 video_url/image_url，final_answer 必须携带该媒体，不要只给文字。"
                    "如果结果明显不唯一或主题含糊，先用一句话向用户确认候选；确认后再解析/发送。"
                    "用户指定平台时在 query 中保留平台词或 site: 限定。"
                ),
                "fallback_sections": ["video_url", "web_research", "creative_generation", "fallback_debug"],
                "failure_policy": "找不到可发送媒体时给出候选来源或询问更具体的关键词，不要假装已经发送。",
            },
            "download_resources": {
                "name": "资源下载与文件候选",
                "when_to_use": "用户要安装包、文件直链、下载候选，或 URL/文件名带下载型扩展名。",
                "tools": [
                    "search_download_resources",
                    "smart_download",
                    "web_search",
                    "fetch_webpage",
                    "scrape_extract",
                    "think",
                    "final_answer",
                    "navigate_section",
                ],
                "instructions": (
                    "先识别平台和扩展名，再找真实候选链接。smart_download 只能传真实直链或高可信候选，"
                    "遇到 HTML 壳/签名不匹配要换源或回到资源检索。第三方来源需按当前安全策略处理。"
                ),
                "fallback_sections": ["web_research", "video_url", "fallback_debug"],
                "failure_policy": "下载失败要给出已尝试路径、失败原因和可执行替代候选。",
            },
            "music_audio": {
                "name": "音乐点歌与音频",
                "when_to_use": "用户要点歌、找歌、播放音乐、提取音频或发送音乐卡片。",
                "tools": [
                    "music_search",
                    "music_play_by_id",
                    "music_play",
                    "bilibili_audio_extract",
                    "send_music_card",
                    "parse_video",
                    "split_video",
                    "web_search",
                    "think",
                    "final_answer",
                    "navigate_section",
                ],
                "instructions": (
                    "点歌先 music_search，再基于返回结果选择 music_play_by_id。能识别时拆出歌名/歌手；"
                    "第三方视频回退链只在用户允许时使用。"
                ),
                "fallback_sections": ["video_url", "web_research", "fallback_debug"],
                "failure_policy": "版本不明确或只有试听时，先澄清歌手/版本，不要乱播。",
            },
            "creative_generation": {
                "name": "创作生成与富消息",
                "when_to_use": "用户要画图、生图、语音合成、JSON 卡片、合并转发或创作型输出。",
                "tools": [
                    "generate_image_enhanced",
                    "generate_image",
                    "list_image_models",
                    "send_json_card",
                    "send_forward_message",
                    "think",
                    "final_answer",
                    "navigate_section",
                ],
                "instructions": (
                    "生成图片优先 generate_image_enhanced；需要模型列表用 list_image_models；"
                    "长内容展示可用 send_forward_message。"
                ),
                "fallback_sections": ["multimodal_media", "web_research", "fallback_debug"],
                "failure_policy": "生成失败时保留用户目标，说明失败原因并给一个可重试的简化方案。",
            },
            "qq_admin_social": {
                "name": "QQ 群管理与社交资料",
                "when_to_use": "用户要操作群、成员、消息、公告、名片、资料、Qzone、头像或社交关系。",
                "tools": [
                    "get_group_member_list",
                    "get_group_info",
                    "get_user_info",
                    "get_message",
                    "delete_message",
                    "recall_recent_messages",
                    "set_group_ban",
                    "set_group_card",
                    "set_group_kick",
                    "set_group_special_title",
                    "send_group_notice",
                    "set_group_whole_ban",
                    "set_group_admin",
                    "set_group_name",
                    "get_qzone_profile",
                    "get_qzone_moods",
                    "get_qzone_albums",
                    "get_qzone_photos",
                    "analyze_qzone",
                    "send_poke",
                    "send_like",
                    "think",
                    "final_answer",
                    "navigate_section",
                ],
                "instructions": (
                    "严格使用当前用户权限和本轮对象解析。@对象通常是操作对象；回复消息通常是引用对象。"
                    "群管理操作需要明确点名机器人，并遵守高风险确认。"
                ),
                "fallback_sections": ["memory_knowledge", "web_research", "fallback_debug"],
                "failure_policy": "权限不足或对象不明确时，不执行，先说明需要的权限或目标。",
            },
            "memory_knowledge": {
                "name": "记忆知识与长期上下文",
                "when_to_use": "用户要记住、回忆、修正记忆、查询知识库或总结对话。",
                "tools": [
                    "memory_list",
                    "memory_add",
                    "memory_update",
                    "memory_delete",
                    "memory_audit",
                    "memory_compact",
                    "remember_user_fact",
                    "recall_about_user",
                    "search_knowledge",
                    "learn_knowledge",
                    "summarize_conversation",
                    "think",
                    "final_answer",
                    "navigate_section",
                ],
                "instructions": (
                    "只把明确、可归属、用户授权的事实写入记忆；偏好和身份不要套给其他人。"
                    "回忆时优先引用当前用户相关记录。"
                ),
                "fallback_sections": ["web_research", "general_chat", "fallback_debug"],
                "failure_policy": "无法确认事实归属时先澄清，不要写入模糊记忆。",
            },
            "fallback_debug": {
                "name": "兜底排错与安全收敛",
                "when_to_use": "当前分区无法处理、工具连续失败、参数缺失、任务跨域或需要安全收敛。",
                "tools": ["think", "final_answer", "navigate_section"],
                "instructions": (
                    "整理已知信息、缺失条件、已失败工具和下一步。能切到明确分区就切；"
                    "不能切时用 final_answer 给用户一个清楚的状态和补充请求。"
                ),
                "fallback_sections": ["general_chat", "web_research", "multimodal_media"],
                "failure_policy": "停止循环，输出最小可用结论或向用户要一个关键补充。",
            },
        },
    }


def load_prompt_navigator_config(raw: Any) -> PromptNavigatorConfig:
    if not isinstance(raw, dict):
        raw = default_prompt_navigator_payload()
    defaults = default_prompt_navigator_payload()
    merged = dict(defaults)
    merged.update(raw)
    if not isinstance(merged.get("sections"), dict):
        merged["sections"] = defaults["sections"]

    sections: dict[str, PromptSection] = {}
    for section_id, value in (merged.get("sections") or {}).items():
        sid = normalize_text(str(section_id))
        if not sid or not isinstance(value, dict):
            continue
        sections[sid] = PromptSection(
            id=sid,
            name=normalize_text(str(value.get("name", ""))),
            when_to_use=normalize_text(str(value.get("when_to_use", ""))),
            tools=_as_list(value.get("tools")),
            instructions=normalize_text(str(value.get("instructions", ""))),
            fallback_sections=_as_list(value.get("fallback_sections")),
            failure_policy=normalize_text(str(value.get("failure_policy", ""))),
        )

    default_section = normalize_text(str(merged.get("default_section", "general_chat"))) or "general_chat"
    if default_section not in sections and sections:
        default_section = next(iter(sections.keys()))

    try:
        max_switches = int(merged.get("max_switches", 3))
    except (TypeError, ValueError):
        max_switches = 3
    max_switches = max(0, min(12, max_switches))

    return PromptNavigatorConfig(
        enable=_as_bool(merged.get("enable", True), default=True),
        mode=normalize_text(str(merged.get("mode", "local_prefilter_llm_review")))
        or "local_prefilter_llm_review",
        strict_tool_routing=_as_bool(
            merged.get("strict_tool_routing", True),
            default=True,
        ),
        default_section=default_section,
        max_switches=max_switches,
        root_prompt=normalize_text(str(merged.get("root_prompt", ""))),
        sections=sections,
    )


def validate_prompt_navigator_payload(
    raw: Any,
    known_tools: set[str] | list[str] | tuple[str, ...] | None = None,
) -> tuple[list[str], list[str]]:
    """Return (errors, warnings) for WebUI validation."""
    if raw is None:
        return [], []
    if not isinstance(raw, dict):
        return ["prompt_navigator 必须是对象"], []
    errors: list[str] = []
    warnings: list[str] = []
    sections_raw = raw.get("sections")
    if not isinstance(sections_raw, dict) or not sections_raw:
        errors.append("prompt_navigator.sections 必须是非空对象")
        return errors, warnings
    section_ids = {normalize_text(str(key)) for key in sections_raw.keys() if normalize_text(str(key))}
    default_section = normalize_text(str(raw.get("default_section", "")))
    if default_section and default_section not in section_ids:
        errors.append(f"默认分区不存在: {default_section}")
    known_tool_set = {normalize_text(str(name)) for name in (known_tools or []) if normalize_text(str(name))}

    for section_id, value in sections_raw.items():
        sid = normalize_text(str(section_id))
        if not sid:
            errors.append("存在空分区 ID")
            continue
        if not isinstance(value, dict):
            errors.append(f"分区 {sid} 必须是对象")
            continue
        for fallback in _as_list(value.get("fallback_sections")):
            if fallback not in section_ids:
                errors.append(f"分区 {sid} 的 fallback 不存在: {fallback}")
        if known_tool_set:
            for tool_name in _as_list(value.get("tools")):
                if tool_name not in known_tool_set:
                    warnings.append(f"分区 {sid} 引用了未知工具: {tool_name}")
    return errors, warnings


class PromptNavigator:
    def __init__(self, config: PromptNavigatorConfig) -> None:
        self.config = config

    @classmethod
    def from_payload(cls, raw: Any) -> "PromptNavigator":
        return cls(load_prompt_navigator_config(raw))

    @property
    def enabled(self) -> bool:
        return bool(self.config.enable and self.config.sections)

    def initial_state(self, ctx: Any, visible_tools: list[str]) -> NavigatorState:
        active, candidates, evidence = self._preselect(ctx)
        return NavigatorState(
            active_section=active,
            candidate_sections=candidates,
            evidence=evidence,
            visible_tools=list(visible_tools),
            switch_count=0,
            visited_sections=[active],
        )

    def scoped_tools(self, state: NavigatorState) -> list[str]:
        visible = {normalize_text(str(name)) for name in state.visible_tools if normalize_text(str(name))}
        section = self.config.sections.get(state.active_section)
        requested = list(CONTROL_TOOLS)
        if section:
            requested.extend(section.tools)
        return _dedupe([name for name in requested if name in visible])

    def switch_section(self, state: NavigatorState, section_id: str) -> tuple[bool, str]:
        target = normalize_text(section_id)
        if not target:
            return False, "missing_section_id"
        if target not in self.config.sections:
            return False, f"unknown_section:{target}"
        if state.switch_count >= self.config.max_switches:
            return False, f"max_switches_reached:{self.config.max_switches}"
        if target == state.active_section:
            return True, "same_section"
        state.active_section = target
        state.switch_count += 1
        if target not in state.visited_sections:
            state.visited_sections.append(target)
        if target not in state.candidate_sections:
            state.candidate_sections.append(target)
        return True, "switched"

    def render_system_block(self, state: NavigatorState, scoped_tools: list[str]) -> str:
        section = self.config.sections.get(state.active_section)
        lines: list[str] = ["## Prompt Navigator"]
        if self.config.root_prompt:
            lines.append(self.config.root_prompt)
        lines.append(f"模式: {self.config.mode}")
        if state.evidence:
            lines.append("本地结构信号: " + "；".join(state.evidence[:6]))
        if state.candidate_sections:
            lines.append("候选分区: " + ", ".join(state.candidate_sections))
        lines.append("协议: 当前分区不够用或缺少工具时，先调用 navigate_section(section_id, reason) 切分区；切换后再调用新分区工具。")
        lines.append("")
        lines.append("分区目录:")
        for sid, item in self.config.sections.items():
            label = item.name or sid
            when = item.when_to_use or "按分区说明判断"
            fallbacks = ", ".join(item.fallback_sections) if item.fallback_sections else "-"
            tools = ", ".join(item.tools[:12]) if item.tools else "-"
            lines.append(f"- {sid} ({label}): {when} | tools: {tools} | fallback: {fallbacks}")
        lines.append("")
        lines.append(self.render_active_section_block(state, scoped_tools))
        return "\n".join(lines).strip()

    def render_active_section_block(self, state: NavigatorState, scoped_tools: list[str]) -> str:
        section = self.config.sections.get(state.active_section)
        if not section:
            return f"当前分区: {state.active_section}\n当前分区未找到，请切到 fallback_debug。"
        lines = [
            f"当前分区: {section.id} ({section.name or section.id})",
            f"使用条件: {section.when_to_use or '-'}",
            f"分区指令: {section.instructions or '-'}",
            f"失败策略: {section.failure_policy or '-'}",
            "当前分区可见工具: " + (", ".join(scoped_tools) if scoped_tools else "-"),
            "建议 fallback: " + (", ".join(section.fallback_sections) if section.fallback_sections else "-"),
        ]
        return "\n".join(lines)

    def render_switch_result(
        self,
        state: NavigatorState,
        scoped_tools: list[str],
        tool_docs: str = "",
    ) -> str:
        block = self.render_active_section_block(state, scoped_tools)
        if normalize_text(tool_docs):
            block += "\n\n新分区工具 schema:\n" + clip_text(tool_docs, 2400)
        return block

    def _preselect(self, ctx: Any) -> tuple[str, list[str], list[str]]:
        candidates: list[str] = []
        evidence: list[str] = []

        def add(section_id: str, why: str) -> None:
            if section_id not in self.config.sections:
                return
            if section_id not in candidates:
                candidates.append(section_id)
            if why and why not in evidence:
                evidence.append(why)

        urls = self._collect_urls(ctx)
        segment_kinds = self._collect_segment_kinds(ctx)
        recent_artifact = getattr(ctx, "recent_media_artifact", None)
        if isinstance(recent_artifact, dict):
            artifact_type = normalize_text(str(recent_artifact.get("type", ""))).lower()
            artifact_video = normalize_text(
                str(
                    recent_artifact.get("video_url", "")
                    or recent_artifact.get("video_file", "")
                    or recent_artifact.get("path", "")
                )
            )
            artifact_images = recent_artifact.get("image_urls", [])
            if artifact_type == "video" or artifact_video:
                add("video_url", "recent_media_artifact")
            elif artifact_type in {"image", "images"} or artifact_images:
                add("multimodal_media", "recent_media_artifact")
        if {"image", "voice", "audio", "video"} & segment_kinds:
            add("multimodal_media", "message_or_reply_media")
        if any(self._looks_like_video_url(url) for url in urls):
            add("video_url", "video_url")
        if any(_DOWNLOAD_EXT_RE.search(url) for url in urls):
            add("download_resources", "download_file_extension")
        if urls:
            add("web_research", "url")
        if self._looks_like_media_search_request(ctx):
            add("media_search", "media_search_request")
        if self._looks_like_web_research_request(ctx):
            add("web_research", "external_research_request")
        if getattr(ctx, "at_other_user_ids", None):
            add("qq_admin_social", "mention_target")

        default = self.config.default_section
        if default in self.config.sections and default not in candidates:
            candidates.append(default)
        if "fallback_debug" in self.config.sections and "fallback_debug" not in candidates:
            candidates.append("fallback_debug")
        active = candidates[0] if candidates else default
        return active, candidates, evidence

    @staticmethod
    def _collect_segment_kinds(ctx: Any) -> set[str]:
        kinds: set[str] = set()
        for attr in ("raw_segments", "reply_media_segments"):
            for segment in getattr(ctx, attr, None) or []:
                if not isinstance(segment, dict):
                    continue
                seg_type = normalize_text(str(segment.get("type", ""))).lower()
                data = segment.get("data", {})
                if seg_type in {"image", "pic", "picture"}:
                    kinds.add("image")
                elif seg_type in {"record", "voice", "audio", "ptt"}:
                    kinds.add("voice")
                elif seg_type in {"video", "shortvideo"}:
                    kinds.add("video")
                if isinstance(data, dict):
                    if data.get("image") or data.get("url") and seg_type == "image":
                        kinds.add("image")
                    if data.get("file") and seg_type in {"record", "voice"}:
                        kinds.add("voice")
        return kinds

    @staticmethod
    def _collect_urls(ctx: Any) -> list[str]:
        parts: list[str] = []
        for attr in ("message_text", "original_message_text", "reply_to_text"):
            text = normalize_text(str(getattr(ctx, attr, "") or ""))
            if text:
                parts.append(text)
        for attr in ("media_summary", "reply_media_summary"):
            for item in getattr(ctx, attr, None) or []:
                text = normalize_text(str(item))
                if text:
                    parts.append(text)
        recent_artifact = getattr(ctx, "recent_media_artifact", None)
        if isinstance(recent_artifact, dict):
            for key in ("video_url", "video_file", "image_url", "url", "source_url", "path"):
                text = normalize_text(str(recent_artifact.get(key, "")))
                if text:
                    parts.append(text)
            raw_image_urls = recent_artifact.get("image_urls", [])
            if isinstance(raw_image_urls, list):
                for item in raw_image_urls:
                    text = normalize_text(str(item))
                    if text:
                        parts.append(text)
        for attr in ("raw_segments", "reply_media_segments"):
            for segment in getattr(ctx, attr, None) or []:
                if not isinstance(segment, dict):
                    continue
                data = segment.get("data", {})
                if isinstance(data, dict):
                    for key in ("url", "file", "path"):
                        text = normalize_text(str(data.get(key, "")))
                        if text:
                            parts.append(text)
        urls: list[str] = []
        for part in parts:
            for match in _URL_RE.findall(part):
                url = match.rstrip(").,，。!?！？】》」』")
                if url not in urls:
                    urls.append(url)
            for match in _BARE_WEB_HOST_RE.findall(part):
                url = "https://" + match.rstrip(").,，。!?！？】》」』")
                if url not in urls:
                    urls.append(url)
        return urls

    @staticmethod
    def _looks_like_video_url(url: str) -> bool:
        text = normalize_text(url).lower()
        if not text:
            return False
        if _VIDEO_EXT_RE.search(text):
            return True
        try:
            host = (urlparse(text).hostname or "").lower()
        except Exception:
            host = ""
        if not host:
            return False
        return any(host == domain or host.endswith("." + domain) for domain in _VIDEO_DOMAINS)

    @staticmethod
    def _looks_like_web_research_request(ctx: Any) -> bool:
        parts: list[str] = []
        for attr in ("message_text", "original_message_text", "reply_to_text"):
            text = normalize_text(str(getattr(ctx, attr, "") or ""))
            if text:
                parts.append(text)
        text = normalize_text(" ".join(parts)).lower()
        if not text:
            return False
        cues = (
            "搜索",
            "搜一下",
            "搜下",
            "查一下",
            "查下",
            "查查",
            "找一下",
            "找下",
            "你找",
            "找啊",
            "去找",
            "帮我找",
            "帮我查",
            "网络时光机",
            "wayback",
            "官网",
            "教程",
            "攻略",
            "资料",
            "新闻",
            "最新",
            "下载地址",
        )
        return any(cue in text for cue in cues)

    @staticmethod
    def _looks_like_media_search_request(ctx: Any) -> bool:
        parts: list[str] = []
        for attr in ("message_text", "original_message_text", "reply_to_text"):
            text = normalize_text(str(getattr(ctx, attr, "") or ""))
            if text:
                parts.append(text)
        text = normalize_text(" ".join(parts)).lower()
        if not text:
            return False
        if _URL_RE.search(text):
            return False
        media_cues = (
            "视频",
            "影片",
            "片段",
            "图片",
            "壁纸",
            "头像",
            "gif",
            "动图",
            "image",
            "photo",
            "video",
            "clip",
            "youtube",
            "b站",
            "bilibili",
            "抖音",
            "douyin",
            "快手",
            "kuaishou",
            "acfun",
            "爱奇艺",
            "iqiyi",
            "腾讯视频",
            "v.qq.com",
        )
        action_cues = (
            "找",
            "搜",
            "看",
            "发",
            "给我",
            "来个",
            "来张",
            "整",
            "推",
            "推荐",
            "want",
            "show",
            "send",
            "find",
            "search",
        )
        return any(cue in text for cue in media_cues) and any(cue in text for cue in action_cues)


def _dedupe(items: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        text = normalize_text(str(item))
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text)
    return out
