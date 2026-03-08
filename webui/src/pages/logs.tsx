import { useEffect, useMemo, useRef, useState, useCallback } from "react";
import { Button, Chip, Input, Switch } from "@heroui/react";
import { Download, Pause, Play, Search, Copy, Trash2, Radio } from "lucide-react";
import { api } from "../api/client";

type LogLevel = "ALL" | "ERROR" | "WARNING" | "INFO" | "DEBUG" | "SUCCESS";
type WsState = "connecting" | "open" | "closed";

const LEVELS: LogLevel[] = ["ALL", "ERROR", "WARNING", "INFO", "DEBUG", "SUCCESS"];
const LEVEL_ORDER: Record<Exclude<LogLevel, "ALL">, number> = {
  ERROR: 5,
  WARNING: 4,
  INFO: 3,
  SUCCESS: 2,
  DEBUG: 1,
};

function parseLevel(line: string): Exclude<LogLevel, "ALL"> {
  const mPipe = line.match(/\|\s*(ERROR|WARNING|INFO|DEBUG|SUCCESS)\s*\|/i);
  if (mPipe) return mPipe[1].toUpperCase() as Exclude<LogLevel, "ALL">;
  const mBracket = line.match(/\[(ERROR|WARNING|INFO|DEBUG|SUCCESS)\]/i);
  if (mBracket) return mBracket[1].toUpperCase() as Exclude<LogLevel, "ALL">;
  return "INFO";
}

function parseTime(line: string): string {
  const m = line.match(/^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(?:,\d{3})?|\d{2}-\d{2} \d{2}:\d{2}:\d{2})/);
  return m ? m[1] : "--:--:--";
}

