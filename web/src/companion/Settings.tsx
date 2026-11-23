import { useCallback, useEffect, useState, type ReactNode } from "react";
import { BarChart3, Bell, Cpu, Globe, GraduationCap, KeyRound, Mic, Palette, PlugZap, Server, ShieldCheck, Smartphone, Volume2 } from "lucide-react";

import { fetchMcpPresets } from "@/lib/api";
import { useClient } from "@/providers/ClientProvider";

import { androidShell } from "./android-shell";
import {
  fetchCompanionState,
  fetchModels,
  fetchSkills,
  fetchUsage,
  type CompanionState,
  type ModelsPayload,
  type Routines,
  type UsagePayload,
} from "./companion-api";
import { isConnected } from "./ConnectSheet";
import { MenuGroup, MenuRow, Switch } from "./Menu";
import { readsAfterVoice, setReadsAfterVoice } from "./MessageActions";
import { usePetSize, useThemeMode } from "./appearance";
import { MODE_LABEL, SIZE_LABEL } from "./AppearanceDock";
import { NotificationsRow, notificationsNote, useNotifications, type PushStatus } from "./NotificationsAction";
import { LazyBoundary, lazyScreen, PageSheet } from "./Screen";
import { ConnectionsView } from "./TabViews";
import { formatCost, formatTokens, UsagePanel } from "./UsagePanel";

const ModelPanel = lazyScreen(() => import("./ModelPanel").then((m) => ({ default: m.ModelPanel })));
const ModelDetail = lazyScreen(() => import("./ModelPanel").then((m) => ({ default: m.ModelDetail })));
const SkillsPanel = lazyScreen(() => import("./SkillsPanel").then((m) => ({ default: m.SkillsPanel })));
const PermissionLevels = lazyScreen(() => import("./PermissionLevels").then((m) => ({ default: m.PermissionLevels })));
const VaultPanel = lazyScreen(() => import("./VaultPanel").then((m) => ({ default: m.VaultPanel })));
const DevicesPanel = lazyScreen(() => import("./DevicesPanel").then((m) => ({ default: m.DevicesPanel })));

export type SettingsPage = "root" | "models" | "model" | "usage" | "skills" | "connections" | "permissions" | "vault" | "devices";

const TITLES: Record<SettingsPage, string> = {
  root: "设置",
  models: "模型",
  model: "模型",
  usage: "用量",
  skills: "技能",
  connections: "连接",
  permissions: "权限",
  vault: "安全凭证",
  devices: "设备",
};

/** "早报 08:00 · 关心", "关闭"; nothing while loading. */
export function routinesShort(routines: Routines | null): string | undefined {
  if (!routines) return undefined;
  const parts = [routines.brief_time ? `早报 ${routines.brief_time}` : null, routines.checkins ? "关心" : null];
  return parts.filter(Boolean).join(" · ") || "关闭";
}

/** "自动", the pinned model, or the default one. */
export function modelsShort(models: ModelsPayload | null): string | undefined {
  if (!models) return undefined;
  if (models.settings.enabled) return "自动";
  return models.settings.fixed ?? models.default ?? undefined;
}

function usageShort(usage: UsagePayload | null): string | undefined {
  if (!usage) return undefined;
  const total = Object.values(usage.today.by_source).reduce((sum, n) => sum + n, 0);
  // What it cost reads at a glance, but only when every model used has a price (a partial sum would
  // look far too cheap); tokens and the cache share are on the 用量 page itself.
  const priced = [...usage.today.by_model, ...(usage.today.media ?? [])].every((row) => row.cost !== null);
  const cost = priced ? formatCost(usage.today.cost) : null;
  return cost ? `今天${cost}` : `今天 ${formatTokens(total)} token`;
}

export type CapabilityTone = "ok" | "warn" | "bad" | "off" | "neutral";

export interface CapabilityDiagnostic {
  key: "stt" | "push" | "browser";
  label: string;
  status: string;
  tone: CapabilityTone;
  guidance: string;
}

