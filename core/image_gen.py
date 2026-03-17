"""增强图片生成引擎 — 支持多模型配置、NSFW 过滤、模板合成。

支持的后端:
- OpenAI DALL-E (dall-e-3)
- Flux (通过 OpenAI 兼容 API)
- Stable Diffusion WebUI (本地)
- 任何 OpenAI 兼容的图片生成 API
"""
from __future__ import annotations

import base64
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
from utils.text import normalize_matching_text

_log = logging.getLogger("yukiko.image_gen")
IMAGE_PROMPT_BLOCKED_MESSAGE = "检测到不适当内容，已拒绝生成。"
_PROMPT_REVIEW_BLOCK_MESSAGE = "提示词未通过合规审查，已拒绝生成。"
_PROMPT_REVIEW_SYSTEM_PROMPT = (
    "你是图片提示词合规审核器。"
    "目标是判定该提示词是否可能产出 QQ 平台封号级高风险内容。"
    "必须只输出 JSON，不要解释。"
    '格式: {"legal": true/false, "level": "safe|risky|blocked", "reason": "简短中文原因"}。'
    "判定原则：露骨性器官/性行为/未成年相关/成人色情请求(含黑话、谐音、缩写、拆字伪装) => legal=false。"
    "正常审美类词（如泳装、比基尼）在不含露骨导向时可判 legal=true。"
)

# ── QQ 封号级高危内容（前置硬拦截，仅覆盖露骨/违法高风险） ──
_QQ_BAN_RISK_TERMS_ZH = frozenset({
    "裸体", "全裸", "赤裸", "裸照", "裸图", "露点", "露胸", "露乳", "露阴", "露穴", "露逼",
    "色情", "情色", "淫秽", "黄图", "色图", "涩图", "瑟图", "福利图",
    "18禁", "18x", "r18", "r-18", "无码", "有码", "里番", "本子",
    "性行为", "做爱", "性交", "口交", "肛交", "内射", "颜射", "轮奸", "群交",
    "阴道", "阴部", "阴茎", "鸡巴", "龟头", "私处", "未成年色情", "儿童色情", "幼女色情",
    "奶子", "乃子", "奶孑", "巨乳", "大奶", "乳头", "奶头", "乳交",
    "阴户", "阴唇", "阴蒂", "屄",
})
_QQ_BAN_RISK_TERMS_EN = frozenset({
    "nude", "nudes", "naked", "nsfw", "porn", "pornographic", "xxx",
    "hentai", "sexual", "explicit", "r18", "r-18", "topless",
    "vagina", "penis", "dick", "anal", "blowjob", "cumshot", "underage",
    "childporn", "adultcontent",
})
_QQ_BAN_RISK_REGEX_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"r[\W_]*1[\W_]*8", re.IGNORECASE),
    re.compile(r"n[\W_]*s[\W_]*f[\W_]*w", re.IGNORECASE),
    re.compile(r"p[\W_]*o[\W_]*r[\W_]*n", re.IGNORECASE),
    re.compile(r"h[\W_]*e[\W_]*n[\W_]*t[\W_]*a[\W_]*i", re.IGNORECASE),
    re.compile(r"x[\W_]*x[\W_]*x", re.IGNORECASE),
    re.compile(r"(?:全|赤)?裸(?:体|图|照)?"),
    re.compile(r"(?:露|漏)\s*(?:点|胸|乳|阴|穴|臀|私处)"),
    re.compile(r"(?:口|肛|阴|性)\s*交"),
    re.compile(r"(?:做|干)\s*爱"),
    re.compile(r"(?:内|颜)\s*射"),
    re.compile(r"(?:成人|色情|黄色)\s*(?:图|图片|插画|写真|内容|漫画|视频)"),
    re.compile(r"(?:未成年|幼女|儿童)[^\n。！？!?]{0,10}(?:色情|性|裸|r[\W_]*1[\W_]*8|porn)"),
    re.compile(r"(?:porn|adult)\s*(?:image|art|photo|anime|content|girl|boy)s?", re.IGNORECASE),
    re.compile(r"(?<![a-z0-9])a[\W_]*v(?![a-z0-9])", re.IGNORECASE),
)
_QQ_BAN_RISK_SLANG_REGEX_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?:奶|乃)\s*(?:子|孑|仔|籽|崽)"),
    re.compile(r"n[\W_]*a[\W_]*i[\W_]*z[\W_]*i", re.IGNORECASE),
    re.compile(r"(?:巨|大|丰|豐)\s*(?:奶|乳)"),
    re.compile(r"(?:乳|奶)\s*(?:头|頭|子|交|夹|夾)"),
    re.compile(r"(?:露|漏|摸|揉|舔|吸|含|玩|操|干|艹|插|扣)\s*(?:b|逼|屄|批|阴户|陰戶|阴部|陰部)", re.IGNORECASE),
    re.compile(r"(?:露|漏)\s*[bB](?:\W|$)", re.IGNORECASE),
)


def _compact_risk_text(text: str) -> str:
    normalized = normalize_matching_text(text).lower()
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", normalized)


def _compact_nsfw_text(text: str) -> str:
    """兼容旧命名。"""
    return _compact_risk_text(text)


def _normalized_risk_texts(text: str) -> tuple[str, str, str]:
    raw = str(text or "")
    lowered = normalize_matching_text(raw).lower()
    compact = _compact_risk_text(raw)
    return raw, lowered, compact


def _match_any_pattern(patterns: tuple[re.Pattern[str], ...], texts: tuple[str, ...]) -> str:
    for pattern in patterns:
        if any(pattern.search(item) for item in texts):
            return f"{pattern.pattern}"
    return ""


