"""首次运行向导 — CLI 交互式配置生成器。

用法:
    python -m core.setup          # 手动运行
    在 main.py 中自动检测并触发
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from core.config_templates import deep_merge_dict, ensure_prompts_file, load_config_template

_ROOT = Path(__file__).resolve().parents[1]
_CONFIG_DIR = _ROOT / "config"
_STORAGE_DIR = _ROOT / "storage"
_CONFIG_FILE = _CONFIG_DIR / "config.yml"
_PROMPTS_FILE = _CONFIG_DIR / "prompts.yml"


def needs_setup() -> bool:
    """config.yml 不存在时需要初始化。"""
    return not _CONFIG_FILE.exists()


def run() -> None:
    """交互式向导，生成 config.yml。"""
    print("\n╔══════════════════════════════════════════╗")
    print("║  YuKiKo Bot 首次运行配置向导             ║")
    print("╚══════════════════════════════════════════╝\n")

    cfg: dict[str, Any] = {}

    # 1. API 提供商
    cfg["api"] = _ask_api()

    # 2. 功能开关
    cfg["bot"], cfg["search"], cfg["image"], cfg["image_gen"] = _ask_features()

    # 3. 超级管理员
    cfg["admin"] = _ask_admin()

    # 4. 输出风格
    cfg["output"] = _ask_output()

    # 5. 音乐能力（Alger API）
    cfg["music"] = _ask_music()

    # 6. 平台 cookie（可选）
    cfg["video_analysis"] = _ask_cookies()

    # 7. 生成配置文件
    _write_config(cfg)
    print(f"\n配置已写入: {_CONFIG_FILE}")
    print("你可以随时编辑 config/config.yml，然后发 /yukibot 热重载。\n")


def _safe_input(prompt: str) -> str:
    """兼容 PyCharm / 非 TTY 环境的 input，确保 prompt 先刷新到屏幕。"""
    sys.stdout.write(prompt)
    sys.stdout.flush()
    try:
        return input().strip()
    except EOFError:
        return ""


def _input(prompt: str, default: str = "") -> str:
    hint = f" [{default}]" if default else ""
    val = _safe_input(f"{prompt}{hint}: ")
    return val or default


def _yes_no(prompt: str, default: bool = True) -> bool:
    hint = "Y/n" if default else "y/N"
    val = _safe_input(f"{prompt} ({hint}): ").lower()
    if not val:
        return default
    return val in ("y", "yes", "是", "1")


def _choice(prompt: str, options: list[str], default: int = 0) -> str:
    print(f"\n{prompt}")
    for i, opt in enumerate(options):
        marker = " *" if i == default else ""
        print(f"  {i + 1}. {opt}{marker}")
    val = _safe_input(f"选择 [1-{len(options)}，默认 {default + 1}]: ")
    try:
        idx = int(val) - 1
        if 0 <= idx < len(options):
            return options[idx]
    except (ValueError, IndexError):
        pass
    return options[default]


def _ask_api() -> dict[str, Any]:
    providers = [
        "skiapi",
        "openai",
        "anthropic",
        "gemini",
        "deepseek",
        "newapi",
        "openrouter",
        "xai",
        "qwen",
        "moonshot",
        "mistral",
        "zhipu",
        "siliconflow",
    ]
    provider = _choice("选择 API 提供商:", providers, default=0)
    api_key = _input(f"输入 {provider} 的 API Key（留空则从环境变量读取）")

    models = {
        "skiapi": "claude-opus-4-6",
        "openai": "gpt-5.2",
        "anthropic": "claude-sonnet-4-5-20250929",
        "gemini": "gemini-2.5-pro",
        "deepseek": "deepseek-chat",
        "newapi": "gpt-5-codex",
        "openrouter": "openrouter/auto",
        "xai": "grok-3-latest",
        "qwen": "qwen-max-latest",
        "moonshot": "kimi-thinking-preview",
        "mistral": "mistral-medium-latest",
        "zhipu": "glm-4-plus",
        "siliconflow": "Qwen/Qwen2.5-72B-Instruct",
    }
    model = _input("模型名称", models.get(provider, ""))

    result: dict[str, Any] = {
        "provider": provider,
        "model": model,
        "temperature": 0.8,
        "max_tokens": 8192,
        "timeout_seconds": 60,
    }

    if api_key:
        # 尝试加密
        try:
            from core.crypto import SecretManager
            sm = SecretManager(_STORAGE_DIR / ".secret_key")
            encrypted = sm.encrypt(api_key)
            result["api_key"] = encrypted
            print("  API Key 已加密存储。")
        except Exception:
            result["api_key"] = api_key
            print("  加密不可用，API Key 以明文存储。")
    else:
        env_map = {
            "skiapi": "${SKIAPI_KEY}",
            "openai": "${OPENAI_API_KEY}",
            "deepseek": "${DEEPSEEK_API_KEY}",
            "newapi": "${NEWAPI_API_KEY}",
            "anthropic": "${ANTHROPIC_API_KEY}",
            "gemini": "${GEMINI_API_KEY}",
            "openrouter": "${OPENROUTER_API_KEY}",
            "xai": "${XAI_API_KEY}",
            "qwen": "${QWEN_API_KEY}",
            "moonshot": "${MOONSHOT_API_KEY}",
            "mistral": "${MISTRAL_API_KEY}",
            "zhipu": "${ZHIPU_API_KEY}",
            "siliconflow": "${SILICONFLOW_API_KEY}",
        }
        result["api_key"] = env_map.get(provider, "${API_KEY}")

    return result


def _ask_features() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    print("\n── 功能开关 ──")
    allow_search = _yes_no("启用网络搜索?", True)
    allow_image = _yes_no("启用 AI 画图?", True)
    allow_markdown = _yes_no("启用 Markdown 格式输出?", True)

    bot = {
        "name": "YuKiKo",
        "nicknames": ["雪", "yukiko", "yuki"],
        "language": "zh",
        "allow_markdown": allow_markdown,
        "allow_image": allow_image,
        "allow_search": allow_search,
        "allow_non_to_me": True,
    }
    search = {
        "enable": allow_search,
        "max_results": 8,
        "max_image_results": 4,
        "timeout_seconds": 18,
        "searxng_base": "",
        "allow_private_network": False,
        "video_resolver": {
            "enable": True,
            "cookies_from_browser": "auto",
            "download_max_mb": 64,
            "download_timeout_seconds": 50,
            "resolve_total_timeout_seconds": 65,
            "search_max_duration_seconds": 600,
            "search_send_max_duration_seconds": 1800,
            "search_analysis_max_duration_seconds": 2400,
            "parse_api_enable": False,
            "parse_api_base": "",
        },
        "tool_interface": {
            "enable": True,
            "browser_enable": True,
            "github_enable": True,
            "github_api_base": "https://api.github.com",
            "github_token": "${GITHUB_TOKEN}",
            "web_fetch_timeout_seconds": 18,
            "web_fetch_max_chars": 1800,
            "web_fetch_max_pages": 2,
        },
    }
    image = {"enable": allow_image}
    image_gen = _ask_image_gen() if allow_image else {"enable": False}
    return bot, search, image, image_gen


def _ask_admin() -> dict[str, Any]:
    print("\n── 管理员设置 ──")
    qq = _input("超级管理员 QQ 号（留空 = 不启用权限系统）")
    return {
        "super_admin_qq": qq,
        "non_whitelist_mode": "silent",
    }


def _ask_output() -> dict[str, Any]:
    print("\n── 输出风格 ──")
    levels = ["verbose (详细)", "medium (中等)", "brief (偏短)", "minimal (极简)"]
    choice = _choice("选择默认输出详细度:", levels, default=1)
    verbosity = choice.split(" ")[0]
    print("  提示：省 token 模式默认关闭（否），仅在你明确要压缩上下文成本时再开启。")
    token_saving = _yes_no("启用省 token 模式?", False)
    return {
        "verbosity": verbosity,
        "token_saving": token_saving,
        "style_instruction": "",
        "group_overrides": {},
        "group_style_overrides": {},
    }


def _ask_music() -> dict[str, Any]:
    print("\n── 音乐能力（Alger API）──")
    enable = _yes_no("启用点歌/听歌功能?", True)
    api_base = _input("音乐 API 地址", "http://mc.alger.fun/api")
    return {
        "enable": enable,
        "api_base": api_base,
        "cache_dir": "storage/cache/music",
        "timeout_seconds": 15,
        "cache_keep_files": 50,
    }


def _ask_cookies() -> dict[str, Any]:
    print("\n── 平台 Cookie（可选，用于视频解析增强）──")
    print("  Cookie 可以让 Bot 获取更多视频信息（弹幕、评论等）")
    print("  没有 Cookie 也能用，只是功能受限\n")

    result: dict[str, Any] = {
        "keyframe_count": 4,
        "keyframe_max_dimension": 720,
        "keyframe_quality": 5,
        "bilibili": {"enable": True, "sessdata": "", "bili_jct": "", "danmaku_top_n": 8, "comments_top_n": 3},
        "douyin": {"enable": True, "cookie": ""},
        "kuaishou": {"enable": True, "cookie": ""},
        "qzone": {"enable": True, "cookie": ""},
    }

    # ── B站 ──
    if _yes_no("配置 B站 Cookie?", True):
        from core.cookie_auth import interactive_bilibili_cookie
        bili = interactive_bilibili_cookie()
        sessdata = bili.get("sessdata", "")
        bili_jct = bili.get("bili_jct", "")
        if sessdata or bili_jct:
            sessdata, bili_jct = _encrypt_pair(sessdata, bili_jct)
            result["bilibili"]["sessdata"] = sessdata
            result["bilibili"]["bili_jct"] = bili_jct
            print("  B站 Cookie 已配置。")
        else:
            print("  B站 Cookie 未配置，跳过。")

    # ── 抖音 ──
    if _yes_no("配置抖音 Cookie?", True):
        from core.cookie_auth import interactive_douyin_cookie
        cookie = interactive_douyin_cookie()
        if cookie:
            cookie = _encrypt_value(cookie)
            result["douyin"]["cookie"] = cookie
            print("  抖音 Cookie 已配置。")
        else:
            print("  抖音 Cookie 未配置，跳过。")

    # ── 快手 ──
    if _yes_no("配置快手 Cookie?", True):
        from core.cookie_auth import interactive_kuaishou_cookie
        cookie = interactive_kuaishou_cookie()
        if cookie:
            cookie = _encrypt_value(cookie)
            result["kuaishou"]["cookie"] = cookie
            print("  快手 Cookie 已配置。")

    # ── QQ空间 ──
    if _yes_no("配置 QQ空间 Cookie?（用于查看空间资料/说说）", False):
        from core.cookie_auth import interactive_qzone_cookie
        cookie = interactive_qzone_cookie()
        if cookie:
            cookie = _encrypt_value(cookie)
            result["qzone"]["cookie"] = cookie
            print("  QQ空间 Cookie 已配置。")
        else:
            print("  QQ空间 Cookie 未配置，跳过。")

    return result


def _ask_image_gen() -> dict[str, Any]:
    """配置图片生成功能。"""
    print("\n── 图片生成配置 ──")
    print("  支持多模型配置（DALL-E / Flux / SD / 任何 OpenAI 兼容 API）")
    print("  NSFW 过滤强制开启，确保内容安全\n")

    default_model = _input("默认模型名称", "dall-e-3")
    default_size = _input("默认图片尺寸", "1024x1024")

    models = []
    if _yes_no("添加自定义图片生成模型?", False):
        while True:
            print("\n添加模型配置:")
            name = _input("  模型名称（如 flux-1）")
            if not name:
                break
            api_base = _input("  API 地址")
            api_key = _input("  API Key")
            model = _input("  模型 ID", name)
            size = _input("  默认尺寸", "1024x1024")

            if api_key:
                api_key = _encrypt_value(api_key)

            models.append({
                "name": name,
                "api_base": api_base,
                "api_key": api_key,
                "model": model,
                "default_size": size,
            })
            print(f"  模型 {name} 已添加。")

            if not _yes_no("继续添加更多模型?", False):
                break

    return {
        "enable": True,
        "default_model": default_model,
        "default_size": default_size,
        "nsfw_filter": True,
        "max_prompt_length": 1000,
        "models": models,
    }


def _encrypt_value(value: str) -> str:
    """尝试加密单个值。"""
    if not value:
        return value
    try:
        from core.crypto import SecretManager
        sm = SecretManager(_STORAGE_DIR / ".secret_key")
        return sm.encrypt(value)
    except Exception:
        return value


def _encrypt_pair(a: str, b: str) -> tuple[str, str]:
    """尝试加密一对值。"""
    try:
        from core.crypto import SecretManager
        sm = SecretManager(_STORAGE_DIR / ".secret_key")
        if a:
            a = sm.encrypt(a)
        if b:
            b = sm.encrypt(b)
    except Exception:
        pass
    return a, b


def _write_config(cfg: dict[str, Any]) -> None:
    """生成带注释的 config.yml。"""
    import yaml

    _CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    _STORAGE_DIR.mkdir(parents=True, exist_ok=True)
    base = load_config_template()
    deep_merge_dict(base, cfg)

    header = (
        "# YuKiKo Bot 配置文件\n"
        "# 由首次运行向导自动生成\n"
        "# 修改后发送 /yukibot 或 /yukiko 即可热重载\n"
        "# 敏感值支持 ENC() 加密，详见 core/crypto.py\n\n"
    )

    with open(_CONFIG_FILE, "w", encoding="utf-8") as f:
        f.write(header)
        yaml.safe_dump(base, f, allow_unicode=True, default_flow_style=False, sort_keys=False)

    ensure_prompts_file(_PROMPTS_FILE)


if __name__ == "__main__":
    run()
