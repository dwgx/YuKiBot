import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { Button, Card, CardBody, Chip, Spinner, Switch } from "@heroui/react";
import { motion } from "framer-motion";
import clsx from "clsx";
import {
  Bot,
  Clock,
  CloudDownload,
  Cpu,
  FileText,
  GitBranch,
  Github,
  MessageSquare,
  Puzzle,
  RefreshCw,
  Settings,
  Shield,
  Terminal,
  Users,
  Wrench,
} from "lucide-react";
import { api, StatusData, SystemUpdateStatus, SystemUpdateTask } from "../api/client";
import { NotificationContainer } from "../components/notification";
import { useNotifications } from "../hooks/useNotifications";

function formatUptime(seconds: number): string {
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = seconds % 60;
  if (h > 0) return `${h}h ${m}m`;
  if (m > 0) return `${m}m ${s}s`;
  return `${s}s`;
}

const UPDATE_STAGE_LABEL: Record<string, string> = {
  queued: "已排队",
  checking: "检查仓库状态",
  pulling: "拉取 GitHub 代码",
  pip_install: "同步 Python 依赖",
  npm_install: "安装前端依赖",
  npm_build: "构建前端资源",
  finalize: "刷新更新状态",
  completed: "更新完成",
  failed: "更新失败",
};

function formatUpdateStage(stage: string): string {
  const safe = (stage || "").trim().toLowerCase();
  if (!safe) return "等待开始";
  return UPDATE_STAGE_LABEL[safe] || safe;
}

const cardClass = clsx(
  "backdrop-blur-sm border border-white/10 shadow-sm transition-all",
  "bg-content1/60 hover:bg-content1/80 hover:shadow-md",
);

interface StatCardProps {
  icon: ReactNode;
  label: string;
  value: string | number;
  delay?: number;
}

function StatCard({ icon, label, value, delay = 0 }: StatCardProps) {
  return (
    <motion.div
      initial={{ opacity: 0, y: 12 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ delay, duration: 0.3, type: "spring", stiffness: 150 }}
    >
      <Card className={cardClass}>
        <CardBody className="flex flex-row items-center gap-3 px-4 py-4">
          <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-xl bg-primary/10 text-primary">
            {icon}
          </div>
          <div className="min-w-0">
            <p className="truncate text-xs text-default-400">{label}</p>
            <p className="truncate text-lg font-semibold">{String(value)}</p>
          </div>
        </CardBody>
      </Card>
    </motion.div>
  );
}