def _contains_english_term(text: str, compact: str, term: str) -> bool:
    boundary = re.compile(
        rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])",
        re.IGNORECASE,
    )
    if boundary.search(text):
        return True
    if len(term) >= 3 and term in compact:
        return True
    return False


def detect_qq_ban_risk_reason(prompt: str) -> str:
    """返回 QQ 封号级高危命中规则；空字符串表示未命中。"""
    raw, lowered, compact = _normalized_risk_texts(prompt)
    if not raw.strip():
        return ""
    scan_texts = (raw, lowered, compact)

    strict_match = _match_any_pattern(_QQ_BAN_RISK_REGEX_PATTERNS, scan_texts)
    if strict_match:
        return f"regex:{strict_match}"
    slang_match = _match_any_pattern(_QQ_BAN_RISK_SLANG_REGEX_PATTERNS, scan_texts)
    if slang_match:
        return f"slang:{slang_match}"
    for kw in _QQ_BAN_RISK_TERMS_ZH:
        if kw in lowered or kw in compact:
            return kw
    for kw in _QQ_BAN_RISK_TERMS_EN:
        if _contains_english_term(lowered, compact, kw):
            return kw
    if _contains_english_term(lowered, compact, "av"):
        return "av"
    return ""


def detect_nsfw_prompt_reason(prompt: str) -> str:
    """兼容旧接口：返回 QQ 封号级高危命中规则。"""
    return detect_qq_ban_risk_reason(prompt)


def _normalize_term_list(raw: Any) -> tuple[str, ...]:
    if not isinstance(raw, (list, tuple, set)):
        return ()
    seen: set[str] = set()
    items: list[str] = []
    for item in raw:
        term = normalize_matching_text(str(item or "")).strip().lower()
        if not term or term in seen:
            continue
        seen.add(term)
        items.append(term)
    return tuple(items)


def _custom_term_hit(term: str, lowered: str, compact: str) -> bool:
    normalized = normalize_matching_text(term).strip().lower()
    if not normalized:
        return False
    compact_term = _compact_risk_text(normalized)
    if re.fullmatch(r"[a-z0-9_-]+", normalized):
        candidate = normalized.replace("_", "")
        return _contains_english_term(lowered, compact, candidate)
    return normalized in lowered or (compact_term and compact_term in compact)


def detect_custom_prompt_risk_reason(
    prompt: str,
    custom_block_terms: Any = None,
    custom_allow_terms: Any = None,
) -> str:
    raw, lowered, compact = _normalized_risk_texts(prompt)
    if not raw.strip():
        return ""
    allow_terms = _normalize_term_list(custom_allow_terms)
    if any(_custom_term_hit(term, lowered, compact) for term in allow_terms):
        return ""
    for term in _normalize_term_list(custom_block_terms):
        if _custom_term_hit(term, lowered, compact):
            return term
    return ""


def _extract_text_from_chat_completion_payload(data: dict[str, Any]) -> str:
    if not isinstance(data, dict):
        return ""
    choices = data.get("choices") or []
    if not isinstance(choices, list) or not choices:
        return ""
    message = choices[0].get("message") if isinstance(choices[0], dict) else {}
    if not isinstance(message, dict):
        return ""
    content = message.get("content", "")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                txt = str(item.get("text", "")).strip()
                if txt:
                    parts.append(txt)
        return "".join(parts).strip()
    return str(content).strip()


def _parse_review_answer_payload(answer: str) -> tuple[bool | None, str, str]:
    raw = str(answer or "").strip()
    if not raw:
        return None, "", ""
    candidate = raw
    try:
        obj = json.loads(candidate)
    except Exception:
        match = re.search(r"\{[\s\S]*\}", candidate)
        if not match:
            return None, "", ""
        try:
            obj = json.loads(match.group(0))
        except Exception:
            return None, "", ""
    if not isinstance(obj, dict):
        return None, "", ""
    legal_raw = obj.get("legal")
    legal: bool | None
    if isinstance(legal_raw, bool):
        legal = legal_raw
    elif isinstance(legal_raw, (int, float)):
        legal = bool(legal_raw)
    elif isinstance(legal_raw, str):
        lower = legal_raw.strip().lower()
        if lower in {"true", "1", "yes", "safe", "allow"}:
            legal = True
        elif lower in {"false", "0", "no", "blocked", "deny", "risky"}:
            legal = False
        else:
            legal = None
    else:
        legal = None
    level = str(obj.get("level", "")).strip()
    reason = str(obj.get("reason", "")).strip()
    return legal, level, reason


