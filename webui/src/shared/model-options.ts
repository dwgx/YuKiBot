/**
 * Shared model options used by Config and Setup pages.
 * These are suggestions only: the UI must still allow custom model names.
 */
export type ModelOption = { value: string; label: string; description?: string };

const opt = (value: string, description = ""): ModelOption => ({
  value,
  label: value,
  ...(description ? { description } : {}),
});

export const uniqueModelOptions = (...groups: Array<ModelOption[] | undefined>): ModelOption[] => {
  const seen = new Set<string>();
  const out: ModelOption[] = [];
  for (const group of groups) {
    for (const item of group || []) {
      const value = String(item.value || "").trim();
      if (!value) continue;
      const key = value.toLowerCase();
      if (seen.has(key)) continue;
      seen.add(key);
      out.push({ ...item, value, label: item.label || value });
    }
  }
  return out;
};

export const MODEL_OPTIONS: Record<string, ModelOption[]> = {
  skiapi: [
    opt("gpt-5.5", "Codex / frontier"),
    opt("gpt-5.4"),
    opt("gpt-5.4-mini"),
    { value: "claude-opus-4-6", label: "claude-opus-4-6" },
    { value: "claude-sonnet-4-5-20250929", label: "claude-sonnet-4-5-20250929" },
    { value: "claude-haiku-4-5-20251001", label: "claude-haiku-4-5-20251001" },
    { value: "grok-4.1-mini", label: "grok-4.1-mini" },
    { value: "grok-4", label: "grok-4" },
    { value: "grok-4.1-thinking", label: "grok-4.1-thinking" },
    { value: "grok-4.1-fast", label: "grok-4.1-fast" },
    { value: "grok-4.1-expert", label: "grok-4.1-expert" },
    { value: "grok-4.20-beta", label: "grok-4.20-beta" },
    { value: "grok-imagine-1.0", label: "grok-imagine-1.0" },
    { value: "grok-imagine-1.0-fast", label: "grok-imagine-1.0-fast" },
    { value: "grok-imagine-1.0-edit", label: "grok-imagine-1.0-edit" },
    { value: "grok-imagine-1.0-video", label: "grok-imagine-1.0-video" },
    { value: "gpt-5.3", label: "gpt-5.3" },
    { value: "gpt-5.3-codex", label: "gpt-5.3-codex" },
    { value: "gpt-5.3-codex-spark", label: "gpt-5.3-codex-spark" },
    { value: "gpt-5.2-codex", label: "gpt-5.2-codex" },
    { value: "gpt-5.1-codex-mini", label: "gpt-5.1-codex-mini" },
    { value: "gpt-5.1-codex-max", label: "gpt-5.1-codex-max" },
    { value: "gpt-5.1-codex", label: "gpt-5.1-codex" },
    { value: "gpt-5-codex", label: "gpt-5-codex" },
    { value: "gpt-5-codex-mini", label: "gpt-5-codex-mini" },
    { value: "codex-mini-latest", label: "codex-mini-latest" },
    { value: "gpt-5.2", label: "gpt-5.2" },
    { value: "gpt-5", label: "gpt-5" },
  ],
  openai: [
    opt("gpt-5.5", "Codex / frontier"),
    opt("gpt-5.4"),
    opt("gpt-5.4-mini"),
    { value: "gpt-5.3", label: "gpt-5.3" },
    { value: "gpt-5.3-codex", label: "gpt-5.3-codex" },
    { value: "gpt-5.3-codex-spark", label: "gpt-5.3-codex-spark" },
    { value: "gpt-5.2-codex", label: "gpt-5.2-codex" },
    { value: "gpt-5.1-codex-mini", label: "gpt-5.1-codex-mini" },
    { value: "gpt-5.1-codex-max", label: "gpt-5.1-codex-max" },
    { value: "gpt-5.1-codex", label: "gpt-5.1-codex" },
    { value: "gpt-5-codex", label: "gpt-5-codex" },
    { value: "gpt-5.2", label: "gpt-5.2" },
    { value: "gpt-5", label: "gpt-5" },
    { value: "gpt-5-mini", label: "gpt-5-mini" },
    { value: "gpt-5-nano", label: "gpt-5-nano" },
  ],
  deepseek: [
    { value: "deepseek-chat", label: "deepseek-chat" },
    { value: "deepseek-reasoner", label: "deepseek-reasoner" },
  ],
  anthropic: [
    { value: "claude-sonnet-4-5-20250929", label: "claude-sonnet-4-5-20250929" },
    { value: "claude-opus-4-1", label: "claude-opus-4-1" },
  ],
  gemini: [
    { value: "gemini-2.5-pro", label: "gemini-2.5-pro" },
    { value: "gemini-2.5-flash", label: "gemini-2.5-flash" },
  ],
  newapi: [
    opt("gpt-5.5", "Codex / frontier"),
    opt("gpt-5.4"),
    opt("gpt-5.4-mini"),
    { value: "gpt-5.3", label: "gpt-5.3" },
    { value: "gpt-5.3-codex", label: "gpt-5.3-codex" },
    { value: "gpt-5.3-codex-spark", label: "gpt-5.3-codex-spark" },
    { value: "gpt-5.2-codex", label: "gpt-5.2-codex" },
    { value: "gpt-5.1-codex-mini", label: "gpt-5.1-codex-mini" },
    { value: "gpt-5.1-codex-max", label: "gpt-5.1-codex-max" },
    { value: "gpt-5.1-codex", label: "gpt-5.1-codex" },
    { value: "gpt-5-codex", label: "gpt-5-codex" },
    { value: "gpt-5-codex-mini", label: "gpt-5-codex-mini" },
    { value: "codex-mini-latest", label: "codex-mini-latest" },
    { value: "gpt-5", label: "gpt-5" },
    { value: "gpt-5-mini", label: "gpt-5-mini" },
    { value: "gpt-5-nano", label: "gpt-5-nano" },
  ],
  openrouter: [
    { value: "openrouter/auto", label: "openrouter/auto" },
    { value: "openai/gpt-5", label: "openai/gpt-5" },
  ],
  xai: [
    { value: "grok-4.1-mini", label: "grok-4.1-mini" },
    { value: "grok-4", label: "grok-4" },
    { value: "grok-4.1-thinking", label: "grok-4.1-thinking" },
    { value: "grok-4.1-fast", label: "grok-4.1-fast" },
    { value: "grok-4.1-expert", label: "grok-4.1-expert" },
    { value: "grok-4.20-beta", label: "grok-4.20-beta" },
    { value: "grok-imagine-1.0", label: "grok-imagine-1.0" },
    { value: "grok-imagine-1.0-fast", label: "grok-imagine-1.0-fast" },
    { value: "grok-imagine-1.0-edit", label: "grok-imagine-1.0-edit" },
    { value: "grok-imagine-1.0-video", label: "grok-imagine-1.0-video" },
  ],
  qwen: [
    { value: "qwen-max-latest", label: "qwen-max-latest" },
    { value: "qwen-plus-latest", label: "qwen-plus-latest" },
  ],
  moonshot: [
    { value: "kimi-thinking-preview", label: "kimi-thinking-preview" },
    { value: "moonshot-v1-128k", label: "moonshot-v1-128k" },
  ],
  mistral: [
    { value: "mistral-medium-latest", label: "mistral-medium-latest" },
    { value: "mistral-large-latest", label: "mistral-large-latest" },
  ],
  zhipu: [
    { value: "glm-4-plus", label: "glm-4-plus" },
    { value: "glm-4-air", label: "glm-4-air" },
  ],
  siliconflow: [
    { value: "Qwen/Qwen2.5-72B-Instruct", label: "Qwen/Qwen2.5-72B-Instruct" },
    { value: "deepseek-ai/DeepSeek-V3", label: "deepseek-ai/DeepSeek-V3" },
  ],
};