export function describeCapabilities(
  companion: CompanionState | null,
  pushStatus: PushStatus,
  hasAndroidReminders = false,
): CapabilityDiagnostic[] {
  const caps = companion?.capabilities;

  const stt: CapabilityDiagnostic =
    caps?.stt === true
      ? {
          key: "stt",
          label: "语音识别",
          status: "已就绪",
          tone: "ok",
          guidance: "长按聊天输入框右侧麦克风即可语音转文字。",
        }
      : caps?.stt === false
        ? {
            key: "stt",
            label: "语音识别",
            status: "未就绪",
            tone: "off",
            guidance: "需在服务器安装 ffmpeg，并配置语音识别模型或本地 SenseVoice 引擎。",
          }
        : {
            key: "stt",
            label: "语音识别",
            status: companion === null ? "检测中" : "默认可用",
            tone: "neutral",
            guidance:
              companion === null
                ? "正在读取服务端语音识别运行状态…"
                : "按住聊天输入框右侧麦克风即可说话；若转写失败请检查服务器 ffmpeg 与语音模型。",
          };

  const note = notificationsNote(pushStatus, caps?.push);
  const push: CapabilityDiagnostic =
    caps?.push === false && !hasAndroidReminders
      ? {
          key: "push",
          label: "消息推送",
          status: "服务端未启用",
          tone: "off",
          guidance: "服务器尚未配置 Web Push 密钥（VAPID），启用后可向锁屏和后台发送提醒。",
        }
      : pushStatus.kind === "on"
        ? {
            key: "push",
            label: "消息推送",
            status: "已开启",
            tone: "ok",
            guidance: note ?? "锁屏与后台均可接收早报、定时提醒和主动关心通知。",
          }
        : pushStatus.kind === "denied"
          ? {
              key: "push",
              label: "消息推送",
              status: "权限已拒",
              tone: "bad",
              guidance: note ?? "请在手机或浏览器的系统通知设置中允许通知权限。",
            }
          : pushStatus.kind === "unsupported"
            ? {
                key: "push",
                label: "消息推送",
                status: "当前受限",
                tone: "off",
                guidance: pushStatus.reason,
              }
            : pushStatus.kind === "error"
              ? {
                  key: "push",
                  label: "消息推送",
                  status: "连接异常",
                  tone: "bad",
                  guidance: note ?? pushStatus.message,
                }
              : pushStatus.kind === "off"
                ? {
                    key: "push",
                    label: "消息推送",
                    status: "待开启",
                    tone: "warn",
                    guidance:
                      caps?.push === true
                        ? "服务端推送已就绪，在上方「通用 → 通知」打开开关即可接收提醒。"
                        : (note ?? "在上方「通用 → 通知」打开开关即可接收早报与提醒。"),
                  }
                : {
                    key: "push",
                    label: "消息推送",
                    status: "检查中",
                    tone: "neutral",
                    guidance: "正在检查当前设备的通知权限与后台推送服务…",
                  };

  const browser: CapabilityDiagnostic =
    caps?.browser === true
      ? {
          key: "browser",
          label: "共享浏览器",
          status: "已就绪",
          tone: "ok",
          guidance: "伙伴可自主打开网页查资料，遇到登录或验证码时可随时交给你接手。",
        }
      : caps?.browser === false
        ? {
            key: "browser",
            label: "共享浏览器",
            status: "未就绪",
            tone: "off",
            guidance: "服务器未检测到可用浏览器环境，需安装 Playwright 与 Chromium（playwright install chromium）。",
          }
        : {
            key: "browser",
            label: "共享浏览器",
            status: companion === null ? "检测中" : "按需启动",
            tone: "neutral",
            guidance:
              companion === null
                ? "正在读取服务端共享浏览器运行状态…"
                : "伙伴需要上网操作时会自动启动无头浏览器。",
          };

  return [stt, push, browser];
}

const CAPABILITY_ICONS: Record<CapabilityDiagnostic["key"], ReactNode> = {
  stt: <Mic className="h-4 w-4" />,
  push: <Bell className="h-4 w-4" />,
  browser: <Globe className="h-4 w-4" />,
};

function CapabilityBadge({ tone, label }: { tone: CapabilityTone; label: string }) {
  const dotStyle: React.CSSProperties =
    tone === "ok"
      ? { background: "var(--c-online)" }
      : tone === "bad"
        ? { background: "var(--c-danger)" }
        : tone === "warn"
          ? { background: "var(--c-primary)" }
          : { background: "var(--c-text-muted)" };
  const textStyle: React.CSSProperties | undefined =
    tone === "ok"
      ? { color: "var(--c-online)" }
      : tone === "bad"
        ? { color: "var(--c-danger)" }
        : tone === "warn"
          ? { color: "var(--c-primary)" }
          : undefined;
  return (
    <span className="inline-flex shrink-0 items-center gap-1.5 text-[12.5px] font-medium">
      <span className="h-2 w-2 rounded-full" style={dotStyle} aria-hidden="true" />
      <span className={textStyle ? undefined : "c-muted"} style={textStyle}>
        {label}
      </span>
    </span>
  );
}

