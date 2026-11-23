/*
 * Ported from nanoMuse (GPL-3.0-or-later): the floating tab bar (vendor/nanomuse-web/web/src/App.tsx),
 * the "•••" menu (components/TabHeader.tsx) and the sheet behind the face (screens/MuseSheet.tsx):
 * the face, the name, what it is doing, 停下 while it works, and four panes — what it did, what
 * waits for the user, its daily routine, who it is and what it remembers.
 */
import { ClipboardList, Fingerprint, Globe, Hand, LayoutGrid, Lightbulb, Loader2, MessageCircle, Newspaper, ShieldCheck, Square, SquareCheckBig, UsersRound } from "lucide-react";
import { useCallback, useEffect, useMemo, useState, type ReactNode } from "react";

import { fetchAutomations } from "@/lib/api";
import { copyTextToClipboard } from "@/lib/clipboard";
import type { SessionAutomationJob, UIMessage } from "@/lib/types";
import { cn } from "@/lib/utils";
import { useClient } from "@/providers/ClientProvider";

import { ApprovalCard } from "../ApprovalCard";
import {
  changeTrigger,
  fetchTriggers,
  type ApprovalRecord,
  type ApprovalScope,
  type ComputerStatus,
  type TaughtStep,
  type Trigger,
} from "../companion-api";
import { activityLog, dayLabel } from "../presentation";
import { ActivityLog } from "../TabViews";
import { BrowserFullscreen, BrowserPane } from "./BrowserPane";
import { SoulMemoryPane } from "./Identity";
import { MuseSheet } from "./Sheet";

export type MuseTab = "chat" | "feed" | "ideas" | "goals" | "helpers" | "creations";

/** The five glyphs: outlined at rest, filled with the button's own ink when selected. */
const glyph = (active: boolean) => ({ size: 26, strokeWidth: active ? 2 : 1.7, fill: active ? "currentColor" : "none", fillOpacity: active ? 0.18 : 0 });
export const TABS: Array<{ id: MuseTab; label: string; icon: (active: boolean) => ReactNode }> = [
  { id: "chat", label: "主要聊天", icon: (a) => <MessageCircle {...glyph(a)} /> },
  { id: "feed", label: "动态", icon: (a) => <Newspaper {...glyph(a)} /> },
  { id: "ideas", label: "点子", icon: (a) => <Lightbulb {...glyph(a)} /> },
  { id: "goals", label: "目标", icon: (a) => <SquareCheckBig {...glyph(a)} /> },
  { id: "helpers", label: "帮手", icon: (a) => <UsersRound {...glyph(a)} /> },
  { id: "creations", label: "资源库", icon: (a) => <LayoutGrid {...glyph(a)} /> },
];

const ORDER_KEY = "companion.tab-order";

/** The tabs' order, as the user arranged it in the sidebar (this device only). */
export function useTabOrder(): [MuseTab[], (order: MuseTab[]) => void] {
  const all = TABS.map((t) => t.id);
  const [order, setOrder] = useState<MuseTab[]>(() => {
    try {
      const saved = (JSON.parse(window.localStorage.getItem(ORDER_KEY) ?? "[]") as MuseTab[]).filter((id) => all.includes(id));
      // A tab added since the order was saved goes in at its usual place.
      const next = [...saved];
      all.forEach((id, i) => !next.includes(id) && next.splice(Math.min(i, next.length), 0, id));
      return saved.length ? next : all;
    } catch {
      return all;
    }
  });
  const save = (next: MuseTab[]) => {
    setOrder(next);
    try {
      window.localStorage.setItem(ORDER_KEY, JSON.stringify(next));
    } catch {
      // A per-device convenience.
    }
  };
  return [order, save];
}

/**
 * Muse's bottom bar: a floating frosted pill, five glyphs, icons only; the current tab shows its
 * filled glyph on an ink disc (the companion's color here). Badges: approvals on 对话, new posts
 * on 动态, plan changes on 目标.
 */