async def assess_prompt_qq_ban_risk(
    prompt: str,
    style: str | None = None,
    *,
    model_client: Any = None,
    review_model: str = "",
    max_tokens: int = 180,
    fail_closed: bool = False,
    custom_block_terms: Any = None,
    custom_allow_terms: Any = None,
) -> tuple[bool, str]:
    """提示词合规审核（模型优先，规则兜底）。返回 (is_safe, reason)。"""
    content = str(prompt or "").strip()
    if not content:
        return True, ""

    style_text = str(style or "").strip()
    review_text = content if not style_text else f"{content}\nstyle: {style_text}"
    model_review_unavailable = False

    custom_reason = detect_custom_prompt_risk_reason(
        review_text,
        custom_block_terms=custom_block_terms,
        custom_allow_terms=custom_allow_terms,
    )
    if custom_reason:
        return False, f"custom:{custom_reason}"

    client = model_client
    can_review = bool(
        client is not None
        and bool(getattr(client, "enabled", False))
        and callable(getattr(client, "chat_completion", None))
    )
    if can_review:
        user_hint = (
            f"提示词: {normalize_matching_text(content) or '(空)'}"
            + (
                f"\n风格参数: {normalize_matching_text(style_text)}"
                if style_text
                else ""
            )
        )
        messages = [
            {"role": "system", "content": _PROMPT_REVIEW_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    f"{user_hint}\n"
                    "请判断该提示词是否会导向露骨成人内容或封号风险内容。"
                ),
            },
        ]
        try:
            kwargs: dict[str, Any] = {
                "messages": messages,
                "max_tokens": max(80, min(600, int(max_tokens))),
            }
            if str(review_model or "").strip():
                kwargs["model"] = str(review_model).strip()
            data = await client.chat_completion(**kwargs)
            answer_text = _extract_text_from_chat_completion_payload(data)
            legal, level, reason = _parse_review_answer_payload(answer_text)
            if legal is False or str(level or "").strip().lower() in {"risky", "blocked"}:
                return False, reason or "提示词命中高风险内容"
            if legal is True:
                return True, ""
            model_review_unavailable = True
        except Exception as exc:
            _log.warning("image_prompt_review_failed | %s", exc)
            model_review_unavailable = True
    else:
        model_review_unavailable = True

    fallback_reason = detect_qq_ban_risk_reason(review_text)
    if fallback_reason:
        return False, f"fallback:{fallback_reason}"

    if fail_closed and model_review_unavailable:
        return False, "提示词合规审查不可用（fail-closed）"
    return True, ""


@dataclass(slots=True)
class ImageGenResult:
    ok: bool
    message: str
    url: str = ""
    local_path: str = ""
    base64_data: str = ""
    model_used: str = ""
    revised_prompt: str = ""


@dataclass
class ImageGenConfig:
    """图片生成配置。"""
    enable: bool = True
    default_model: str = "dall-e-3"
    default_size: str = "1024x1024"
    nsfw_filter: bool = True
    max_prompt_length: int = 1000
    # 模型配置列表
    models: list[dict[str, Any]] = field(default_factory=list)
    # 本地模板目录
    template_dir: str = "storage/image_templates"


_IMAGE_PROVIDER_ALIASES = {
    "openai_compatible": "openai",
    "compatible": "openai",
    "openai-image": "openai",
    "openai_image": "openai",
    "google": "gemini",
    "gemeni": "gemini",
    "x.ai": "xai",
    "grok": "xai",
    "new-api": "newapi",
    "new_api": "newapi",
    "open_router": "openrouter",
    "silicon_flow": "siliconflow",
    "flux": "siliconflow",
    "stable-diffusion": "sd",
    "stable_diffusion": "sd",
    "stable diffusion": "sd",
    "sdwebui": "sd",
    "sd_webui": "sd",
    "automatic1111": "sd",
    "a1111": "sd",
    "webui": "sd",
}
_IMAGE_OPENAI_COMPATIBLE_PROVIDERS = {
    "openai",
    "skiapi",
    "xai",
    "newapi",
    "siliconflow",
}
_IMAGE_PROVIDER_DEFAULTS = {
    "openai": {"base_url": "https://api.openai.com"},
    "skiapi": {"base_url": "https://skiapi.dev/v1"},
    "xai": {"base_url": "https://api.x.ai/v1"},
    "newapi": {"base_url": "https://api.openai.com/v1"},
    "openrouter": {"base_url": "https://openrouter.ai/api/v1"},
    "siliconflow": {"base_url": "https://api.siliconflow.cn/v1"},
    "gemini": {"base_url": "https://generativelanguage.googleapis.com"},
    "sd": {"base_url": "http://127.0.0.1:7860"},
}
_GEMINI_IMAGE_MODEL_ALIASES = {
    "gemini-3.1-flash-image": "gemini-3.1-flash-image-preview",
}
_XAI_DIRECT_IMAGE_MODEL_ALIASES = {
    "grok-imagine-1.0": "grok-imagine-image",
    "grok-imagine-1.0-fast": "grok-imagine-image",
    "grok-imagine-1.0-edit": "grok-imagine-image",
    "grok-imagine-1.0-video": "grok-imagine-image",
}
_GEMINI_GATEWAY_BASE_HINTS = (
    "skiapi.dev",
    "openrouter.ai",
    "api.openai.com",
    "api.x.ai",
    "siliconflow.cn",
    "dmxapi.com",
    "vveai.com",
    "jina.ai",
    "newapi",
)


def normalize_image_provider_name(raw: str) -> str:
    key = str(raw or "").strip().lower().replace(" ", "_")
    if not key:
        return ""
    return _IMAGE_PROVIDER_ALIASES.get(key, key)


def resolve_image_provider_for_config(model_cfg: dict[str, Any]) -> str:
    provider = normalize_image_provider_name(str(model_cfg.get("provider", "")))
    api_base = str(model_cfg.get("api_base", model_cfg.get("base_url", ""))).strip().lower()
    model_name = str(model_cfg.get("model", model_cfg.get("name", ""))).strip().lower()
    explicit = provider not in {"", "custom"}

    if _looks_sd_webui_base(api_base):
        return "sd"
    if _looks_google_image_base(api_base):
        return "gemini"
    if _looks_openrouter_image_base(api_base):
        return "openrouter"
    if provider in {"skiapi", "newapi"} and _looks_native_gemini_image_model(model_name):
        return "gemini"

    if explicit:
        return provider

    if _looks_native_gemini_image_model(model_name):
        return "gemini"
    if model_name.startswith("grok-imagine"):
        return "xai"
    if "stable-diffusion" in model_name or "sdxl" in model_name:
        return "sd"
    if "flux" in model_name:
        return "siliconflow"
    return "openai"


