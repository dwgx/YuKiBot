import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ChangeEvent,
  type ClipboardEvent as ReactClipboardEvent,
  type KeyboardEvent as ReactKeyboardEvent,
  type MouseEvent as ReactMouseEvent,
  type PointerEvent as ReactPointerEvent,
} from "react";
import { Button, Card, CardBody, CardHeader, Chip, Input, Spinner, Textarea } from "@heroui/react";
import { BrainCircuit, ChevronDown, ChevronUp, Copy, ImagePlus, MessageSquare, Minus, Pause, Play, Plus, Quote, RefreshCw, SendHorizontal, SmilePlus, Sparkles, Square, Star, Trash2, UserRound, X } from "lucide-react";
import { AnimatePresence, motion, useDragControls } from "framer-motion";
import { api, ChatAgentStateItem, ChatConversationItem, ChatHistoryPermission, ChatMessageItem } from "../api/client";
import "../styles/stapxs-chat.css";

function fmtTs(ts: number): string {
  if (!ts || Number.isNaN(ts)) return "-";
  const d = new Date(ts * 1000);
  if (Number.isNaN(d.getTime())) return "-";
  return d.toLocaleString();
}

function clip(text: string, max = 64): string {
  const raw = String(text || "").trim();
  if (!raw) return "";
  return raw.length > max ? `${raw.slice(0, max)}...` : raw;
}

function hasImageSegment(msg: ChatMessageItem | null): boolean {
  if (!msg) return false;
  if (Array.isArray(msg.segments) && msg.segments.some((seg) => String(seg?.type || "").toLowerCase() === "image")) {
    return true;
  }
  return String(msg.text || "").includes("[image]");
}

function resolveQQAvatar(userId: string, size = 100): string {
  const id = String(userId || "").trim();
  if (!/^\d{5,}$/.test(id)) return "";
  return `https://q1.qlogo.cn/g?b=qq&nk=${encodeURIComponent(id)}&s=${encodeURIComponent(String(size))}`;
}

function avatarInitial(label: string): string {
  const text = String(label || "").trim().replace(/\s+/g, "");
  if (!text) return "?";
  return Array.from(text)[0]?.toUpperCase() || "?";
}

const CONTEXT_EASTER_EGGS = [
  "右键彩蛋: 喵~",
  "右键彩蛋: 好耶!",
  "右键彩蛋: 今天也要顺滑回复",
];

const DEFAULT_CHAT_PERMISSION: ChatHistoryPermission = {
  bot_role: "",
  can_recall: false,
  can_set_essence: false,
};

const INPUT_CLASSES = {
  label: "text-default-500 text-xs",
  input: "text-sm !bg-transparent !outline-none !ring-0 focus:!outline-none focus:!ring-0 focus-visible:!outline-none focus-visible:!ring-0 group-data-[focus=true]:!bg-transparent group-data-[has-value=true]:!bg-transparent",
  inputWrapper: "bg-content2/55 border border-default-400/35 shadow-none transition-all duration-200 data-[hover=true]:bg-content2/70 data-[focus=true]:bg-content2/80 data-[focus=true]:border-primary/55 data-[focus=true]:shadow-[0_0_0_1px_rgba(96,165,250,0.18)] data-[focus=true]:before:!bg-transparent data-[focus=true]:after:!bg-transparent before:!shadow-none after:!shadow-none",
  innerWrapper: "!bg-transparent !shadow-none group-data-[focus=true]:!bg-transparent",
  mainWrapper: "!bg-transparent",
  base: "w-full",
} as const;

const THINKING_ISLAND_STORAGE_KEY = "yukiko-thinking-island-offset-v1";
const THINKING_ISLAND_SIZE_STORAGE_KEY = "yukiko-thinking-island-size-v1";
const THINKING_ISLAND_WIDTH_STORAGE_KEY = "yukiko-thinking-island-width-v2";

type ThinkingIslandOffset = {
  x: number;
  y: number;
};

type PendingThinkingLog = {
  raw: string;
  at: number;
};

type ThinkingIslandSize = "sm" | "md" | "lg";
type ThinkingStage = "idle" | "routing" | "planning" | "executing" | "replying" | "done" | "cancelled" | "error";

const THINKING_ISLAND_DEFAULT_WIDTH: Record<ThinkingIslandSize, number> = {
  sm: 480,
  md: 640,
  lg: 820,
};
const THINKING_ISLAND_MIN_WIDTH = 360;
const THINKING_ISLAND_MAX_WIDTH = 920;
const THINKING_STAGE_STEPS: Array<{ key: "routing" | "planning" | "executing" | "replying"; label: string }> = [
  { key: "routing", label: "理解" },
  { key: "planning", label: "规划" },
  { key: "executing", label: "执行" },
  { key: "replying", label: "回复" },
];

const THINKING_ISLAND_PREVIEW_HEIGHT_CLASS: Record<ThinkingIslandSize, string> = {
  sm: "max-h-28",
  md: "max-h-36",
  lg: "max-h-44",
};

const THINKING_ISLAND_MIN_HEIGHT = 100;
const THINKING_ISLAND_MAX_HEIGHT = 500;
const THINKING_ISLAND_DEFAULT_HEIGHT = 160;
const THINKING_ISLAND_HEIGHT_STORAGE_KEY = "yukiko-thinking-island-height-v1";

function stateBelongsToConversation(stateConversationId: string, selectedConversationId: string): boolean {
  const stateId = String(stateConversationId || "").trim();
  const selectedId = String(selectedConversationId || "").trim();
  if (!stateId || !selectedId) return false;
  if (stateId === selectedId) return true;
  return stateId.startsWith(`${selectedId}:user:`);
}

function toTimestamp(value: string | undefined): number {
  if (!value) return 0;
  const parsed = Date.parse(String(value));
  return Number.isFinite(parsed) ? parsed : 0;
}

function mergeAgentStates(states: ChatAgentStateItem[], selectedConversationId: string): ChatAgentStateItem | null {
  const matched = states.filter((item) =>
    stateBelongsToConversation(String(item?.conversation_id || ""), selectedConversationId),
  );
  if (matched.length === 0) return null;
  if (matched.length === 1) return matched[0] ?? null;

  const sorted = [...matched].sort((a, b) => {
    const delta = toTimestamp(String(b?.last_update || "")) - toTimestamp(String(a?.last_update || ""));
    if (delta !== 0) return delta;
    return Number(String(b?.latest_trace_id || "").length) - Number(String(a?.latest_trace_id || "").length);
  });
  const latest = sorted[0] ?? matched[0];
  return {
    conversation_id: selectedConversationId,
    pending_count: matched.reduce((sum, item) => sum + Number(item?.pending_count || 0), 0),
    running_count: matched.reduce((sum, item) => sum + Number(item?.running_count || 0), 0),
    queued_count: matched.reduce((sum, item) => sum + Number(item?.queued_count || 0), 0),
    interruptible_count: matched.reduce((sum, item) => sum + Number(item?.interruptible_count || 0), 0),
    latest_trace_id: String(latest?.latest_trace_id || ""),
    last_trace_id: String(latest?.last_trace_id || latest?.latest_trace_id || ""),
    last_user_id: String(latest?.last_user_id || ""),
    last_text_preview: String(latest?.last_text_preview || ""),
    last_update: String(latest?.last_update || ""),
  };
}

function clampThinkingIslandWidth(width: number): number {
  const safe = Number.isFinite(width) ? width : THINKING_ISLAND_DEFAULT_WIDTH.md;
  if (typeof window === "undefined") {
    return Math.max(THINKING_ISLAND_MIN_WIDTH, Math.min(THINKING_ISLAND_MAX_WIDTH, safe));
  }
  return Math.max(
    THINKING_ISLAND_MIN_WIDTH,
    Math.min(THINKING_ISLAND_MAX_WIDTH, window.innerWidth - 24, safe),
  );
}

function widthToThinkingIslandSize(width: number): ThinkingIslandSize {
  if (width >= 760) return "lg";
  if (width >= 580) return "md";
  return "sm";
}

function getThinkingIslandRenderWidth(width: number): number {
  return clampThinkingIslandWidth(width);
}

function getThinkingIslandWidthStyle(width: number): string {
  return `${clampThinkingIslandWidth(width)}px`;
}

function clampThinkingIslandOffset(
  offset: ThinkingIslandOffset,
  width: number = THINKING_ISLAND_DEFAULT_WIDTH.md,
): ThinkingIslandOffset {
  if (typeof window === "undefined") return offset;
  const islandWidth = getThinkingIslandRenderWidth(width);
  const maxX = Math.max(0, Math.floor((window.innerWidth - islandWidth) / 2) - 8);
  const maxY = Math.max(0, Math.floor(window.innerHeight * 0.42));
  return {
    x: Math.max(-maxX, Math.min(maxX, Number.isFinite(offset.x) ? offset.x : 0)),
    y: Math.max(-8, Math.min(maxY, Number.isFinite(offset.y) ? offset.y : 0)),
  };
}

function loadThinkingIslandOffset(width: number = THINKING_ISLAND_DEFAULT_WIDTH.md): ThinkingIslandOffset {
  if (typeof window === "undefined") return { x: 0, y: 0 };
  try {
    const raw = window.localStorage.getItem(THINKING_ISLAND_STORAGE_KEY);
    if (!raw) return { x: 0, y: 0 };
    const data = JSON.parse(raw) as Partial<ThinkingIslandOffset>;
    return clampThinkingIslandOffset({
      x: Number(data?.x ?? 0),
      y: Number(data?.y ?? 0),
    }, width);
  } catch {
    return { x: 0, y: 0 };
  }
}

function loadThinkingIslandWidth(): number {
  if (typeof window === "undefined") return THINKING_ISLAND_DEFAULT_WIDTH.md;
  const rawWidth = Number(window.localStorage.getItem(THINKING_ISLAND_WIDTH_STORAGE_KEY) || 0);
  if (Number.isFinite(rawWidth) && rawWidth > 0) {
    return clampThinkingIslandWidth(rawWidth);
  }
  const raw = String(window.localStorage.getItem(THINKING_ISLAND_SIZE_STORAGE_KEY) || "").trim().toLowerCase();
  if (raw === "sm" || raw === "md" || raw === "lg") {
    return THINKING_ISLAND_DEFAULT_WIDTH[raw];
  }
  return THINKING_ISLAND_DEFAULT_WIDTH.md;
}

