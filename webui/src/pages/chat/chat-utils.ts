import type { ChatHistoryPermission, ChatMessageItem } from "../../api/client";

export function fmtTs(ts: number): string {
  if (!ts || Number.isNaN(ts)) return "-";
  const d = new Date(ts * 1000);
  if (Number.isNaN(d.getTime())) return "-";
  const now = new Date();
  const diffMs = now.getTime() - d.getTime();
  const diffMin = Math.floor(diffMs / 60000);
  if (diffMin < 1) return "刚刚";
  if (diffMin < 60) return `${diffMin}分钟前`;
  const isToday = d.toDateString() === now.toDateString();
  const yesterday = new Date(now);
  yesterday.setDate(yesterday.getDate() - 1);
  const isYesterday = d.toDateString() === yesterday.toDateString();
  const time = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  if (isToday) return time;
  if (isYesterday) return `昨天 ${time}`;
  const diffDays = Math.floor(diffMs / 86400000);
  if (diffDays < 7) {
    const weekdays = ["周日", "周一", "周二", "周三", "周四", "周五", "周六"];
    return `${weekdays[d.getDay()]} ${time}`;
  }
  if (d.getFullYear() === now.getFullYear()) {
    return `${d.getMonth() + 1}/${d.getDate()} ${time}`;
  }
  return d.toLocaleString();
}

export function clip(text: string, max = 64): string {
  const raw = String(text || "").trim();
  if (!raw) return "";
  return raw.length > max ? `${raw.slice(0, max)}...` : raw;
}

export function hasImageSegment(msg: ChatMessageItem | null): boolean {
  if (!msg) return false;
  if (Array.isArray(msg.segments) && msg.segments.some((seg) => {
    const segType = String(seg?.type || "").toLowerCase();
    return segType === "image" || segType === "mface";
  })) {
    return true;
  }
  return (
    String(msg.text || "").includes("[image]")
    || String(msg.text || "").includes("[mface]")
    || String(msg.text || "").includes("[图片]")
  );
}

export type MessageMediaKind = "image" | "video" | "audio" | "face";
export type MessageMediaItem = {
  key: string;
  kind: MessageMediaKind;
  url: string;
  label: string;
  faceId: string;
  fileToken: string;
};

function asSegmentData(raw: unknown): Record<string, unknown> {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return {};
  return raw as Record<string, unknown>;
}

function firstNonEmptyValue(data: Record<string, unknown>, keys: string[]): string {
  for (const key of keys) {
    const value = String(data[key] ?? "").trim();
    if (value) return value;
  }
  return "";
}