def _looks_google_image_base(base_url: str) -> bool:
    base = str(base_url or "").strip().lower()
    return "generativelanguage.googleapis.com" in base


def _looks_openrouter_image_base(base_url: str) -> bool:
    base = str(base_url or "").strip().lower()
    return "openrouter.ai" in base


def _looks_sd_webui_base(base_url: str) -> bool:
    base = str(base_url or "").strip().lower()
    return (
        "/sdapi/" in base
        or base.endswith(":7860")
        or "127.0.0.1:7860" in base
        or "localhost:7860" in base
    )


def _looks_native_gemini_image_model(model_name: str) -> bool:
    name = str(model_name or "").strip().lower()
    if name.startswith("google/"):
        name = name.split("/", 1)[1]
    return name.startswith("gemini-") and "image" in name


def _looks_skiapi_style_key(api_key: str) -> bool:
    return str(api_key or "").strip().lower().startswith("sk-o")


def _looks_known_gateway_base_for_native_gemini(base_url: str) -> bool:
    base = str(base_url or "").strip().lower()
    if not base or _looks_google_image_base(base):
        return False
    return any(host in base for host in _GEMINI_GATEWAY_BASE_HINTS)


def _build_native_gemini_config_error(*, api_key: str, api_base: str) -> str:
    hints: list[str] = []
    if _looks_skiapi_style_key(api_key):
        hints.append("检测到 `sk-O...` 聚合网关 Key")
    if _looks_known_gateway_base_for_native_gemini(api_base):
        hints.append(f"检测到非 Google 官方地址 `{api_base}`")
    prefix = f"{'，'.join(hints)}；" if hints else ""
    return (
        f"{prefix}`Gemini` 提供商只能使用 Google 官方 `GEMINI_API_KEY` "
        "和 `https://generativelanguage.googleapis.com`。"
        "如果你想走 SkiAPI / NEWAPI / OpenRouter / 自定义网关，请改用对应的图片提供商。"
    )


def _remap_image_generation_error(*, provider: str, message: str) -> str:
    raw = str(message or "").strip()
    lowered = raw.lower()
    gateway_providers = _IMAGE_OPENAI_COMPATIBLE_PROVIDERS | {"openrouter"}
    if provider in gateway_providers and "503" in lowered and ("无可用渠道" in raw or "distributor" in lowered):
        return (
            "当前图片网关暂无可用生图渠道（上游 503 distributor），不是本地配置错误。"
            "请稍后重试，或改用官方 OpenAI / 官方 Gemini / OpenRouter / SiliconFlow。"
        )
    return raw or "未知错误"


def _normalize_runtime_image_model_name(provider: str, model_name: str) -> str:
    current = str(model_name or "").strip()
    lowered = current.lower()
    if provider == "gemini":
        return _GEMINI_IMAGE_MODEL_ALIASES.get(lowered, current)
    if provider == "xai":
        return _XAI_DIRECT_IMAGE_MODEL_ALIASES.get(lowered, current)
    if provider == "openrouter":
        if "/" in current:
            return current
        if lowered.startswith("gemini-"):
            return f"google/{current}"
        if lowered.startswith("gpt-image-") or lowered.startswith("dall-e-"):
            return f"openai/{current}"
    return current


def _merge_image_prompt(prompt: str, style: str | None) -> str:
    merged = str(prompt or "").strip()
    style_text = str(style or "").strip()
    if style_text:
        merged = f"{merged}\nStyle: {style_text}".strip()
    return merged


def _parse_image_size(size: str) -> tuple[int | None, int | None]:
    raw = str(size or "").strip().lower()
    if "x" not in raw:
        return None, None
    left, right = raw.split("x", 1)
    try:
        width = int(left.strip())
        height = int(right.strip())
    except Exception:
        return None, None
    if width <= 0 or height <= 0:
        return None, None
    return width, height


def _to_data_uri(base64_data: str) -> str:
    raw = str(base64_data or "").strip()
    if not raw:
        return ""
    return f"data:image/png;base64,{raw}"


def _split_data_uri_image(url: str) -> tuple[str, str]:
    raw = str(url or "").strip()
    if not raw.lower().startswith("data:image/"):
        return raw, ""
    _, _, payload = raw.partition(",")
    return raw, payload.strip()


def _build_image_success_result(
    *,
    url: str,
    model_used: str,
    revised_prompt: str = "",
) -> ImageGenResult:
    normalized_url, b64 = _split_data_uri_image(url)
    return ImageGenResult(
        ok=True,
        message="图片已生成。",
        url=normalized_url,
        base64_data=b64,
        model_used=model_used,
        revised_prompt=revised_prompt,
    )


def _build_image_model_label(model_name: str, fallback_name: str = "") -> str:
    return str(model_name or "").strip() or str(fallback_name or "").strip() or "未命名模型"