function parseThinkingLine(rawLine: string): string {
  const line = String(rawLine || "").trim();
  if (!line) return "";
  if (line.includes("queue_submit")) return "消息已进入队列";
  if (line.includes("qq_recv")) return "收到新消息，正在开始处理";
  if (line.includes("router_llm")) return "AI 正在理解问题并做路由判断";
  if (line.includes("router_decision")) {
    const action = line.match(/\|\s*action=([a-zA-Z0-9_.-]+)/)?.[1] || "";
    return action ? `已决策动作: ${action}` : "路由决策完成";
  }
  if (line.includes("effective_threshold_trace")) return "通过触发阈值，准备执行";
  if (line.includes("tool_dispatch")) {
    const action = line.match(/\|\s*action=([a-zA-Z0-9_.-]+)/)?.[1] || "";
    const tool = line.match(/\|\s*tool=([a-zA-Z0-9_.-]+)/)?.[1] || "";
    const name = action || tool;
    return name ? `开始执行: ${name}` : "开始执行工具";
  }
  if (line.includes("tool_result")) {
    const tool = line.match(/\|\s*tool=([a-zA-Z0-9_.-]+)/)?.[1] || "";
    const ok = line.match(/\|\s*ok=(true|false)/)?.[1] || "";
    const err = line.match(/\|\s*error=([^|]+)/)?.[1] || "";
    if (ok === "false") return `执行失败${tool ? `: ${tool}` : ""}${err ? ` (${clip(err, 72)})` : ""}`;
    if (tool) return `执行完成: ${tool}`;
    return "执行完成";
  }
  if (line.includes("send_final")) return "正在发送回复";
  if (line.includes("queue_final")) {
    const status = line.match(/\|\s*status=([a-zA-Z0-9_.-]+)/)?.[1] || "";
    if (status === "finished") return "任务已完成";
    if (status === "cancelled") return "任务已取消";
    return status ? `队列状态: ${status}` : "队列处理完成";
  }
  if (line.includes("queue_cancelled")) return "队列已取消当前任务";
  if (line.includes("agent_done")) return "任务完成";
  if (line.includes("agent_timeout_budget")) return "正在分配思考预算";
  if (line.includes("cancelled_by_webui_interrupt")) return "已收到中断请求";
  if (line.includes("agent_direct_reply")) return "准备直接回复";
  if (line.includes("agent_unparseable_json")) return "正在修正模型输出";
  if (line.includes("agent_force_tool_first")) return "切到工具优先路径";
  if (line.includes("agent_final_answer")) return "正在整理最终回复";
  if (line.includes("agent_llm_timeout")) return "模型响应超时";
  if (line.includes("agent_total_timeout")) return "整体处理超时";

  const callMatch = line.match(/agent_tool_call.*\|\s*tool=([a-zA-Z0-9_.-]+)/);
  if (callMatch) {
    const tool = String(callMatch[1] || "");
    if (tool === "think") {
      const thoughtMatch = line.match(/"thought"\s*:\s*"(.+?)"/);
      if (thoughtMatch) {
        const thought = thoughtMatch[1]
          .replace(/\\n/g, " ")
          .replace(/\\"/g, "\"")
          .replace(/\\\\/g, "\\");
        return `思考: ${clip(thought, 140)}`;
      }
      return "思考中...";
    }
    return `调用工具: ${tool}`;
  }

  const resultMatch = line.match(/agent_tool_result.*\|\s*tool=([a-zA-Z0-9_.-]+)/);
  if (resultMatch) {
    return `工具完成: ${String(resultMatch[1] || "")}`;
  }

  const fallbackMatch = line.match(/agent_tool_fallback_try.*from=([a-zA-Z0-9_.-]+).*to=([a-zA-Z0-9_.-]+)/);
  if (fallbackMatch) {
    return `工具兜底: ${fallbackMatch[1]} -> ${fallbackMatch[2]}`;
  }

  if (line.includes("agent_")) return "正在规划下一步...";

  return "";
}

function compactThinkingRawLine(rawLine: string): string {
  const line = String(rawLine || "").trim();
  if (!line) return "";
  if (!/(agent_|queue_|router_|tool_dispatch|tool_call|tool_result|send_final|effective_threshold_trace|interrupt|trace=|step=)/.test(line)) return "";
  const compact = line
    .replace(/^\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}\s*\|\s*[A-Z]+\s*\|\s*[^|]*\|\s*/, "")
    .replace(/^\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}\s*\[[A-Z]+\]\s*[^|]*\|\s*/, "")
    .replace(/\s+/g, " ")
    .trim();
  if (!compact) return "";
  return `日志: ${clip(compact, 150)}`;
}

function extractConversationHint(rawLine: string): string {
  const line = String(rawLine || "");
  const fromConversation = line.match(/\|\s*conversation=([^|]+)/)?.[1] || "";
  if (fromConversation.trim()) return fromConversation.trim();
  const fromConversationCn = line.match(/\|\s*会话=([^|]+)/)?.[1] || "";
  if (fromConversationCn.trim()) return fromConversationCn.trim();
  return "";
}

function decorateThinkingLine(displayLine: string, rawLine: string, selectedConversationId: string): string {
  const base = String(displayLine || "").trim();
  if (!base) return "";
  const conversationHint = extractConversationHint(rawLine);
  if (!conversationHint) return base;
  if (stateBelongsToConversation(conversationHint, selectedConversationId)) return base;
  return `[${conversationHint}] ${base}`;
}

function thinkingStatusLabel(active: boolean): string {
  return active ? "处理中" : "已完成";
}

function thinkingStreamLabel(state: "connecting" | "open" | "closed"): string {
  if (state === "open") return "任务流已连接";
  if (state === "connecting") return "连接任务流中";
  return "任务流已断开";
}

function inferThinkingStage(line: string, active: boolean): ThinkingStage {
  const normalized = String(line || "").toLowerCase();
  if (!normalized) return active ? "planning" : "idle";
  if (/取消|中断|interrupt|cancel/.test(normalized)) return "cancelled";
  if (/失败|超时|error|failed|timeout/.test(normalized)) return "error";
  if (/任务已完成|处理完成|agent_done|queue_final/.test(normalized)) return "done";
  if (/发送回复|最终回复|send_final|final/.test(normalized)) return "replying";
  if (/执行|工具|dispatch|tool/.test(normalized)) return "executing";
  if (/规划|思考|计划|agent_/.test(normalized)) return "planning";
  if (/路由|理解问题|router|qq_recv/.test(normalized)) return "routing";
  return active ? "planning" : "idle";
}

function thinkingStageLabel(stage: ThinkingStage): string {
  if (stage === "routing") return "理解问题";
  if (stage === "planning") return "规划策略";
  if (stage === "executing") return "执行动作";
  if (stage === "replying") return "整理回复";
  if (stage === "done") return "任务完成";
  if (stage === "cancelled") return "已中断";
  if (stage === "error") return "异常兜底";
  return "等待任务";
}

function thinkingStageColor(stage: ThinkingStage): "default" | "primary" | "success" | "warning" | "danger" {
  if (stage === "done") return "success";
  if (stage === "cancelled") return "warning";
  if (stage === "error") return "danger";
  if (stage === "idle") return "default";
  return "primary";
}

function thinkingStageProgress(stage: ThinkingStage): number {
  if (stage === "idle") return 6;
  if (stage === "routing") return 22;
  if (stage === "planning") return 46;
  if (stage === "executing") return 72;
  if (stage === "replying") return 88;
  if (stage === "done") return 100;
  if (stage === "cancelled") return 100;
  return 100;
}

function isOneBotUnavailableError(error: unknown): boolean {
  const msg = String(error instanceof Error ? error.message : error || "").toLowerCase();
  if (!msg) return false;
  return (
    msg.includes("http 503")
    || msg.includes("503")
    || msg.includes("未检测到在线 onebot 实例")
    || msg.includes("nonebot 不可用")
    || msg.includes("运行时不可用")
  );
}

const TRANSIENT_NETWORK_HINT = "连接暂时中断，正在自动重试...";

function isTransientNetworkError(error: unknown): boolean {
  const msg = String(error instanceof Error ? error.message : error || "").trim().toLowerCase();
  if (!msg) return false;
  return (
    msg === "failed to fetch"
    || msg.includes("failed to fetch")
    || msg.includes("networkerror")
    || msg.includes("network request failed")
    || msg.includes("fetch failed")
    || msg.includes("load failed")
  );
}

function normalizeRequestError(error: unknown, fallback: string): string {
  if (isTransientNetworkError(error)) return TRANSIENT_NETWORK_HINT;
  return error instanceof Error ? error.message : fallback;
}

