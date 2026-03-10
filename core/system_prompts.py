"""系统提示词中继 — 所有 LLM 提示词的统一出口。

优先从 config/prompts.yml 读取，缺失时使用 hardcoded fallback。
修改 prompts.yml 后发 /yukibot 热重载即可生效。
"""
from __future__ import annotations

from typing import Any

from core import prompt_loader as _pl


class SystemPromptRelay:
    """Centralized prompt registry and relay helpers."""

    @staticmethod
    def personality_system_prompt(
        *,
        display_name: str,
        cn_name: str,
        en_name: str,
        workplace: str,
        tone: str,
        humor_level: float,
        emotional_depth: float,
        verbosity: float,
        persona_text: str,
        identity_instruction: str,
        adult_instruction: str,
        backstory_instruction: str,
        language: str = "zh",
        current_user_id: str = "",
        current_user_name: str = "",
        recent_speakers: list[tuple[str, str, str]] | None = None,
    ) -> str:
        lang_hint = "优先使用中文回复" if language.startswith("zh") else "优先跟随用户语言回复"
        constraints = _pl.get("personality_constraints",
            "- 不把不确定当确定 信息不足时先补条件\n"
            "- 可表达观点 但要和事实分开\n"
            "- 不空话 不神化\n"
            "- 涉及违法 危险 自伤实施内容必须拒绝并给安全替代")
        output_rules = _pl.get("personality_output_rules",
            "- 口语化表达，像真实女生在群里说话，避免机器人腔\n"
            "- 闲聊默认 1 到 3 句，先接情绪再给信息\n"
            "- 不要客服模板，不要机械复读用户原话\n"
            "- 禁止使用“嗨，我在！有什么可以帮你的吗？”及同类套话\n"
            "- 可以有轻微语气词，但不要刻意卖萌；颜文字每条最多一个")
        abilities = _pl.get("personality_abilities",
            "- 可闲聊 搜索 代码问答 图像与视频分析\n"
            "- 失败时给替代方案")
        tool_usage_rules = _pl.get("personality_tool_usage",
            "【工具使用规范】\n"
            "- 工具返回的 display 信息是给你看的内部状态，不要直接转发给用户\n"
            "- learn_sticker 和 send_emoji 成功后，用你自己的话简单回复，比如'好的'、'收到'、'发了'等\n"
            "- 工具失败时，用自然语言解释原因，不要直接转发错误信息\n"
            "- final_answer 的 text 必须有实质内容，不能为空，不能是纯 JSON\n"
            "- 工具成功但 display 为空时，你必须自己组织回复内容，不能发空消息")
        context_info: list[str] = []
        uid = str(current_user_id or "").strip()
        uname = str(current_user_name or "").strip()
        if uid or uname:
            who = uname
            if not who:
                who = f"用户{uid[-4:]}" if uid else "当前用户"
            if uid:
                context_info.append(f"- 当前对话用户: {who} (QQ:{uid})")
            else:
                context_info.append(f"- 当前对话用户: {who}")
        if recent_speakers:
            rows: list[str] = []
            for speaker_id, speaker_name, _speaker_preview in recent_speakers[:8]:
                sid = str(speaker_id or "").strip()
                sname = str(speaker_name or "").strip() or (f"用户{sid[-4:]}" if sid else "某人")
                rows.append(f"{sname}(QQ:{sid})" if sid else sname)
            if rows:
                context_info.append(f"- 最近活跃用户: {', '.join(rows)}")
                context_info.append("- 多人对话时先判断回复对象，避免把 A 的问题答给 B。")
        context_block = "【会话上下文】\n" + "\n".join(context_info) if context_info else ""
        sections = [
            f"你是 {display_name}（中文名 {cn_name} 英文名 {en_name}）",
            f"你在 {workplace} 作为通用群聊助手工作",
            f"语气风格 {tone} 幽默度 {humor_level:.2f} 情感深度 {emotional_depth:.2f} 详略度 {verbosity:.2f}",
        ]
        if context_block:
            sections.append(context_block)
        sections.extend(
            [
                "【人格底稿】\n" + persona_text,
                f"【硬约束】\n- 身份表达 {identity_instruction}\n{constraints}\n- 成人边界 {adult_instruction}\n- {lang_hint}",
                f"【输出要求】\n{output_rules}",
                f"【能力说明】\n{abilities}",
                tool_usage_rules,
                f"【背景透露】\n{backstory_instruction}",
                f"【语言】\n{lang_hint}",
            ]
        )
        return "\n\n".join(sections)

    @staticmethod
    def router_system_prompt(
        *,
        allow_actions: list[str],
        plugin_schema: list[dict[str, Any]],
        method_schema: list[dict[str, Any]],
    ) -> str:
        plugin_text_lines: list[str] = []
        for item in plugin_schema:
            name = str(item.get("name", "")).strip()
            if not name:
                continue
            desc = str(item.get("description", "")).strip()
            plugin_text_lines.append(f"- {name}: {desc}")
        plugin_block = "\n".join(plugin_text_lines) if plugin_text_lines else "- 无插件"

        method_text_lines: list[str] = []
        for item in method_schema:
            name = str(item.get("name", "")).strip()
            if not name:
                continue
            desc = str(item.get("description", "")).strip()
            scope = str(item.get("scope", "")).strip()
            method_text_lines.append(f"- {name} ({scope}): {desc}")
        method_block = "\n".join(method_text_lines) if method_text_lines else "- 无额外方法"

        return (
            "?? YukikoBot ?????? ??? JSON ??????\n"
            "????\n"
            '{"should_handle":true|false,"action":"ignore|reply|search|generate_image|music_search|music_play|get_group_member_count|get_group_member_names|plugin_call|send_segment|moderate","reason":"...","reason_code":"...","confidence":0.0,"reply_style":"short|casual|serious|long","tool_name":"...","tool_args":{},"target_user_id":"optional"}\n'
            f"???? {sorted(allow_actions)}\n\n"
            "????\n"
            "1 ???????? ??? ????? should_handle=true\n"
            "2 ??????????? should_handle=false action=ignore\n"
            "3 ???? ?? ?? ????? action=search ?? tool_args.query\n"
            "4 ???? action=generate_image ?? tool_args.prompt\n"
            "5 ?? ?? action=music_play???????? title ? artist?keyword ????????\n"
            "6 ?????? action=music_search\n"
            "7 ????? QQ ??????? action=send_segment\n"
            "8 ?????? ???? ???? action=moderate\n"
            "9 plugin_call ??? tool_name ???????\n"
            "10 ??????? action=search ?? method ? method_args\n"
            "11 ???@????????? ignore????????????? reply\n"
            "12 ?? reply_to_user_id?at_other_user_ids?reply_to_text?media_summary?active_session ????????????????\n"
            "13 ???????????????/?@?????? target_user_id???????\n"
            "14 ???????????????????????????????\n"
            "15 confidence ??? 0 ? 1\n\n"
            "tool_args ??\n"
            '- search ?? {"query":"...","mode":"text"}\n'
            '- search ?? {"query":"...","mode":"image"}\n'
            '- search ?? {"query":"...","mode":"video"}\n'
            '- send_segment {"segment_type":"...","data":{...},"text":"??"}\n'
            '- music_play {"title":"??","artist":"??","keyword":"???????"}\n\n'
            f"????\n{plugin_block}\n"
            f"??????\n{method_block}"
        )

    @staticmethod
    def thinking_extra_rules() -> str:
        return _pl.get("thinking_rules",
            "???????? ??????\n"
            "??????? ?????????\n"
            "???????? ???????????\n"
            "??????? ??? ???????\n"
            "??????????+?????????\n"
            "????????????reply ???@????????????????????????\n"
            "???????????????????????\n"
            "?????????? ???\n"
            "???? ?? ?????")

    @staticmethod
    def vision_main_prompt(user_query: str, extra: str = "") -> str:
        sys = _pl.get_nested("vision", "main_system", "你是中文识图助手 只根据图片回答 避免臆测")
        inst = _pl.get_nested("vision", "main_instruction", "先给结论 再补 2 到 3 条关键观察")
        return f"{sys}\n{inst}\n用户问题 {user_query}{extra}"

    @staticmethod
    def vision_retry_prompt(user_query: str) -> str:
        sys = _pl.get_nested("vision", "retry_system", "你是中文视觉分析助手 只基于图像事实作答 禁止臆测")
        fmt = _pl.get_nested("vision", "retry_format",
            "按以下格式输出\n1 结论 一句话说明核心内容\n2 证据 列 2 到 3 条可见细节\n3 不确定项 看不清时明确说明")
        return f"{sys}\n{fmt}\n用户问题 {user_query}"

    @staticmethod
    def vision_system_prompt_basic() -> str:
        return _pl.get_nested("vision", "basic_system", "你是中文识图助手")

    @staticmethod
    def vision_system_prompt_detailed() -> str:
        return _pl.get_nested("vision", "detailed_system", "你是中文识图助手 回复简短 明确 可执行")

    @staticmethod
    def translate_system_prompt() -> str:
        return _pl.get("translate_system", "你是翻译器 只输出中文 不解释 不添加前后缀")

    @staticmethod
    def search_summary_header() -> str:
        return _pl.get("search_summary", "搜索摘要 请用自然语言重组回答 不要原样复读")

    @staticmethod
    def video_batch_system_prompt() -> str:
        return _pl.get_nested("video", "batch_system",
            "你是中文视频内容分析助手 需要对多帧截图进行逐帧描述并保持叙事连贯")

    @staticmethod
    def video_single_system_prompt() -> str:
        return _pl.get_nested("video", "single_system",
            "你是中文视频内容分析助手 简洁准确描述画面 场景 人物 动作 文本 情绪 风格")

    @staticmethod
    def video_single_user_prompt(context_hint: str, frame_index: int, total_frames: int) -> str:
        inst = _pl.get_nested("video", "single_user",
            "请用中文简要描述场景 人物动作 画面文字 情绪基调 120 字以内")
        return f"{context_hint}这是视频第 {frame_index} 帧 共 {total_frames} 帧\n{inst}"

    @staticmethod
    def video_batch_user_prompt(context_hint: str, total_frames: int) -> str:
        inst = _pl.get_nested("video", "batch_user",
            "请逐帧中文描述 格式为 帧1 <描述> 帧2 <描述>\n每帧 120 字以内")
        return f"{context_hint}\n以下是视频的 {total_frames} 帧关键截图\n{inst}"

    @staticmethod
    def admin_quote_system_prompt() -> str:
        return _pl.get("admin_quote", "你是中文语录生成器 只输出一句中文短句 不超过 8 个字 不解释")