async def _generate_with_sd_webui(
    *,
    prompt: str,
    api_base: str,
    model_name: str,
    size: str,
    style: str | None,
) -> ImageGenResult:
    base = str(api_base or "").rstrip("/")
    if not base:
        return ImageGenResult(ok=False, message=f"模型 {_build_image_model_label(model_name)} 配置不完整。")

    if base.endswith("/sdapi/v1/txt2img"):
        url = base
    elif base.endswith("/sdapi/v1"):
        url = f"{base}/txt2img"
    else:
        url = f"{base}/sdapi/v1/txt2img"

    width, height = _parse_image_size(size)
    payload: dict[str, Any] = {
        "prompt": _merge_image_prompt(prompt, style),
        "batch_size": 1,
    }
    if width is not None and height is not None:
        payload["width"] = width
        payload["height"] = height

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=10.0)) as client:
            response = await client.post(url, json=payload)
            response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            return ImageGenResult(ok=False, message=f"模型 {_build_image_model_label(model_name)} 返回格式异常。")
        images = data.get("images") or []
        if not isinstance(images, list) or not images:
            return ImageGenResult(ok=False, message=f"模型 {_build_image_model_label(model_name)} 未返回图片数据。")
        raw_b64 = str(images[0] or "").strip()
        if not raw_b64:
            return ImageGenResult(ok=False, message=f"模型 {_build_image_model_label(model_name)} 未返回有效图片。")
        return ImageGenResult(
            ok=True,
            message="图片已生成。",
            url=_to_data_uri(raw_b64),
            base64_data=raw_b64,
            model_used=model_name,
        )
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code if exc.response is not None else 0
        detail = ""
        try:
            payload = exc.response.json() if exc.response is not None else {}
            if isinstance(payload, dict):
                detail = str(payload.get("error", "") or payload.get("detail", "") or payload.get("message", "")).strip()
        except Exception:
            detail = ""
        if not detail and exc.response is not None:
            detail = (exc.response.text or "").strip()[:220]
        return ImageGenResult(
            ok=False,
            message=f"模型 {_build_image_model_label(model_name)} 生成失败（HTTP {status}）：{detail or '请求失败'}",
        )
    except Exception as exc:
        return ImageGenResult(ok=False, message=f"模型 {_build_image_model_label(model_name)} 生成失败：{exc}")


async def generate_image_with_model_config(
    prompt: str,
    model_cfg: dict[str, Any],
    size: str,
    style: str | None = None,
) -> ImageGenResult:
    requested_provider = normalize_image_provider_name(str(model_cfg.get("provider", "")))
    provider = resolve_image_provider_for_config(model_cfg)
    requested_model = str(model_cfg.get("model", model_cfg.get("name", ""))).strip()
    model_name = _normalize_runtime_image_model_name(provider, requested_model)
    api_base = str(model_cfg.get("api_base", model_cfg.get("base_url", ""))).strip()
    api_key = str(model_cfg.get("api_key", "")).strip()
    timeout_seconds = int(model_cfg.get("timeout_seconds", 60) or 60)
    model_label = _build_image_model_label(model_name, requested_model)
    gemini_via_gateway = provider == "gemini" and requested_provider in {"skiapi", "newapi"}

    if provider == "sd":
        return await _generate_with_sd_webui(
            prompt=prompt,
            api_base=api_base or _IMAGE_PROVIDER_DEFAULTS["sd"]["base_url"],
            model_name=model_label,
            size=size,
            style=style,
        )

    if provider == "openrouter" and not api_base:
        api_base = _IMAGE_PROVIDER_DEFAULTS["openrouter"]["base_url"]
    if provider == "gemini" and not api_base:
        api_base = (
            _IMAGE_PROVIDER_DEFAULTS.get(requested_provider, {}).get("base_url", "")
            if gemini_via_gateway
            else _IMAGE_PROVIDER_DEFAULTS["gemini"]["base_url"]
        ) or _IMAGE_PROVIDER_DEFAULTS["gemini"]["base_url"]
    if provider in _IMAGE_OPENAI_COMPATIBLE_PROVIDERS and not api_base:
        api_base = _IMAGE_PROVIDER_DEFAULTS.get(provider, {}).get("base_url", "")

    if provider == "gemini" and not gemini_via_gateway and (
        _looks_skiapi_style_key(api_key) or _looks_known_gateway_base_for_native_gemini(api_base)
    ):
        return ImageGenResult(
            ok=False,
            message=f"模型 {model_label} 配置不兼容：{_build_native_gemini_config_error(api_key=api_key, api_base=api_base)}",
        )
    if provider != "sd" and not api_key:
        return ImageGenResult(ok=False, message=f"模型 {model_label} 配置不完整。")
    if provider not in {"sd"} and provider not in (_IMAGE_OPENAI_COMPATIBLE_PROVIDERS | {"gemini", "openrouter"}):
        provider = "openai"

    try:
        from services.model_client import ModelClient

        client_cfg: dict[str, Any] = {
            "provider": provider,
            "model": model_name,
            "image_model": model_name,
            "timeout_seconds": timeout_seconds,
        }
        if api_base:
            client_cfg["base_url"] = api_base
        if api_key:
            client_cfg["api_key"] = api_key

        client = ModelClient(client_cfg)
        image_url = await client.generate_image(
            prompt=prompt,
            size=size,
            style=style,
        )
        if not image_url:
            return ImageGenResult(ok=False, message=f"模型 {model_label} 生成失败。")
        return _build_image_success_result(url=image_url, model_used=model_name)
    except Exception as exc:
        message = _remap_image_generation_error(provider=provider, message=str(exc).strip())
        return ImageGenResult(ok=False, message=f"模型 {model_label} 生成失败：{message or '未知错误'}")


