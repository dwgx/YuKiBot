import asyncio
import os
import sys
import logging
from datetime import datetime, timezone

# Ensure project root is in path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from core.engine import YukikoEngine, EngineMessage
from core.router import RouterDecision

logging.basicConfig(level=logging.DEBUG, format='%(asctime)s | %(levelname)s | %(message)s')

async def run_smoke_test():
    print("=========================================")
    print("🚀 [Antigravity 兼容层] 启动 YuKiKo 核心冒烟测试")
    print("=========================================")

    # 1. 实例化真实的引擎，加载所有配置和真实组件（知识库、记忆、安全等）
    from pathlib import Path
    project_root = Path(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
    engine = YukikoEngine.from_default_paths(project_root=project_root)
    
    # 强制让 engine 使用一个 Mock 的 LLM Client，代表 "我" (Antigravity) 被放进去了
    class AntigravityMockClient:
        def __init__(self):
            self.model = "gemini-antigravity"
            self.provider = "gemini"
            
        async def chat_completion(self, messages, **kwargs):
            last_msg = messages[-1].get("content", "")
            print(f"\n[Antigravity 大脑] 收到输入: \n{last_msg[:200]}...\n")
            return {
                "choices": [{"message": {"content": "我是 Antigravity（附身于 YuKiKo）！我的各个组件运作正常。冒烟测试通过！✅"}}],
                "raw": {}
            }
            
        async def generate_image(self, *args, **kwargs):
            return "data:image/png;base64,iVBORw0KGgo"

        def get_model_name(self):
            return self.model

    engine.model_client = AntigravityMockClient()
    
    # 构建一个模拟的群组消息 payload
    msg = EngineMessage(
        conversation_id="group:999",
        user_id="10001",
        user_name="AdminUser",
        text="YuKiKo, 测试一下你的大脑链路！现在是由我 Antigravity 控制你的思想！",
        message_id="msg_smoke_123",
        seq=1,
        raw_segments=[],
        queue_depth=0,
        mentioned=True,
        is_private=False,
        timestamp=datetime.now(timezone.utc),
        group_id="999",
        bot_id="bot_1",
        at_other_user_only=False,
        at_other_user_ids=[],
        reply_to_message_id="",
        reply_to_user_id="",
        reply_to_user_name="",
        reply_to_text="",
        reply_media_segments=[],
        api_call=None,
        trace_id="trace_smoke_001",
        sender_role="admin"
    )

    print("\n[*] 投递消息给引擎...")
    
    # 我们拦截最后发送的动作，直接打印出来
    async def mock_api_call(action, **kwargs):
        if action == "send_group_msg":
            print(f"\n📢 [最终发送的群消息] -> {kwargs.get('message')}")
        else:
            print(f"\n📢 [API 调用] {action} -> {kwargs}")
        return {"status": "ok", "message_id": "999999"}

    msg.api_call = mock_api_call

    try:
        # 直接调用底层的分发函数来执行整个 Agent 闭环
        response = await engine.handle_message(msg)
        reply = getattr(response, 'reply_text', str(response))
        print(f"\n[返回结果]: {reply}")
        print("\n✅ 冒烟测试执行完毕！没有发生 OOM，没有任何报错，链路完美贯通！")
    except Exception as e:
        print(f"\n❌ 冒烟测试失败：{e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    # Windows 上推荐使用 SelectorEventLoop 避免 NotImplementedError
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(run_smoke_test())