function normalizeCopyPayload(msg: ChatMessageItem): string {
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

async function readImageFilePayload(file: File): Promise<{ dataUrl: string; base64Body: string }> {
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

export default function ChatPage() {
  const [loadingConvs, setLoadingConvs] = useState(false);
  const [loadingHistory, setLoadingHistory] = useState(false);
  const [loadingState, setLoadingState] = useState(false);
  const [sending, setSending] = useState(false);
  const [error, setError] = useState("");
  const [botRuntimeStatus, setBotRuntimeStatus] = useState<"online" | "offline">("online");
  const [pauseAutoRefresh, setPauseAutoRefresh] = useState(false);
  const [conversationKeyword, setConversationKeyword] = useState("");
  const [showScrollToBottom, setShowScrollToBottom] = useState(false);

  const [conversations, setConversations] = useState<ChatConversationItem[]>([]);
  const [selectedId, setSelectedId] = useState("");
  const [messages, setMessages] = useState<ChatMessageItem[]>([]);
  const [agentStates, setAgentStates] = useState<ChatAgentStateItem[]>([]);

  const [text, setText] = useState("");
  const [imageUrl, setImageUrl] = useState("");
  const [imageBase64, setImageBase64] = useState("");
  const [imageFileName, setImageFileName] = useState("");
  const [imagePreviewUrl, setImagePreviewUrl] = useState("");
  const [replyToMessage, setReplyToMessage] = useState<ChatMessageItem | null>(null);
  const [permission, setPermission] = useState<ChatHistoryPermission>(DEFAULT_CHAT_PERMISSION);
  const [contextEgg, setContextEgg] = useState("");
  const [thinkingLines, setThinkingLines] = useState<string[]>([]);
  const [thinkingDraft, setThinkingDraft] = useState("");
  const [retargeting, setRetargeting] = useState(false);
  const [thinkingPanelOpen, setThinkingPanelOpen] = useState(true);
  const [thinkingIslandExpanded, setThinkingIslandExpanded] = useState(false);
  const [thinkingIslandWidth, setThinkingIslandWidth] = useState<number>(() => loadThinkingIslandWidth());
  const [thinkingIslandOffset, setThinkingIslandOffset] = useState<ThinkingIslandOffset>(() => loadThinkingIslandOffset(loadThinkingIslandWidth()));
  const [thinkingStreamState, setThinkingStreamState] = useState<"connecting" | "open" | "closed">("connecting");
  const [thinkingStreamPacketCount, setThinkingStreamPacketCount] = useState(0);
  const [thinkingStreamLastPacketAt, setThinkingStreamLastPacketAt] = useState(0);
  const [thinkingWarmConversationId, setThinkingWarmConversationId] = useState("");
  const [thinkingWarmUntil, setThinkingWarmUntil] = useState(0);
  const [clockMs, setClockMs] = useState(() => Date.now());
  const [contextMenu, setContextMenu] = useState<{
    open: boolean;
    x: number;
    y: number;
    message: ChatMessageItem | null;
  }>({ open: false, x: 0, y: 0, message: null });
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const messagesScrollRef = useRef<HTMLDivElement | null>(null);
  const thinkingScrollRef = useRef<HTMLDivElement | null>(null);
  const thinkingWsRef = useRef<WebSocket | null>(null);
  const thinkingReconnectTimerRef = useRef<number | null>(null);
  const thinkingActiveRef = useRef(false);
  const loadConversationsInFlightRef = useRef(false);
  const loadAgentStateInFlightRef = useRef(false);
  const historyRequestSeqRef = useRef(0);
  const historyCacheRef = useRef<Record<string, { items: ChatMessageItem[]; permission: ChatHistoryPermission }>>({});
  const thinkingIslandDragOriginRef = useRef<ThinkingIslandOffset>({ x: 0, y: 0 });
  const thinkingIslandWidthRef = useRef(thinkingIslandWidth);
  const thinkingIslandResizeOriginRef = useRef<{ width: number; startX: number; height: number; startY: number }>({ width: thinkingIslandWidth, startX: 0, height: THINKING_ISLAND_DEFAULT_HEIGHT, startY: 0 });
  const [thinkingIslandHeight, setThinkingIslandHeight] = useState(() => {
    if (typeof window === "undefined") return THINKING_ISLAND_DEFAULT_HEIGHT;
    const raw = Number(window.localStorage.getItem(THINKING_ISLAND_HEIGHT_STORAGE_KEY) || 0);
    return (Number.isFinite(raw) && raw > 0) ? Math.max(THINKING_ISLAND_MIN_HEIGHT, Math.min(THINKING_ISLAND_MAX_HEIGHT, raw)) : THINKING_ISLAND_DEFAULT_HEIGHT;
  });
  const thinkingIslandHeightRef = useRef(thinkingIslandHeight);
  const pendingThinkingLogsRef = useRef<PendingThinkingLog[]>([]);
  const autoStickToBottomRef = useRef(true);
  const traceRef = useRef("");
  const traceCandidatesRef = useRef<string[]>([]);
  const conversationRef = useRef("");
  const transientErrorTimerRef = useRef<number | null>(null);
  const thinkingDragControls = useDragControls();
  const thinkingIslandSize = useMemo<ThinkingIslandSize>(
    () => widthToThinkingIslandSize(thinkingIslandWidth),
    [thinkingIslandWidth],
  );
  const scrollMessagesToBottom = useCallback(() => {
    const el = messagesScrollRef.current;
    if (!el) return;
    el.scrollTop = el.scrollHeight;
  }, []);

  const selected = useMemo(
    () => conversations.find((item) => item.conversation_id === selectedId) ?? null,
    [conversations, selectedId],
  );
  const selectedStateMatches = useMemo(
    () =>
      agentStates.filter((item) =>
        stateBelongsToConversation(String(item?.conversation_id || ""), selectedId),
      ),
    [agentStates, selectedId],
  );
  const selectedState = useMemo(
    () => mergeAgentStates(selectedStateMatches, selectedId),
    [selectedStateMatches, selectedId],
  );
  const selectedTraceCandidates = useMemo(() => {
    const traces = new Set<string>();
    const addTrace = (value: unknown) => {
      const normalized = String(value ?? "").trim();
      if (normalized) traces.add(normalized);
    };
    for (const row of selectedStateMatches) {
      addTrace(row?.last_trace_id);
      addTrace(row?.latest_trace_id);
    }
    addTrace(selectedState?.last_trace_id);
    addTrace(selectedState?.latest_trace_id);
    return Array.from(traces).slice(0, 8);
  }, [selectedState, selectedStateMatches]);
  const filteredConversations = useMemo(() => {
    const q = conversationKeyword.trim().toLowerCase();
    if (!q) return conversations;
    return conversations.filter((item) => {
      const haystack = [
        item.peer_name,
        item.peer_id,
        item.last_message,
        item.chat_type,
      ]
        .map((part) => String(part || "").toLowerCase())
        .join(" ");
      return haystack.includes(q);
    });
  }, [conversationKeyword, conversations]);
  const isThinking = Boolean(selectedState && ((selectedState.running_count ?? 0) > 0 || (selectedState.pending_count ?? 0) > 0));
  const optimisticThinking = Boolean(
    selected
    && thinkingWarmConversationId === selected.conversation_id
    && thinkingWarmUntil > clockMs,
  );
  const thinkingActive = Boolean(isThinking || optimisticThinking);
  const thinkingIslandVisible = Boolean(selected && (thinkingPanelOpen || thinkingActive || thinkingLines.length > 0));
  const latestThinkingLine = useMemo(
    () => thinkingLines[thinkingLines.length - 1] || (thinkingActive ? "正在建立计划..." : "刚刚处理完成"),
    [thinkingActive, thinkingLines],
  );
  const thinkingStage = useMemo(
    () => inferThinkingStage(latestThinkingLine, thinkingActive),
    [latestThinkingLine, thinkingActive],
  );
  const thinkingStageLabelText = useMemo(() => thinkingStageLabel(thinkingStage), [thinkingStage]);
  const thinkingStageColorValue = useMemo(() => thinkingStageColor(thinkingStage), [thinkingStage]);
  const thinkingProgressValue = useMemo(() => thinkingStageProgress(thinkingStage), [thinkingStage]);
  const thinkingPreviewLines = useMemo(() => thinkingLines.slice(-6), [thinkingLines]);
  const lastStreamPacketLabel = useMemo(
    () => (thinkingStreamLastPacketAt > 0 ? new Date(thinkingStreamLastPacketAt).toLocaleTimeString() : "-"),
    [thinkingStreamLastPacketAt],
  );
  const canRecallInMenu = Boolean(selected?.chat_type === "group" && permission.can_recall);
  const canSetEssenceInMenu = Boolean(selected?.chat_type === "group" && permission.can_set_essence);
  const contextMessageIsEssence = Boolean(contextMenu.message?.is_essence);
  const refreshPausedHint = pauseAutoRefresh ? "自动刷新已暂停" : "自动刷新中";
  const closeContextMenu = useCallback(() => {
    setContextMenu((prev) => (prev.open ? { open: false, x: 0, y: 0, message: null } : prev));
  }, []);
  const clearTransientErrorTimer = useCallback(() => {
    if (transientErrorTimerRef.current != null) {
      window.clearTimeout(transientErrorTimerRef.current);
      transientErrorTimerRef.current = null;
    }
  }, []);
  const showTransientNetworkHint = useCallback(() => {
    clearTransientErrorTimer();
    setError(TRANSIENT_NETWORK_HINT);
    transientErrorTimerRef.current = window.setTimeout(() => {
      transientErrorTimerRef.current = null;
      setError((prev) => (prev === TRANSIENT_NETWORK_HINT ? "" : prev));
    }, 3200);
  }, [clearTransientErrorTimer]);
  const touchThinkingPresence = useCallback((conversationId: string, ttlMs = 12000) => {
    if (!conversationId) return;
    setClockMs(Date.now());
    setThinkingWarmConversationId(conversationId);
    setThinkingWarmUntil(Date.now() + ttlMs);
    setThinkingPanelOpen(true);
  }, []);
  const pushThinkingLine = useCallback((line: string) => {
    const content = String(line || "").trim();
    if (!content) return;
    setThinkingLines((prev) => {
      if (prev[prev.length - 1] === content) return prev;
      return [...prev.slice(-80), content];
    });
  }, []);
  const clearThinkingReconnectTimer = useCallback(() => {
    if (thinkingReconnectTimerRef.current != null) {
      window.clearTimeout(thinkingReconnectTimerRef.current);
      thinkingReconnectTimerRef.current = null;
    }
  }, []);
  useEffect(() => {
    thinkingIslandWidthRef.current = thinkingIslandWidth;
  }, [thinkingIslandWidth]);
  useEffect(() => () => {
    clearTransientErrorTimer();
  }, [clearTransientErrorTimer]);
  useEffect(() => {
    thinkingIslandHeightRef.current = thinkingIslandHeight;
  }, [thinkingIslandHeight]);
  const beginThinkingIslandDrag = useCallback((evt: ReactPointerEvent<HTMLDivElement>) => {
    if (evt.button !== 0) return;
    evt.preventDefault();
    evt.stopPropagation();
    thinkingIslandDragOriginRef.current = thinkingIslandOffset;
    thinkingDragControls.start(evt, { snapToCursor: false });
  }, [thinkingDragControls, thinkingIslandOffset]);
  const growThinkingIsland = useCallback(() => {
    setThinkingIslandWidth((prev) => clampThinkingIslandWidth(prev + 120));
  }, []);
  const shrinkThinkingIsland = useCallback(() => {
    setThinkingIslandWidth((prev) => clampThinkingIslandWidth(prev - 120));
  }, []);
  const beginThinkingIslandResize = useCallback((evt: ReactPointerEvent<HTMLDivElement>) => {
    if (evt.button !== 0) return;
    evt.preventDefault();
    evt.stopPropagation();
    const isBottom = evt.currentTarget.dataset.resizeDir === "bottom";
    const isLeft = evt.currentTarget.dataset.resizeDir === "left";
    thinkingIslandResizeOriginRef.current = {
      width: thinkingIslandWidthRef.current,
      startX: evt.clientX,
      height: thinkingIslandHeightRef.current,
      startY: evt.clientY,
    };
    const handleMove = (moveEvt: PointerEvent) => {
      if (isBottom) {
        const nextHeight = Math.max(THINKING_ISLAND_MIN_HEIGHT, Math.min(THINKING_ISLAND_MAX_HEIGHT,
          thinkingIslandResizeOriginRef.current.height + (moveEvt.clientY - thinkingIslandResizeOriginRef.current.startY),
        ));
        setThinkingIslandHeight(nextHeight);
      } else {
        const delta = moveEvt.clientX - thinkingIslandResizeOriginRef.current.startX;
        const nextWidth = clampThinkingIslandWidth(
          thinkingIslandResizeOriginRef.current.width + (isLeft ? -delta : delta),
        );
        setThinkingIslandWidth(nextWidth);
      }
    };
    const handleUp = () => {
      window.removeEventListener("pointermove", handleMove);
      window.removeEventListener("pointerup", handleUp);
      setThinkingIslandOffset((prev) => clampThinkingIslandOffset(prev, thinkingIslandWidthRef.current));
    };
    window.addEventListener("pointermove", handleMove);
    window.addEventListener("pointerup", handleUp, { once: true });
  }, []);

  const loadConversations = useCallback(async (opts?: { silent?: boolean }) => {
    if (loadConversationsInFlightRef.current) return;
    loadConversationsInFlightRef.current = true;
    if (!opts?.silent) setLoadingConvs(true);
    try {
      const res = await api.getChatConversations({ limit: 200 });
      const items = Array.isArray(res.items) ? res.items : [];
      setConversations(items);
      if (items.length > 0 && !items.some((item) => item.conversation_id === selectedId)) {
        setSelectedId(items[0].conversation_id);
      }
      if (items.length === 0) {
        setSelectedId("");
      }
      setBotRuntimeStatus("online");
      setError("");
    } catch (e: unknown) {
      if (isOneBotUnavailableError(e)) {
        setBotRuntimeStatus("offline");
      }
      if (opts?.silent && isTransientNetworkError(e)) {
        showTransientNetworkHint();
        return;
      }
      setError(normalizeRequestError(e, "读取会话失败"));
    } finally {
      loadConversationsInFlightRef.current = false;
      if (!opts?.silent) setLoadingConvs(false);
    }
  }, [selectedId, showTransientNetworkHint]);

  const loadHistory = useCallback(async (opts?: { silent?: boolean }) => {
    const target = selected;
    if (!target) {
      setMessages([]);
      setPermission(DEFAULT_CHAT_PERMISSION);
      return;
    }
    const reqSeq = ++historyRequestSeqRef.current;
    if (!opts?.silent) setLoadingHistory(true);
    try {
      const res = await api.getChatHistory({
        chatType: target.chat_type,
        peerId: target.peer_id,
        limit: 60,
      });
      if (reqSeq !== historyRequestSeqRef.current) return;
      const items = Array.isArray(res.items) ? res.items : [];
      const nextPermission = res.permission ?? DEFAULT_CHAT_PERMISSION;
      setMessages(items);
      setPermission(nextPermission);
      historyCacheRef.current[target.conversation_id] = {
        items,
        permission: nextPermission,
      };
      setBotRuntimeStatus("online");
      setError("");
    } catch (e: unknown) {
      if (reqSeq !== historyRequestSeqRef.current) return;
      if (isOneBotUnavailableError(e)) {
        setBotRuntimeStatus("offline");
      }
      if (opts?.silent && isTransientNetworkError(e)) {
        showTransientNetworkHint();
        return;
      }
      setError(normalizeRequestError(e, "读取聊天记录失败"));
    } finally {
      if (reqSeq === historyRequestSeqRef.current && !opts?.silent) {
        setLoadingHistory(false);
      }
    }
  }, [selected, showTransientNetworkHint]);

  const loadAgentState = useCallback(async (opts?: { silent?: boolean }) => {
    if (loadAgentStateInFlightRef.current) return;
    loadAgentStateInFlightRef.current = true;
    if (!opts?.silent) setLoadingState(true);
    try {
      const res = await api.getChatAgentState({ limit: 200 });
      setAgentStates(Array.isArray(res.items) ? res.items : []);
      setError("");
    } catch (e: unknown) {
      if (opts?.silent && isTransientNetworkError(e)) {
        showTransientNetworkHint();
        return;
      }
      setError(normalizeRequestError(e, "读取运行状态失败"));
    } finally {
      loadAgentStateInFlightRef.current = false;
      if (!opts?.silent) setLoadingState(false);
    }
  }, [showTransientNetworkHint]);

  useEffect(() => {
    loadConversations();
    loadAgentState();
  }, [loadConversations, loadAgentState]);

  useEffect(() => {
    if (!selected) {
      setMessages([]);
      setPermission(DEFAULT_CHAT_PERMISSION);
      return;
    }
    const cached = historyCacheRef.current[selected.conversation_id];
    if (cached) {
      setMessages(cached.items);
      setPermission(cached.permission);
    } else {
      setMessages([]);
      setPermission(DEFAULT_CHAT_PERMISSION);
    }
    void loadHistory();
  }, [loadHistory, selected]);

  useEffect(() => {
    if (pauseAutoRefresh) return undefined;
    const intervalMs = thinkingActive || optimisticThinking ? 8000 : 16000;
    const timer = window.setInterval(() => {
      void loadConversations({ silent: true });
    }, intervalMs);
    return () => window.clearInterval(timer);
  }, [loadConversations, optimisticThinking, pauseAutoRefresh, thinkingActive]);

  useEffect(() => {
    if (pauseAutoRefresh) return undefined;
    if (!selectedId) return undefined;
    const intervalMs = thinkingActive || optimisticThinking ? 3200 : 15000;
    const timer = window.setInterval(() => {
      void loadHistory({ silent: true });
    }, intervalMs);
    return () => window.clearInterval(timer);
  }, [loadHistory, optimisticThinking, pauseAutoRefresh, selectedId, thinkingActive]);

  useEffect(() => {
    if (pauseAutoRefresh) return undefined;
    const intervalMs = selectedId && (thinkingActive || optimisticThinking) ? 1200 : 3500;
    const timer = window.setInterval(() => {
      void loadAgentState({ silent: true });
    }, intervalMs);
    return () => window.clearInterval(timer);
  }, [loadAgentState, optimisticThinking, pauseAutoRefresh, selectedId, thinkingActive]);

  useEffect(() => {
    setReplyToMessage(null);
    closeContextMenu();
    autoStickToBottomRef.current = true;
    setShowScrollToBottom(false);
  }, [selectedId, closeContextMenu]);

  useEffect(() => {
    scrollMessagesToBottom();
  }, [selectedId, scrollMessagesToBottom]);

  useEffect(() => {
    if (loadingHistory) return;
    if (!autoStickToBottomRef.current) return;
    scrollMessagesToBottom();
  }, [messages, loadingHistory, scrollMessagesToBottom]);

  useEffect(() => {
    if (!contextMenu.open) return undefined;
    const onClickOutside = (evt: MouseEvent) => {
      const target = evt.target as HTMLElement | null;
      if (target?.closest("[data-chat-context-menu='1']")) return;
      closeContextMenu();
    };
    const onEsc = (evt: KeyboardEvent) => {
      if (evt.key === "Escape") closeContextMenu();
    };
    window.addEventListener("click", onClickOutside, true);
    window.addEventListener("contextmenu", onClickOutside, true);
    window.addEventListener("scroll", closeContextMenu, true);
    window.addEventListener("resize", closeContextMenu);
    window.addEventListener("keydown", onEsc);
    return () => {
      window.removeEventListener("click", onClickOutside, true);
      window.removeEventListener("contextmenu", onClickOutside, true);
      window.removeEventListener("scroll", closeContextMenu, true);
      window.removeEventListener("resize", closeContextMenu);
      window.removeEventListener("keydown", onEsc);
    };
  }, [contextMenu.open, closeContextMenu]);

  useEffect(() => {
    traceCandidatesRef.current = selectedTraceCandidates;
    traceRef.current = String(selectedTraceCandidates[0] || selectedState?.last_trace_id || selectedState?.latest_trace_id || "");
    conversationRef.current = String(selected?.conversation_id || "");
  }, [selected, selectedState, selectedTraceCandidates]);

  useEffect(() => {
    thinkingActiveRef.current = thinkingActive;
  }, [thinkingActive]);

  useEffect(() => {
    if (typeof window === "undefined") return;
    window.localStorage.setItem(THINKING_ISLAND_STORAGE_KEY, JSON.stringify(thinkingIslandOffset));
  }, [thinkingIslandOffset]);

  useEffect(() => {
    if (typeof window === "undefined") return;
    window.localStorage.setItem(THINKING_ISLAND_WIDTH_STORAGE_KEY, String(Math.round(thinkingIslandWidth)));
    window.localStorage.setItem(THINKING_ISLAND_SIZE_STORAGE_KEY, thinkingIslandSize);
  }, [thinkingIslandSize, thinkingIslandWidth]);

  useEffect(() => {
    if (typeof window === "undefined") return;
    window.localStorage.setItem(THINKING_ISLAND_HEIGHT_STORAGE_KEY, String(Math.round(thinkingIslandHeight)));
  }, [thinkingIslandHeight]);

  useEffect(() => {
    const handleResize = () => {
      setThinkingIslandWidth((prev) => clampThinkingIslandWidth(prev));
      setThinkingIslandOffset((prev) => clampThinkingIslandOffset(prev, thinkingIslandWidthRef.current));
    };
    window.addEventListener("resize", handleResize);
    return () => window.removeEventListener("resize", handleResize);
  }, []);

  useEffect(() => {
    setThinkingIslandOffset((prev) => clampThinkingIslandOffset(prev, thinkingIslandWidth));
  }, [thinkingIslandWidth]);

  useEffect(() => {
    setThinkingLines([]);
    setThinkingDraft("");
    setThinkingPanelOpen(true);
    setThinkingIslandExpanded(false);
    setThinkingStreamPacketCount(0);
    setThinkingStreamLastPacketAt(0);
    setThinkingWarmConversationId("");
    setThinkingWarmUntil(0);
    pendingThinkingLogsRef.current = [];
  }, [selectedId]);

  useEffect(() => {
    if (!selected?.conversation_id) return;
    if (!thinkingActive) return;
    touchThinkingPresence(selected.conversation_id, 16000);
  }, [selected?.conversation_id, thinkingActive, touchThinkingPresence]);

  useEffect(() => {
    if (thinkingWarmUntil <= clockMs) return undefined;
    const timer = window.setTimeout(() => setClockMs(Date.now()), Math.max(300, thinkingWarmUntil - clockMs));
    return () => window.clearTimeout(timer);
  }, [clockMs, thinkingWarmUntil]);

  useEffect(() => {
    let disposed = false;

    const connect = () => {
      if (disposed || thinkingWsRef.current) return;
      setThinkingStreamState("connecting");
      const ws = new WebSocket(api.logsStreamUrl());
      thinkingWsRef.current = ws;

      ws.onopen = () => {
        setThinkingStreamState("open");
      };

      ws.onmessage = (evt) => {
        let payload: { line?: string; lines?: string[] } | null = null;
        try {
          payload = JSON.parse(String(evt.data || "{}"));
        } catch {
          payload = null;
        }
        const incoming = Array.isArray(payload?.lines)
          ? payload.lines.filter((line): line is string => typeof line === "string" && !!line.trim())
          : [String(payload?.line || "")].filter((line) => !!line.trim());
        if (incoming.length === 0) return;
        setThinkingStreamPacketCount((prev) => prev + incoming.length);
        setThinkingStreamLastPacketAt(Date.now());

        for (const raw of incoming) {
          const line = String(raw || "").trim();
          if (!line) continue;
          const trace = traceRef.current;
          const traceCandidates = traceCandidatesRef.current;
          const conversationId = conversationRef.current;
          const matchesTrace = (trace ? line.includes(trace) : false)
            || traceCandidates.some((candidate) => !!candidate && line.includes(candidate));
          const matchesConversation = !!conversationId && line.includes(conversationId);
          const looksAgentLine = /(agent_|queue_|router_|tool_dispatch|tool_call|tool_result|send_final|effective_threshold_trace|interrupt)/.test(line);
          const rawDisplay = parseThinkingLine(line) || compactThinkingRawLine(line);
          const display = decorateThinkingLine(rawDisplay, line, conversationId);

          if ((matchesTrace || matchesConversation) && display) {
            touchThinkingPresence(conversationId, 15000);
            pushThinkingLine(display);
            continue;
          }

          // 全局 thinking 流：即使不是当前会话，也在灵动岛显示，避免"有处理但看不到"。
          if (looksAgentLine && display) {
            if (conversationId) {
              touchThinkingPresence(conversationId, 15000);
            }
            pushThinkingLine(display);
            continue;
          }

          if (conversationId && thinkingActiveRef.current && looksAgentLine) {
            pendingThinkingLogsRef.current = [
              ...pendingThinkingLogsRef.current.slice(-119),
              { raw: line, at: Date.now() },
            ];
          }
        }
      };

      ws.onerror = () => {
        try {
          ws.close();
        } catch {
          // ignore
        }
      };

      ws.onclose = () => {
        if (thinkingWsRef.current === ws) {
          thinkingWsRef.current = null;
        }
        setThinkingStreamState("closed");
        if (disposed) return;
        clearThinkingReconnectTimer();
        thinkingReconnectTimerRef.current = window.setTimeout(() => {
          thinkingReconnectTimerRef.current = null;
          connect();
        }, 1500);
      };
    };

    connect();
    return () => {
      disposed = true;
      clearThinkingReconnectTimer();
      if (thinkingWsRef.current) {
        try {
          thinkingWsRef.current.close();
        } catch {
          // ignore
        }
        thinkingWsRef.current = null;
      }
    };
  }, [clearThinkingReconnectTimer, pushThinkingLine, touchThinkingPresence]);

  useEffect(() => {
    const trace = traceRef.current;
    const traceCandidates = traceCandidatesRef.current;
    const conversationId = conversationRef.current;
    if (!conversationId || pendingThinkingLogsRef.current.length === 0) return;
    const keep: PendingThinkingLog[] = [];
    for (const item of pendingThinkingLogsRef.current) {
      if (Date.now() - item.at > 25000) continue;
      const matchesTrace = (trace ? item.raw.includes(trace) : false)
        || traceCandidates.some((candidate) => !!candidate && item.raw.includes(candidate));
      const matchesConversation = item.raw.includes(conversationId);
      if (!matchesTrace && !matchesConversation) {
        keep.push(item);
        continue;
      }
      const parsed = parseThinkingLine(item.raw) || compactThinkingRawLine(item.raw);
      if (!parsed) continue;
      touchThinkingPresence(conversationId, 15000);
      pushThinkingLine(parsed);
    }
    pendingThinkingLogsRef.current = keep.slice(-120);
  }, [pushThinkingLine, selectedId, selectedTraceCandidates, touchThinkingPresence]);

  useEffect(() => {
    if (!thinkingScrollRef.current) return;
    thinkingScrollRef.current.scrollTop = thinkingScrollRef.current.scrollHeight;
  }, [thinkingLines]);

  const agentContextUser = useMemo(() => {
    const recentPeerMessage = [...messages].reverse().find((item) => !item.is_self);
    if (recentPeerMessage) {
      return {
        userId: String(recentPeerMessage.sender_id || "").trim(),
        userName: String(recentPeerMessage.sender_name || recentPeerMessage.sender_id || "").trim(),
        senderRole: String(recentPeerMessage.sender_role || "").trim(),
      };
    }
    if (replyToMessage && !replyToMessage.is_self) {
      return {
        userId: String(replyToMessage.sender_id || "").trim(),
        userName: String(replyToMessage.sender_name || replyToMessage.sender_id || "").trim(),
        senderRole: String(replyToMessage.sender_role || "").trim(),
      };
    }
    if (selected?.chat_type === "private") {
      return {
        userId: String(selected.peer_id || "").trim(),
        userName: String(selected.peer_name || selected.peer_id || "").trim(),
        senderRole: "member",
      };
    }
    return {
      userId: "",
      userName: "",
      senderRole: "",
    };
  }, [messages, replyToMessage, selected]);

  const sendText = async () => {
    const content = text.trim();
    if (!selected || !content) return;
    setSending(true);
    try {
      const inFlight = Boolean(thinkingActive || isThinking);
      touchThinkingPresence(selected.conversation_id, 18000);
      if (inFlight) {
        setThinkingLines((prev) => [...prev.slice(-79), `追加需求: ${clip(content, 80)}`]);
      } else {
        setThinkingLines((prev) => [...prev.slice(-79), `已交给 AI: ${clip(content, 80)}`]);
      }
      await api.sendChatAgentText({
        chatType: selected.chat_type,
        peerId: selected.peer_id,
        text: content,
        replyToMessageId: replyToMessage?.message_id || undefined,
        contextUserId: agentContextUser.userId || undefined,
        contextUserName: agentContextUser.userName || undefined,
        contextSenderRole: agentContextUser.senderRole || undefined,
      });
      setText("");
      setReplyToMessage(null);
      setBotRuntimeStatus("online");
      await Promise.all([
        loadAgentState({ silent: true }),
        loadHistory({ silent: true }),
        loadConversations({ silent: true }),
      ]);
      setError(inFlight ? "[OK] 已向当前任务追加需求（不中断）" : "");
    } catch (e: unknown) {
      if (isOneBotUnavailableError(e)) {
        setBotRuntimeStatus("offline");
      }
      setError(e instanceof Error ? e.message : "发送文本失败");
    } finally {
      setSending(false);
    }
  };

  const sendRawText = async () => {
    const content = text.trim();
    if (!selected || !content) return;
    setSending(true);
    try {
      await api.sendChatText({
        chatType: selected.chat_type,
        peerId: selected.peer_id,
        text: content,
        replyToMessageId: replyToMessage?.message_id || undefined,
      });
      setText("");
      setReplyToMessage(null);
      setBotRuntimeStatus("online");
      await Promise.all([
        loadHistory({ silent: true }),
        loadConversations({ silent: true }),
      ]);
      setError("[OK] 已原样发送文本");
    } catch (e: unknown) {
      if (isOneBotUnavailableError(e)) {
        setBotRuntimeStatus("offline");
      }
      setError(e instanceof Error ? e.message : "原样发送文本失败");
    } finally {
      setSending(false);
    }
  };

  const sendImage = async () => {
    const url = imageUrl.trim();
    const b64 = imageBase64.trim();
    if (!selected || (!url && !b64)) return;
    setSending(true);
    try {
      await api.sendChatImage({
        chatType: selected.chat_type,
        peerId: selected.peer_id,
        imageUrl: url || undefined,
        imageBase64: b64 || undefined,
      });
      setImageUrl("");
      setImageBase64("");
      setImageFileName("");
      setImagePreviewUrl("");
      setBotRuntimeStatus("online");
      await Promise.all([
        loadHistory({ silent: true }),
        loadConversations({ silent: true }),
      ]);
      setError("");
    } catch (e: unknown) {
      if (isOneBotUnavailableError(e)) {
        setBotRuntimeStatus("offline");
      }
      setError(e instanceof Error ? e.message : "发送图片失败");
    } finally {
      setSending(false);
    }
  };

  const pickImageFile = () => {
    fileInputRef.current?.click();
  };

  const clearPendingImage = () => {
    setImageUrl("");
    setImageBase64("");
    setImageFileName("");
    setImagePreviewUrl("");
    setError("");
  };

  const onImageFileChange = async (evt: ChangeEvent<HTMLInputElement>) => {
    const file = evt.target.files?.[0];
    if (!file) return;
    try {
      const { dataUrl, base64Body } = await readImageFilePayload(file);
      setImageBase64(base64Body);
      setImageFileName(file.name);
      setImagePreviewUrl(dataUrl);
      setImageUrl("");
      setError("");
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : "图片读取失败");
    } finally {
      evt.target.value = "";
    }
  };

  const onTextPaste = async (evt: ReactClipboardEvent<HTMLInputElement | HTMLTextAreaElement>) => {
    const items = Array.from(evt.clipboardData?.items || []);
    const imageItem = items.find((item) => item.kind === "file" && item.type.startsWith("image/"));
    if (!imageItem) return;

    const file = imageItem.getAsFile();
    if (!file) {
      setError("剪贴板图片读取失败");
      return;
    }
    evt.preventDefault();
    try {
      const { dataUrl, base64Body } = await readImageFilePayload(file);
      const mimeSuffix = file.type.split("/")[1] || "png";
      setImageBase64(base64Body);
      setImageFileName(file.name || `clipboard-image.${mimeSuffix}`);
      setImagePreviewUrl(dataUrl);
      setImageUrl("");
      setError("[OK] 已从剪贴板读取图片，点击发图即可发送");
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : "剪贴板图片读取失败");
    }
  };

  const pendingImageSrc = imagePreviewUrl || imageUrl.trim();
  const pendingImageLabel = imageFileName || clip(imageUrl.trim(), 48) || "待发送图片";

  const onMessagesScroll = useCallback(() => {
    const el = messagesScrollRef.current;
    if (!el) return;
    const distanceToBottom = el.scrollHeight - el.scrollTop - el.clientHeight;
    const atBottom = distanceToBottom <= 24;
    autoStickToBottomRef.current = atBottom;
    setShowScrollToBottom(!atBottom);
  }, []);

  const scrollToLatest = useCallback(() => {
    autoStickToBottomRef.current = true;
    setShowScrollToBottom(false);
    scrollMessagesToBottom();
  }, [scrollMessagesToBottom]);

  const interruptConversation = async () => {
    if (!selected) return;
    setSending(true);
    try {
      await api.interruptChat(selected.conversation_id);
      setThinkingWarmUntil(0);
      setBotRuntimeStatus("online");
      await loadAgentState();
      setError("");
    } catch (e: unknown) {
      if (isOneBotUnavailableError(e)) {
        setBotRuntimeStatus("offline");
      }
      setError(e instanceof Error ? e.message : "中断会话失败");
    } finally {
      setSending(false);
    }
  };

  const openMessageMenu = (evt: ReactMouseEvent, msg: ChatMessageItem) => {
    evt.preventDefault();
    evt.stopPropagation();
    const menuWidth = 220;
    const menuHeight = 280;
    const x = Math.max(8, Math.min(evt.clientX, window.innerWidth - menuWidth - 8));
    const y = Math.max(8, Math.min(evt.clientY, window.innerHeight - menuHeight - 8));
    const randomEgg = CONTEXT_EASTER_EGGS[Math.floor(Math.random() * CONTEXT_EASTER_EGGS.length)] || "";
    setContextEgg(randomEgg);
    setContextMenu({ open: true, x, y, message: msg });
  };

  const copyMessageText = async () => {
    const msg = contextMenu.message;
    if (!msg) return;
    const payload = normalizeCopyPayload(msg);
    closeContextMenu();
    try {
      await navigator.clipboard.writeText(payload);
      setError("[OK] 已复制消息内容");
    } catch {
      setError("复制失败：浏览器未授予剪贴板权限");
    }
  };

  const useQuoteMessage = () => {
    if (!contextMenu.message) return;
    setReplyToMessage(contextMenu.message);
    closeContextMenu();
    setError("");
  };

  const recallMessage = async () => {
    const msg = contextMenu.message;
    if (!selected || !msg?.message_id) return;
    closeContextMenu();
    setSending(true);
    try {
      await api.recallChatMessage({
        messageId: msg.message_id,
        chatType: selected.chat_type,
        peerId: selected.peer_id,
      });
      await loadHistory();
      await loadConversations();
      setError("[OK] 已撤回该消息");
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : "撤回失败");
    } finally {
      setSending(false);
    }
  };

  const setEssenceMessage = async () => {
    const msg = contextMenu.message;
    if (!selected || !msg?.message_id) return;
    closeContextMenu();
    setSending(true);
    try {
      await api.setChatMessageEssence({
        messageId: msg.message_id,
        chatType: selected.chat_type,
        peerId: selected.peer_id,
      });
      setMessages((prev) =>
        prev.map((item) => item.message_id === msg.message_id ? { ...item, is_essence: true } : item),
      );
      setError("[OK] 已设为精华");
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : "设置精华失败");
    } finally {
      setSending(false);
    }
  };

  const removeEssenceMessage = async () => {
    const msg = contextMenu.message;
    if (!selected || !msg?.message_id) return;
    closeContextMenu();
    setSending(true);
    try {
      await api.removeChatMessageEssence({
        messageId: msg.message_id,
        chatType: selected.chat_type,
        peerId: selected.peer_id,
      });
      setMessages((prev) =>
        prev.map((item) => item.message_id === msg.message_id ? { ...item, is_essence: false } : item),
      );
      setError("[OK] 已移除精华");
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : "移除精华失败");
    } finally {
      setSending(false);
    }
  };

  const addMessageToSticker = async () => {
    const msg = contextMenu.message;
    if (!msg?.message_id || !hasImageSegment(msg)) return;
    closeContextMenu();
    setSending(true);
    try {
      const res = await api.addChatMessageToSticker({
        messageId: msg.message_id,
        sourceUserId: msg.sender_id || "",
        description: msg.text && !msg.text.startsWith("[") ? clip(msg.text, 20) : "",
      });
      setError(`[OK] 已添加到表情包: ${res.key}`);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : "添加表情包失败");
    } finally {
      setSending(false);
    }
  };

  const retargetConversation = async () => {
    const goal = thinkingDraft.trim();
    if (!selected || !goal) return;
    setRetargeting(true);
    try {
      touchThinkingPresence(selected.conversation_id, 18000);
      if (isThinking) {
        await api.interruptChat(selected.conversation_id);
        setThinkingLines((prev) => [...prev.slice(-79), "已中断当前任务，切换到新目标"]);
      }
      await api.sendChatAgentText({
        chatType: selected.chat_type,
        peerId: selected.peer_id,
        text: goal,
        contextUserId: agentContextUser.userId || undefined,
        contextUserName: agentContextUser.userName || undefined,
        contextSenderRole: agentContextUser.senderRole || undefined,
      });
      setThinkingLines((prev) => [...prev.slice(-79), `临时改目标: ${clip(goal, 150)}`]);
      setThinkingDraft("");
      setBotRuntimeStatus("online");
      await Promise.all([
        loadAgentState({ silent: true }),
        loadHistory({ silent: true }),
      ]);
      setError("[OK] 已发送临时目标");
    } catch (e: unknown) {
      if (isOneBotUnavailableError(e)) {
        setBotRuntimeStatus("offline");
      }
      setError(e instanceof Error ? e.message : "临时改目标失败");
    } finally {
      setRetargeting(false);
    }
  };

  const onMainInputKeyDown = useCallback((evt: ReactKeyboardEvent<HTMLInputElement>) => {
    if (evt.key === "Escape") {
      evt.preventDefault();
      evt.stopPropagation();
      if (text.trim()) setText("");
      if (replyToMessage) setReplyToMessage(null);
      return;
    }
    if (evt.key !== "Enter") return;
    if (evt.nativeEvent.isComposing || evt.repeat) return;
    evt.preventDefault();
    evt.stopPropagation();
    if (!selected || sending || !text.trim()) return;
    if (evt.ctrlKey || evt.metaKey) {
      void sendText();
      return;
    }
    void sendRawText();
  }, [replyToMessage, selected, sendRawText, sendText, sending, text]);

  const onRetargetInputKeyDown = useCallback((evt: ReactKeyboardEvent<HTMLInputElement>) => {
    if (evt.key !== "Enter") return;
    if (evt.nativeEvent.isComposing || evt.repeat) return;
    evt.preventDefault();
    evt.stopPropagation();
    if (retargeting || !thinkingDraft.trim()) return;
    void retargetConversation();
  }, [retargetConversation, retargeting, thinkingDraft]);

  return (
    <section className="stapxs-chat-shell h-[calc(100vh-96px)] min-h-0 flex flex-col gap-2 overflow-hidden">
      <div className="stapxs-chat-toolbar flex items-center justify-between gap-1.5">
        <div className="flex items-center gap-2">
          <MessageSquare size={18} />
          <h2 className="text-lg font-semibold">聊天控制台</h2>
          <Chip size="sm" variant="flat">{conversations.length} 会话</Chip>
        </div>
        <div className="flex items-center gap-2">
          <Chip size="sm" variant="flat" color={pauseAutoRefresh ? "warning" : "success"}>
            {refreshPausedHint}
          </Chip>
          <Button
            size="sm"
            variant={pauseAutoRefresh ? "solid" : "flat"}
            color={pauseAutoRefresh ? "warning" : "default"}
            startContent={pauseAutoRefresh ? <Play size={14} /> : <Pause size={14} />}
            onPress={() => setPauseAutoRefresh((prev) => !prev)}
          >
            {pauseAutoRefresh ? "恢复自动刷新" : "暂停自动刷新"}
          </Button>
          <Button size="sm" variant="flat" startContent={<RefreshCw size={14} />} onPress={() => { void loadConversations(); }} isLoading={loadingConvs}>
            刷新会话
          </Button>
          <Button size="sm" variant="flat" startContent={<RefreshCw size={14} />} onPress={() => { void loadHistory(); }} isLoading={loadingHistory} isDisabled={!selected}>
            刷新消息
          </Button>
          <Button size="sm" variant="flat" startContent={<RefreshCw size={14} />} onPress={() => { void loadAgentState(); }} isLoading={loadingState}>
            刷新状态
          </Button>
        </div>
      </div>

      {error && (
        <div className="rounded-lg border border-default-300/35 bg-content2/30 px-3 py-2 flex items-start justify-between gap-3">
          <p className={`${error.startsWith("[OK]") ? "text-success" : "text-danger"} text-sm whitespace-pre-wrap`}>
            {error.replace(/^\[OK\]\s*/, "")}
          </p>
          <Button size="sm" variant="light" isIconOnly onPress={() => setError("")}>
            <X size={14} />
          </Button>
        </div>
      )}

      <div className="stapxs-chat-grid grid grid-cols-1 lg:grid-cols-[300px_minmax(0,1fr)] gap-2 flex-1 min-h-0">
        <Card className="stapxs-conv-card h-full overflow-hidden">
          <CardHeader className="pb-2">
            <div className="flex items-center justify-between w-full">
              <span className="text-sm font-semibold">会话列表</span>
              {loadingConvs && <Spinner size="sm" />}
            </div>
            <Input
              size="sm"
              placeholder="搜索会话（名称/ID/内容）"
              value={conversationKeyword}
              onValueChange={setConversationKeyword}
              classNames={INPUT_CLASSES}
            />
          </CardHeader>
          <CardBody className="stapxs-conv-list pt-1 overflow-auto">
            {filteredConversations.map((item) => {
              const active = selectedId === item.conversation_id;
              const title = item.peer_name || item.peer_id || "未知会话";
              const avatarUrl = resolveQQAvatar(item.peer_id, 100);
              return (
                <button
                  type="button"
                  key={item.conversation_id}
                  className={`stapxs-conv-item ${active ? "is-active" : ""}`}
                  onClick={() => setSelectedId(item.conversation_id)}
                >
                  <div className="stapxs-conv-item-main">
                    <span className="stapxs-avatar size-sm">
                      {avatarUrl ? (
                        <img src={avatarUrl} alt={`${title} avatar`} loading="lazy" />
                      ) : (
                        <span className="stapxs-avatar-fallback">{avatarInitial(title)}</span>
                      )}
                    </span>
                    <div className="stapxs-conv-meta">
                      <div className="stapxs-conv-title-row">
                        <div className="stapxs-conv-title">
                          {item.chat_type === "group" ? "群聊" : "私聊"} · {title}
                        </div>
                        <span className="stapxs-conv-time">{fmtTs(item.last_time)}</span>
                      </div>
                      <div className="stapxs-conv-preview">{clip(item.last_message, 80) || "暂无预览"}</div>
                    </div>
                    {item.unread_count > 0 && (
                      <Chip size="sm" color="danger" variant="flat">{item.unread_count}</Chip>
                    )}
                  </div>
                </button>
              );
            })}
            {conversations.length === 0 && <p className="text-default-400 text-sm">暂无最近会话</p>}
            {conversations.length > 0 && filteredConversations.length === 0 && (
              <p className="text-default-400 text-sm">没有匹配会话，试试更短关键词。</p>
            )}
          </CardBody>
        </Card>

        <div className="grid grid-rows-[auto_minmax(0,1fr)_auto] gap-2 h-full min-h-0">
          <Card className="stapxs-status-card">
            <CardBody className="py-3">
              <div className="flex flex-wrap items-center gap-2 justify-between">
                <div className="min-w-0">
                  <div className="font-semibold truncate">
                    {selected ? `${selected.chat_type === "group" ? "群聊" : "私聊"} · ${selected.peer_name}` : "未选择会话"}
                  </div>
                  {selected && (
                    <div className="text-xs text-default-500">
                      ID: {selected.peer_id} · 最近消息: {fmtTs(selected.last_time)}
                    </div>
                  )}
                </div>
                <div className="flex items-center gap-2">
                  <Chip size="sm" variant="flat" color={botRuntimeStatus === "online" ? "success" : "warning"}>
                    Bot: {botRuntimeStatus === "online" ? "在线" : "重连中"}
                  </Chip>
                  <Chip size="sm" variant="flat" color={selectedState && selectedState.pending_count > 0 ? "warning" : "success"}>
                    队列: {selectedState?.pending_count ?? 0}
                  </Chip>
                  {!thinkingPanelOpen && selected && (
                    <Button
                      size="sm"
                      variant="flat"
                      startContent={<Sparkles size={14} />}
                      onPress={() => setThinkingPanelOpen(true)}
                    >
                      显示灵动岛
                    </Button>
                  )}
                  <Button
                    size="sm"
                    color="warning"
                    variant="flat"
                    startContent={<Square size={14} />}
                    onPress={interruptConversation}
                    isDisabled={!selected || sending}
                  >
                    中断当前会话
                  </Button>
                </div>
              </div>
              {selectedState && (
                <div className="mt-2 text-xs text-default-500">
                  running={selectedState.running_count} · queued={selectedState.queued_count} · latest={selectedState.last_trace_id || selectedState.latest_trace_id || "-"}
                </div>
              )}
              {selected?.chat_type === "group" && (
                <div className="mt-1 text-xs text-default-500">
                  Bot群权限: {permission.bot_role || "member"}
                </div>
              )}
            </CardBody>
          </Card>

          <Card className="stapxs-msg-card h-full min-h-0 overflow-hidden relative">
            <CardBody
              ref={messagesScrollRef}
              className="stapxs-msg-scroll min-h-0 overflow-auto"
              onScroll={onMessagesScroll}
            >
              {loadingHistory && <Spinner size="sm" />}
              {!loadingHistory && messages.length === 0 && (
                <p className="text-default-400 text-sm">暂无聊天记录</p>
              )}
              {messages.map((msg) => {
                const msgAvatarUrl = resolveQQAvatar(msg.sender_id, 100);
                const msgDisplayName = msg.sender_name || msg.sender_id || "未知";
                return (
                  <div key={`${msg.message_id}-${msg.seq}-${msg.timestamp}`} className={`stapxs-msg-row ${msg.is_self ? "is-self" : ""}`}>
                    {!msg.is_self && (
                      <span className="stapxs-avatar size-xs">
                        {msgAvatarUrl ? (
                          <img
                            src={msgAvatarUrl}
                            alt={`${msgDisplayName} avatar`}
                            loading="lazy"
                          />
                        ) : (
                          <span className="stapxs-avatar-fallback">{avatarInitial(msgDisplayName || "U")}</span>
                        )}
                      </span>
                    )}
                    <div
                      className={`stapxs-msg-bubble ${
                        msg.is_recalled
                          ? "is-recalled"
                          : msg.is_self
                            ? "is-self"
                            : ""
                      }`}
                      onContextMenu={(evt) => openMessageMenu(evt, msg)}
                      title="右键可操作：复制 / 引用 / 设精华 / 移除精华 / 添加表情包 / 撤回"
                    >
                      <div className="stapxs-msg-header">
                        <span className="inline-flex items-center gap-1">
                          <UserRound size={12} />
                          <span>{msgDisplayName}</span>
                        </span>
                        <span>·</span>
                        <span>{fmtTs(msg.timestamp)}</span>
                        {msg.is_essence && <Chip size="sm" variant="flat" color="warning">精华</Chip>}
                        {msg.is_recalled && <Chip size="sm" variant="flat" color="warning">此消息已撤回</Chip>}
                      </div>
                      <div className="stapxs-msg-text">
                        {msg.text || "[空消息]"}
                      </div>
                      {msg.is_recalled && (
                        <div className="mt-2 text-[11px] text-warning-700/90">
                          此消息已撤回，保留原文仅用于对话追踪。
                        </div>
                      )}
                    </div>
                    {msg.is_self && (
                      <span className="stapxs-avatar size-xs">
                        {msgAvatarUrl ? (
                          <img
                            src={msgAvatarUrl}
                            alt={`${msgDisplayName} avatar`}
                            loading="lazy"
                          />
                        ) : (
                          <span className="stapxs-avatar-fallback">{avatarInitial(msgDisplayName || "Y")}</span>
                        )}
                      </span>
                    )}
                  </div>
                );
              })}
            </CardBody>
            {showScrollToBottom && (
              <div className="pointer-events-none absolute bottom-3 right-3 z-30">
                <Button
                  size="sm"
                  color="primary"
                  variant="shadow"
                  className="pointer-events-auto"
                  startContent={<ChevronDown size={14} />}
                  onPress={scrollToLatest}
                >
                  回到底部
                </Button>
              </div>
            )}
            <AnimatePresence mode="wait">
              {thinkingIslandVisible && (
                <motion.div
                  initial={{ opacity: 0, y: -20, scale: 0.92 }}
                  animate={{ opacity: 1, y: 0, scale: 1 }}
                  exit={{ opacity: 0, y: -16, scale: 0.94 }}
                  transition={{
                    type: "spring",
                    stiffness: 240,
                    damping: 24,
                    mass: 0.75
                  }}
                  className="pointer-events-none fixed inset-x-0 top-3 z-[90] flex justify-center px-3"
                >
                  <motion.div
                    drag
                    dragControls={thinkingDragControls}
                    dragListener={false}
                    dragMomentum={false}
                    dragElastic={0.05}
                    dragTransition={{ bounceStiffness: 600, bounceDamping: 30 }}
                    style={{
                      x: thinkingIslandOffset.x,
                      y: thinkingIslandOffset.y,
                      width: getThinkingIslandWidthStyle(thinkingIslandWidth),
                      maxWidth: "calc(100vw - 24px)",
                      touchAction: "none",
                    }}
                    whileDrag={{ scale: 1.02, boxShadow: "0 20px 40px rgba(0,0,0,0.15)" }}
                    whileHover={{ scale: 1.005 }}
                    onDragStart={() => {
                      thinkingIslandDragOriginRef.current = thinkingIslandOffset;
                    }}
                    onDragEnd={(_, info) => {
                      setThinkingIslandOffset(clampThinkingIslandOffset({
                        x: thinkingIslandDragOriginRef.current.x + info.offset.x,
                        y: thinkingIslandDragOriginRef.current.y + info.offset.y,
                      }, thinkingIslandWidth));
                    }}
                    className={`pointer-events-auto select-none overflow-hidden rounded-2xl border border-primary/20 bg-content1/95 shadow-lg backdrop-blur-xl ${thinkingActive ? "thinking-island-live" : ""}`}
                  >
                    <div
                      className="thinking-island-resize-handle absolute inset-y-3 right-0 z-20 w-2 cursor-ew-resize"
                      onPointerDown={beginThinkingIslandResize}
                      title="拖动右侧边缘调整宽度"
                    />
                    <div
                      className="thinking-island-resize-handle absolute inset-y-3 left-0 z-20 w-2 cursor-ew-resize"
                      data-resize-dir="left"
                      onPointerDown={beginThinkingIslandResize}
                      title="拖动左侧边缘调整宽度"
                    />
                    <div
                      className="flex select-none items-start gap-2 px-3 py-2"
                      onPointerDown={beginThinkingIslandDrag}
                      onDoubleClick={() => setThinkingIslandExpanded((prev) => !prev)}
                    >
                      <div
                        className="flex h-9 w-9 shrink-0 cursor-grab items-center justify-center rounded-full bg-primary/10 ring-1 ring-primary/20 select-none touch-none active:cursor-grabbing"
                        onPointerDown={beginThinkingIslandDrag}
                        onDoubleClick={() => setThinkingIslandOffset({ x: 0, y: 0 })}
                        title="拖动移动，双击回中"
                      >
                        <BrainCircuit size={16} className={`${thinkingActive ? "text-primary animate-pulse" : "text-success"}`} />
                      </div>
                      <div className="min-w-0 flex-1">
                        <div className="flex flex-wrap items-center gap-2">
                          <span className="text-sm font-semibold text-default-800">YuKiKo Thinking</span>
                          <Chip size="sm" variant="flat" color={thinkingActive ? "primary" : "success"}>
                            {thinkingStatusLabel(thinkingActive)}
                          </Chip>
                          <Chip size="sm" variant="flat" color={thinkingStageColorValue}>
                            {thinkingStageLabelText}
                          </Chip>
                          <Chip size="sm" variant="flat">
                            {thinkingLines.length} 条
                          </Chip>
                        </div>
                        <div className="mt-1.5 flex items-center gap-2 text-[11px] text-default-600">
                          <span className={`h-2 w-2 rounded-full ${thinkingActive ? "bg-primary animate-pulse" : "bg-success"}`} />
                          <span className="truncate">{latestThinkingLine}</span>
                        </div>
                        <div className="mt-2">
                          <div className="relative h-1.5 overflow-hidden rounded-full bg-default-200/80">
                            <motion.div
                              initial={false}
                              animate={{ width: `${thinkingProgressValue}%` }}
                              transition={{ type: "spring", stiffness: 220, damping: 26, mass: 0.8 }}
                              className={`h-full ${thinkingStage === "error" ? "bg-red-500" : thinkingStage === "cancelled" ? "bg-amber-500" : thinkingStage === "done" ? "bg-emerald-500" : "bg-primary"} ${thinkingActive ? "thinking-progress-flow" : ""}`}
                            />
                            {thinkingActive && (
                              <>
                                <span className="thinking-progress-sweep absolute inset-y-0 w-10 bg-white/35 blur-[1px]" />
                                <span className="thinking-progress-sweep thinking-progress-sweep-delay absolute inset-y-0 w-8 bg-white/25 blur-[1px]" />
                              </>
                            )}
                          </div>
                          <div className="mt-1 flex items-center justify-between text-[10px] text-default-500">
                            <span>{thinkingStreamLabel(thinkingStreamState)}</span>
                            <span>{thinkingStreamState === "open" ? `流包 ${thinkingStreamPacketCount}` : `最近流包 ${lastStreamPacketLabel}`}</span>
                          </div>
                        </div>
                        <div className="mt-1.5 grid grid-cols-4 gap-1 text-[10px] text-default-500">
                          {THINKING_STAGE_STEPS.map((step) => {
                            const current = THINKING_STAGE_STEPS.findIndex((item) => item.key === thinkingStage);
                            const stepIndex = THINKING_STAGE_STEPS.findIndex((item) => item.key === step.key);
                            const done = thinkingStage === "done" || stepIndex <= current;
                            return (
                              <div key={step.key} className={`rounded-full px-1.5 py-0.5 text-center ${done ? "bg-primary/10 text-primary-700" : "bg-default-100 text-default-500"}`}>
                                {step.label}
                              </div>
                            );
                          })}
                        </div>
                      </div>
                      <div className="flex items-center gap-1" onPointerDown={(evt) => evt.stopPropagation()}>
                        <Button
                          size="sm"
                          variant="light"
                          isIconOnly
                          onPress={shrinkThinkingIsland}
                          className="text-default-600"
                          isDisabled={thinkingIslandWidth <= THINKING_ISLAND_MIN_WIDTH + 4}
                        >
                          <Minus size={13} />
                        </Button>
                        <Button
                          size="sm"
                          variant="light"
                          isIconOnly
                          onPress={growThinkingIsland}
                          className="text-default-600"
                          isDisabled={thinkingIslandWidth >= clampThinkingIslandWidth(THINKING_ISLAND_MAX_WIDTH) - 4}
                        >
                          <Plus size={13} />
                        </Button>
                        <Button
                          size="sm"
                          variant="light"
                          isIconOnly
                          onPress={() => setThinkingIslandExpanded((prev) => !prev)}
                          className="text-default-600"
                        >
                          {thinkingIslandExpanded ? <ChevronUp size={16} /> : <ChevronDown size={16} />}
                        </Button>
                        <Button
                          size="sm"
                          variant="light"
                          isIconOnly
                          onPress={() => {
                            if (thinkingActive) {
                              setThinkingIslandExpanded(false);
                            } else {
                              setThinkingPanelOpen(false);
                            }
                          }}
                          className="text-default-600"
                          title={thinkingActive ? "最小化" : "关闭"}
                        >
                          <X size={14} />
                        </Button>
                      </div>
                    </div>
                    <AnimatePresence initial={false}>
                      {thinkingIslandExpanded && (
                        <motion.div
                          initial={{ height: 0, opacity: 0 }}
                          animate={{ height: "auto", opacity: 1 }}
                          exit={{ height: 0, opacity: 0 }}
                          transition={{
                            height: { type: "spring", stiffness: 320, damping: 30, mass: 0.6 },
                            opacity: { duration: 0.15 }
                          }}
                          className="overflow-hidden border-t border-default-200"
                        >
                          <div className="grid grid-cols-2 gap-2 px-3 pt-2 text-[10px] text-default-500">
                            <div className="truncate">trace: {selectedState?.last_trace_id || selectedState?.latest_trace_id || "等待分配"}</div>
                            <div className="text-right">尺寸: {thinkingIslandSize.toUpperCase()} / {Math.round(thinkingIslandWidth)}px</div>
                            <div>最近流包: {lastStreamPacketLabel}</div>
                            <div className="text-right">累计流包: {thinkingStreamPacketCount}</div>
                          </div>
                          <div
                            ref={thinkingScrollRef}
                            style={{ maxHeight: `${thinkingIslandHeight}px` }}
                            className="mt-1 space-y-1.5 overflow-auto px-3 py-2"
                          >
                            {thinkingPreviewLines.length === 0 && (
                              <p className="text-xs text-default-500">已接入会话，等待更具体的思考流。</p>
                            )}
                            {thinkingPreviewLines.map((line, idx) => (
                              <motion.div
                                key={`${idx}-${line}`}
                                initial={{ opacity: 0, y: 8, scale: 0.96 }}
                                animate={{ opacity: 1, y: 0, scale: 1 }}
                                exit={{ opacity: 0, scale: 0.96 }}
                                transition={{
                                  type: "spring",
                                  stiffness: 400,
                                  damping: 30,
                                  mass: 0.5,
                                  delay: idx * 0.015
                                }}
                                className="rounded-2xl border border-default-200 bg-content2/80 px-3 py-2 text-xs text-default-700 backdrop-blur-sm"
                              >
                                {line}
                              </motion.div>
                            ))}
                          </div>
                          <div className="space-y-2 px-3 pb-3">
                            <Textarea
                              label="临时改目标"
                              labelPlacement="outside"
                              minRows={1}
                              value={thinkingDraft}
                              onValueChange={setThinkingDraft}
                              onKeyDown={onRetargetInputKeyDown}
                              placeholder="比如：先别继续搜图，先给我3个候选并说明理由"
                              classNames={INPUT_CLASSES}
                            />
                            <div className="flex flex-wrap items-center justify-end gap-2">
                              <Button
                                size="sm"
                                color="warning"
                                variant="flat"
                                startContent={<Square size={13} />}
                                onPress={interruptConversation}
                                isDisabled={!thinkingActive || sending}
                              >
                                取消当前任务
                              </Button>
                              <Button
                                size="sm"
                                color="primary"
                                onPress={retargetConversation}
                                isDisabled={!thinkingDraft.trim() || retargeting}
                                isLoading={retargeting}
                              >
                                强制改目标
                              </Button>
                            </div>
                          </div>
                        </motion.div>
                      )}
                    </AnimatePresence>
                    <div
                      className="thinking-island-resize-handle-bottom absolute inset-x-3 bottom-0 z-20 h-2 cursor-ns-resize"
                      data-resize-dir="bottom"
                      onPointerDown={beginThinkingIslandResize}
                      title="拖动底部边缘调整高度"
                    />
                  </motion.div>
                </motion.div>
              )}
            </AnimatePresence>
          </Card>

          <Card className="stapxs-composer-card border border-primary/15 bg-content1">
            <CardBody className="space-y-2.5">
              {replyToMessage && (
                <div className="stapxs-reply-bar rounded-lg border border-primary/35 bg-primary/10 px-3 py-2 flex items-start justify-between gap-3">
                  <div className="text-xs min-w-0">
                    <p className="text-primary font-medium">正在引用消息</p>
                    <p className="text-default-600 truncate">
                      {replyToMessage.sender_name || replyToMessage.sender_id || "未知"}: {clip(replyToMessage.text || "[媒体消息]", 88)}
                    </p>
                  </div>
                  <Button
                    size="sm"
                    variant="light"
                    isIconOnly
                    onPress={() => setReplyToMessage(null)}
                  >
                    <X size={14} />
                  </Button>
                </div>
              )}
              {selected && (
                <div className={`stapxs-mode-bar rounded-lg border px-3 py-2 text-xs ${
                  thinkingActive
                    ? "border-warning/35 bg-warning/10 text-warning-700"
                    : "border-primary/30 bg-primary/10 text-primary-700"
                }`}>
                  <div className="flex flex-wrap items-center gap-2">
                    <Chip size="sm" variant="flat" color={thinkingActive ? "warning" : "primary"}>
                      {thinkingActive ? "处理中途沟通模式" : "普通提问模式"}
                    </Chip>
                    <span>Enter 原样发送 · Ctrl+Enter 交给AI · Esc 清空输入</span>
                    {thinkingActive && <span>"交给AI"会追加需求，不中断；要改方向请点"取消当前任务"</span>}
                  </div>
                </div>
              )}
              <Textarea
                label="对 AI 说"
                labelPlacement="outside"
                minRows={1}
                value={text}
                onValueChange={setText}
                onPaste={onTextPaste}
                onKeyDown={onMainInputKeyDown}
                placeholder={selected ? "输入要交给 AI 处理的话，它会按当前会话上下文继续完成..." : "请先选择会话"}
                isDisabled={!selected || sending}
                classNames={INPUT_CLASSES}
              />
              <div className="stapxs-input-row flex items-center gap-2">
                <Button
                  color="primary"
                  startContent={<SendHorizontal size={14} />}
                  onPress={sendRawText}
                  isDisabled={!selected || !text.trim() || sending}
                  isLoading={sending}
                >
                  原样发送
                </Button>
                <Button
                  variant="flat"
                  onPress={sendText}
                  isDisabled={!selected || !text.trim() || sending}
                >
                  交给AI
                </Button>
                <div className="stapxs-input-grow">
                  <Input
                    className="flex-1"
                    placeholder="图片 URL（http/https）"
                    value={imageUrl}
                    onValueChange={(v) => {
                      setImageUrl(v);
                      if (v.trim()) {
                        setImageBase64("");
                        setImageFileName("");
                        setImagePreviewUrl("");
                      }
                    }}
                    isDisabled={!selected || sending}
                    classNames={INPUT_CLASSES}
                  />
                </div>
                <Button
                  variant="flat"
                  startContent={<ImagePlus size={14} />}
                  onPress={pickImageFile}
                  isDisabled={!selected || sending}
                >
                  上传图
                </Button>
                <Button
                  variant="flat"
                  startContent={<ImagePlus size={14} />}
                  onPress={sendImage}
                  isDisabled={!selected || (!imageUrl.trim() && !imageBase64.trim()) || sending}
                  isLoading={sending}
                >
                  发图
                </Button>
              </div>
              {pendingImageSrc && (
                <div className="stapxs-pending-image inline-flex max-w-full items-center gap-2 self-start rounded-full border border-default-300/40 bg-content2/55 px-2.5 py-1.5">
                  <div className="flex min-w-0 items-center gap-2">
                    <img
                      src={pendingImageSrc}
                      alt={pendingImageLabel}
                      className="h-7 w-7 rounded-full border border-default-300/40 object-cover"
                    />
                    <p className="max-w-[220px] truncate text-sm font-medium">{pendingImageLabel}</p>
                  </div>
                  <Button
                    size="sm"
                    variant="light"
                    isIconOnly
                    onPress={clearPendingImage}
                    isDisabled={sending}
                    className="h-6 min-w-6 w-6"
                  >
                    <X size={12} />
                  </Button>
                </div>
              )}
              <input ref={fileInputRef} type="file" accept="image/*" className="hidden" onChange={onImageFileChange} />
            </CardBody>
          </Card>
        </div>
      </div>

      {contextMenu.open && contextMenu.message && (
        <div
          data-chat-context-menu="1"
          className="stapxs-context-menu fixed z-[70] w-[220px] rounded-xl border border-default-400/45 bg-content1/95 backdrop-blur p-1 shadow-xl"
          style={{ left: contextMenu.x, top: contextMenu.y }}
        >
          {contextEgg && (
            <div className="px-3 py-1 text-[11px] text-primary/80 flex items-center gap-1">
              <Sparkles size={12} />
              {contextEgg}
            </div>
          )}
          <button
            type="button"
            className="stapxs-context-item w-full text-left px-3 py-2 rounded-lg hover:bg-content2/70 text-sm flex items-center gap-2"
            onClick={copyMessageText}
          >
            <Copy size={14} />
            复制消息
          </button>
          <button
            type="button"
            className="stapxs-context-item w-full text-left px-3 py-2 rounded-lg hover:bg-content2/70 text-sm flex items-center gap-2"
            onClick={useQuoteMessage}
            disabled={!contextMenu.message?.message_id}
          >
            <Quote size={14} />
            引用这条消息
          </button>
          <button
            type="button"
            className="stapxs-context-item w-full text-left px-3 py-2 rounded-lg hover:bg-content2/70 text-sm flex items-center gap-2 disabled:opacity-45"
            onClick={addMessageToSticker}
            disabled={sending || !hasImageSegment(contextMenu.message)}
          >
            <SmilePlus size={14} />
            添加到表情包
          </button>
          {canSetEssenceInMenu && (
            <>
              {!contextMessageIsEssence && (
                <button
                  type="button"
                  className="stapxs-context-item w-full text-left px-3 py-2 rounded-lg hover:bg-content2/70 text-sm flex items-center gap-2 disabled:opacity-45"
                  onClick={setEssenceMessage}
                  disabled={sending || !contextMenu.message?.message_id}
                >
                  <Star size={14} />
                  设为精华
                </button>
              )}
              {contextMessageIsEssence && (
                <button
                  type="button"
                  className="stapxs-context-item w-full text-left px-3 py-2 rounded-lg hover:bg-content2/70 text-sm flex items-center gap-2 disabled:opacity-45"
                  onClick={removeEssenceMessage}
                  disabled={sending || !contextMenu.message?.message_id}
                >
                  <Star size={14} />
                  移除精华
                </button>
              )}
            </>
          )}
          {canRecallInMenu && (
            <>
              <div className="my-1 h-px bg-default-300/45" />
              <button
                type="button"
                className="stapxs-context-item danger w-full text-left px-3 py-2 rounded-lg hover:bg-danger/15 text-sm flex items-center gap-2 text-danger disabled:opacity-45"
                onClick={recallMessage}
                disabled={sending || !contextMenu.message?.message_id || Boolean(contextMenu.message?.is_recalled)}
              >
                <Trash2 size={14} />
                {contextMenu.message?.is_recalled ? "已撤回" : "撤回"}
              </button>
            </>
          )}
        </div>
      )}
    </section>
  );
}