/** The settings list: 通用 / 模型 / 能力 / 能力与诊断 / 连接 / 安全, like Muse's settings. */
function SettingsRoot({ onOpen, onAppearance }: { onOpen: (page: SettingsPage) => void; onAppearance: () => void }) {
  const { token } = useClient();
  const mode = useThemeMode();
  const size = usePetSize();
  const notifications = useNotifications();
  const android = androidShell();
  const [readAfterVoice, setReadAfterVoice] = useState(readsAfterVoice);
  const [companion, setCompanion] = useState<CompanionState | null>(null);
  const [models, setModels] = useState<ModelsPayload | null>(null);
  const [usage, setUsage] = useState<UsagePayload | null>(null);
  const [connected, setConnected] = useState<number | null>(null);
  const [skills, setSkills] = useState<number | null>(null);

  useEffect(() => {
    let cancelled = false;
    fetchCompanionState(token)
      .then((state) => !cancelled && setCompanion(state))
      .catch(() => !cancelled && setCompanion(null));
    fetchModels(token)
      .then((payload) => !cancelled && setModels(payload))
      .catch(() => !cancelled && setModels(null));
    fetchUsage(token)
      .then((payload) => !cancelled && setUsage(payload))
      .catch(() => !cancelled && setUsage(null));
    fetchSkills(token)
      .then((s) => !cancelled && setSkills(s.learned.length))
      .catch(() => !cancelled && setSkills(null));
    fetchMcpPresets(token)
      .then((payload) => !cancelled && setConnected(payload.presets.filter(isConnected).length))
      .catch(() => !cancelled && setConnected(null));
    return () => {
      cancelled = true;
    };
  }, [token]);

  const diagnostics = describeCapabilities(companion, notifications.status, Boolean(android?.remindersEnabled));

  return (
    <div className="flex flex-col gap-6">
      <section className="flex flex-col gap-1.5">
        <h2 className="c-muted px-3.5 text-[13px]">通用</h2>
        <MenuGroup footnote={notificationsNote(notifications.status, companion?.capabilities?.push)}>
          <NotificationsRow
            status={notifications.status}
            serverPush={companion?.capabilities?.push}
            onToggle={() => void notifications.toggle()}
          />
          <MenuRow
            icon={<Palette className="h-4 w-4" />}
            label="外观"
            value={`${MODE_LABEL[mode]} · 形象${SIZE_LABEL[size]}`}
            onSelect={onAppearance}
          />
          {android?.speak && (
            <MenuRow
              icon={<Volume2 className="h-4 w-4" />}
              label="语音输入后朗读回复"
              trailing={
                <Switch
                  checked={readAfterVoice}
                  label="语音输入后朗读回复"
                  onChange={() => {
                    setReadsAfterVoice(!readAfterVoice);
                    setReadAfterVoice(!readAfterVoice);
                  }}
                />
              }
            />
          )}
        </MenuGroup>
      </section>

      <section className="flex flex-col gap-1.5">
        <h2 className="c-muted px-3.5 text-[13px]">模型</h2>
        <MenuGroup>
          <MenuRow icon={<Cpu className="h-4 w-4" />} label="模型" value={modelsShort(models)} onSelect={() => onOpen("models")} />
          <MenuRow icon={<BarChart3 className="h-4 w-4" />} label="用量" value={usageShort(usage)} onSelect={() => onOpen("usage")} />
        </MenuGroup>
      </section>

      <section className="flex flex-col gap-1.5">
        <h2 className="c-muted px-3.5 text-[13px]">能力</h2>
        <MenuGroup>
          <MenuRow
            icon={<GraduationCap className="h-4 w-4" />}
            label="技能"
            value={skills === null ? undefined : skills ? `学会了 ${skills} 个` : "还没有"}
            onSelect={() => onOpen("skills")}
          />
        </MenuGroup>
      </section>

      <section className="flex flex-col gap-1.5" aria-label="能力与诊断">
        <h2 className="c-muted px-3.5 text-[13px]">能力与诊断</h2>
        <MenuGroup>
          {diagnostics.map((item) => (
            <div key={item.key} className="flex items-start gap-3 px-3.5 py-3">
              <span
                className="mt-0.5 grid h-7 w-7 shrink-0 place-items-center rounded-lg"
                style={{ background: "var(--c-fill)" }}
                aria-hidden="true"
              >
                {CAPABILITY_ICONS[item.key]}
              </span>
              <div className="min-w-0 flex-1">
                <div className="flex items-center justify-between gap-2">
                  <span className="text-[15px] font-medium leading-snug">{item.label}</span>
                  <CapabilityBadge tone={item.tone} label={item.status} />
                </div>
                <p className="c-muted mt-0.5 text-[12px] leading-relaxed">{item.guidance}</p>
              </div>
            </div>
          ))}
        </MenuGroup>
      </section>

      <section className="flex flex-col gap-1.5">
        <h2 className="c-muted px-3.5 text-[13px]">连接</h2>
        <MenuGroup>
          <MenuRow
            icon={<PlugZap className="h-4 w-4" />}
            label="已连接的服务"
            value={connected === null ? undefined : connected ? `${connected} 个` : "还没有"}
            onSelect={() => onOpen("connections")}
          />
        </MenuGroup>
      </section>

      <section className="flex flex-col gap-1.5">
        <h2 className="c-muted px-3.5 text-[13px]">安全</h2>
        <MenuGroup>
          <MenuRow
            icon={<ShieldCheck className="h-4 w-4" />}
            label="权限"
            onSelect={() => onOpen("permissions")}
          />
          <MenuRow icon={<KeyRound className="h-4 w-4" />} label="安全凭证" onSelect={() => onOpen("vault")} />
          <MenuRow icon={<Smartphone className="h-4 w-4" />} label="设备" onSelect={() => onOpen("devices")} />
        </MenuGroup>
      </section>

      {android && (
        <section className="flex flex-col gap-1.5">
          <h2 className="c-muted px-3.5 text-[13px]">高级</h2>
          <MenuGroup>
            <MenuRow icon={<Server className="h-4 w-4" />} label="更换服务器地址" onSelect={() => android.changeServer()} />
          </MenuGroup>
        </section>
      )}
    </div>
  );
}

