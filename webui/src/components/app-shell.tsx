import { Outlet, useNavigate, useLocation } from "react-router-dom";
import { Button } from "@heroui/react";
import { motion, AnimatePresence } from "framer-motion";
import {
  LayoutDashboard, Settings, FileText, Terminal,
  RefreshCw, LogOut, ChevronLeft, ChevronRight, MoonStar, SunMedium, Database, Cookie, Puzzle, Brain, MessageSquare,
} from "lucide-react";
import { api } from "../api/client";
import { useState } from "react";
import clsx from "clsx";

const NAV_ITEMS = [
  { path: "/", icon: LayoutDashboard, label: "仪表盘" },
  { path: "/config", icon: Settings, label: "配置" },
  { path: "/prompts", icon: FileText, label: "提示词" },
  { path: "/plugins", icon: Puzzle, label: "插件" },
  { path: "/logs", icon: Terminal, label: "日志" },
  { path: "/database", icon: Database, label: "数据库" },
  { path: "/chat", icon: MessageSquare, label: "聊天控制台" },
  { path: "/memory", icon: Brain, label: "记忆库" },
  { path: "/cookies", icon: Cookie, label: "Cookie" },
];

export default function AppShell() {
  const navigate = useNavigate();
  const location = useLocation();
  const [reloading, setReloading] = useState(false);
  const [open, setOpen] = useState(true);
  const [theme, setTheme] = useState<"dark" | "light">(
    document.documentElement.classList.contains("dark") ? "dark" : "light",
  );

  const handleReload = async () => {
    setReloading(true);
    try { await api.reload(); } catch {} finally { setReloading(false); }
  };

  const handleLogout = () => { api.clearToken(); navigate("/login"); };
  const currentPath = location.pathname;
  const contentMaxWidth = (currentPath.startsWith("/config") || currentPath.startsWith("/database") || currentPath.startsWith("/chat"))
    || currentPath.startsWith("/memory")
    ? "max-w-[1600px]"
    : "max-w-[1000px]";

  const applyTheme = (nextTheme: "dark" | "light", animate = true) => {
    const root = document.documentElement;
    if (animate) {
      root.classList.add("theme-animating");
      window.setTimeout(() => root.classList.remove("theme-animating"), 320);
    }
    root.classList.toggle("dark", nextTheme === "dark");
    root.setAttribute("data-theme", nextTheme);
    localStorage.setItem("yukiko_theme", nextTheme);
    setTheme(nextTheme);
  };

  const handleThemeToggle = () => {
    applyTheme(theme === "dark" ? "light" : "dark", true);
  };

  return (
    <div className="flex h-screen items-stretch overflow-hidden">
      {/* Sidebar */}
      <motion.aside
        animate={{ width: open ? "15rem" : "4.5rem" }}
        transition={{ type: "spring", stiffness: 200, damping: 24 }}
        className={clsx(
          "flex flex-col h-full overflow-hidden shrink-0",
          "bg-content1/70 backdrop-blur-xl backdrop-saturate-150",
          "border-r border-default-300/30 shadow-xl"
        )}
      >
        <div className="flex flex-col h-full p-3">
          {/* Brand */}
          <div className="flex items-center gap-3 px-2 my-6">
            <div className="h-5 w-1 bg-primary rounded-full shadow-sm shrink-0" />
            <AnimatePresence>
              {open && (
                <motion.span
                  initial={{ opacity: 0, x: -10 }}
                  animate={{ opacity: 1, x: 0 }}
                  exit={{ opacity: 0, x: -10 }}
                  className="text-xl font-bold tracking-wide select-none whitespace-nowrap"
                >
                  YuKiKo
                </motion.span>
              )}
            </AnimatePresence>
          </div>

          {/* Nav items */}
          <nav className="flex flex-col gap-1.5 flex-1">
            {NAV_ITEMS.map((item) => {
              const active = item.path === "/" ? currentPath === "/" : currentPath.startsWith(item.path);
              const Icon = item.icon;
              return (
                <Button
                  key={item.path}
                  variant={active ? "flat" : "light"}
                  color={active ? "primary" : "default"}
                  radius="lg"
                  className={clsx(
                    "justify-start gap-3 h-11 font-medium transition-all",
                    active
                      ? "bg-primary/15 text-primary shadow-sm"
                      : "text-default-600 hover:bg-default-100/80",
                    !open && "justify-center px-0"
                  )}
                  onPress={() => navigate(item.path)}
                >
                  <Icon size={20} className="shrink-0" />
                  {open && <span className="truncate">{item.label}</span>}
                </Button>
              );
            })}
          </nav>

          {/* Bottom actions */}
          <div className="space-y-1.5 mt-auto">
            <Button
              variant="flat"
              radius="lg"
              className={clsx(
                "w-full justify-start gap-3 h-10 font-medium",
                "bg-default-100/60 dark:bg-default-100/20",
                "text-default-700 dark:text-default-300",
                "border border-default-300/40 dark:border-default-400/30",
                "shadow-sm hover:shadow-md transition-all backdrop-blur-sm",
                !open && "justify-center px-0"
              )}
              onPress={handleThemeToggle}
            >
              <motion.span
                key={theme}
                initial={{ rotate: -20, opacity: 0.5, scale: 0.9 }}
                animate={{ rotate: 0, opacity: 1, scale: 1 }}
                transition={{ duration: 0.2 }}
                className="inline-flex"
              >
                {theme === "dark" ? <SunMedium size={18} className="shrink-0" /> : <MoonStar size={18} className="shrink-0" />}
              </motion.span>
              {open && (theme === "dark" ? "切到浅色" : "切到深色")}
            </Button>
            <Button
              variant="flat"
              radius="lg"
              className={clsx(
                "w-full justify-start gap-3 h-10 font-medium",
                "bg-primary-50/50 hover:bg-primary-100/80 text-primary-600",
                "shadow-sm hover:shadow-md transition-all backdrop-blur-sm",
                !open && "justify-center px-0"
              )}
              isLoading={reloading}
              onPress={handleReload}
            >
              <RefreshCw size={18} className="shrink-0" />
              {open && "热重载"}
            </Button>
            <Button
              variant="flat"
              radius="lg"
              className={clsx(
                "w-full justify-start gap-3 h-10 font-medium",
                "bg-danger-50/50 hover:bg-danger-100/80 text-danger-500",
                "shadow-sm hover:shadow-md transition-all backdrop-blur-sm",
                !open && "justify-center px-0"
              )}
              onPress={handleLogout}
            >
              <LogOut size={18} className="shrink-0" />
              {open && "退出登录"}
            </Button>
          </div>

          {/* Collapse toggle */}
          <Button
            isIconOnly
            variant="light"
            radius="full"
            size="sm"
            className="mx-auto mt-3"
            onPress={() => setOpen(!open)}
          >
            {open ? <ChevronLeft size={16} /> : <ChevronRight size={16} />}
          </Button>
        </div>
      </motion.aside>

      {/* Main content */}
      <motion.main
        layout
        className="flex-1 overflow-y-auto"
        initial={{ opacity: 0, scale: 0.98 }}
        animate={{ opacity: 1, scale: 1 }}
        transition={{ duration: 0.3 }}
      >
        <div className={`w-full ${contentMaxWidth} mx-auto p-4 md:p-6`}>
          <Outlet />
        </div>
      </motion.main>
    </div>
  );
}