export function MuseTabBar({
  tab,
  onTab,
  badges,
  order,
}: {
  tab: MuseTab;
  onTab: (tab: MuseTab) => void;
  badges: Partial<Record<MuseTab, number>>;
  /** The sidebar's order. */
  order?: MuseTab[];
}) {
  const tabs = order ? order.map((id) => TABS.find((t) => t.id === id)!) : TABS;
  return (
    <nav aria-label="主导航" className="flex justify-center">
      <ul className="c-float flex w-full max-w-[420px] items-center gap-0.5 rounded-full px-3.5 py-1.5">
        {tabs.map((item) => {
          const active = tab === item.id;
          const badge = badges[item.id] ?? 0;
          return (
            <li key={item.id} className="min-w-0 flex-1">
              <button
                type="button"
                onClick={() => onTab(item.id)}
                aria-label={badge ? `${item.label}（${badge}）` : item.label}
                aria-current={active ? "page" : undefined}
                className={cn("flex h-[46px] w-full items-center justify-center", active ? "text-om-ink-fg" : "text-om-muted")}
              >
                <span
                  className={cn(
                    "relative flex h-[46px] w-full max-w-[60px] items-center justify-center rounded-full transition-colors duration-300",
                    active && "bg-om-ink",
                  )}
                >
                  {item.icon(active)}
                  {badge > 0 && (
                    <span className="absolute right-2.5 top-1 flex h-[17px] min-w-[17px] items-center justify-center rounded-full bg-om-danger px-1 text-[10px] font-bold text-white">
                      {badge}
                    </span>
                  )}
                </span>
              </button>
            </li>
          );
        })}
      </ul>
    </nav>
  );
}

type Pane = 0 | 1 | 2 | 3;

