import { useCallback, useEffect, useMemo, useState, type ReactNode } from "react";
import {
  CalendarClock,
  FileText,
  Globe,
  History,
  ImageIcon,
  Mail,
  PlugZap,
  RefreshCw,
  Search,
  Send,
  Sparkles,
  SquareTerminal,
  Users,
  type LucideIcon,
} from "lucide-react";

import { fetchMcpPresets } from "@/lib/api";
import type { McpPresetInfo, SessionAutomationJob } from "@/lib/types";
import { useClient } from "@/providers/ClientProvider";

import { fetchIdeas, refreshIdeas, type IdeasPayload } from "./companion-api";

import { ConnectSheet, connectTitle, isConnected, RECOMMENDED_GROUPS, recommendedPresets, ServiceLogo } from "./ConnectSheet";
import { MenuGroup, MenuRow } from "./Menu";
import { MoreServicesSheet } from "./MoreServices";
import { isViewOnly, ManageConnectionSheet } from "./ManageConnectionSheet";
import { GOAL_CHECKIN_PREFIX, cronLabel } from "./muse/goals";
import { CompanionPet } from "./Pet";
import {
  APPROVAL_TRIGGER_NAME,
  dayLabel,
  type ActivityEntry,
  type ActivityKind,
} from "./presentation";

/** Jobs behind 自动触发; listed as triggers, not as timed tasks. */
export const TRIGGER_JOB_PREFIX = "自动触发：";

/** User-facing tasks: not system jobs, not the internal approval-receipt trigger. */
export function visibleJobs(jobs: SessionAutomationJob[]): SessionAutomationJob[] {
  return jobs.filter(
    (job) =>
      !job.protected &&
      job.name !== APPROVAL_TRIGGER_NAME &&
      !job.name.startsWith(GOAL_CHECKIN_PREFIX) &&
      !job.name.startsWith(TRIGGER_JOB_PREFIX),
  );
}

/** A page's title, as 动态's: big and plain; an explanation only where the page needs one. */
export function PageTitle({ title, subtitle, trailing }: { title: string; subtitle?: string; trailing?: ReactNode }) {
  return (
    <div className="flex items-center gap-3 px-5 pb-3">
      <div className="min-w-0 flex-1">
        <h2 className="text-[32px] font-bold leading-tight">{title}</h2>
        {subtitle && <p className="c-muted mt-1 text-[15px]">{subtitle}</p>}
      </div>
      {trailing}
    </div>
  );
}

function EmptyState({ title, body }: { title: string; body: string }) {
  return (
    <div className="flex flex-col items-center px-8 pt-12 text-center">
      <CompanionPet size={64} />
      <p className="mt-4 text-[16px] font-medium">{title}</p>
      <p className="c-muted mt-1 text-[14px]">{body}</p>
    </div>
  );
}

/* ---------- 任务 ---------- */

export function describeSchedule(job: SessionAutomationJob): string {
  const { schedule } = job;
  if (schedule.kind === "at" && schedule.at_ms) return `一次 · ${dayLabel(schedule.at_ms)}`;
  if (schedule.kind === "every" && schedule.every_ms) {
    const minutes = Math.round(schedule.every_ms / 60_000);
    return minutes >= 60 && minutes % 60 === 0 ? `每 ${minutes / 60} 小时` : `每 ${minutes} 分钟`;
  }
  if (schedule.kind === "cron" && schedule.expr) return cronLabel(schedule.expr);
  if (schedule.kind === "local") return "由外部触发";
  return "定时";
}

const TONE_STYLE: Record<ActivityEntry["tone"], React.CSSProperties> = {
  ok: { color: "var(--c-online)" },
  bad: { color: "var(--c-danger)" },
  pending: { color: "var(--c-primary)" },
  muted: { color: "var(--c-text-muted)" },
};

const ACTIVITY_ICONS: Record<ActivityKind, LucideIcon> = {
  thinking: Sparkles,
  typing: Sparkles,
  search: Search,
  read: Globe,
  files: FileText,
  run: SquareTerminal,
  image: ImageIcon,
  schedule: CalendarClock,
  send: Send,
  recall: History,
  delegate: Users,
  app: PlugZap,
  working: Sparkles,
};

function EntryIcon({ entry }: { entry: ActivityEntry }) {
  const Icon = entry.kind === "approval" ? Mail : entry.kind === "task" ? CalendarClock : ACTIVITY_ICONS[entry.activity ?? "working"];
  return <Icon className="c-muted h-4 w-4 shrink-0" aria-hidden="true" />;
}

