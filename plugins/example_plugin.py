from __future__ import annotations


class Plugin:
    name = "example"
    commands = ["/ping", "/echo"]

    async def handle(self, message: str, context: dict) -> str:
        if message.startswith("/ping"):
            return "在线。"
        if message.startswith("/echo "):
            return message[6:].strip() or "echo 为空。"
        return "示例插件已就绪。"