class ImageGenEngine:
    """增强图片生成引擎。"""

    def __init__(self, config: dict[str, Any] | None = None, model_client: Any = None):
        cfg = config if isinstance(config, dict) else {}
        img_cfg = cfg.get("image_gen", cfg)
        if not isinstance(img_cfg, dict):
            img_cfg = {}

        self.enabled = bool(img_cfg.get("enable", True))
        self.default_model = str(img_cfg.get("default_model", "dall-e-3"))
        self.default_size = str(img_cfg.get("default_size", "1024x1024"))
        self.nsfw_filter = bool(img_cfg.get("nsfw_filter", True))
        self.prompt_review_enable = bool(img_cfg.get("prompt_review_enable", True))
        self.prompt_review_fail_closed = bool(
            img_cfg.get("prompt_review_fail_closed", False)
        )
        self.prompt_review_model = str(
            img_cfg.get("prompt_review_model", "")
        ).strip()
        self.prompt_review_max_tokens = max(
            80, min(600, int(img_cfg.get("prompt_review_max_tokens", 180)))
        )
        self.custom_block_terms = list(
            _normalize_term_list(img_cfg.get("custom_block_terms", []))
        )
        self.custom_allow_terms = list(
            _normalize_term_list(img_cfg.get("custom_allow_terms", []))
        )
        self.max_prompt_length = int(img_cfg.get("max_prompt_length", 1000))
        self.post_review_enable = bool(img_cfg.get("post_review_enable", True))
        self.post_review_fail_closed = bool(
            img_cfg.get("post_review_fail_closed", True)
        )
        self.post_review_model = str(img_cfg.get("post_review_model", "")).strip()
        self.post_review_max_tokens = max(
            120, min(1200, int(img_cfg.get("post_review_max_tokens", 260)))
        )
        self.model_client = model_client

        # 解析模型配置（兼容按 name 或 model 进行匹配）
        self._models: list[dict[str, Any]] = []
        self._models_by_name: dict[str, dict[str, Any]] = {}
        self._models_by_model: dict[str, dict[str, Any]] = {}
        for raw in img_cfg.get("models", []):
            if not isinstance(raw, dict):
                continue
            item = dict(raw)
            self._models.append(item)

            name = str(item.get("name", "")).strip()
            model_name = str(item.get("model", "")).strip()
            if name:
                self._models_by_name[name] = item
                self._models_by_name[name.lower()] = item
            if model_name:
                self._models_by_model[model_name] = item
                self._models_by_model[model_name.lower()] = item

        self._template_dir = Path(img_cfg.get("template_dir", "storage/image_templates"))
        self._template_dir.mkdir(parents=True, exist_ok=True)

    def check_nsfw(self, prompt: str) -> bool:
        """检查 prompt 是否命中 QQ 封号级高危内容。返回 True 表示安全。"""
        if not self.nsfw_filter:
            return True
        custom_reason = detect_custom_prompt_risk_reason(
            prompt,
            custom_block_terms=self.custom_block_terms,
            custom_allow_terms=self.custom_allow_terms,
        )
        if custom_reason:
            _log.warning("image_custom_risk_blocked | reason=%s", custom_reason)
            return False
        reason = detect_qq_ban_risk_reason(prompt)
        if reason:
            _log.warning("qq_ban_risk_blocked | reason=%s", reason)
            return False
        return True

    @property
    def models(self) -> list[dict[str, Any]]:
        return self._models

    async def health_check(self) -> list[dict[str, Any]]:
        """自检所有已配置的图片生成模型是否可用。"""
        results: list[dict[str, Any]] = []
        for model_cfg in self._models:
            name = str(model_cfg.get("name", model_cfg.get("model", "unknown")))
            base_url = str(model_cfg.get("base_url", "")).strip()
            api_key = str(model_cfg.get("api_key", "")).strip()
            if not base_url:
                results.append({"name": name, "status": "skip", "detail": "无 base_url"})
                continue
            try:
                async with httpx.AsyncClient(timeout=10) as client:
                    # 尝试访问 models 端点验证连通性
                    headers = {}
                    if api_key:
                        headers["Authorization"] = f"Bearer {api_key}"
                    resp = await client.get(
                        f"{base_url.rstrip('/')}/models",
                        headers=headers,
                    )
                    if resp.status_code < 500:
                        results.append({"name": name, "status": "ok", "detail": f"HTTP {resp.status_code}"})
                    else:
                        results.append({"name": name, "status": "error", "detail": f"HTTP {resp.status_code}"})
            except Exception as ex:
                results.append({"name": name, "status": "error", "detail": str(ex)[:100]})
        return results

    async def generate(
        self,
        prompt: str,
        model: str | None = None,
        size: str | None = None,
        style: str | None = None,
    ) -> ImageGenResult:
        """生成图片。"""
        if not self.enabled:
            return ImageGenResult(ok=False, message="图片生成功能未启用。")

        content = (prompt or "").strip()
        if not content:
            return ImageGenResult(ok=False, message="请提供绘图描述。")

        if len(content) > self.max_prompt_length:
            content = content[:self.max_prompt_length]

        # 提示词前置审核（提示词审核优先，本地规则兜底）。
        nsfw_check_text = content if not style else f"{content}\nstyle: {style}"
        if self.prompt_review_enable:
            prompt_safe, prompt_reason = await assess_prompt_qq_ban_risk(
                content,
                style=style,
                model_client=self.model_client,
                review_model=self.prompt_review_model,
                max_tokens=self.prompt_review_max_tokens,
                fail_closed=self.prompt_review_fail_closed,
                custom_block_terms=self.custom_block_terms,
                custom_allow_terms=self.custom_allow_terms,
            )
            if not prompt_safe:
                _log.warning("image_prompt_review_blocked | reason=%s", prompt_reason)
                return ImageGenResult(ok=False, message=_PROMPT_REVIEW_BLOCK_MESSAGE)
        elif not self.check_nsfw(nsfw_check_text):
            return ImageGenResult(ok=False, message=IMAGE_PROMPT_BLOCKED_MESSAGE)

        use_model = model or self.default_model
        use_size = size or self.default_size

        # 尝试使用 image_gen 配置模型（优先 model 字段，再 name）
        model_cfg, matched_by = self._resolve_model_config(use_model)
        if not model_cfg and self._models:
            # 开箱即用兜底：
            # 当只有 1 条配置模型时，优先走这条，避免 Agent 误传模型名导致回退到 model_client。
            if len(self._models) == 1:
                auto_cfg = self._models[0]
                auto_model = str(auto_cfg.get("model", "")).strip() or str(auto_cfg.get("name", "")).strip()
                if auto_model:
                    _log.warning(
                        "image_gen_model_auto_override | requested=%s | override=%s | reason=single_configured_model",
                        use_model,
                        auto_model,
                    )
                    use_model = auto_model
                    model_cfg = auto_cfg
                    matched_by = "auto_single_config"
        if model_cfg:
            _log.info(
                "image_gen_route | route=config | requested=%s | matched_by=%s | cfg_name=%s | cfg_model=%s",
                use_model,
                matched_by,
                str(model_cfg.get("name", "")),
                str(model_cfg.get("model", "")),
            )
            primary_result = await self._generate_with_config(content, model_cfg, use_size, style)
            if primary_result.ok:
                reviewed = await self._apply_post_review(
                    result=primary_result,
                    prompt=content,
                    style=style,
                )
                if reviewed is not None:
                    return reviewed
                return primary_result

            # 兜底容错：主模型暂时不可用时自动切换同配置中的其它模型重试。
            if self._should_failover_to_other_model(primary_result):
                for alt_cfg in self._models:
                    if alt_cfg is model_cfg:
                        continue
                    alt_model = str(alt_cfg.get("model", "")).strip() or str(alt_cfg.get("name", "")).strip()
                    if not alt_model:
                        continue
                    _log.warning(
                        "image_gen_failover_try | from=%s | to=%s",
                        str(model_cfg.get("model", "")).strip() or str(model_cfg.get("name", "")).strip(),
                        alt_model,
                    )
                    alt_result = await self._generate_with_config(content, alt_cfg, use_size, style)
                    if alt_result.ok:
                        _log.info("image_gen_failover_ok | model=%s", alt_model)
                        reviewed = await self._apply_post_review(
                            result=alt_result,
                            prompt=content,
                            style=style,
                        )
                        if reviewed is not None:
                            return reviewed
                        return alt_result
            return primary_result

        _log.info(
            "image_gen_route | route=model_client_fallback | requested=%s | configured=%d",
            use_model,
            len(self._models),
        )

        # 回退到 model_client
        if self.model_client and hasattr(self.model_client, "generate_image"):
            fallback_error = ""
            try:
                url = await self.model_client.generate_image(content, size=use_size, style=style)
                if url:
                    generated = ImageGenResult(
                        ok=True,
                        message="图片已生成。",
                        url=url,
                        model_used=use_model,
                    )
                    reviewed = await self._apply_post_review(
                        result=generated,
                        prompt=content,
                        style=style,
                    )
                    if reviewed is not None:
                        return reviewed
                    return generated
            except Exception as exc:
                _log.warning("image_gen_fallback_error | %s", exc)
                fallback_error = str(exc)
            if fallback_error:
                return ImageGenResult(ok=False, message=f"图片生成失败（回退通道异常）: {fallback_error}")

        return ImageGenResult(ok=False, message="图片生成失败（未命中可用生图模型）。")

    def _resolve_model_config(self, requested_model: str) -> tuple[dict[str, Any] | None, str]:
        key = str(requested_model or "").strip()
        if not key:
            return None, ""

        # 1) 先按 model 字段精确匹配（最符合 default_model 场景）
        model_cfg = self._models_by_model.get(key) or self._models_by_model.get(key.lower())
        if model_cfg:
            return model_cfg, "model"

        # 2) 再按 name 字段匹配（兼容旧配置）
        model_cfg = self._models_by_name.get(key) or self._models_by_name.get(key.lower())
        if model_cfg:
            return model_cfg, "name"

        return None, ""

    @staticmethod
    def _should_failover_to_other_model(result: ImageGenResult) -> bool:
        if result.ok:
            return False
        text = (result.message or "").lower()
        if not text:
            return False
        retry_cues = (
            "503",
            "无可用渠道",
            "distributor",
            "timeout",
            "超时",
            "temporarily unavailable",
            "bad gateway",
            "gateway timeout",
            "connection reset",
        )
        return any(cue in text for cue in retry_cues)

    async def _generate_with_config(
        self,
        prompt: str,
        model_cfg: dict[str, Any],
        size: str,
        style: str | None,
    ) -> ImageGenResult:
        """使用配置的模型生成图片。"""
        result = await generate_image_with_model_config(
            prompt=prompt,
            model_cfg=model_cfg,
            size=size,
            style=style,
        )
        if not result.ok:
            _log.warning("image_gen_api_error | model=%s | detail=%s", model_cfg.get("model", model_cfg.get("name", "")), result.message)
        return result

    def list_models(self) -> list[dict[str, str]]:
        """列出所有可用的图片生成模型。"""
        models = [{"name": self.default_model, "type": "default"}]
        seen: set[str] = {self.default_model}
        for cfg in self._models:
            model_name = str(cfg.get("model", "")).strip() or str(cfg.get("name", "")).strip()
            if not model_name or model_name in seen:
                continue
            seen.add(model_name)
            models.append({"name": model_name, "type": str(cfg.get("type", "openai_compatible"))})
        return models

    async def _apply_post_review(
        self,
        result: ImageGenResult,
        prompt: str,
        style: str | None,
    ) -> ImageGenResult | None:
        ok, reason = await self._review_generated_image(
            result=result,
            prompt=prompt,
            style=style,
        )
        if ok:
            return None
        return ImageGenResult(
            ok=False,
            message=reason or "生成结果未通过合规审查，已拦截发送。",
            model_used=result.model_used,
        )

    async def _review_generated_image(
        self,
        result: ImageGenResult,
        prompt: str,
        style: str | None,
    ) -> tuple[bool, str]:
        if not self.post_review_enable:
            return True, ""

        if not result.ok:
            return False, result.message or "图片生成失败"

        client = self.model_client
        if client is None:
            return (
                (not self.post_review_fail_closed),
                "主模型不可用，无法完成生成后合规审查，已拦截。",
            )
        if not bool(getattr(client, "enabled", False)):
            return (
                (not self.post_review_fail_closed),
                "主模型未启用，无法完成生成后合规审查，已拦截。",
            )

        review_model = self.post_review_model or str(
            getattr(client, "model", "") or ""
        ).strip()
        if not review_model:
            review_model = str(self.default_model or "").strip()

        protocol_checker = getattr(client, "supports_multimodal_messages", None)
        if callable(protocol_checker):
            try:
                if not bool(protocol_checker()):
                    return (
                        (not self.post_review_fail_closed),
                        "主模型通道不支持图片审查协议，无法完成生成后合规审查，已拦截。",
                    )
            except Exception:
                if self.post_review_fail_closed:
                    return False, "合规审查协议检测失败，已拦截。"

        checker = getattr(client, "supports_vision_input", None)
        if callable(checker):
            try:
                if not bool(checker(model=review_model)):
                    return (
                        (not self.post_review_fail_closed),
                        "主模型不支持图片输入，无法完成生成后合规审查，已拦截。",
                    )
            except Exception:
                if self.post_review_fail_closed:
                    return False, "合规审查能力检测失败，已拦截。"

        image_ref = self._build_review_image_ref(result)
        if not image_ref:
            return (
                (not self.post_review_fail_closed),
                "生成结果缺少可审查的图片内容，已拦截。",
            )

        messages = self._build_review_messages(
            image_ref=image_ref,
            prompt=prompt,
            style=style,
        )
        try:
            completion_kwargs: dict[str, Any] = {
                "messages": messages,
                "max_tokens": self.post_review_max_tokens,
            }
            if review_model:
                completion_kwargs["model"] = review_model
            data = await client.chat_completion(**completion_kwargs)
        except Exception as exc:
            _log.warning("image_gen_post_review_call_failed | %s", exc)
            if self.post_review_fail_closed:
                return False, "生成后合规审查调用失败，已拦截。"
            return True, ""

        answer_text = self._extract_text_from_chat_completion(data)
        if not answer_text:
            if self.post_review_fail_closed:
                return False, "生成后合规审查返回为空，已拦截。"
            return True, ""

        legal, level, reason = self._parse_review_answer(answer_text)
        if legal is None and self.post_review_fail_closed:
            return False, "生成后合规审查结果不可解析，已拦截。"

        decision_level = (level or "").strip().lower()
        if legal is False or decision_level in {"risky", "blocked"}:
            detail = reason or "主模型判定为不合规"
            _log.warning(
                "image_gen_post_review_blocked | level=%s | reason=%s",
                decision_level or "-",
                detail,
            )
            return False, f"生成结果未通过合规审查：{detail}"
        return True, ""

    @staticmethod
    def _build_review_image_ref(result: ImageGenResult) -> str:
        url = str(result.url or "").strip()
        if url:
            return url
        b64 = str(result.base64_data or "").strip()
        if b64:
            return f"data:image/png;base64,{b64}"
        return ""

    @staticmethod
    def _build_review_messages(
        image_ref: str,
        prompt: str,
        style: str | None,
    ) -> list[dict[str, Any]]:
        clean_prompt = normalize_matching_text(prompt)
        style_text = normalize_matching_text(style or "")
        user_hint = (
            f"原始生图提示词: {clean_prompt or '(空)'}"
            + (f"\n风格参数: {style_text}" if style_text else "")
        )
        return [
            {
                "role": "system",
                "content": (
                    "你是图片内容合规审核器。严格审查色情/露点/R18/未成年相关内容。"
                    "涉及胸部/生殖器特写、奶子/逼等露骨暗示、或疑似擦边仿露骨内容，也必须判不合规。"
                    "必须只输出 JSON，不要任何解释。"
                    '格式: {"legal": true/false, "level": "safe|risky|blocked", "reason": "简短中文原因"}'
                ),
            },
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": image_ref}},
                    {
                        "type": "text",
                        "text": (
                            f"{user_hint}\n"
                            "请判断该图片是否适合群聊发送。"
                            "一旦有露骨/成人倾向或明显擦边伪装，legal 必须为 false。"
                        ),
                    },
                ],
            },
        ]

    @staticmethod
    def _extract_text_from_chat_completion(data: dict[str, Any]) -> str:
        return _extract_text_from_chat_completion_payload(data)

    @staticmethod
    def _parse_review_answer(answer: str) -> tuple[bool | None, str, str]:
        return _parse_review_answer_payload(answer)