/** Audit trail: everything it did for the user, including actions waiting for approval. */
export function ActivityLog({ entries }: { entries: ActivityEntry[] }) {
  if (entries.length === 0) return null;
  return (
    <section className="px-4 pt-5">
      <p className="c-muted px-1 pb-2 text-[13px] font-medium">最近做过的事</p>
      <ul className="c-card overflow-hidden rounded-[22px]">
        {entries.map((entry, index) => (
          <li key={entry.id} className={`flex items-center gap-3 px-4 py-3 ${index > 0 ? "border-t c-divider" : ""}`}>
            <EntryIcon entry={entry} />
            <div className="min-w-0 flex-1">
              <p className="line-clamp-2 text-[14px] leading-snug">{entry.title}</p>
              <p className="c-muted text-[12px]">{dayLabel(entry.at)}</p>
            </div>
            {entry.status && (
              <span className="shrink-0 text-[13px] font-medium" style={TONE_STYLE[entry.tone]}>
                {entry.status}
              </span>
            )}
          </li>
        ))}
      </ul>
    </section>
  );
}

/* ---------- 连接 ---------- */

function connectionState(preset: McpPresetInfo): { label: string; ok: boolean } {
  if (preset.runtime_status === "connected") return { label: "已连接", ok: true };
  if (preset.runtime_status === "connecting") return { label: "连接中", ok: false };
  if (preset.runtime_status === "failed") return { label: "连接失败", ok: false };
  if (preset.status === "missing_credentials") return { label: "缺少凭据", ok: false };
  return { label: preset.enabled === false ? "已停用" : "已配置", ok: preset.enabled !== false };
}

