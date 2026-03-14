from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from utils.text import clip_text, normalize_text

_SCENE_HINTS = {
    "chat": "闲聊场景，先接住当前说话人的情绪和语气，再决定要不要延续玩笑、关心或亲近感。",
    "emotion_support": "情绪场景，先共情再建议，可以自然表达关心、在意、陪伴感。",
    "conflict_mediation": "冲突场景，分别理解每个人的立场，不混淆对象，不轻易替任何一方下结论。",
    "tech_support": "技术场景，先确认是谁的设备、环境或报错，再给步骤，不要把别人的配置套过来。",
    "search_synthesis": "搜索场景，优先回应当前提问者真正想知道的点，再补充来源和依据。",
    "video_analysis": "解析场景，先说明当前在替谁分析什么，再输出结论，避免把围观者和提问者混掉。",
}

_BOT_MOOD_LABELS = {
    "happy": "开心",
    "neutral": "平静",
    "tired": "疲惫",
    "annoyed": "烦躁",
    "excited": "兴奋",
    "melancholy": "低落",
}


@dataclass(slots=True)
class CompatContextInput:
    conversation_id: str = ""
    user_id: str = ""
    user_name: str = ""
    preferred_name: str = ""
    scene_hint: str = "chat"
    is_private: bool = False
    mentioned: bool = False
    bot_id: str = ""
    reply_to_user_id: str = ""
    reply_to_user_name: str = ""
    reply_to_text: str = ""
    at_other_user_ids: list[str] = field(default_factory=list)
    at_other_user_names: dict[str, str] = field(default_factory=dict)
    recent_speakers: list[tuple[str, str, str]] = field(default_factory=list)
    thread_state: dict[str, Any] = field(default_factory=dict)
    user_profile_summary: str = ""
    affinity_summary: str = ""
    bot_mood: str = ""


def build_affinity_summary(affinity_engine: Any, user_id: str) -> str:
    uid = normalize_text(str(user_id))
    if affinity_engine is None or not uid:
        return ""
    try:
        user = affinity_engine.get_user(uid)
    except Exception:
        return ""

    total_interactions = int(getattr(user, "total_interactions", 0) or 0)
    streak = int(getattr(user, "daily_checkin_streak", 0) or 0)
    if total_interactions <= 0 and streak <= 0:
        return ""

    level = int(getattr(user, "level", 1) or 1)
    level_name = normalize_text(str(getattr(user, "level_name", ""))) or "熟人"
    affinity = float(getattr(user, "affinity", 0.0) or 0.0)
    parts = [f"关系热度 Lv.{level} {level_name}", f"好感度 {affinity:.0f}/100"]
    if total_interactions > 0:
        parts.append(f"累计互动 {total_interactions} 次")
    if streak > 0:
        parts.append(f"连续打卡 {streak} 天")
    return " / ".join(parts)


def build_context_compat_block(payload: CompatContextInput) -> str:
    speaker_id = normalize_text(payload.user_id)
    speaker_name = _display_name(
        user_id=speaker_id,
        display_name=payload.user_name,
        fallback_name=payload.preferred_name,
    )
    rows = [
        f"互动模式: {_resolve_mode_label(payload)}",
        f"当前主要回应对象: {speaker_name}(QQ:{speaker_id})" if speaker_id else f"当前主要回应对象: {speaker_name}",
    ]

    preferred_name = normalize_text(payload.preferred_name)
    if preferred_name and preferred_name != speaker_name:
        rows.append(f"建议优先称呼当前用户为: {preferred_name}")

    mood = normalize_text(payload.bot_mood).lower()
    if mood:
        rows.append(f"你当前的情绪基调: {_BOT_MOOD_LABELS.get(mood, mood)}")

    affinity_summary = normalize_text(payload.affinity_summary)
    if affinity_summary:
        rows.append(affinity_summary)

    profile_summary = normalize_text(payload.user_profile_summary)
    if profile_summary:
        rows.append(f"当前用户画像摘要: {clip_text(profile_summary, 180)}")

    reply_line = _build_reply_line(payload)
    if reply_line:
        rows.append(reply_line)

    mention_line = _build_mentions_line(payload)
    if mention_line:
        rows.append(mention_line)

    speaker_line = _build_recent_speakers_line(payload, current_user_id=speaker_id)
    if speaker_line:
        rows.append(speaker_line)

    thread_line = _build_thread_line(payload, current_user_id=speaker_id, current_user_name=speaker_name)
    if thread_line:
        rows.append(thread_line)

    scene_hint = normalize_text(payload.scene_hint).lower() or "chat"
    scene_line = _SCENE_HINTS.get(scene_hint)
    if scene_line:
        rows.append(f"场景策略: {scene_line}")

    rows.append(
        "关系兼容层规则: 把群里每个人都当成独立、持续存在的真实个体；"
        "先回应当前说话人的情绪和诉求；允许关系自然延续和升温，但只能基于真实对话证据；"
        "不要把其他成员的画像、偏好、记忆、情绪或结论套到当前用户身上，也不要编造不存在的共同经历。"
    )

    return "【群聊关系兼容层】\n" + "\n".join(f"- {row}" for row in rows if normalize_text(row))