export const IMAGE_MODEL_OPTIONS: Record<string, ModelOption[]> = {
  skiapi: [
    opt("gpt-image-1"),
    opt("gemini-2.5-flash-image"),
    opt("gemini-3.1-flash-image"),
    opt("grok-imagine-1.0"),
    opt("grok-imagine-1.0-fast"),
    opt("grok-imagine-1.0-edit"),
  ],
  openai: [
    opt("gpt-image-1"),
    opt("dall-e-3"),
    opt("dall-e-2"),
  ],
  gemini: [
    opt("gemini-2.5-flash-image"),
    opt("gemini-3.1-flash-image"),
  ],
  xai: [
    opt("grok-imagine-image"),
    opt("grok-imagine-1.0"),
    opt("grok-imagine-1.0-fast"),
    opt("grok-imagine-1.0-edit"),
    opt("grok-imagine-1.0-video"),
  ],
  newapi: [
    opt("gpt-image-1"),
    opt("dall-e-3"),
    opt("gemini-2.5-flash-image"),
    opt("gemini-3.1-flash-image"),
    opt("grok-imagine-1.0"),
    opt("black-forest-labs/FLUX.1-schnell"),
  ],
  openrouter: [
    opt("google/gemini-2.5-flash-image"),
    opt("openai/gpt-image-1"),
  ],
  siliconflow: [
    opt("black-forest-labs/FLUX.1-schnell"),
    opt("black-forest-labs/FLUX.1-dev"),
    opt("stabilityai/stable-diffusion-xl-base-1.0"),
  ],
  flux: [
    opt("black-forest-labs/FLUX.1-schnell"),
    opt("black-forest-labs/FLUX.1-dev"),
  ],
  sd: [
    opt("stable-diffusion-xl"),
    opt("sdxl"),
  ],
  custom: [
    opt("gpt-image-1"),
    opt("gemini-2.5-flash-image"),
    opt("black-forest-labs/FLUX.1-schnell"),
    opt("stable-diffusion-xl"),
  ],
};

export const allModelOptions = (optionsByProvider: Record<string, ModelOption[]>): ModelOption[] =>
  uniqueModelOptions(...Object.values(optionsByProvider));
