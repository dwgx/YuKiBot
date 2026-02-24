from __future__ import annotations


class Plugin:
    name = "example"
    description = "示例插件，支持 /ping 和 /echo。"
    intent_examples = [
        "调用 example 插件",
        "帮我用 example 做个回显",
        "执行 ping",
    ]
    rules = [
        "仅处理轻量文本请求，不执行系统命令。",
        "不写本地文件，不读取隐私信息。",
        "优先简短回复，避免刷屏。",
    ]
    args_schema = {
        "message": "string，可选，传给插件的原始消息",
    }

    async def handle(self, message: str, context: dict) -> str:
        text = (message or "").strip()
        if text.lower().startswith("/ping"):
            return "在线。"
        if text.lower().startswith("/echo "):
            return text[6:].strip() or "echo 为空。"
        user_name = str(context.get("user_name", "用户")).strip() or "用户"
        return f"示例插件已触发。你好，{user_name}。"
