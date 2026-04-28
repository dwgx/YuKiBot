import type { ChatAgentStateItem } from "../../api/client";
import { clip } from "./chat-utils";

export const THINKING_ISLAND_STORAGE_KEY = "yukiko-thinking-island-offset-v1";
export const THINKING_ISLAND_SIZE_STORAGE_KEY = "yukiko-thinking-island-size-v1";
export const THINKING_ISLAND_WIDTH_STORAGE_KEY = "yukiko-thinking-island-width-v3";
export const THINKING_ISLAND_HEIGHT_STORAGE_KEY = "yukiko-thinking-island-height-v1";

export type ThinkingIslandOffset = { x: number; y: number };
export type PendingThinkingLog = { raw: string; at: number };
export type ThinkingIslandSize = "sm" | "md" | "lg";
export type ThinkingStage = "idle" | "routing" | "planning" | "executing" | "replying" | "done" | "cancelled" | "error";

export const THINKING_ISLAND_DEFAULT_WIDTH: Record<ThinkingIslandSize, number> = { sm: 560, md: 860, lg: 1180 };
export const THINKING_ISLAND_MIN_WIDTH = 360;
export const THINKING_ISLAND_MAX_WIDTH = 1440;
export const THINKING_ISLAND_MIN_HEIGHT = 100;
export const THINKING_ISLAND_MAX_HEIGHT = 640;
export const THINKING_ISLAND_DEFAULT_HEIGHT = 160;

export const THINKING_STAGE_STEPS: Array<{ key: "routing" | "planning" | "executing" | "replying"; label: string }> = [
  { key: "routing", label: "理解" },
  { key: "planning", label: "规划" },
  { key: "executing", label: "执行" },
  { key: "replying", label: "回复" },
];

export const THINKING_ISLAND_PREVIEW_HEIGHT_CLASS: Record<ThinkingIslandSize, string> = {
  sm: "max-h-28",
  md: "max-h-36",
  lg: "max-h-44",
};

export function stateBelongsToConversation(stateConversationId: string, selectedConversationId: string): boolean {
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

export function mergeAgentStates(states: ChatAgentStateItem[], selectedConversationId: string): ChatAgentStateItem | null {
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

export function clampThinkingIslandWidth(width: number): number {
  const safe = Number.isFinite(width) ? width : THINKING_ISLAND_DEFAULT_WIDTH.md;
  if (typeof window === "undefined") {
    return Math.max(THINKING_ISLAND_MIN_WIDTH, Math.min(THINKING_ISLAND_MAX_WIDTH, safe));
  }
  return Math.max(
    THINKING_ISLAND_MIN_WIDTH,
    Math.min(THINKING_ISLAND_MAX_WIDTH, window.innerWidth - 24, safe),
  );
}

export function widthToThinkingIslandSize(width: number): ThinkingIslandSize {
  if (width >= 760) return "lg";
  if (width >= 580) return "md";
  return "sm";
}

export function getThinkingIslandRenderWidth(width: number): number {
  return clampThinkingIslandWidth(width);
}

export function getThinkingIslandWidthStyle(width: number): string {
  return `${clampThinkingIslandWidth(width)}px`;
}

export function clampThinkingIslandOffset(
  offset: ThinkingIslandOffset,
  width: number = THINKING_ISLAND_DEFAULT_WIDTH.md,
): ThinkingIslandOffset {
  if (typeof window === "undefined") return offset;
  const islandWidth = getThinkingIslandRenderWidth(width);
  const maxX = Math.max(0, Math.floor((window.innerWidth - islandWidth) / 2) - 8);
  const maxY = Math.max(0, Math.floor(window.innerHeight - 160));
  return {
    x: Math.max(-maxX, Math.min(maxX, Number.isFinite(offset.x) ? offset.x : 0)),
    y: Math.max(-8, Math.min(maxY, Number.isFinite(offset.y) ? offset.y : 0)),
  };
}

export function loadThinkingIslandOffset(width: number = THINKING_ISLAND_DEFAULT_WIDTH.md): ThinkingIslandOffset {
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

export function loadThinkingIslandWidth(): number {
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

export function parseThinkingLine(rawLine: string): string {
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

export function compactThinkingRawLine(rawLine: string): string {
  const line = String(rawLine || "").trim();
  if (!line) return "";
  if (!/(agent_|queue_|router_|tool_dispatch|tool_call|tool_result|send_final|effective_threshold_trace|interrupt|trace=|step=)/.test(line)) return "";
  const compact = line
    .replace(/^\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}\s*\|\s*[A-Z]+\s*\|[^|]*\|\s*/, "")
    .replace(/^\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}\s*\[[A-Z]+\]\s*[^|]*\|\s*/, "")
    .replace(/\s+/g, " ")
    .trim();
  if (!compact) return "";
  return `日志: ${clip(compact, 150)}`;
}

export function extractConversationHint(rawLine: string): string {
  const line = String(rawLine || "");
  const fromConversation = line.match(/\|\s*conversation=([^|]+)/)?.[1] || "";
  if (fromConversation.trim()) return fromConversation.trim();
  const fromConversationCn = line.match(/\|\s*会话=([^|]+)/)?.[1] || "";
  if (fromConversationCn.trim()) return fromConversationCn.trim();
  return "";
}

export function decorateThinkingLine(displayLine: string, rawLine: string, selectedConversationId: string): string {
  const base = String(displayLine || "").trim();
  if (!base) return "";
  const conversationHint = extractConversationHint(rawLine);
  if (!conversationHint) return base;
  if (stateBelongsToConversation(conversationHint, selectedConversationId)) return base;
  return `[${conversationHint}] ${base}`;
}

export function thinkingStatusLabel(active: boolean): string {
  return active ? "处理中" : "已完成";
}

export function thinkingStreamLabel(state: "connecting" | "open" | "closed"): string {
  if (state === "open") return "任务流已连接";
  if (state === "connecting") return "连接任务流中";
  return "任务流已断开";
}

export function inferThinkingStage(line: string, active: boolean): ThinkingStage {
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

export function thinkingStageLabel(stage: ThinkingStage): string {
  if (stage === "routing") return "理解问题";
  if (stage === "planning") return "规划策略";
  if (stage === "executing") return "执行动作";
  if (stage === "replying") return "整理回复";
  if (stage === "done") return "任务完成";
  if (stage === "cancelled") return "已中断";
  if (stage === "error") return "异常兜底";
  return "等待任务";
}

export function thinkingStageColor(stage: ThinkingStage): "default" | "primary" | "success" | "warning" | "danger" {
  if (stage === "done") return "success";
  if (stage === "cancelled") return "warning";
  if (stage === "error") return "danger";
  if (stage === "idle") return "default";
  return "primary";
}

export function thinkingStageProgress(stage: ThinkingStage): number {
  if (stage === "idle") return 6;
  if (stage === "routing") return 22;
  if (stage === "planning") return 46;
  if (stage === "executing") return 72;
  if (stage === "replying") return 88;
  if (stage === "done") return 100;
  if (stage === "cancelled") return 100;
  return 100;
}
