import { useCallback, useState } from "react";
import type { NotificationProps, NotificationType } from "../components/notification";

let notificationIdCounter = 0;

export function useNotifications() {
  const [notifications, setNotifications] = useState<NotificationProps[]>([]);

  const addNotification = useCallback(
    (
      type: NotificationType,
      title: string,
      message: string,
      options?: {
        confirmText?: string;
        cancelText?: string;
        onConfirm?: () => void;
        onCancel?: () => void;
        autoClose?: number;
      }
    ) => {
      const id = `notification-${++notificationIdCounter}`;
      const notification: NotificationProps = {
        id,
        type,
        title,
        message,
        confirmText: options?.confirmText,
        cancelText: options?.cancelText,
        onConfirm: options?.onConfirm,
        onCancel: options?.onCancel,
        onClose: () => removeNotification(id),
        autoClose: options?.autoClose,
      };

      setNotifications((prev) => [...prev, notification]);

      if (options?.autoClose) {
        setTimeout(() => {
          removeNotification(id);
        }, options.autoClose);
      }

      return id;
    },
    []
  );

  const removeNotification = useCallback((id: string) => {
    setNotifications((prev) => prev.filter((n) => n.id !== id));
  }, []);

  const confirm = useCallback(
    (
      title: string,
      message: string,
      onConfirm: () => void,
      options?: {
        confirmText?: string;
        cancelText?: string;
        type?: "warning" | "danger";
      }
    ) => {
      return addNotification(options?.type || "warning", title, message, {
        confirmText: options?.confirmText || "确认",
        cancelText: options?.cancelText || "取消",
        onConfirm,
        onCancel: () => {},
      });
    },
    [addNotification]
  );

  const info = useCallback(
    (title: string, message: string, autoClose = 3000) => {
      return addNotification("info", title, message, { autoClose });
    },
    [addNotification]
  );

  const success = useCallback(
    (title: string, message: string, autoClose = 3000) => {
      return addNotification("success", title, message, { autoClose });
    },
    [addNotification]
  );

  const warning = useCallback(
    (title: string, message: string, autoClose = 4000) => {
      return addNotification("warning", title, message, { autoClose });
    },
    [addNotification]
  );

  const danger = useCallback(
    (title: string, message: string, autoClose = 5000) => {
      return addNotification("danger", title, message, { autoClose });
    },
    [addNotification]
  );

  return {
    notifications,
    addNotification,
    removeNotification,
    confirm,
    info,
    success,
    warning,
    danger,
  };
}