export default function DashboardPage() {
  const { notifications, info, success, danger } = useNotifications();
  const [data, setData] = useState<StatusData | null>(null);
  const [statusError, setStatusError] = useState("");
  const [updateInfo, setUpdateInfo] = useState<SystemUpdateStatus | null>(null);
  const [updateError, setUpdateError] = useState("");
  const [checkingUpdate, setCheckingUpdate] = useState(false);
  const [runningUpdate, setRunningUpdate] = useState(false);
  const [updateTaskId, setUpdateTaskId] = useState("");
  const [updateTask, setUpdateTask] = useState<SystemUpdateTask | null>(null);
  const [updateLogs, setUpdateLogs] = useState<string[]>([]);
  const [allowDirty, setAllowDirty] = useState(false);
  const stageNoticeRef = useRef("");
  const finalNoticeRef = useRef("");

  const fetchStatus = useCallback(async () => {
    try {
      const res = await api.getStatus();
      setData(res);
      setStatusError("");
    } catch (e: unknown) {
      setStatusError(e instanceof Error ? e.message : "获取状态失败");
    }
  }, []);

  const fetchUpdateStatus = useCallback(async () => {
    setCheckingUpdate(true);
    try {
      const res = await api.getSystemUpdateStatus();
      setUpdateInfo(res.status);
      setUpdateLogs(res.status.logs || []);
      setUpdateError("");
    } catch (e: unknown) {
      setUpdateError(e instanceof Error ? e.message : "获取更新状态失败");
    } finally {
      setCheckingUpdate(false);
    }
  }, []);

  const pollUpdateTask = useCallback(async (taskId: string) => {
    const safeTaskId = taskId.trim();
    if (!safeTaskId) return;

    try {
      const res = await api.getSystemUpdateTask(safeTaskId);
      const task = res.task;
      setUpdateTask(task);
      setUpdateLogs(task.logs || []);
      if (task.result?.status) {
        setUpdateInfo(task.result.status);
      }

      const stage = (task.stage || "").trim().toLowerCase();
      if (task.status === "running") {
        if (stage && stage !== stageNoticeRef.current) {
          stageNoticeRef.current = stage;
          info("更新进度", `${formatUpdateStage(stage)} (${task.progress || 0}%)`, 2200);
        }
        return;
      }

      if (finalNoticeRef.current === safeTaskId) {
        return;
      }
      finalNoticeRef.current = safeTaskId;
      setRunningUpdate(false);

      if (task.status === "done") {
        setUpdateError("");
        success("更新完成", task.result?.restart_hint || "更新流程已完成", 8000);
      } else {
        const message = task.error || task.result?.message || "执行更新失败";
        setUpdateError(message);
        danger("更新失败", message, 8000);
      }
      void fetchUpdateStatus();
    } catch (e: unknown) {
      const message = e instanceof Error ? e.message : "获取更新进度失败";
      setUpdateError(message);
      setRunningUpdate(false);
      danger("更新中断", message, 6500);
      void fetchUpdateStatus();
    }
  }, [danger, fetchUpdateStatus, info, success]);

  useEffect(() => {
    void fetchStatus();
    if (!runningUpdate) {
      void fetchUpdateStatus();
    }
    const timer = setInterval(() => {
      void fetchStatus();
      if (!runningUpdate) {
        void fetchUpdateStatus();
      }
    }, 15000);
    return () => clearInterval(timer);
  }, [fetchStatus, fetchUpdateStatus, runningUpdate]);

  useEffect(() => {
    if (!runningUpdate || !updateTaskId) return;
    const timer = window.setInterval(() => {
      void pollUpdateTask(updateTaskId);
    }, 1500);
    return () => window.clearInterval(timer);
  }, [pollUpdateTask, runningUpdate, updateTaskId]);

  const runLatestUpdate = useCallback(async () => {
    setRunningUpdate(true);
    setUpdateError("");
    setUpdateLogs([]);
    setUpdateTask(null);
    stageNoticeRef.current = "";
    finalNoticeRef.current = "";
    try {
      const res = await api.startSystemUpdate({
        syncPython: true,
        buildWebui: true,
        allowDirty,
      });
      setUpdateTaskId(res.task_id);
      setUpdateTask(res.task);
      setUpdateLogs(res.task.logs || []);
      info(
        "更新已启动",
        res.reused ? "检测到已有更新任务，已切换到该任务的实时进度。" : "正在执行更新，会实时显示阶段和日志。",
        4000,
      );
      await pollUpdateTask(res.task_id);
    } catch (e: unknown) {
      const message = e instanceof Error ? e.message : "启动更新失败";
      setUpdateError(message);
      setRunningUpdate(false);
      danger("更新失败", message, 6500);
      void fetchUpdateStatus();
    }
  }, [allowDirty, danger, fetchUpdateStatus, info, pollUpdateTask]);

  if (statusError && !data) {
    return (
      <div className="flex flex-col items-center gap-4 py-20">
        <p className="text-danger text-center">{statusError}</p>
        <Button color="primary" variant="flat" onPress={() => void fetchStatus()}>重试</Button>
      </div>
    );
  }
  if (!data) {
    return <div className="flex justify-center py-20"><Spinner size="lg" /></div>;
  }

  const scaleNames: Record<number, string> = { 1: "宽松", 2: "标准", 3: "严格", 4: "最严" };
  const updateStageLabel = formatUpdateStage(updateTask?.stage || "");
  const updateProgress = (() => {
    const value = Number(updateTask?.progress || 0);
    if (!Number.isFinite(value)) return 0;
    return Math.max(0, Math.min(100, Math.round(value)));
  })();

  return (
    <>
      <NotificationContainer notifications={notifications} />
      <section className="space-y-6">
        <motion.div
          initial={{ opacity: 0, y: -10 }}
          animate={{ opacity: 1, y: 0 }}
          className="flex items-center gap-3"
        >
          <div className="h-6 w-1.5 rounded-full bg-primary" />
          <h2 className="text-xl font-bold tracking-wide">{data.bot_name}</h2>
          <Chip size="sm" variant="flat" color="success" className="ml-1">运行中</Chip>
          <Chip
            size="sm"
            variant="flat"
            color={data.queue.multi_conversation_enabled ? "primary" : "warning"}
          >
            {data.queue.multi_conversation_enabled ? "多会话并发开启" : "会话串行受限"}
          </Chip>
        </motion.div>

        <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
          <StatCard icon={<Clock size={20} />} label="运行时长" value={formatUptime(data.uptime_seconds)} delay={0} />
          <StatCard icon={<MessageSquare size={20} />} label="消息计数" value={data.message_count} delay={0.05} />
          <StatCard icon={<Cpu size={20} />} label="当前模型" value={data.model} delay={0.1} />
          <StatCard icon={<Bot size={20} />} label="Agent 模式" value={data.agent_enabled ? "开启" : "关闭"} delay={0.15} />
          <StatCard icon={<Shield size={20} />} label="安全尺度" value={`${data.safety_scale} (${scaleNames[data.safety_scale] || "?"})`} delay={0.2} />
          <StatCard icon={<Users size={20} />} label="白名单群" value={`${data.whitelist_groups.length} 个`} delay={0.25} />
          <StatCard icon={<Wrench size={20} />} label="可用工具" value={`${data.tool_count} 个`} delay={0.3} />
          <StatCard icon={<Puzzle size={20} />} label="活跃 AI 会话" value={data.queue.active_conversations} delay={0.35} />
        </div>

        <div className="grid gap-4 xl:grid-cols-[1.35fr,1fr]">
          <motion.div
            initial={{ opacity: 0, y: 12 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ delay: 0.25 }}
          >
            <Card className={cardClass}>
              <CardBody className="space-y-4 p-5">
                <div className="flex flex-wrap items-center justify-between gap-3">
                  <div>
                    <div className="flex items-center gap-2">
                      <Github size={18} className="text-default-600" />
                      <p className="text-sm font-semibold">GitHub 最新版本更新</p>
                    </div>
                    <p className="text-xs text-default-500">
                      WebUI 只拉当前仓库上游的最新代码，不允许切换到别的分支或别的源。
                    </p>
                  </div>
                  <div className="flex items-center gap-2">
                    <Button
                      variant="flat"
                      startContent={<RefreshCw size={16} />}
                      onPress={() => void fetchUpdateStatus()}
                      isLoading={checkingUpdate}
                    >
                      检查更新
                    </Button>
                    <Button
                      color="primary"
                      startContent={<CloudDownload size={16} />}
                      onPress={() => void runLatestUpdate()}
                      isLoading={runningUpdate}
                    >
                      拉取最新
                    </Button>
                  </div>
                </div>

                {updateInfo?.dirty && (
                  <div className="flex items-center gap-3 rounded-2xl border border-warning/40 bg-warning/5 px-4 py-3">
                    <Switch
                      size="sm"
                      color="warning"
                      isSelected={allowDirty}
                      onValueChange={setAllowDirty}
                    />
                    <div className="min-w-0">
                      <p className="text-sm font-medium text-warning-700">允许 dirty 更新</p>
                      <p className="text-xs text-default-500">工作区有未提交改动，开启后将跳过 dirty 检查强制拉取</p>
                    </div>
                  </div>
                )}

                <div className="grid gap-3 md:grid-cols-2">
                  <div className="rounded-2xl border border-default-200/60 bg-content2/40 p-4">
                    <div className="flex items-center gap-2 text-sm font-semibold">
                      <GitBranch size={16} />
                      当前仓库
                    </div>
                    <p className="mt-3 text-xs text-default-500">分支：{updateInfo?.branch || "-"}</p>
                    <p className="mt-1 text-xs text-default-500">上游：{updateInfo?.upstream || "-"}</p>
                    <p className="mt-1 text-xs text-default-500">
                      本地：{updateInfo?.local_commit || "-"} / 远端：{updateInfo?.remote_commit || "-"}
                    </p>
                  </div>
                  <div className="rounded-2xl border border-default-200/60 bg-content2/40 p-4">
                    <p className="text-sm font-semibold">更新状态</p>
                    <div className="mt-3 flex flex-wrap gap-2">
                      <Chip size="sm" variant="flat" color={(updateInfo?.behind || 0) > 0 ? "warning" : "success"}>
                        落后 {updateInfo?.behind || 0}
                      </Chip>
                      <Chip size="sm" variant="flat" color={(updateInfo?.ahead || 0) > 0 ? "secondary" : "default"}>
                        超前 {updateInfo?.ahead || 0}
                      </Chip>
                      <Chip size="sm" variant="flat" color={updateInfo?.dirty ? "danger" : "success"}>
                        {updateInfo?.dirty ? "工作区有改动" : "工作区干净"}
                      </Chip>
                      {updateTask && (
                        <Chip
                          size="sm"
                          variant="flat"
                          color={
                            runningUpdate
                              ? "primary"
                              : updateTask.status === "failed"
                                ? "danger"
                                : "success"
                          }
                        >
                          {runningUpdate ? `任务进行中 ${updateProgress}%` : updateTask.status === "failed" ? "任务失败" : "任务完成"}
                        </Chip>
                      )}
                    </div>
                    {updateTask && (
                      <p className="mt-3 text-xs text-default-500">
                        阶段：{updateStageLabel} · 任务ID：{updateTask.task_id.slice(0, 8)}
                      </p>
                    )}
                    <p className="mt-3 text-xs text-default-500">
                      {updateError || updateInfo?.message || "等待检查"}
                    </p>
                  </div>
                </div>

                <div className="rounded-2xl border border-warning/40 bg-warning/5 p-4">
                  <p className="text-sm font-semibold text-warning-700">更新后提示</p>
                  <p className="mt-2 text-xs text-default-600">
                    WebUI 会拉代码、同步 Python 依赖、重建前端，但 Python 新代码要在你手动重启服务后才会完整生效。
                  </p>
                </div>

                {updateLogs.length > 0 && (
                  <div className="rounded-2xl border border-default-200/60 bg-content2/50 p-4">
                    <p className="text-sm font-semibold">{runningUpdate ? "实时更新输出" : "最近一次更新输出"}</p>
                    <pre className="mt-3 max-h-56 overflow-auto whitespace-pre-wrap break-words text-xs text-default-600">
                      {updateLogs.join("\n\n")}
                    </pre>
                  </div>
                )}
              </CardBody>
            </Card>
          </motion.div>

          <div className="space-y-4">
            <motion.div
              initial={{ opacity: 0, y: 12 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ delay: 0.3 }}
            >
              <Card className={cardClass}>
                <CardBody className="space-y-4 p-5">
                  <div>
                    <p className="text-sm font-semibold">快捷导航</p>
                    <p className="text-xs text-default-500">常用管理页面入口</p>
                  </div>
                  <div className="grid gap-2 sm:grid-cols-2">
                    <Button
                      variant="flat"
                      startContent={<Settings size={16} />}
                      onPress={() => window.location.assign("/webui/config")}
                    >
                      配置编辑
                    </Button>
                    <Button
                      variant="flat"
                      startContent={<Puzzle size={16} />}
                      onPress={() => window.location.assign("/webui/plugins")}
                    >
                      插件管理
                    </Button>
                    <Button
                      variant="flat"
                      startContent={<FileText size={16} />}
                      onPress={() => window.location.assign("/webui/prompts")}
                    >
                      提示词编辑
                    </Button>
                    <Button
                      variant="flat"
                      startContent={<Terminal size={16} />}
                      onPress={() => window.location.assign("/webui/logs")}
                    >
                      实时日志
                    </Button>
                  </div>
                </CardBody>
              </Card>
            </motion.div>

            <motion.div
              initial={{ opacity: 0, y: 12 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ delay: 0.35 }}
            >
              <Card className={cardClass}>
                <CardBody className="space-y-4 p-5">
                  <div>
                    <p className="text-sm font-semibold">队列与路由</p>
                    <p className="text-xs text-default-500">当前消息处理管线状态</p>
                  </div>
                  <div className="grid gap-3 sm:grid-cols-2">
                    <div className="rounded-2xl border border-default-200/60 bg-content2/40 p-3">
                      <p className="text-xs text-default-400">并发槽位</p>
                      <p className="mt-1 text-lg font-semibold">{data.queue.group_concurrency}</p>
                    </div>
                    <div className="rounded-2xl border border-default-200/60 bg-content2/40 p-3">
                      <p className="text-xs text-default-400">活跃会话</p>
                      <p className="mt-1 text-lg font-semibold">{data.queue.active_conversations}</p>
                    </div>
                  </div>
                  <div className="flex flex-wrap gap-2">
                    <Chip size="sm" variant="flat" color={data.queue.multi_conversation_enabled ? "success" : "warning"}>
                      {data.queue.multi_conversation_enabled ? "多会话并发" : "串行模式"}
                    </Chip>
                    <Chip size="sm" variant="flat" color={data.agent_enabled ? "success" : "default"}>
                      Agent {data.agent_enabled ? "开启" : "关闭"}
                    </Chip>
                    <Chip size="sm" variant="flat" color="primary">
                      安全尺度 {data.safety_scale}
                    </Chip>
                  </div>
                </CardBody>
              </Card>
            </motion.div>
          </div>
        </div>

        {data.plugins.length > 0 && (
          <motion.div
            initial={{ opacity: 0, y: 12 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ delay: 0.4 }}
          >
            <Card className={cardClass}>
              <CardBody>
                <p className="mb-3 text-sm font-semibold">已加载插件</p>
                <div className="flex flex-wrap gap-2">
                  {data.plugins.map((p) => (
                    <Chip key={p.name} size="sm" variant="flat" color="primary">{p.name}</Chip>
                  ))}
                </div>
              </CardBody>
            </Card>
          </motion.div>
        )}
      </section>
    </>
  );
}
