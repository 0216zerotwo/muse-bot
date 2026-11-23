import { useCallback, useEffect, useState } from "react";
import { Bell, BellOff } from "lucide-react";

import { useClient } from "@/providers/ClientProvider";

import { androidShell } from "./android-shell";
import { fetchPushConfig, sendTestPush, subscribePush, unsubscribePush } from "./companion-api";
import { MenuRow } from "./Menu";
import { Switch } from "./Menu";

export type PushStatus =
  | { kind: "checking" }
  | { kind: "unsupported"; reason: string }
  | { kind: "denied" }
  | { kind: "off" }
  | { kind: "on" }
  | { kind: "busy"; label: string }
  | { kind: "error"; message: string };

function applicationServerKey(base64: string): Uint8Array<ArrayBuffer> {
  const padded = base64.replace(/-/g, "+").replace(/_/g, "/") + "=".repeat((4 - (base64.length % 4)) % 4);
  const raw = atob(padded);
  const bytes = new Uint8Array(new ArrayBuffer(raw.length));
  for (let i = 0; i < raw.length; i += 1) bytes[i] = raw.charCodeAt(i);
  return bytes;
}

function unsupportedReason(): string | null {
  const ios = /iPhone|iPad|iPod/.test(navigator.userAgent);
  const standalone = window.matchMedia?.("(display-mode: standalone)").matches;
  if (androidShell() && !androidShell()?.remindersEnabled) return "安卓 App 这一版还不能推送通知";
  if (!window.isSecureContext) return "需要通过 HTTPS 打开才能开启通知";
  if (ios && !standalone) return "先在 Safari 里「添加到主屏幕」，从主屏幕打开后再开启";
  if (!("serviceWorker" in navigator) || !("PushManager" in window) || !("Notification" in window)) {
    return "这个浏览器不支持通知";
  }
  return null;
}

const SW_READY_TIMEOUT_MS = 4_000;

function serviceWorkerReady(timeoutMs = SW_READY_TIMEOUT_MS): Promise<ServiceWorkerRegistration> {
  return Promise.race([
    navigator.serviceWorker.ready,
    new Promise<never>((_, reject) => {
      window.setTimeout(() => reject(new Error("后台通知服务未就绪，刷新页面后再试")), timeoutMs);
    }),
  ]);
}

/** Phone notifications for this device: whether they are on, and turning them on or off. */
export function useNotifications() {
  const { client, token } = useClient();
  const [status, setStatus] = useState<PushStatus>({ kind: "checking" });

  const refresh = useCallback(async () => {
    const android = androidShell();
    // The Android app (0.3+) notifies on its own when a reminder is due; there is no push.
    if (android?.remindersEnabled) return setStatus({ kind: android.remindersEnabled() ? "on" : "off" });
    const reason = unsupportedReason();
    if (reason) return setStatus({ kind: "unsupported", reason });
    if (Notification.permission === "denied") return setStatus({ kind: "denied" });
    const registration = await serviceWorkerReady();
    const subscription = await registration.pushManager.getSubscription();
    setStatus({ kind: subscription && Notification.permission === "granted" ? "on" : "off" });
  }, []);

  useEffect(() => {
    void refresh().catch((e: unknown) => setStatus({ kind: "error", message: e instanceof Error ? e.message : String(e) }));
  }, [refresh]);

  const toggle = async () => {
    const android = androidShell();
    if (android?.setRemindersEnabled) {
      android.setRemindersEnabled(status.kind !== "on");
      return void refresh();
    }
    try {
      const registration = await serviceWorkerReady();
      const existing = await registration.pushManager.getSubscription();
      if (status.kind === "on" && existing) {
        setStatus({ kind: "busy", label: "正在关闭…" });
        await unsubscribePush(client, existing.endpoint);
        await existing.unsubscribe();
        return setStatus({ kind: "off" });
      }
      setStatus({ kind: "busy", label: "正在开启…" });
      if ((await Notification.requestPermission()) !== "granted") return setStatus({ kind: "denied" });
      const config = await fetchPushConfig(token);
      const subscription =
        existing ??
        (await registration.pushManager.subscribe({
          userVisibleOnly: true,
          applicationServerKey: applicationServerKey(config.public_key),
        }));
      await subscribePush(client, subscription.toJSON());
      await sendTestPush(client);
      setStatus({ kind: "on" });
    } catch (e) {
      setStatus({ kind: "error", message: e instanceof Error ? e.message : String(e) });
    }
  };

  return { status, toggle };
}

/** Why the switch cannot be used right now, shown under the settings group. */
export function notificationsNote(status: PushStatus, serverPush?: boolean): string | null {
  if (androidShell()?.remindersEnabled && (status.kind === "on" || status.kind === "off")) {
    return "提醒到点时，手机会通知你。别的消息（回复、主动找你）要打开 App 才看得到。";
  }
  if (serverPush === false && !androidShell()?.remindersEnabled) {
    return "服务器当前未启用 Web Push 推送服务，配置 VAPID 密钥后即可开启跨设备通知。";
  }
  if (status.kind === "unsupported") return status.reason;
  if (status.kind === "denied") return "通知权限被拒绝了，到手机或浏览器的系统设置里打开";
  if (status.kind === "error") return `通知出错了：${status.message}`;
  return null;
}

/** 通知 row: a switch, or a short word when it cannot be switched here. */
export function NotificationsRow({
  status,
  onToggle,
  serverPush,
}: {
  status: PushStatus;
  onToggle: () => void;
  serverPush?: boolean;
}) {
  const blockedByServer = serverPush === false && !androidShell()?.remindersEnabled;
  const usable = !blockedByServer && (status.kind === "on" || status.kind === "off" || status.kind === "busy");
  return (
    <MenuRow
      icon={status.kind === "on" && !blockedByServer ? <Bell className="h-4 w-4" /> : <BellOff className="h-4 w-4" />}
      label="通知"
      value={
        blockedByServer
          ? "未启用"
          : status.kind === "busy"
            ? status.label
            : usable
              ? undefined
              : status.kind === "checking"
                ? "检查中"
                : "不可用"
      }
      trailing={
        usable ? (
          <Switch checked={status.kind === "on"} disabled={status.kind === "busy"} label="通知" onChange={onToggle} />
        ) : undefined
      }
    />
  );
}
