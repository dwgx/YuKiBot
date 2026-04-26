"""
联网自学习模块的 E2E 冒烟测试
验证 self_learning 插件能否在 learn_from_web 期间成功穿透调用底层的 SearchEngine 获取摘要。
"""

import asyncio
import logging

from core.engine import YukikoEngine

# 设置日志级别以观察底层模块抛出的信息
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")


async def run_learning_smoke_test():
    engine = YukikoEngine.from_default_paths()
    await engine.async_init()
    
    print("\n" + "="*50)
    print("🚀 启动网络自学习增强版穿透大区冒烟测试...")
    print("="*50 + "\n")
    
    # 模拟拿到自我学习插件的实体句柄
    self_learning_plugin = engine.plugins.plugins.get("self_learning")
    if not self_learning_plugin:
        print("❌ 致命错误：未能取到 self_learning 插件实例！")
        return
        
    print("✅ self_learning 模块已定位，尝试触发 'learn_from_web'")
    
    args = {
        "topic": "Python asyncio 3.12 新特性",
        "goal": "了解并在项目中应用 Python 3.12 针对 asyncio 的更新",
        "context": "我们正在升级后端从 3.10 到 3.12"
    }
    
    context = {"engine": engine}
    
    # 强制执行 `_handle_learn_from_web` 穿透
    try:
        # 直接调用内部 handler，忽略 agent 权限约束，验证底层链条
        result = await self_learning_plugin._handle_learn_from_web(args, context)
        print("\n" + "="*50)
        print("🎯 获取完成，大模型感知到的工具返回如下：")
        print("="*50)
        print(f"成功标识: {result.ok}")
        print(f"解析参数: {result.data}")
        print("-" * 50)
        print("【给模型展示的视觉提纲与摘要】：\n")
        print(result.display)
        print("-" * 50)
        
    except Exception as e:
        print(f"💥 执行失败抛出异常: {e}")

if __name__ == "__main__":
    asyncio.run(run_learning_smoke_test())
