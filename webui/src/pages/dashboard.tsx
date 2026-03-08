import { useEffect, useState } from "react";
import { Card, CardBody, Chip, Spinner } from "@heroui/react";
import { motion } from "framer-motion";
import clsx from "clsx";
import {
  Clock, MessageSquare, Bot, Shield, Wrench, Users, Cpu, Puzzle,
} from "lucide-react";
import { api, StatusData } from "../api/client";

function formatUptime(seconds: number): string {
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = seconds % 60;
  if (h > 0) return `${h}h ${m}m`;
  if (m > 0) return `${m}m ${s}s`;
  return `${s}s`;
}

const cardClass = clsx(
  "backdrop-blur-sm border border-white/10 shadow-sm transition-all",
  "bg-content1/60 hover:bg-content1/80 hover:shadow-md"
);

interface StatCardProps {
  icon: React.ReactNode;
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
        <CardBody className="flex flex-row items-center gap-3 py-4 px-4">
          <div className="w-10 h-10 rounded-xl bg-primary/10 flex items-center justify-center text-primary shrink-0">
            {icon}
          </div>
          <div className="min-w-0">
            <p className="text-xs text-default-400 truncate">{label}</p>
            <p className="text-lg font-semibold truncate">{String(value)}</p>
          </div>
        </CardBody>
      </Card>
    </motion.div>
  );
}
export default function DashboardPage() {
  const [data, setData] = useState<StatusData | null>(null);
  const [error, setError] = useState("");

  const fetchStatus = async () => {
    try {
      const res = await api.getStatus();
      setData(res);
      setError("");
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : "获取状态失败");
    }
  };

  useEffect(() => {
    fetchStatus();
    const timer = setInterval(fetchStatus, 10000);
    return () => clearInterval(timer);
  }, []);

  if (error && !data) return <p className="text-danger">{error}</p>;
  if (!data) return <div className="flex justify-center py-20"><Spinner size="lg" /></div>;

  const scaleNames: Record<number, string> = { 1: "宽松", 2: "标准", 3: "严格", 4: "最严" };

  return (
    <section className="space-y-6">
      <motion.div
        initial={{ opacity: 0, y: -10 }}
        animate={{ opacity: 1, y: 0 }}
        className="flex items-center gap-3"
      >
        <div className="h-6 w-1.5 bg-primary rounded-full" />
        <h2 className="text-xl font-bold tracking-wide">{data.bot_name}</h2>
        <Chip size="sm" variant="flat" color="success" className="ml-1">运行中</Chip>
      </motion.div>

      <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
        <StatCard icon={<Clock size={20} />} label="运行时长" value={formatUptime(data.uptime_seconds)} delay={0} />
        <StatCard icon={<MessageSquare size={20} />} label="消息计数" value={data.message_count} delay={0.05} />
        <StatCard icon={<Cpu size={20} />} label="当前模型" value={data.model} delay={0.1} />
        <StatCard icon={<Bot size={20} />} label="Agent 模式" value={data.agent_enabled ? "开启" : "关闭"} delay={0.15} />
        <StatCard icon={<Shield size={20} />} label="安全尺度" value={`${data.safety_scale} (${scaleNames[data.safety_scale] || "?"})`} delay={0.2} />
        <StatCard icon={<Users size={20} />} label="白名单群" value={`${data.whitelist_groups.length} 个`} delay={0.25} />
        <StatCard icon={<Wrench size={20} />} label="可用工具" value={`${data.tool_count} 个`} delay={0.3} />
        <StatCard icon={<Puzzle size={20} />} label="插件" value={`${data.plugins.length} 个`} delay={0.35} />
      </div>

      {data.plugins.length > 0 && (
        <motion.div
          initial={{ opacity: 0, y: 12 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ delay: 0.4 }}
        >
          <Card className={cardClass}>
            <CardBody>
              <p className="text-sm font-semibold mb-3">已加载插件</p>
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
  );
}