/**
 * Full-screen settings with page navigation: each item pushes a page with a
 * back button, and back (the button, Escape, or the phone's back key through
 * the Android app) returns one level, then closes.
 */
export function SettingsScreen({
  start,
  onClose,
  onLogout,
  onDraft,
  onTeach,
  onAppearance,
}: {
  /** The page to open on; deeper pages keep 设置 underneath, so back leads there. */
  start: SettingsPage;
  onClose: () => void;
  onLogout: () => void;
  /** Start a message in the main chat (a skill's 用一次 / 定时做). */
  onDraft: (text: string) => void;
  /** 教它一个新技能: the browser, to do a task while it watches. */
  onTeach: () => void;
  /** 外观: chosen on the main chat itself (see AppearanceDock). */
  onAppearance: () => void;
}) {
  const [stack, setStack] = useState<SettingsPage[]>(start === "root" ? ["root"] : ["root", start]);
  const page = stack[stack.length - 1];
  // The model the "model" page shows; its title is the model's name.
  const [modelName, setModelName] = useState<string | null>(null);
  const title = (entry: SettingsPage) => (entry === "model" && modelName ? modelName : TITLES[entry]);
  const push = useCallback((next: SettingsPage) => setStack((current) => [...current, next]), []);
  const back = useCallback(() => {
    if (stack.length > 1) setStack((current) => current.slice(0, -1));
    else onClose();
  }, [stack.length, onClose]);

  return (
    <PageSheet
      title={title(page)}
      backLabel={stack.length > 1 ? `返回${title(stack[stack.length - 2])}` : undefined}
      onBack={back}
      onClose={onClose}
      pageKey={page === "model" ? `model:${modelName}` : page}
      flush={page === "connections"}
    >
      {page === "root" && <SettingsRoot onOpen={push} onAppearance={onAppearance} />}
      {page !== "root" && (
        <LazyBoundary key={page}>
          {page === "models" && (
            <ModelPanel
              active
              onOpenModel={(name) => {
                setModelName(name);
                push("model");
              }}
            />
          )}
          {page === "model" && modelName && <ModelDetail name={modelName} />}
          {page === "usage" && <UsagePanel active />}
          {page === "skills" && <SkillsPanel onDraft={onDraft} onTeach={onTeach} />}
          {page === "connections" && <ConnectionsView active />}
          {page === "permissions" && <PermissionLevels />}
          {page === "vault" && <VaultPanel />}
          {page === "devices" && <DevicesPanel onDisconnect={onLogout} />}
        </LazyBoundary>
      )}
    </PageSheet>
  );
}