/** Tap the face: the companion's own page, as Muse draws it. */
export function StatusSheet({
  open,
  onClose,
  face,
  name,
  statusLine,
  working,
  onStop,
  approvals,
  sessionKey,
  history,
  computer,
  onDecide,
  onTaught,
  identity,
}: {
  open: boolean;
  onClose: () => void;
  face: ReactNode;
  name: string;
  statusLine: string;
  working: boolean;
  onStop: () => void;
  approvals: ApprovalRecord[];
  sessionKey?: string;
  history: UIMessage[];
  /** The browser it works in (polled by the screen). */
  computer: ComputerStatus | null;
  onDecide: (approval: ApprovalRecord, decision: "approve" | "reject", scope?: ApprovalScope) => Promise<void>;
  /** 教它做 from the browser: the steps the user did. */
  onTaught: (steps: TaughtStep[]) => void;
  /** 名字与形象, at the top of 身份与记忆. */
  identity?: ReactNode;
}) {
  const { token } = useClient();
  const pending = approvals.filter((a) => a.status === "pending");
  const [pane, setPane] = useState<Pane>(0);
  // Open full screen: "watch" keeps the companion in control, "take" (去接手) gives it to the user.
  const [browserOpen, setBrowserOpen] = useState<false | "watch" | "take">(false);
  const [jobs, setJobs] = useState<SessionAutomationJob[]>([]);

  useEffect(() => {
    if (!open) return;
    setPane(computer?.handoff ? 2 : pending.length > 0 ? 1 : 0);
    fetchAutomations(token).then((payload) => setJobs(payload.jobs)).catch(() => setJobs([]));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  const log = useMemo(() => (open ? activityLog(approvals, jobs, history) : []), [open, approvals, jobs, history]);

  const panes: Array<{ icon: ReactNode; label: string; badge?: number }> = [
    { icon: <ClipboardList size={22} />, label: "做过的事" },
    { icon: <ShieldCheck size={22} />, label: "等你确认", badge: pending.length + (computer?.handoff ? 1 : 0) },
    { icon: <Globe size={22} />, label: "浏览器" },
    { icon: <Fingerprint size={22} />, label: "身份与记忆" },
  ];

  return (
    <MuseSheet open={open} onClose={() => !browserOpen && onClose()}>
      <div className="relative flex flex-col items-center px-1 pb-3 text-center">
        {face}
        <div className="mt-2.5 text-[21px] font-semibold leading-[26px]">{name}</div>
        <div className={cn("mt-0.5 flex items-center gap-1.5 text-[14px]", working || pending.length > 0 ? "text-om-fg" : "text-om-muted")}>
          {working && <Loader2 size={13} className="animate-spin text-om-accent" />}
          <span className="line-clamp-2">{statusLine}</span>
        </div>
        {working && (
          <button
            type="button"
            onClick={onStop}
            className="mt-2.5 inline-flex items-center gap-1.5 rounded-full bg-om-fg px-4 py-1.5 text-[13.5px] font-semibold text-om-bg transition active:scale-95"
          >
            <Square size={12} fill="currentColor" /> 停下
          </button>
        )}
      </div>

      {/* Muse's four-way switch: glyphs on a bar, a raised pill under the current one. */}
      <div role="tablist" className="mb-5 flex rounded-2xl bg-om-surface-2 p-1">
        {panes.map((p, i) => (
          <button
            key={p.label}
            type="button"
            role="tab"
            aria-selected={pane === i}
            aria-label={p.label}
            title={p.label}
            onClick={() => setPane(i as Pane)}
            className={cn("relative flex h-11 flex-1 items-center justify-center rounded-[13px] text-om-fg transition", pane === i && "bg-om-surface shadow-sm")}
          >
            {p.icon}
            {p.badge ? (
              <span className="absolute right-1.5 top-1 flex h-[17px] min-w-[17px] items-center justify-center rounded-full bg-om-danger px-1 text-[10px] font-bold text-white">
                {p.badge}
              </span>
            ) : null}
          </button>
        ))}
      </div>

      {pane === 0 &&
        (log.length ? (
          <div className="-mx-4 -mt-5">
            <ActivityLog entries={log} />
          </div>
        ) : (
          <p className="py-6 text-center text-[13.5px] text-om-muted">还没做过什么事。</p>
        ))}
      {pane === 1 && (
        <div className="flex flex-col gap-4">
          {computer?.handoff && <HandoffCard reason={computer.handoff.reason} onOpen={() => setBrowserOpen("take")} />}
          {pending.length === 0 && !computer?.handoff ? (
            <p className="text-center text-[13.5px] text-om-muted">没有等你确认的事。</p>
          ) : (
            pending.map((approval) => (
              <div key={approval.id} className="flex flex-col gap-1">
                {sessionKey && approval.session_key && approval.session_key !== sessionKey && (
                  <span className="px-1 text-[12px] text-om-muted">来自后台任务</span>
                )}
                <ApprovalCard approval={approval} onDecide={onDecide} />
              </div>
            ))
          )}
        </div>
      )}
      {pane === 2 && open && !browserOpen && <BrowserPane name={name} status={computer} onOpen={() => setBrowserOpen("watch")} />}
      {browserOpen && (
        <BrowserFullscreen
          name={name}
          take={browserOpen === "take"}
          onTaught={(steps) => {
            onClose();
            onTaught(steps);
          }}
          onClose={() => setBrowserOpen(false)}
        />
      )}
      {pane === 3 && <SoulMemoryPane identity={identity} />}
    </MuseSheet>
  );
}

/** In 等你确认: the companion is waiting for the user to do something in the browser. */
function HandoffCard({ reason, onOpen }: { reason: string; onOpen: () => void }) {
  return (
    <div className="overflow-hidden rounded-2xl bg-[#1e1e1e] text-[#f1efe8]">
      <div className="flex items-center gap-2 px-3.5 pt-3 text-[11.5px] font-semibold text-rose-300">
        <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-rose-400" /> 浏览器 · 等你接手
      </div>
      <div className="px-3.5 pb-3 pt-1.5 text-[15px] leading-snug">{reason}</div>
      <button type="button" onClick={onOpen} className="flex w-full items-center justify-center gap-1.5 bg-om-accent py-2.5 text-[14px] font-semibold text-om-accent-fg">
        <Hand size={15} /> 去接手
      </button>
    </div>
  );
}

/** Its daily routine: the next scheduled things, in order. */
const TRIGGER_EMOJI: Record<Trigger["kind"], string> = { email: "📧", calendar: "📅", page: "🌐", webhook: "🔗" };

function triggerWhen(trigger: Trigger): string {
  if (trigger.kind === "webhook") return trigger.fired_at ? `上次触发 ${dayLabel(trigger.fired_at)}` : "收到通知时";
  const every = trigger.every ?? 60;
  const span = every % 60 === 0 ? `${every / 60} 小时` : `${every} 分钟`;
  return trigger.kind === "email" ? `每 ${span}看一次新邮件` : trigger.kind === "calendar" ? `每 ${span}看一次日程` : `每 ${span}看一次网页`;
}

/** 自动触发: what it does when something happens, each with pause and remove. ``owner``: only that
helper's (on its profile); without it, all of them, a helper's marked with its name. */
export function TriggerList({ name, owner }: { name: string; owner?: string }) {
  const { client, token } = useClient();
  const [triggers, setTriggers] = useState<Trigger[] | null>(null);
  const [copied, setCopied] = useState<string | null>(null);
  const mine = useCallback((all: Trigger[]) => (owner ? all.filter((t) => t.owner === owner) : all), [owner]);
  useEffect(() => {
    fetchTriggers(token).then((all) => setTriggers(mine(all))).catch(() => setTriggers([]));
  }, [token, mine]);
  const change = (id: string, action: "remove" | "pause" | "resume") =>
    changeTrigger(client, id, action).then((all) => setTriggers(mine(all))).catch(() => undefined);
  const copy = (trigger: Trigger) => {
    if (!trigger.hook_url) return;
    void copyTextToClipboard(trigger.hook_url).then((ok) => ok && setCopied(trigger.id));
  };
  return (
    <>
      {owner ? (
        // On a helper's profile, under its 例行 and in the same style.
        <h4 className="c-muted mb-1.5 mt-5 text-[13px] font-medium">自动触发</h4>
      ) : (
        <div className="mb-1.5 mt-4 text-[12px] font-semibold text-om-muted">自动触发</div>
      )}
      {triggers === null ? null : triggers.length === 0 ? (
        <p className="text-[13.5px] text-om-muted">
          {owner
            ? `还没有。在${name}的聊天里说「来了新邮件就帮我分一下类」「这个网页变了告诉我」，它就会自己设好。`
            : `还没有。跟${name}说「来了重要邮件告诉我」「开会前 15 分钟提醒我」或「这个网页降价了叫我」，就会设好。`}
        </p>
      ) : (
        <ul className="divide-y divide-[color:var(--om-border)] overflow-hidden rounded-2xl bg-om-surface-2">
          {triggers.map((trigger) => (
            <li key={trigger.id} className={cn("flex items-center gap-3 px-3.5 py-2.5", trigger.paused && "opacity-60")}>
              <span className="text-[18px] leading-none" aria-hidden="true">
                {TRIGGER_EMOJI[trigger.kind]}
              </span>
              <div className="min-w-0 flex-1">
                <div className="truncate text-[14.5px] font-medium">{trigger.name}</div>
                <div className="truncate text-[12px] text-om-muted">
                  {trigger.paused ? "已暂停" : triggerWhen(trigger)}
                  {!owner && trigger.owner_name ? ` · ${trigger.owner_name}负责` : ""} · {trigger.instruction}
                </div>
              </div>
              {trigger.hook_url && (
                <button type="button" onClick={() => copy(trigger)} className="shrink-0 text-[12.5px] font-medium text-om-accent">
                  {copied === trigger.id ? "已复制" : "复制地址"}
                </button>
              )}
              <button
                type="button"
                onClick={() => change(trigger.id, trigger.paused ? "resume" : "pause")}
                className="shrink-0 text-[12.5px] text-om-muted"
              >
                {trigger.paused ? "继续" : "暂停"}
              </button>
              <button type="button" onClick={() => change(trigger.id, "remove")} className="shrink-0 text-[12.5px] text-om-danger">
                删除
              </button>
            </li>
          ))}
        </ul>
      )}
    </>
  );
}
