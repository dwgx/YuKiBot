import { Cpu, Zap, Shield, Volume2, Cookie } from "lucide-react";

export const BASE = "/api/webui";
export const SETUP_DRAFT_KEY = "yukiko.setup.draft.v1";

export const PROVIDERS = [
  { value: "newapi", label: "NEWAPI", model: "gpt-5-codex", env: "NEWAPI_API_KEY", endpointType: "openai" },
  { value: "openai", label: "OpenAI", model: "gpt-5.3", env: "OPENAI_API_KEY", endpointType: "openai_response" },
  { value: "anthropic", label: "Anthropic", model: "claude-sonnet-4-5-20250929", env: "ANTHROPIC_API_KEY", endpointType: "anthropic" },
  { value: "gemini", label: "Gemini", model: "gemini-2.5-pro", env: "GEMINI_API_KEY", endpointType: "gemini" },
  { value: "deepseek", label: "DeepSeek", model: "deepseek-chat", env: "DEEPSEEK_API_KEY", endpointType: "openai" },
  { value: "openrouter", label: "OpenRouter", model: "openrouter/auto", env: "OPENROUTER_API_KEY", endpointType: "openai" },
  { value: "xai", label: "xAI (Grok)", model: "grok-4.1-mini", env: "XAI_API_KEY", endpointType: "openai" },
  { value: "qwen", label: "Qwen", model: "qwen-max-latest", env: "QWEN_API_KEY", endpointType: "openai" },
  { value: "moonshot", label: "Moonshot (Kimi)", model: "kimi-thinking-preview", env: "MOONSHOT_API_KEY", endpointType: "openai" },
  { value: "mistral", label: "Mistral", model: "mistral-medium-latest", env: "MISTRAL_API_KEY", endpointType: "openai" },
  { value: "zhipu", label: "Zhipu", model: "glm-4-plus", env: "ZHIPU_API_KEY", endpointType: "openai" },
  { value: "siliconflow", label: "SiliconFlow", model: "Qwen/Qwen2.5-72B-Instruct", env: "SILICONFLOW_API_KEY", endpointType: "openai" },
];

export const DEFAULT_SETUP_PROVIDER = PROVIDERS[0]?.value || "newapi";
export const DEFAULT_IMAGE_GEN_PROVIDER = "newapi";

export const ENDPOINT_TYPE_OPTIONS = [
  { value: "openai_response", label: "OpenAI-Response" },
  { value: "openai", label: "OpenAI" },
  { value: "anthropic", label: "Anthropic" },
  { value: "dmxapi", label: "DMXAPI" },
  { value: "gemini", label: "Gemini" },
  { value: "weiyi_ai", label: "唯—AI (A)" },
];

export const IMAGE_GEN_DEFAULTS: Record<string, { model: string; baseUrl: string; env: string }> = {
  skiapi: { model: "gpt-image-1", baseUrl: "https://skiapi.dev/v1", env: "SKIAPI_KEY" },
  openai: { model: "gpt-image-1", baseUrl: "https://api.openai.com/v1", env: "OPENAI_API_KEY" },
  gemini: { model: "gemini-2.5-flash-image", baseUrl: "https://generativelanguage.googleapis.com", env: "GEMINI_API_KEY" },
  xai: { model: "grok-imagine-image", baseUrl: "https://api.x.ai/v1", env: "XAI_API_KEY" },
  newapi: { model: "gpt-image-1", baseUrl: "https://api.openai.com/v1", env: "NEWAPI_API_KEY" },
  openrouter: { model: "google/gemini-2.5-flash-image", baseUrl: "https://openrouter.ai/api/v1", env: "OPENROUTER_API_KEY" },
  siliconflow: { model: "black-forest-labs/FLUX.1-schnell", baseUrl: "https://api.siliconflow.cn/v1", env: "SILICONFLOW_API_KEY" },
  flux: { model: "black-forest-labs/FLUX.1-schnell", baseUrl: "https://api.siliconflow.cn/v1", env: "SILICONFLOW_API_KEY" },
  sd: { model: "stable-diffusion-xl", baseUrl: "http://127.0.0.1:7860", env: "API_KEY" },
  custom: { model: "gpt-image-1", baseUrl: "", env: "API_KEY" },
};

