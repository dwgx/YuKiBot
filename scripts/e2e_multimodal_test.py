"""
端到端多模态全真冒烟测试脚本 (E2E Multimodal Smoke Test)
用于验证“增强版语音识别分析”与“图片分析”在真实的 Engine->Router->Agent 链路下的运行表现。
"""

import asyncio
import io
import logging
import os
import wave
from pathlib import Path
from datetime import datetime

from PIL import Image

# 设置日志级别以观察详细运转过程
logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(name)s | %(message)s")

from core.engine import YukikoEngine, EngineMessage

async def create_fake_media() -> tuple[Path, Path]:
    """生成合法的媒体文件用于测试"""
    cache_dir = Path("storage/cache/test")
    cache_dir.mkdir(parents=True, exist_ok=True)
    
    # 1. 生成占位图片
    img_path = cache_dir / "smoke_test_image.jpg"
    img = Image.new("RGB", (200, 200), color=(73, 109, 137))
    img.save(img_path)
    
    # 2. 生成合法的 WAV 语音（静音 1 秒）
    voice_path = cache_dir / "smoke_test_voice.wav"
    with wave.open(str(voice_path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(16000)
        wf.writeframes(b"\x00" * 16000 * 2)  # 1秒静音
        
    return img_path, voice_path

async def run_e2e_smoke_test():
    engine = YukikoEngine.from_default_paths()
    await engine.async_init()
    
    img_path, voice_path = await create_fake_media()
    
    print("\n" + "="*50)
    print("🚀 启动 YuKiKo 多模态全仿真冒烟测试...")
    print("="*50 + "\n")
    
    # 准备假上下文
    fake_time = 1711116000
    msg_id = 99998888
    
    # 我们的“魔法数据结构”来承接拦截到的回复
    captured_replies = []
    
    async def mock_api_call(action, **kwargs):
        """Mock NapCat API"""
        if action == "send_group_msg":
            captured_replies.append(kwargs.get("message"))
            return {"message_id": msg_id + 1}
        return {}

    # 构建带语音和图片的原始消息段
    raw_segments = [
        {"type": "at", "data": {"qq": engine.config.get("login_qq", 123456)}},
        {"type": "text", "data": {"text": " 请帮我看看这张图，顺便听听这段语音说了啥？"}},
        {"type": "image", "data": {"file": f"file://{img_path.absolute()}"}},
        {"type": "record", "data": {"file": f"file://{voice_path.absolute()}"}}
    ]

    event_payload = {
        "time": fake_time,
        "self_id": engine.config.get("login_qq", 123456),
        "post_type": "message",
        "message_type": "group",
        "group_id": 9999,
        "user_id": 1111,
        "message_id": msg_id,
        "message": raw_segments,
        "raw_message": f"[CQ:at,qq={engine.config.get('login_qq', 123456)}] 请帮我看看这张图，顺便听听这段语音说了啥？[CQ:image,file=test][CQ:record,file=test]",
        "sender": {
            "user_id": 1111,
            "nickname": "TestUser",
            "role": "admin"
        }
    }

    # 包装成 EngineMessage
    engine_msg = EngineMessage(
        conversation_id="g_9999",
        user_id="1111",
        user_name="TestUser",
        text="请帮我看看这张图，顺便听听这段语音说了啥？",
        message_id=str(msg_id),
        seq=1,
        raw_segments=raw_segments,
        queue_depth=0,
        mentioned=True,
        is_private=False,
        timestamp=datetime.fromtimestamp(fake_time),
        group_id=9999,
        bot_id=str(engine.config.get("login_qq", 123456)),
        at_other_user_only=False,
        at_other_user_ids=[],
        reply_to_message_id="",
        reply_to_user_id="",
        reply_to_user_name="",
        reply_to_text="",
        reply_media_segments=[],
        api_call=mock_api_call,
        trace_id="test_001",
        sender_role="admin",
        event_payload=event_payload
    )

    print("📨 正在向引擎提交多模态有效载荷：")
    print(f"包含 1 张图片: {img_path.name}")
    print(f"包含 1 段语音: {voice_path.name}")
    print("-" * 50)

    # 喂入引擎并指定阻塞回调
    await engine.handle_message(engine_msg)
    
    await asyncio.sleep(2) # 缓冲等待
    
    print("\n" + "="*50)
    print("🎯 测试完成！引擎拦截到的回复内容为：")
    for r in captured_replies:
        print(r)
    print("="*50 + "\n")
    
if __name__ == "__main__":
    asyncio.run(run_e2e_smoke_test())
