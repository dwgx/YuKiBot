"""NapCat 高级消息构建器 — JSON 卡片、合并转发、自定义音乐卡片等。

基于 NapCat/OneBot11 协议，提供 Agent 可调用的高级消息能力。
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

_log = logging.getLogger("yukiko.card_builder")


@dataclass(slots=True)
class CardMessage:
    """构建好的卡片消息，可直接传给 NapCat 发送。"""
    segments: list[dict[str, Any]]
    preview_text: str = ""  # 在消息列表中的预览文本


class CardBuilder:
    """构建各种 NapCat 高级消息段。"""

    # ── JSON 卡片 ──

    @staticmethod
    def json_card(
        title: str,
        desc: str,
        prompt: str = "",
        url: str = "",
        image: str = "",
        source_name: str = "YuKiKo",
        source_icon: str = "",
    ) -> CardMessage:
        """构建通用 JSON 卡片消息（类似链接分享卡片）。"""
        meta = {
            "detail_1": {
                "title": title,
                "desc": desc,
                "icon": image or source_icon,
                "preview": image,
                "url": url,
            }
        }
        ark_json = {
            "app": "com.tencent.miniapp_01",
            "desc": "",
            "view": "detail",
            "ver": "1.0.0.89",
            "prompt": prompt or title,
            "meta": meta,
        }
        return CardMessage(
            segments=[{"type": "json", "data": {"data": json.dumps(ark_json, ensure_ascii=False)}}],
            preview_text=prompt or title,
        )

    @staticmethod
    def info_card(
        title: str,
        fields: list[tuple[str, str]],
        image: str = "",
        color: str = "#7C4DFF",
    ) -> CardMessage:
        """构建信息展示卡片（好感度、打卡、排行榜等）。"""
        desc_lines = [f"{'─' * 20}"]
        for label, value in fields:
            desc_lines.append(f"  {label}: {value}")
        desc_lines.append(f"{'─' * 20}")
        desc = "\n".join(desc_lines)
        return CardBuilder.json_card(
            title=title,
            desc=desc,
            image=image,
            prompt=f"[{title}]",
        )

    # ── 自定义音乐卡片 ──

    @staticmethod
    def custom_music_card(
        title: str,
        singer: str = "",
        audio_url: str = "",
        jump_url: str = "",
        image_url: str = "",
    ) -> CardMessage:
        """构建自定义音乐分享卡片。"""
        segment = {
            "type": "music",
            "data": {
                "type": "custom",
                "url": jump_url or audio_url,
                "audio": audio_url,
                "title": title,
                "image": image_url,
                "singer": singer,
            },
        }
        return CardMessage(
            segments=[segment],
            preview_text=f"[音乐] {singer} - {title}" if singer else f"[音乐] {title}",
        )

    @staticmethod
    def platform_music_card(platform: str, song_id: str | int) -> CardMessage:
        """构建平台音乐卡片（QQ音乐/网易云/酷狗等）。"""
        platform_map = {"qq": "qq", "netease": "163", "163": "163", "kugou": "kugou", "migu": "migu", "kuwo": "kuwo"}
        ptype = platform_map.get(platform.lower(), "163")
        segment = {
            "type": "music",
            "data": {"type": ptype, "id": str(song_id)},
        }
        return CardMessage(
            segments=[segment],
            preview_text=f"[音乐卡片] {platform}:{song_id}",
        )

    # ── 合并转发 ──

    @staticmethod
    def forward_message(nodes: list[dict[str, Any]]) -> CardMessage:
        """构建合并转发消息。

        nodes 格式:
        [
            {"nickname": "张三", "user_id": "10001", "content": "消息内容"},
            {"nickname": "李四", "user_id": "10002", "content": [segments...]},
        ]
        """
        forward_nodes = []
        for node in nodes:
            content = node.get("content", "")
            if isinstance(content, str):
                content = [{"type": "text", "data": {"text": content}}]
            forward_nodes.append({
                "type": "node",
                "data": {
                    "nickname": str(node.get("nickname", "YuKiKo")),
                    "user_id": str(node.get("user_id", "10000")),
                    "content": content,
                },
            })
        return CardMessage(segments=forward_nodes, preview_text="[合并转发]")

    # ── 图文混排 ──

    @staticmethod
    def rich_message(parts: list[dict[str, Any]]) -> CardMessage:
        """构建图文混排消息。

        parts 格式:
        [
            {"type": "text", "content": "文字"},
            {"type": "image", "url": "https://..."},
            {"type": "at", "qq": "123456"},
        ]
        """
        segments = []
        preview_parts = []
        for part in parts:
            ptype = part.get("type", "text")
            if ptype == "text":
                segments.append({"type": "text", "data": {"text": part.get("content", "")}})
                preview_parts.append(part.get("content", ""))
            elif ptype == "image":
                segments.append({"type": "image", "data": {"file": part.get("url", "")}})
                preview_parts.append("[图片]")
            elif ptype == "at":
                segments.append({"type": "at", "data": {"qq": str(part.get("qq", ""))}})
                preview_parts.append(f"@{part.get('qq', '')}")
            elif ptype == "face":
                segments.append({"type": "face", "data": {"id": str(part.get("id", ""))}})
            elif ptype == "reply":
                segments.append({"type": "reply", "data": {"id": str(part.get("id", ""))}})
        return CardMessage(segments=segments, preview_text="".join(preview_parts)[:50])

    # ── 好感度卡片 ──

    @staticmethod
    def affinity_card(
        nickname: str,
        affinity: float,
        level: int,
        level_name: str,
        streak: int,
        total: int,
        mood: str = "neutral",
    ) -> CardMessage:
        """构建好感度信息卡片。"""
        bar_len = 20
        filled = int(affinity / 100 * bar_len)
        bar = "█" * filled + "░" * (bar_len - filled)
        return CardBuilder.info_card(
            title=f"💕 {nickname} 的好感度",
            fields=[
                ("好感度", f"{affinity:.1f}/100 [{bar}]"),
                ("等级", f"Lv.{level} {level_name}"),
                ("连续打卡", f"{streak} 天"),
                ("总互动", f"{total} 次"),
                ("Bot心情", mood),
            ],
        )

    # ── 排行榜卡片 ──

    @staticmethod
    def leaderboard_card(users: list[dict[str, Any]], title: str = "好感度排行榜") -> CardMessage:
        """构建排行榜卡片。"""
        medals = ["🥇", "🥈", "🥉"]
        fields = []
        for i, user in enumerate(users[:10]):
            prefix = medals[i] if i < 3 else f" {i+1}."
            name = user.get("nickname", user.get("user_id", "???"))
            aff = user.get("affinity", 0)
            fields.append((prefix, f"{name} — {aff:.0f}"))
        return CardBuilder.info_card(title=f"🏆 {title}", fields=fields)
