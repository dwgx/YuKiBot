import { AnimatePresence, motion } from "framer-motion";
import { AlertTriangle, CheckCircle, Info, X, XCircle } from "lucide-react";
import { Button } from "@heroui/react";

export type NotificationType = "info" | "success" | "warning" | "danger";

export interface NotificationProps {
  id: string;
  type: NotificationType;
  title: string;
  message: string;
  confirmText?: string;
  cancelText?: string;
  onConfirm?: () => void;
  onCancel?: () => void;
  onClose?: () => void;
  autoClose?: number;
}

const NOTIFICATION_ICONS = {
  info: Info,
  success: CheckCircle,
  warning: AlertTriangle,
  danger: XCircle,
};

const NOTIFICATION_COLORS = {
  info: {
    bg: "bg-blue-50 dark:bg-blue-950/30",
    border: "border-blue-200 dark:border-blue-800",
    icon: "text-blue-600 dark:text-blue-400",
    title: "text-blue-900 dark:text-blue-100",
  },
  success: {
    bg: "bg-green-50 dark:bg-green-950/30",
    border: "border-green-200 dark:border-green-800",
    icon: "text-green-600 dark:text-green-400",
    title: "text-green-900 dark:text-green-100",
  },
  warning: {
    bg: "bg-yellow-50 dark:bg-yellow-950/30",
    border: "border-yellow-200 dark:border-yellow-800",
    icon: "text-yellow-600 dark:text-yellow-400",
    title: "text-yellow-900 dark:text-yellow-100",
  },
  danger: {
    bg: "bg-red-50 dark:bg-red-950/30",
    border: "border-red-200 dark:border-red-800",
    icon: "text-red-600 dark:text-red-400",
    title: "text-red-900 dark:text-red-100",
  },
};

export function Notification({
  id,
  type,
  title,
  message,
  confirmText = "确认",
  cancelText = "取消",
  onConfirm,
  onCancel,
  onClose,
  autoClose,
}: NotificationProps) {
  const Icon = NOTIFICATION_ICONS[type];
  const colors = NOTIFICATION_COLORS[type];
  const hasActions = Boolean(onConfirm || onCancel);

  return (
    <motion.div
      layout
      initial={{ opacity: 0, y: -100, scale: 0.9 }}
      animate={{ opacity: 1, y: 0, scale: 1 }}
      exit={{ opacity: 0, y: -50, scale: 0.95 }}
      transition={{
        type: "spring",
        stiffness: 400,
        damping: 30,
        mass: 0.8,
      }}
      className={`relative w-full max-w-md overflow-hidden rounded-2xl border ${colors.border} ${colors.bg} shadow-xl backdrop-blur-xl`}
    >
      <div className="flex items-start gap-3 p-4">
        <div className={`flex h-10 w-10 shrink-0 items-center justify-center rounded-full ${colors.bg} ring-2 ${colors.border}`}>
          <Icon size={20} className={colors.icon} />
        </div>
        <div className="min-w-0 flex-1">
          <h3 className={`text-sm font-semibold ${colors.title}`}>{title}</h3>
          <p className="mt-1 text-xs text-default-600">{message}</p>
          {hasActions && (
            <div className="mt-3 flex items-center gap-2">
              {onConfirm && (
                <Button
                  size="sm"
                  color={type === "danger" ? "danger" : "primary"}
                  onPress={() => {
                    onConfirm();
                    onClose?.();
                  }}
                >
                  {confirmText}
                </Button>
              )}
              {onCancel && (
                <Button
                  size="sm"
                  variant="flat"
                  onPress={() => {
                    onCancel();
                    onClose?.();
                  }}
                >
                  {cancelText}
                </Button>
              )}
            </div>
          )}
        </div>
        {onClose && (
          <button
            onClick={onClose}
            className="flex h-6 w-6 shrink-0 items-center justify-center rounded-full text-default-400 transition-colors hover:bg-default-200 hover:text-default-600"
          >
            <X size={14} />
          </button>
        )}
      </div>
      {autoClose && (
        <motion.div
          initial={{ scaleX: 1 }}
          animate={{ scaleX: 0 }}
          transition={{ duration: autoClose / 1000, ease: "linear" }}
          className={`h-1 origin-left ${type === "danger" ? "bg-red-500" : "bg-primary"}`}
        />
      )}
    </motion.div>
  );
}

export interface NotificationContainerProps {
  notifications: NotificationProps[];
}

export function NotificationContainer({ notifications }: NotificationContainerProps) {
  return (
    <div className="pointer-events-none fixed inset-x-0 top-0 z-[100] flex flex-col items-center gap-3 p-4">
      <AnimatePresence mode="popLayout">
        {notifications.map((notification) => (
          <div key={notification.id} className="pointer-events-auto">
            <Notification {...notification} />
          </div>
        ))}
      </AnimatePresence>
    </div>
  );
}