/** 设置 → 连接: the services it can use for you. */
export function ConnectionsView({ active }: { active: boolean }) {
  const { token } = useClient();
  const [presets, setPresets] = useState<McpPresetInfo[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [editing, setEditing] = useState<McpPresetInfo | null>(null);
  const [managingName, setManagingName] = useState<string | null>(null);
  const [reloadAt, setReloadAt] = useState(0);
  const [moreOpen, setMoreOpen] = useState(false);
  // Follow the latest payload so the sheet reflects each change it makes.
  const managing = presets?.find((preset) => preset.name === managingName) ?? null;

  useEffect(() => {
    if (!active) return;
    let cancelled = false;
    fetchMcpPresets(token)
      .then((payload) => {
        if (!cancelled) setPresets(payload.presets);
      })
      .catch((e) => {
        if (!cancelled) setError(e instanceof Error ? e.message : String(e));
      });
    return () => {
      cancelled = true;
    };
  }, [active, token, reloadAt]);

  const connected = useMemo(() => (presets ?? []).filter(isConnected), [presets]);
  const groups = useMemo(
    () =>
      RECOMMENDED_GROUPS.map((group) => ({
        title: group.title,
        presets: recommendedPresets(presets, group.names).filter((preset) => !isConnected(preset)),
      })).filter((group) => group.presets.length > 0),
    [presets],
  );

  // A freshly enabled server finishes starting in the background; check back until it settles.
  const settling = connected.some((preset) => preset.runtime_status === "connecting");
  useEffect(() => {
    if (!active || !settling) return;
    const timer = window.setTimeout(() => setReloadAt(Date.now()), 3000);
    return () => window.clearTimeout(timer);
  }, [active, settling, presets]);

  return (
    <div className="flex flex-col gap-6 px-4">
      {error && <p className="c-danger text-sm">{error}</p>}
      {presets !== null && connected.length === 0 && groups.length === 0 && (
        <EmptyState title="还没有连接任何服务" body="邮箱、日历、地图等服务接入后，它就能直接替你操作。" />
      )}

      {connected.length > 0 && (
        <section className="flex flex-col gap-1.5">
          <h3 className="c-muted px-3.5 text-[13px]">已连接</h3>
          <MenuGroup>
            {connected.map((preset) => {
              const state = connectionState(preset);
              const detail = [
                preset.tool_count ? `${preset.tool_count} 项能力` : null,
                isViewOnly(preset) ? "只允许查看" : null,
                !state.ok && preset.required_fields.length > 0 ? "点一下重新填写" : null,
              ]
                .filter(Boolean)
                .join(" · ");
              return (
                <MenuRow
                  key={preset.name}
                  icon={<ServiceLogo preset={preset} size={28} />}
                  label={preset.display_name || preset.name}
                  detail={detail || preset.description}
                  ariaLabel={`管理${preset.display_name || preset.name}`}
                  onSelect={() => setManagingName(preset.name)}
                  trailing={
                    <span className="flex shrink-0 items-center gap-1.5 text-[13px]">
                      <span
                        className={`h-2 w-2 rounded-full ${state.ok ? "c-online" : ""}`}
                        style={state.ok ? undefined : { background: "var(--c-text-muted)" }}
                        aria-hidden="true"
                      />
                      <span className="c-muted">{state.label}</span>
                    </span>
                  }
                />
              );
            })}
          </MenuGroup>
        </section>
      )}

      {groups.map((group) => (
        <section key={group.title} className="flex flex-col gap-1.5">
          <h3 className="c-muted px-3.5 text-[13px]">{group.title}</h3>
          <MenuGroup>
            {group.presets.map((preset) => (
              <MenuRow
                key={preset.name}
                icon={<ServiceLogo preset={preset} size={28} />}
                label={preset.display_name || preset.name}
                detail={preset.description}
                trailing={
                  <button
                    type="button"
                    onClick={() => setEditing(preset)}
                    className="c-primary min-h-8 shrink-0 rounded-full px-3.5 text-[13px] font-semibold"
                    aria-label={connectTitle(preset)}
                  >
                    连接
                  </button>
                }
              />
            ))}
          </MenuGroup>
        </section>
      ))}

      <MenuGroup footnote="Notion、GitHub、飞书等更多服务，或者自己添加一个。">
        <MenuRow icon={<PlugZap className="h-4 w-4" />} label="添加其他服务" onSelect={() => setMoreOpen(true)} />
      </MenuGroup>

      <MoreServicesSheet
        open={moreOpen && !editing}
        presets={presets}
        onClose={() => setMoreOpen(false)}
        onConnect={(preset) => setEditing(preset)}
        onChanged={(payload) => setPresets(payload.presets)}
      />

      <ManageConnectionSheet
        preset={editing ? null : managing}
        onClose={() => {
          setManagingName(null);
          setReloadAt(Date.now());
        }}
        onChanged={(payload) => setPresets(payload.presets)}
        onEditCredentials={setEditing}
      />
      <ConnectSheet
        preset={editing}
        onClose={() => {
          setEditing(null);
          setReloadAt(Date.now());
        }}
        onChanged={(payload) => setPresets(payload.presets)}
      />
    </div>
  );
}

/* ---------- 点子 ---------- */

export function IdeasView({ active, onDo }: { active: boolean; onDo: (prompt: string) => void }) {
  const { client, token } = useClient();
  const [payload, setPayload] = useState<IdeasPayload | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const run = useCallback(
    async (refresh: boolean) => {
      setLoading(true);
      setError(null);
      try {
        setPayload(refresh ? await refreshIdeas(client) : await fetchIdeas(token));
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e));
      } finally {
        setLoading(false);
      }
    },
    [client, token],
  );

  useEffect(() => {
    if (active && payload === null && !loading && !error) void run(false);
  }, [active, error, loading, payload, run]);

  return (
    <div>
      <PageTitle
        title="点子"
        trailing={
          <button
            type="button"
            disabled={loading}
            onClick={() => run(true)}
            aria-label="换一批"
            className="c-pressable grid h-11 w-11 shrink-0 place-items-center rounded-full disabled:opacity-50"
            style={{ background: "var(--c-fill)" }}
          >
            <RefreshCw className={`h-[18px] w-[18px] ${loading ? "animate-spin" : ""}`} aria-hidden="true" />
          </button>
        }
      />
      {error && <p className="c-danger px-5 pb-2 text-sm">{error}</p>}
      {loading && !payload ? (
        <div className="flex flex-col items-center px-8 pt-12 text-center" role="status">
          <CompanionPet size={64} animation="review" />
        </div>
      ) : (
        // A plain list, one idea per row: tap it and it starts.
        <ul className="c-divider divide-y">
          {payload?.ideas.map((idea) => (
            <li key={idea.title}>
              <button
                type="button"
                onClick={() => onDo(idea.prompt)}
                className="c-pressable flex w-full items-center gap-3 px-5 py-4 text-left"
              >
                <span className="min-w-0 flex-1">
                  <span className="block text-[17px] font-semibold leading-snug">{idea.title}</span>
                  {idea.reason && <span className="c-muted mt-1 block text-[15px] leading-snug">{idea.reason}</span>}
                </span>
                <span className="shrink-0 rounded-full px-3.5 py-1.5 text-[14px] font-semibold" style={{ background: "var(--c-fill)", color: "var(--c-primary)" }}>去做</span>
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
