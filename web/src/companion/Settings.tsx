import { useCallback, useEffect, useState } from "react";
import { BarChart3, Cpu, GraduationCap, KeyRound, Palette, PlugZap, Server, ShieldCheck, Smartphone, Volume2 } from "lucide-react";

import { fetchMcpPresets } from "@/lib/api";
import { useClient } from "@/providers/ClientProvider";

import { androidShell } from "./android-shell";
import {
  fetchModels,
  fetchSkills,
  fetchUsage,
  type ModelsPayload,
  type Routines,
  type UsagePayload,
} from "./companion-api";
import { isConnected } from "./ConnectSheet";
import { MenuGroup, MenuRow, Switch } from "./Menu";
import { readsAfterVoice, setReadsAfterVoice } from "./MessageActions";
import { usePetSize, useThemeMode } from "./appearance";
import { MODE_LABEL, SIZE_LABEL } from "./AppearanceDock";
import { NotificationsRow, notificationsNote, useNotifications } from "./NotificationsAction";
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

/** The settings list: 通用 / 模型 / 连接 / 高级, like Muse's settings (who it is lives on its face). */
function SettingsRoot({ onOpen, onAppearance }: { onOpen: (page: SettingsPage) => void; onAppearance: () => void }) {
  const { token } = useClient();
  const mode = useThemeMode();
  const size = usePetSize();
  const notifications = useNotifications();
  const android = androidShell();
  const [readAfterVoice, setReadAfterVoice] = useState(readsAfterVoice);
  const [models, setModels] = useState<ModelsPayload | null>(null);
  const [usage, setUsage] = useState<UsagePayload | null>(null);
  const [connected, setConnected] = useState<number | null>(null);
  const [skills, setSkills] = useState<number | null>(null);

  useEffect(() => {
    fetchModels(token).then(setModels).catch(() => setModels(null));
    fetchUsage(token).then(setUsage).catch(() => setUsage(null));
    fetchSkills(token).then((s) => setSkills(s.learned.length)).catch(() => setSkills(null));
    fetchMcpPresets(token)
      .then((payload) => setConnected(payload.presets.filter(isConnected).length))
      .catch(() => setConnected(null));
  }, [token]);

  return (
    <div className="flex flex-col gap-6">
      <section className="flex flex-col gap-1.5">
        <h2 className="c-muted px-3.5 text-[13px]">通用</h2>
        <MenuGroup footnote={notificationsNote(notifications.status)}>
          <NotificationsRow status={notifications.status} onToggle={() => void notifications.toggle()} />
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