function stripPrefix(line: string): string {
  return line
    .replace(/^(\d{4}-\d{2}-\d{2}|\d{2}-\d{2}) \d{2}:\d{2}:\d{2}(?:,\d{3})?\s*(\||\[)?\s*/, "")
    .trim();
}

function levelColor(level: Exclude<LogLevel, "ALL">): string {
  switch (level) {
    case "ERROR": return "text-danger";
    case "WARNING": return "text-warning";
    case "DEBUG": return "text-default-400";
    case "SUCCESS": return "text-success";
    default: return "text-foreground";
  }
}

function levelChip(level: Exclude<LogLevel, "ALL">): "danger" | "warning" | "default" | "success" | "primary" {
  switch (level) {
    case "ERROR": return "danger";
    case "WARNING": return "warning";
    case "DEBUG": return "default";
    case "SUCCESS": return "success";
    default: return "primary";
  }
}

export default function LogsPage() {
  const [lines, setLines] = useState<string[]>([]);
  const [filter, setFilter] = useState("");
  const [levelFilter, setLevelFilter] = useState<LogLevel>("ALL");
  const [autoScroll, setAutoScroll] = useState(true);
  const [paused, setPaused] = useState(false);
  const [wsState, setWsState] = useState<WsState>("connecting");
  const containerRef = useRef<HTMLDivElement>(null);
  const bottomRef = useRef<HTMLDivElement>(null);
  const wsRef = useRef<WebSocket | null>(null);
  const reconnectTimerRef = useRef<number | null>(null);
  const programmaticScrollRef = useRef(false);
  const scrollUnlockTimerRef = useRef<number | null>(null);
  const pausedRef = useRef(false);
  const autoScrollRef = useRef(true);

  const normalizeLines = (value: unknown): string[] => {
    if (!Array.isArray(value)) return [];
    return value.filter((item): item is string => typeof item === "string");
  };

  const releaseProgrammaticScroll = () => {
    if (scrollUnlockTimerRef.current != null) {
      window.clearTimeout(scrollUnlockTimerRef.current);
      scrollUnlockTimerRef.current = null;
    }
    scrollUnlockTimerRef.current = window.setTimeout(() => {
      programmaticScrollRef.current = false;
      scrollUnlockTimerRef.current = null;
    }, 120);
  };

  const scrollToBottom = useCallback(() => {
    const el = containerRef.current;
    if (!el) return;
    programmaticScrollRef.current = true;
    el.scrollTop = el.scrollHeight;
    bottomRef.current?.scrollIntoView({ block: "end" });
    releaseProgrammaticScroll();
  }, []);

  const loadHistory = useCallback(async () => {
    try {
      const res = await api.getLogs(800);
      setLines(normalizeLines(res.lines));
      requestAnimationFrame(() => {
        requestAnimationFrame(() => {
          if (autoScrollRef.current) scrollToBottom();
        });
      });
    } catch {
      // ignore
    }
  }, [scrollToBottom]);

  useEffect(() => { loadHistory(); }, [loadHistory]);

  useEffect(() => {
    let disposed = false;

    const clearReconnectTimer = () => {
      if (reconnectTimerRef.current != null) {
        window.clearTimeout(reconnectTimerRef.current);
        reconnectTimerRef.current = null;
      }
    };

    const connect = () => {
      if (disposed || wsRef.current) return;
      setWsState("connecting");
      const ws = new WebSocket(api.logsStreamUrl());
      wsRef.current = ws;

      ws.onopen = () => {
        setWsState("open");
      };

      ws.onmessage = (ev) => {
        if (pausedRef.current) return;
        if (typeof ev.data !== "string") return;
        const payload = ev.data.trim();
        if (!payload) return;

        let incoming: string[] = [];
        try {
          const data = JSON.parse(payload);
          if (typeof data?.line === "string" && data.line.length > 0) {
            incoming = [data.line];
          } else if (Array.isArray(data?.lines)) {
            const payloadLines: unknown[] = data.lines;
            incoming = payloadLines.filter((line): line is string => typeof line === "string" && line.length > 0);
          }
        } catch {
          incoming = payload
            .split(/\r?\n/)
            .map((line) => line.trim())
            .filter((line) => line.length > 0);
        }

        if (incoming.length === 0) return;
        setLines((prev) => {
          const next = [...prev, ...incoming];
          return next.length > 3000 ? next.slice(-2400) : next;
        });
      };

      ws.onerror = () => {
        try {
          ws.close();
        } catch {
          // ignore
        }
      };

      ws.onclose = () => {
        if (wsRef.current === ws) {
          wsRef.current = null;
        }
        setWsState("closed");
        if (disposed) return;
        clearReconnectTimer();
        reconnectTimerRef.current = window.setTimeout(() => {
          reconnectTimerRef.current = null;
          connect();
        }, 1500);
      };
    };

    connect();
    return () => {
      disposed = true;
      clearReconnectTimer();
      if (scrollUnlockTimerRef.current != null) {
        window.clearTimeout(scrollUnlockTimerRef.current);
        scrollUnlockTimerRef.current = null;
      }
      if (wsRef.current) {
        try {
          wsRef.current.close();
        } catch {
          // ignore
        }
        wsRef.current = null;
      }
    };
  }, []);

  const filtered = useMemo(() => {
    const text = filter.trim().toLowerCase();
    return lines.filter((line) => {
      const level = parseLevel(line);
      if (levelFilter !== "ALL" && level !== levelFilter) return false;
      if (!text) return true;
      return line.toLowerCase().includes(text);
    });
  }, [lines, filter, levelFilter]);

  const stats = useMemo(() => {
    const s: Record<Exclude<LogLevel, "ALL">, number> = { ERROR: 0, WARNING: 0, INFO: 0, DEBUG: 0, SUCCESS: 0 };
    lines.forEach((line) => {
      s[parseLevel(line)] += 1;
    });
    return s;
  }, [lines]);

  useEffect(() => {
    if (!autoScrollRef.current || !containerRef.current) return;
    requestAnimationFrame(() => {
      scrollToBottom();
    });
  }, [filtered.length, scrollToBottom]);

  useEffect(() => {
    autoScrollRef.current = autoScroll;
    if (autoScroll) scrollToBottom();
  }, [autoScroll, scrollToBottom]);

  const togglePause = () => {
    setPaused((p) => {
      pausedRef.current = !p;
      return !p;
    });
  };

  const handleScroll = () => {
    if (programmaticScrollRef.current) return;
    const el = containerRef.current;
    if (!el) return;
    const threshold = 24;
    const nearBottom = el.scrollHeight - (el.scrollTop + el.clientHeight) <= threshold;
    if (nearBottom) {
      if (!autoScrollRef.current) {
        autoScrollRef.current = true;
        setAutoScroll(true);
      }
    } else if (autoScrollRef.current) {
      autoScrollRef.current = false;
      setAutoScroll(false);
    }
  };

  const copyVisible = async () => {
    const text = filtered.join("\n");
    await navigator.clipboard.writeText(text).catch(() => {});
  };

  const clearBuffer = () => {
    setLines([]);
  };

  const wsStateText = wsState === "open" ? "实时连接中" : wsState === "connecting" ? "连接中" : "已断开，重连中";
  const wsStateColor = wsState === "open" ? "success" : wsState === "connecting" ? "warning" : "danger";

  return (
    <div className="space-y-3 h-full flex flex-col">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <h2 className="text-xl font-bold">实时日志</h2>
          <Chip size="sm" color={wsStateColor} variant="flat" startContent={<Radio size={12} />}>
            {wsStateText}
          </Chip>
          <Chip size="sm" variant="flat">{filtered.length}/{lines.length}</Chip>
        </div>
        <div className="flex items-center gap-2">
          <Button size="sm" variant="flat" startContent={<Download size={14} />} onPress={loadHistory}>加载历史</Button>
          <Button size="sm" variant="flat" startContent={<Copy size={14} />} onPress={copyVisible}>复制可见</Button>
          <Button size="sm" variant="flat" color="danger" startContent={<Trash2 size={14} />} onPress={clearBuffer}>清空</Button>
        </div>
      </div>

      <div className="rounded-xl border border-default-400/35 bg-content1/40 p-2">
        <div className="flex flex-wrap items-center gap-2">
          <Input
            size="sm"
            placeholder="搜索日志文本..."
            startContent={<Search size={14} />}
            value={filter}
            onValueChange={setFilter}
            className="w-full md:w-72"
            classNames={{
              inputWrapper: "bg-content2/55 border border-default-400/35 data-[focus=true]:border-primary/65",
            }}
          />
          <div className="flex flex-wrap gap-1">
            {LEVELS.map((lv) => (
              <Button
                key={lv}
                size="sm"
                radius="full"
                variant={levelFilter === lv ? "flat" : "light"}
                color={levelFilter === lv ? "primary" : "default"}
                onPress={() => setLevelFilter(lv)}
              >
                {lv === "ALL" ? "全部" : `${lv} (${stats[lv as Exclude<LogLevel, "ALL">] || 0})`}
              </Button>
            ))}
          </div>
          <div className="ml-auto flex items-center gap-2">
            <Button
              size="sm"
              variant="flat"
              startContent={paused ? <Play size={14} /> : <Pause size={14} />}
              onPress={togglePause}
            >
              {paused ? "继续" : "暂停"}
            </Button>
            <Switch size="sm" isSelected={autoScroll} onValueChange={setAutoScroll}>
              自动滚动
            </Switch>
          </div>
        </div>
      </div>

      <div
        ref={containerRef}
        onScroll={handleScroll}
        className="flex-1 min-h-0 bg-content1 border border-default-400/35 rounded-lg overflow-auto font-mono text-xs"
        style={{ height: "calc(100vh - 210px)" }}
      >
        <div className="sticky top-0 z-10 bg-content1/90 backdrop-blur-sm border-b border-default-400/25 px-3 py-2 text-[11px] text-default-500">
          <div className="grid grid-cols-[90px_74px_1fr] gap-3">
            <span>时间</span>
            <span>级别</span>
            <span>内容</span>
          </div>
        </div>
        <div className="p-2 space-y-1">
          {filtered.map((line, i) => {
            const level = parseLevel(line);
            const msg = stripPrefix(line);
            const ts = parseTime(line);
            return (
              <div key={`${i}-${line.slice(0, 12)}`} className="grid grid-cols-[90px_74px_1fr] gap-3 px-2 py-1 rounded-md hover:bg-content2/35 transition-colors">
                <span className="text-default-500">{ts}</span>
                <Chip size="sm" color={levelChip(level)} variant="flat" className="w-fit h-5 min-h-5">{level}</Chip>
                <span className={`${levelColor(level)} whitespace-pre-wrap break-all`}>{msg}</span>
              </div>
            );
          })}
          <div ref={bottomRef} />
          {filtered.length === 0 && (
            <p className="text-default-400 text-center py-10">暂无日志</p>
          )}
        </div>
      </div>
    </div>
  );
}
