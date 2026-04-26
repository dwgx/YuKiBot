"""Agent 工具注册表 — re-export hub。

原始代码已拆分为以下子模块:
- agent_tools_types: 公共类型 (ToolSchema, ToolCallResult, PromptHint)
- agent_tools_registry: AgentToolRegistry + register_builtin_tools
- agent_tools_napcat: NapCat OneBot V11 API 工具
- agent_tools_search: 搜索工具
- agent_tools_media: 媒体工具
- agent_tools_admin: 管理员工具
- agent_tools_utility: 实用工具 + 表情系统
- agent_tools_memory: 记忆管理工具
- agent_tools_knowledge: 知识库 + 爬虫工具
- agent_tools_social: 社交功能工具
- agent_tools_web: AI Method / 爬虫 LLM 工具

所有 `from core.agent_tools import X` 保持向后兼容。
"""
# ── Types ──
# ── Admin (includes music handlers) ──
from core.agent_tools_admin import (  # noqa: F401
    _handle_music_play,
    _handle_music_play_by_id,
    _handle_music_search,
)

# ── Knowledge ──
from core.agent_tools_knowledge import (  # noqa: F401
    _handle_learn_knowledge,
    _handle_search_knowledge,
)

# ── Media ──
from core.agent_tools_media import (  # noqa: F401
    _handle_analyze_local_video,
    _handle_analyze_voice,
    _make_image_gen_handler,
)

# ── Memory ──
from core.agent_tools_memory import _register_memory_tools  # noqa: F401

# ── NapCat handlers ──
from core.agent_tools_napcat import (  # noqa: F401
    _build_onebot_message_segments,
    _handle_delete_message,
    _handle_forward_message,
    _handle_generic_napcat_api,
    _handle_get_chat_history,
    _handle_get_friend_list,
    _handle_get_group_file_url,
    _handle_get_group_files,
    _handle_get_group_history,
    _handle_get_group_honor_info,
    _handle_get_group_info,
    _handle_get_group_list,
    _handle_get_group_member_list,
    _handle_get_group_notice,
    _handle_get_login_info,
    _handle_get_message,
    _handle_get_muted_list,
    _handle_get_user_info,
    _handle_recall_recent_messages,
    _handle_send_group_message,
    _handle_send_group_notice,
    _handle_send_like,
    _handle_send_private_message,
    _handle_set_group_admin,
    _handle_set_group_ban,
    _handle_set_group_card,
    _handle_set_group_kick,
    _handle_set_group_name,
    _handle_set_group_sign,
    _handle_set_group_special_title,
    _handle_set_group_whole_ban,
    _handle_smart_download,
    _napcat_api_call,
    _verify_group_ban_applied,
)

# ── Registry ──
from core.agent_tools_registry import (  # noqa: F401
    AgentToolRegistry,
    register_builtin_tools,
)

# ── Social ──
from core.agent_tools_social import (  # noqa: F401
    _make_qzone_handler,
    _register_daily_report_tools,
    _register_qzone_tools,
)
from core.agent_tools_types import (  # noqa: F401
    ContextProvider,
    PromptHint,
    ToolCallResult,
    ToolHandler,
    ToolSchema,
)

# ── Utility + Sticker ──
from core.agent_tools_utility import (  # noqa: F401
    _handle_final_answer,
    _handle_send_emoji,
    _handle_think,
    _looks_like_explicit_sticker_send_message,
    _looks_like_sticker_management_message,
    _make_learn_sticker_handler,
    _should_block_sticker_send_for_management_turn,
    register_sticker_tools,
)

# ── Web ──
from core.agent_tools_web import (  # noqa: F401
    _register_ai_method_tools,
    _register_scrapy_llm_tools,
)