def _resolve_mode_label(payload: CompatContextInput) -> str:
    if payload.is_private:
        return "私聊直连"
    reply_to_user_id = normalize_text(payload.reply_to_user_id)
    is_reply_to_bot = bool(reply_to_user_id and reply_to_user_id == normalize_text(payload.bot_id))
    if payload.mentioned and is_reply_to_bot:
        return "群聊点名并接续和你的对话"
    if payload.mentioned and reply_to_user_id:
        return "群聊点名并继续别人的话题"
    if payload.mentioned:
        return "群聊点名"
    if reply_to_user_id and not is_reply_to_bot:
        return "群聊接话"
    if is_reply_to_bot:
        return "群聊续聊"
    return "群聊自然接话"


def _build_reply_line(payload: CompatContextInput) -> str:
    reply_to_user_id = normalize_text(payload.reply_to_user_id)
    reply_to_user_name = normalize_text(payload.reply_to_user_name)
    reply_to_text = normalize_text(payload.reply_to_text)
    if not reply_to_user_id and not reply_to_text:
        return ""

    is_reply_to_bot = bool(reply_to_user_id and reply_to_user_id == normalize_text(payload.bot_id))
    if is_reply_to_bot:
        base = "当前用户是在继续接你的上一条话"
    else:
        reply_target = _display_name(
            user_id=reply_to_user_id,
            display_name=reply_to_user_name,
        )
        base = (
            f"当前用户可能在和 {reply_target}(QQ:{reply_to_user_id}) 继续同一段对话"
            if reply_to_user_id
            else "当前用户正在接续上一条被引用的内容"
        )
    if reply_to_text:
        base += f"，被引用原话摘要: {clip_text(reply_to_text, 80)}"
    return base


def _build_mentions_line(payload: CompatContextInput) -> str:
    rows: list[str] = []
    for uid in payload.at_other_user_ids[:4]:
        norm_uid = normalize_text(uid)
        if not norm_uid:
            continue
        name = normalize_text(payload.at_other_user_names.get(norm_uid, ""))
        rows.append(f"{name}(QQ:{norm_uid})" if name else f"QQ:{norm_uid}")
    if not rows:
        return ""
    return "当前消息还点名了: " + "、".join(rows)


def _build_recent_speakers_line(
    payload: CompatContextInput,
    *,
    current_user_id: str,
) -> str:
    rows: list[str] = []
    current_uid = normalize_text(current_user_id)
    reply_uid = normalize_text(payload.reply_to_user_id)
    for uid, name, preview in payload.recent_speakers[:6]:
        norm_uid = normalize_text(uid)
        if not norm_uid or norm_uid in {current_uid, reply_uid}:
            continue
        display_name = _display_name(user_id=norm_uid, display_name=name)
        short_preview = clip_text(normalize_text(preview), 24)
        if short_preview:
            rows.append(f"{display_name}(QQ:{norm_uid}): {short_preview}")
        else:
            rows.append(f"{display_name}(QQ:{norm_uid})")
        if len(rows) >= 3:
            break
    if not rows:
        return ""
    return "最近相关人物: " + "； ".join(rows)


def _build_thread_line(
    payload: CompatContextInput,
    *,
    current_user_id: str,
    current_user_name: str,
) -> str:
    state = payload.thread_state if isinstance(payload.thread_state, dict) else {}
    if not state:
        return ""
    last_topic = normalize_text(str(state.get("last_topic", "")))
    last_action = normalize_text(str(state.get("last_action", "")))
    last_user_id = normalize_text(str(state.get("last_user_id", "")))
    if not last_topic and not last_action and not last_user_id:
        return ""

    actor = ""
    if last_user_id:
        if last_user_id == normalize_text(current_user_id):
            actor = current_user_name
        else:
            actor = _lookup_recent_speaker_name(last_user_id, payload.recent_speakers)
            if not actor:
                actor = f"用户{last_user_id[-4:]}" if last_user_id else ""
    parts: list[str] = []
    if last_topic:
        parts.append(f"上一轮主线话题={last_topic}")
    if last_action:
        parts.append(f"上一轮动作={last_action}")
    if actor:
        parts.append(f"上一轮主要人物={actor}")
    return "线程延续提示: " + "，".join(parts)


def _lookup_recent_speaker_name(
    user_id: str,
    recent_speakers: list[tuple[str, str, str]],
) -> str:
    norm_uid = normalize_text(user_id)
    if not norm_uid:
        return ""
    for uid, name, _preview in recent_speakers:
        if normalize_text(uid) == norm_uid:
            return _display_name(user_id=norm_uid, display_name=name)
    return ""


def _display_name(user_id: str, display_name: str = "", fallback_name: str = "") -> str:
    name = normalize_text(display_name) or normalize_text(fallback_name)
    if name:
        return name
    uid = normalize_text(user_id)
    if uid:
        return f"用户{uid[-4:]}"
    return "当前用户"