function normalizeSegmentMediaUrl(raw: string, kind: MessageMediaKind): string {
  const value = String(raw || "").trim();
  if (!value) return "";
  if (/^https?:\/\//i.test(value) || /^data:/i.test(value) || /^blob:/i.test(value)) return value;
  if (value.startsWith("//")) return `https:${value}`;
  if (value.startsWith("/api/") || value.startsWith("/webui/")) return value;
  if (value.startsWith("base64://")) {
    const body = value.slice("base64://".length);
    if (!body) return "";
    if (kind === "video") return `data:video/mp4;base64,${body}`;
    if (kind === "audio") return `data:audio/mpeg;base64,${body}`;
    return `data:image/png;base64,${body}`;
  }
  return "";
}

function buildImageProxyUrl(fileToken: string): string {
  const file = String(fileToken || "").trim();
  if (!file) return "";
  const query = new URLSearchParams({ file });
  return `/api/webui/chat/media/image?${query.toString()}`;
}

export function faceFallbackLabel(faceId: string): string {
  const id = String(faceId || "").trim();
  const map: Record<string, string> = {
    "14": "😄", "76": "👍", "66": "❤️", "63": "🌹", "182": "😎", "271": "👏",
  };
  return map[id] || "🙂";
}

export function extractMessageMediaItems(msg: ChatMessageItem): MessageMediaItem[] {
  const out: MessageMediaItem[] = [];
  const segs = Array.isArray(msg.segments) ? msg.segments : [];
  segs.forEach((seg, idx) => {
    const segType = String(seg?.type || "").toLowerCase();
    const data = asSegmentData(seg?.data);
    if (segType === "image" || segType === "mface") {
      const directUrl = normalizeSegmentMediaUrl(
        firstNonEmptyValue(data, ["url", "image_url", "src", "origin", "download_url", "file", "path"]),
        "image",
      );
      const fileToken = firstNonEmptyValue(data, ["file", "file_id", "id", "image_id", "path"]);
      const proxyUrl = buildImageProxyUrl(fileToken);
      const url = proxyUrl || directUrl;
      out.push({ key: `${idx}-${segType}`, kind: "image", url, label: segType === "mface" ? "表情包" : "图片", faceId: "", fileToken });
      return;
    }
    if (segType === "video") {
      const url = normalizeSegmentMediaUrl(firstNonEmptyValue(data, ["url", "video_url", "src", "download_url", "file", "path"]), "video");
      out.push({ key: `${idx}-${segType}`, kind: "video", url, label: "视频", faceId: "", fileToken: "" });
      return;
    }
    if (segType === "record" || segType === "audio") {
      const url = normalizeSegmentMediaUrl(firstNonEmptyValue(data, ["url", "audio_url", "src", "download_url", "file", "path"]), "audio");
      out.push({ key: `${idx}-${segType}`, kind: "audio", url, label: "语音", faceId: "", fileToken: "" });
      return;
    }
    if (segType === "face") {
      const faceId = firstNonEmptyValue(data, ["id", "face_id", "emoji_id"]);
      out.push({ key: `${idx}-${segType}`, kind: "face", url: "", label: "QQ表情", faceId, fileToken: "" });
    }
  });
  return out;
}

export function stripMediaPlaceholders(text: string): string {
  const raw = String(text || "");
  if (!raw) return "";
  return raw
    .replace(/\[(?:image|video|record|audio|mface|face|file)\]/gi, " ")
    .replace(/\[(?:图片|视频|语音|音频|表情|消息)\]/g, " ")
    .replace(/\s{2,}/g, " ")
    .trim();
}

export function resolveQQAvatar(userId: string, size = 100): string {
  const id = String(userId || "").trim();
  if (!/^\d{5,}$/.test(id)) return "";
  return `https://q1.qlogo.cn/g?b=qq&nk=${encodeURIComponent(id)}&s=${encodeURIComponent(String(size))}`;
}

export function resolveQQGroupAvatar(groupId: string, size = 100): string {
  const id = String(groupId || "").trim();
  if (!/^\d{5,}$/.test(id)) return "";
  const safeSize = [0, 40, 100, 140, 640].includes(size) ? size : 100;
  return `https://p.qlogo.cn/gh/${encodeURIComponent(id)}/${encodeURIComponent(id)}/${safeSize}`;
}

export function resolveConversationAvatar(chatType: "group" | "private", peerId: string, size = 100): string {
  if (chatType === "group") return resolveQQGroupAvatar(peerId, size);
  return resolveQQAvatar(peerId, size);
}

export function avatarInitial(label: string): string {
  const text = String(label || "").trim().replace(/\s+/g, "");
  if (!text) return "?";
  return Array.from(text)[0]?.toUpperCase() || "?";
}

export const CONTEXT_EASTER_EGGS = [
  "右键彩蛋: 喵~",
  "右键彩蛋: 好耶!",
  "右键彩蛋: 今天也要顺滑回复",
];

export const DEFAULT_CHAT_PERMISSION: ChatHistoryPermission = {
  bot_role: "",
  can_recall: false,
  can_set_essence: false,
};

export const INPUT_CLASSES = {
  label: "text-default-500 text-xs",
  input: "text-sm !bg-transparent !outline-none !ring-0 focus:!outline-none focus:!ring-0 focus-visible:!outline-none focus-visible:!ring-0 group-data-[focus=true]:!bg-transparent group-data-[has-value=true]:!bg-transparent",
  inputWrapper: "bg-content2/55 border border-default-400/35 shadow-none transition-all duration-200 data-[hover=true]:bg-content2/70 data-[focus=true]:bg-content2/80 data-[focus=true]:border-primary/55 data-[focus=true]:shadow-[0_0_0_1px_rgba(96,165,250,0.18)] data-[focus=true]:before:!bg-transparent data-[focus=true]:after:!bg-transparent before:!shadow-none after:!shadow-none",
  innerWrapper: "!bg-transparent !shadow-none group-data-[focus=true]:!bg-transparent",
  mainWrapper: "!bg-transparent",
  base: "w-full",
} as const;

export function isOneBotUnavailableError(error: unknown): boolean {
  const msg = String(error instanceof Error ? error.message : error || "").toLowerCase();
  if (!msg) return false;
  return (
    msg.includes("http 503") || msg.includes("503")
    || msg.includes("未检测到在线 onebot 实例")
    || msg.includes("nonebot 不可用") || msg.includes("运行时不可用")
  );
}

export const TRANSIENT_NETWORK_HINT = "连接暂时中断，正在自动重试...";

export function isTransientNetworkError(error: unknown): boolean {
  const msg = String(error instanceof Error ? error.message : error || "").trim().toLowerCase();
  if (!msg) return false;
  return (
    msg === "failed to fetch" || msg.includes("failed to fetch")
    || msg.includes("networkerror") || msg.includes("network request failed")
    || msg.includes("fetch failed") || msg.includes("load failed")
  );
}

export function normalizeRequestError(error: unknown, fallback: string): string {
  if (isTransientNetworkError(error)) return TRANSIENT_NETWORK_HINT;
  return error instanceof Error ? error.message : fallback;
}

export function normalizeCopyPayload(msg: ChatMessageItem): string {
  const text = String(msg.text || "").trim();
  if (text && text !== "[空消息]") return text;
  const segments = Array.isArray(msg.segments) ? msg.segments : [];
  if (!segments.length) return "";
  return segments
    .map((seg) => {
      const segType = String(seg?.type || "").toLowerCase();
      if (segType === "image") return "[图片]";
      if (segType === "video") return "[视频]";
      if (segType === "record" || segType === "audio") return "[语音]";
      if (segType === "text") return String(seg?.data?.text || "");
      return `[${segType || "消息"}]`;
    })
    .join(" ")
    .trim();
}

export async function readImageFilePayload(file: File): Promise<{ dataUrl: string; base64Body: string }> {
  const dataUrl = await new Promise<string>((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(new Error("图片读取失败"));
    reader.onload = () => resolve(String(reader.result || ""));
    reader.readAsDataURL(file);
  });
  const base64Body = dataUrl.replace(/^data:[^;]+;base64,/, "");
  if (!base64Body) {
    throw new Error("图片编码失败");
  }
  return { dataUrl, base64Body };
}
