import { Outlet, useNavigate, useLocation } from "react-router-dom";
import { Button } from "@heroui/react";
import { motion, AnimatePresence } from "framer-motion";
import {
  LayoutDashboard, Settings, FileText, Terminal,
  RefreshCw, LogOut, ChevronLeft, ChevronRight, MoonStar, SunMedium, Database, Cookie, Puzzle, Brain, MessageSquare,
  RotateCcw, Menu, X,
} from "lucide-react";
import { api } from "../api/client";
import { useState, useEffect, useCallback } from "react";
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

function useIsMobile(breakpoint = 768) {
  const [isMobile, setIsMobile] = useState(window.innerWidth < breakpoint);
  useEffect(() => {
    const onResize = () => setIsMobile(window.innerWidth < breakpoint);
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, [breakpoint]);
  return isMobile;
}

export default function AppShell() {
  const navigate = useNavigate();
  const location = useLocation();
  const isMobile = useIsMobile();
  const [reloading, setReloading] = useState(false);
  const [restarting, setRestarting] = useState(false);
  const [open, setOpen] = useState(!isMobile);
  const [mobileOpen, setMobileOpen] = useState(false);
  const [theme, setTheme] = useState<"dark" | "light">(
    document.documentElement.classList.contains("dark") ? "dark" : "light",
  );

  // 切换到移动端时自动关闭侧栏
  useEffect(() => {
    if (isMobile) {
      setOpen(false);
      setMobileOpen(false);
    } else {
      setOpen(true);
    }
  }, [isMobile]);

  // 移动端导航后自动关闭抽屉
  useEffect(() => {
    if (isMobile) setMobileOpen(false);
  }, [location.pathname, isMobile]);

  const handleReload = async () => {
    setReloading(true);
    try { await api.reload(); } catch {} finally { setReloading(false); }
  };

  const handleRestart = async () => {
    if (!confirm("确定要重启服务吗？重启期间 bot 将暂时离线。")) return;
    setRestarting(true);
    try {
      await fetch("/api/webui/system/restart", {
        method: "POST",
        headers: { Authorization: `Bearer ${api.getToken?.() || localStorage.getItem("webui_token") || ""}` },
      });
    } catch {}
    setTimeout(() => { window.location.reload(); }, 5000);
  };

  const handleLogout = async () => {
    await api.logout();
    navigate("/login");
  };

  const currentPath = location.pathname;
  const widePages = currentPath.startsWith("/config") || currentPath.startsWith("/database")
    || currentPath.startsWith("/chat") || currentPath.startsWith("/memory");
  const contentMaxWidth = widePages ? "max-w-[1600px]" : "max-w-[1200px]";

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

  const handleNavClick = useCallback((path: string) => {
    navigate(path);
    if (isMobile) setMobileOpen(false);
  }, [navigate, isMobile]);

  // 侧栏是否展示（桌面端用 open，移动端用 mobileOpen）
  const sidebarVisible = isMobile ? mobileOpen : true;
  const sidebarExpanded = isMobile ? true : open;

  const sidebarContent = (
    <div className="flex flex-col h-full p-3">
      {/* Brand */}
      <div className="flex items-center gap-3 px-2 my-4 md:my-6">
        <div className="h-5 w-1 bg-primary rounded-full shadow-sm shrink-0" />
        <AnimatePresence>
          {sidebarExpanded && (
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
        {/* 移动端关闭按钮 */}
        {isMobile && (
          <Button
            isIconOnly
            variant="light"
            radius="full"
            size="sm"
            className="ml-auto"
            aria-label="关闭菜单"
            onPress={() => setMobileOpen(false)}
          >
            <X size={18} />
          </Button>
        )}
      </div>

      {/* Nav items */}
      <nav className="flex flex-col gap-1.5 flex-1 overflow-y-auto" aria-label="主导航">
        {NAV_ITEMS.map((item) => {
          const active = item.path === "/" ? currentPath === "/" : currentPath.startsWith(item.path);
          const Icon = item.icon;
          return (
            <Button
              key={item.path}
              variant={active ? "flat" : "light"}
              color={active ? "primary" : "default"}
              radius="lg"
              aria-label={item.label}
              aria-current={active ? "page" : undefined}
              className={clsx(
                "justify-start gap-3 h-11 font-medium transition-all",
                active
                  ? "bg-primary/15 text-primary shadow-sm"
                  : "text-default-600 hover:bg-default-100/80",
                !sidebarExpanded && "justify-center px-0"
              )}
              onPress={() => handleNavClick(item.path)}
            >
              <Icon size={20} className="shrink-0" />
              {sidebarExpanded && <span className="truncate">{item.label}</span>}
            </Button>
          );
        })}
      </nav>

      {/* Bottom actions */}
      <div className="space-y-1.5 mt-auto">
        <Button
          variant="flat"
          radius="lg"
          aria-label={theme === "dark" ? "切到浅色" : "切到深色"}
          className={clsx(
            "w-full justify-start gap-3 h-10 font-medium",
            "bg-default-100/60 dark:bg-default-100/20",
            "text-default-700 dark:text-default-300",
            "border border-default-300/40 dark:border-default-400/30",
            "shadow-sm hover:shadow-md transition-all backdrop-blur-sm",
            !sidebarExpanded && "justify-center px-0"
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
          {sidebarExpanded && (theme === "dark" ? "切到浅色" : "切到深色")}
        </Button>
        <Button
          variant="flat"
          radius="lg"
          className={clsx(
            "w-full justify-start gap-3 h-10 font-medium",
            "bg-primary-50/50 hover:bg-primary-100/80 text-primary-600",
            "shadow-sm hover:shadow-md transition-all backdrop-blur-sm",
            !sidebarExpanded && "justify-center px-0"
          )}
          isLoading={reloading}
          onPress={handleReload}
        >
          <RefreshCw size={18} className="shrink-0" />
          {sidebarExpanded && "热重载"}
        </Button>
        <Button
          variant="flat"
          radius="lg"
          className={clsx(
            "w-full justify-start gap-3 h-10 font-medium",
            "bg-warning-50/50 hover:bg-warning-100/80 text-warning-600",
            "shadow-sm hover:shadow-md transition-all backdrop-blur-sm",
            !sidebarExpanded && "justify-center px-0"
          )}
          isLoading={restarting}
          onPress={handleRestart}
        >
          <RotateCcw size={18} className="shrink-0" />
          {sidebarExpanded && "重启服务"}
        </Button>
        <Button
          variant="flat"
          radius="lg"
          className={clsx(
            "w-full justify-start gap-3 h-10 font-medium",
            "bg-danger-50/50 hover:bg-danger-100/80 text-danger-500",
            "shadow-sm hover:shadow-md transition-all backdrop-blur-sm",
            !sidebarExpanded && "justify-center px-0"
          )}
          onPress={() => { void handleLogout(); }}
        >
          <LogOut size={18} className="shrink-0" />
          {sidebarExpanded && "退出登录"}
        </Button>
      </div>

      {/* Collapse toggle — 仅桌面端显示 */}
      {!isMobile && (
        <Button
          isIconOnly
          variant="light"
          radius="full"
          size="sm"
          className="mx-auto mt-3"
          aria-label={open ? "收起侧栏" : "展开侧栏"}
          onPress={() => setOpen(!open)}
        >
          {open ? <ChevronLeft size={16} /> : <ChevronRight size={16} />}
        </Button>
      )}
    </div>
  );

  return (
    <div className="flex h-dvh items-stretch overflow-hidden">
      {/* 移动端顶栏 */}
      {isMobile && (
        <div className="fixed top-0 left-0 right-0 z-40 flex items-center h-14 px-4 bg-content1/80 backdrop-blur-xl border-b border-default-300/30">
          <Button
            isIconOnly
            variant="light"
            radius="full"
            size="sm"
            aria-label="打开菜单"
            onPress={() => setMobileOpen(true)}
          >
            <Menu size={20} />
          </Button>
          <span className="ml-3 text-lg font-bold tracking-wide select-none">YuKiKo</span>
        </div>
      )}

      {/* 移动端抽屉遮罩 */}
      <AnimatePresence>
        {isMobile && mobileOpen && (
          <motion.div
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            className="fixed inset-0 z-50 bg-black/40 backdrop-blur-sm"
            onClick={() => setMobileOpen(false)}
          />
        )}
      </AnimatePresence>

      {/* Sidebar */}
      {isMobile ? (
        <AnimatePresence>
          {mobileOpen && (
            <motion.aside
              initial={{ x: "-100%" }}
              animate={{ x: 0 }}
              exit={{ x: "-100%" }}
              transition={{ type: "spring", stiffness: 300, damping: 30 }}
              className={clsx(
                "fixed top-0 left-0 bottom-0 z-50 w-[16rem]",
                "flex flex-col h-full overflow-hidden",
                "bg-content1/95 backdrop-blur-xl backdrop-saturate-150",
                "border-r border-default-300/30 shadow-2xl"
              )}
            >
              {sidebarContent}
            </motion.aside>
          )}
        </AnimatePresence>
      ) : (
        <motion.aside
          animate={{ width: open ? "15rem" : "4.5rem" }}
          transition={{ type: "spring", stiffness: 200, damping: 24 }}
          className={clsx(
            "flex flex-col h-full overflow-hidden shrink-0",
            "bg-content1/70 backdrop-blur-xl backdrop-saturate-150",
            "border-r border-default-300/30 shadow-xl"
          )}
        >
          {sidebarContent}
        </motion.aside>
      )}

      {/* Main content */}
      <motion.main
        layout
        className={clsx("flex-1 overflow-y-auto", isMobile && "pt-14")}
        initial={{ opacity: 0, scale: 0.98 }}
        animate={{ opacity: 1, scale: 1 }}
        transition={{ duration: 0.3 }}
      >
        <div className={`w-full ${contentMaxWidth} mx-auto p-3 sm:p-4 md:p-6`}>
          <Outlet />
        </div>
      </motion.main>
    </div>
  );
}