export const IMAGE_GEN_MODEL_HINTS: Record<string, string> = {
  skiapi: "推荐 gpt-image-1；如果填 Gemini 图片模型（如 gemini-2.5-flash-image / gemini-3.1-flash-image），系统会自动走 Gemini generateContent 代理通道。",
  openai: "推荐 gpt-image-1。",
  gemini: "推荐 gemini-2.5-flash-image；这项会走 Google 官方 Gemini 图片接口。",
  xai: "推荐 grok-imagine-image。",
  newapi: "推荐 gpt-image-1 或你的 NEWAPI 网关实际支持的图片模型。",
  openrouter: "推荐 google/gemini-2.5-flash-image。",
  siliconflow: "推荐 black-forest-labs/FLUX.1-schnell。",
  flux: "Flux 预设默认走 SiliconFlow。",
  sd: "本地 Stable Diffusion WebUI 通常填 stable-diffusion-xl 即可。",
  custom: "自定义网关可填 OpenAI 兼容、Gemini 原生或本地 SD WebUI。",
};

export const normalizeImageGenBaseRoot = (value: string) =>
  String(value || "")
    .trim()
    .replace(/\/+$/, "")
    .replace(/\/(v1beta|v1)$/i, "");

export const IMAGE_GEN_DEFAULT_BASE_ROOTS = new Set(
  Object.values(IMAGE_GEN_DEFAULTS)
    .map((item) => normalizeImageGenBaseRoot(item.baseUrl))
    .filter(Boolean),
);

export const shouldResetImageGenBaseUrl = (value: string) => {
  const root = normalizeImageGenBaseRoot(value);
  return !root || IMAGE_GEN_DEFAULT_BASE_ROOTS.has(root);
};

export const getImageGenApiKeyDescription = (provider: string) => {
  const envName = IMAGE_GEN_DEFAULTS[provider]?.env || "API_KEY";
  if (provider === "gemini") {
    return `留空则从环境变量 ${envName} 读取；Gemini 原生生图必须使用 Google 官方 Key，不支持 sk-O... 网关 Key`;
  }
  return `留空则从环境变量 ${envName} 读取`;
};

export { MODEL_OPTIONS } from "../../shared/model-options";

export const STEPS = [
  { icon: Cpu, title: "API 配置" },
  { icon: Zap, title: "功能开关" },
  { icon: Shield, title: "管理 & 输出" },
  { icon: Volume2, title: "音乐 & 画图" },
  { icon: Cookie, title: "平台 Cookie" },
];

export type SetupDraft = {
  step?: number;
  provider?: string;
  endpointType?: string;
  model?: string;
  apiKey?: string;
  baseUrl?: string;
  botName?: string;
  search?: boolean;
  image?: boolean;
  markdown?: boolean;
  superAdmin?: string;
  verbosity?: string;
  tokenSaving?: boolean;
  musicEnable?: boolean;
  musicApi?: string;
  imageGenEnable?: boolean;
  imageGenProvider?: string;
  imageGenApiKey?: string;
  imageGenBaseUrl?: string;
  imageGenModel?: string;
  imageGenSize?: string;
  biliSessdata?: string;
  biliBiliJct?: string;
  douyinCookie?: string;
  kuaishouCookie?: string;
  qzoneCookie?: string;
  cookieBrowser?: string;
  cookieAllowClose?: boolean;
};

export type CookieCapabilities = {
  os?: string;
  browsers?: {
    recommended?: string;
    installed?: string[];
    scan_login_supported?: string[];
  };
  notices?: string[];
  platforms?: {
    bilibili?: { qr_scan?: boolean; browser_extract?: boolean; browser_scan_login?: boolean };
    douyin?: { browser_extract?: boolean; browser_scan_login?: boolean };
    kuaishou?: { browser_extract?: boolean; browser_scan_login?: boolean };
    qzone?: { browser_extract?: boolean; browser_scan_login?: boolean };
  };
};

export type CookieLoginGuide = {
  message?: string;
  login_url?: string;
  after_login_url?: string;
  instructions?: string[];
  notes?: string[];
};

