/*
 * Ported from nanoMuse (vendor/nanomuse-web/web/src/screens/GoalsScreen.tsx, GPL-3.0-or-later):
 * Muse's goals page — "在跟进" (goals with a checkbox and a two-line status), the routines with
 * their switches, then "定个新目标" by life area. A plan change the companion proposes waits on
 * top until the user picks; a goal opens into its detail sheet.
 */
import {
  AlertCircle,
  Bell,
  BellRing,
  CalendarDays,
  CheckCircle2,
  ChevronRight,
  Circle,
  CircleDashed,
  FileText,
  Loader2,
  MessageCircle,
  MoreVertical,
  OctagonAlert,
  Pause,
  Play,
  Plus,
  Sparkles,
  Square,
  SquareCheck,
  Trash2,
  Zap,
} from "lucide-react";
import { useCallback, useEffect, useState, type ReactNode } from "react";

import { fetchAutomations, runAutomationAction } from "@/lib/api";
import type { SessionAutomationJob } from "@/lib/types";
import { cn } from "@/lib/utils";
import { useClient } from "@/providers/ClientProvider";

import {
  addGoal,
  changeGoal,
  fetchGoals,
  type ApprovalRecord,
  type ApprovalScope,
  type Goal,
  type GoalCategory,
  type GoalChanges,
  type GoalStep,
  type ComputerStatus,
  type GoalLogEntry,
  fetchRoutines,
  type Routines,
} from "../companion-api";
import { Switch } from "../Menu";
import { ProactivePanel } from "../ProactivePanel";
import { routinesShort } from "../Settings";
import { TriggerList } from "./Shell";
import { dayLabel } from "../presentation";
import { describeSchedule, visibleJobs } from "../TabViews";
import { CADENCES, CATEGORIES, WEEKDAYS, categoryOf, describeCadence, dueLabel, joinCadence, splitCadence, weekdayName } from "./goals";
import { MusePageTitle, MuseRoundButton } from "./MuseHeader";
import { BrowserFullscreen, BrowserPane } from "./BrowserPane";
import { MuseSheet } from "./Sheet";

const STEP_ICON: Record<GoalStep["status"], (p: { size: number; className?: string }) => ReactNode> = {
  pending: (p) => <Circle {...p} />,
  doing: (p) => <CircleDashed {...p} className={cn(p.className, "text-om-accent")} />,
  done: (p) => <CheckCircle2 {...p} className={cn(p.className, "text-emerald-500")} />,
  blocked: (p) => <OctagonAlert {...p} className={cn(p.className, "text-rose-500")} />,
};

const STEP_NEXT: Record<GoalStep["status"], GoalStep["status"]> = {
  pending: "done",
  doing: "done",
  done: "pending",
  blocked: "pending",
};

function useGoals() {
  const { token } = useClient();
  const [goals, setGoals] = useState<Goal[]>([]);
  const refresh = useCallback(() => {
    fetchGoals(token).then(setGoals).catch(() => undefined);
  }, [token]);
  return { goals, setGoals, refresh };
}

export function GoalsScreen({
  active,
  name,
  approvals,
  onDecide,
  onSend,
  working = null,
  computer = null,
  onOpenCreations,
}: {
  active: boolean;
  name: string;
  /** What the companion is doing right now (null when idle): its activity, and what it was asked. */
  working?: { label: string | null; asked: string } | null;
  /** Its browser, shown live on a goal it is working on. */
  computer?: ComputerStatus | null;
  /** A file in a goal's record: to 资源库. */
  onOpenCreations?: () => void;
  /** Plan changes the companion proposed wait here too (they are approval cards in the chat). */
  approvals: ApprovalRecord[];
  onDecide: (approval: ApprovalRecord, decision: "approve" | "reject", scope?: ApprovalScope) => Promise<void>;
  /** Sends a message in the chat and shows it. */
  onSend: (text: string) => void;
}) {
  const { client, token } = useClient();
  const { goals, setGoals, refresh } = useGoals();
  const [jobs, setJobs] = useState<SessionAutomationJob[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [sheetCategory, setSheetCategory] = useState<(typeof CATEGORIES)[number] | null>(null);
  // 早报与主动关心: set in a sheet from the top of 例行.
  const [proactive, setProactive] = useState(false);
  const [routines, setRoutines] = useState<Routines | null>(null);
  useEffect(() => {
    if (active) fetchRoutines(token).then(setRoutines).catch(() => undefined);
  }, [active, token]);
  const [allFinished, setAllFinished] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const refreshJobs = useCallback(() => {
    fetchAutomations(token)
      .then((payload) => setJobs(visibleJobs(payload.jobs).filter((job) => job.name !== "每日早报")))
      .catch(() => undefined);
  }, [token]);

  useEffect(() => {
    if (!active) return;
    refresh();
    refreshJobs();
  }, [active, refresh, refreshJobs]);

  const proposals = approvals.filter((a) => a.status === "pending" && a.action === "goal_plan");
  // Everything tracked in one place: the live goals first, the paused ones after.
  const tracked = [...goals.filter((g) => !g.done && !g.paused), ...goals.filter((g) => !g.done && g.paused)];
  const finished = goals.filter((g) => g.done);
  const goal = goals.find((g) => g.id === selected) ?? null;

  const change = async (g: Goal, changes: GoalChanges) => {
    try {
      setGoals(await changeGoal(client, g.id, "update", changes));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  const toggleJob = async (job: SessionAutomationJob) => {
    try {
      const payload = await runAutomationAction(client, job.enabled ? "disable" : "enable", job.id);
      setJobs(visibleJobs(payload.jobs).filter((j) => j.name !== "每日早报"));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  /** The category sheet's 开始: the goal is shaped in the chat, the way Muse does it. */
  const startGoal = (c: (typeof CATEGORIES)[number]) => {
    setSheetCategory(null);
    onSend(`我想定一个${c.label}方面的目标。一次问我一个简短的问题（具体想达成什么、为什么、什么时候之前、多久问我一次进展），问清楚后用目标工具建好，拆成具体的步骤。`);
  };

  return (
    <div className="pb-6">
      <div className="flex items-center gap-3 pr-5">
        <MusePageTitle className="flex-1">目标</MusePageTitle>
        <MuseRoundButton className="h-10 w-10" onClick={() => setCreating(true)} label="新目标">
          <Plus size={22} />
        </MuseRoundButton>
      </div>
      {error && (
        <p className="px-5 pb-2 text-[13px] text-om-danger" role="alert">
          {error}
        </p>
      )}

      {proposals.length > 0 && (
        <section className="px-4 pb-2">
          <div className="mb-2 px-1 text-[12px] font-semibold text-om-muted">{name}的建议</div>
          <div className="space-y-2.5">
            {proposals.map((approval) => (
              <ProposalCard key={approval.id} approval={approval} name={name} onDecide={onDecide} />
            ))}
          </div>
        </section>
      )}

      <SectionHeader label="在跟进" dot />
      {tracked.length === 0 ? (
        <p className="px-5 pb-3 text-[13px] text-om-muted">还没有在跟进的目标</p>
      ) : (
        tracked.map((g) => <GoalRow key={g.id} goal={g} onOpen={() => setSelected(g.id)} onToggle={() => void change(g, { done: !g.done })} />)
      )}

      {finished.length > 0 && (
        <>
          <div className="h-1.5" />
          <SectionHeader label="已完成" />
          {(allFinished ? finished : finished.slice(0, 2)).map((g) => (
            <GoalRow key={g.id} goal={g} onOpen={() => setSelected(g.id)} onToggle={() => void change(g, { done: !g.done })} />
          ))}
          {!allFinished && finished.length > 2 && (
            <button type="button" onClick={() => setAllFinished(true)} className="px-5 py-2 text-[13.5px] font-medium text-om-accent">
              再看 {finished.length - 2} 个
            </button>
          )}
        </>
      )}

      {/* 例行: everything that runs on its own (the one place for it). */}
      <div className="h-1.5" />
      <SectionHeader label="例行" />
      <button type="button" onClick={() => setProactive(true)} className="flex w-full items-center gap-3 py-2 pl-5 pr-4 text-left">
        <BellRing size={20} className="shrink-0 text-om-accent" aria-hidden="true" />
        <span className="min-w-0 flex-1">
          <span className="block truncate text-[15px] font-semibold">早报与主动关心</span>
          <span className="block truncate text-[12.5px] text-om-muted">{routinesShort(routines) ?? "…"}</span>
        </span>
        <ChevronRight size={18} className="shrink-0 text-om-muted" aria-hidden="true" />
      </button>
      {jobs.length > 0 && (
        <>
          {jobs.map((job) => (
            <div key={job.id} className="flex items-center gap-3 py-2 pl-5 pr-4">
              <Zap size={20} className="shrink-0 text-om-accent" aria-hidden="true" />
              <div className="min-w-0 flex-1">
                <div className="truncate text-[15px] font-semibold">{job.name}</div>
                <div className="truncate text-[12.5px] text-om-muted">
                  {job.owner ? `${job.owner.emoji} ${job.owner.name} · ` : ""}
                  {describeSchedule(job)}
                  {job.enabled && job.state.next_run_at_ms ? ` · 下次 ${dayLabel(job.state.next_run_at_ms)}` : ""}
                  {!job.enabled ? " · 已暂停" : ""}
                </div>
              </div>
              <Switch checked={job.enabled} label={job.name} onChange={() => void toggleJob(job)} />
            </div>
          ))}
        </>
      )}
      <div className="px-5">
        <TriggerList name={name} />
      </div>
      <MuseSheet
        open={proactive}
        onClose={() => {
          setProactive(false);
          fetchRoutines(token).then(setRoutines).catch(() => undefined);
          refreshJobs();
        }}
        title="早报与主动关心"
      >
        <ProactivePanel />
      </MuseSheet>

      <div className="mt-2.5 border-t border-om-border px-5 pb-1.5 pt-3.5">
        <h3 className="text-[17px] font-semibold">定个新目标</h3>
      </div>
      {CATEGORIES.filter((c) => c.id !== "other").map((c) => (
        <button
          key={c.id}
          type="button"
          onClick={() => setSheetCategory(c)}
          className="flex h-[50px] w-full items-center gap-3.5 pl-5 pr-2 text-left active:bg-om-surface-2"
        >
          <span className="text-om-fg">{c.icon({ size: 22 })}</span>
          <span className="flex-1 text-[15px] font-medium">{c.label}</span>
          <span className="flex h-10 w-10 items-center justify-center text-om-muted">
            <Plus size={22} />
          </span>
        </button>
      ))}

      <GoalDetail
        goal={goal}
        name={name}
        live={goal && working && working.asked.includes(goal.title) ? working : null}
        computer={computer}
        onOpenCreations={() => {
          setSelected(null);
          onOpenCreations?.();
        }}
        onClose={() => setSelected(null)}
        onChange={(changes) => (goal ? change(goal, changes) : Promise.resolve())}
        onCheckIn={async () => {
          if (!goal) return;
          try {
            setGoals(await changeGoal(client, goal.id, "checkin"));
            setSelected(null);
          } catch (e) {
            setError(e instanceof Error ? e.message : String(e));
          }
        }}
        onRemove={async () => {
          if (!goal || !window.confirm(`删除「${goal.title}」？`)) return;
          try {
            setGoals(await changeGoal(client, goal.id, "remove"));
            setSelected(null);
          } catch (e) {
            setError(e instanceof Error ? e.message : String(e));
          }
        }}
        onSend={(text) => {
          setSelected(null);
          onSend(text);
        }}
      />
      <NewGoalSheet
        open={creating}
        name={name}
        onClose={() => setCreating(false)}
        onCreated={(next) => {
          setCreating(false);
          setGoals(next);
        }}
        onSend={(text) => {
          setCreating(false);
          onSend(text);
        }}
      />
      <MuseSheet open={sheetCategory !== null} onClose={() => setSheetCategory(null)}>
        {sheetCategory && (
          <div className="px-1 pb-2">
            <h3 className="text-[20px] font-bold">定一个{sheetCategory.label}目标</h3>
            <p className="c-muted mt-2 text-[15px] leading-[22px]">先在聊天里说清楚想要什么，定好后进度在这里看。</p>
            <button
              type="button"
              onClick={() => startGoal(sheetCategory)}
              className="mt-6 flex h-[50px] w-full items-center justify-center gap-2 rounded-full bg-om-accent text-[16px] font-semibold text-om-accent-fg"
            >
              <MessageCircle size={18} /> 开始
            </button>
          </div>
        )}
      </MuseSheet>
    </div>
  );
}

/** Muse's section header: an optional dot, the label, a "+" at the end. */
function SectionHeader({ label, dot = false, onAdd }: { label: string; dot?: boolean; onAdd?: () => void }) {
  return (
    <div className="flex items-center pl-5 pr-2 pt-1">
      {dot && <span className="mr-2.5 h-2 w-2 rounded-full bg-emerald-500" />}
      <span className={cn("flex-1 text-[15px] font-semibold", dot && "text-emerald-600 dark:text-emerald-400")}>{label}</span>
      {onAdd ? (
        <button type="button" onClick={onAdd} aria-label={`添加${label}`} className="flex h-10 w-10 items-center justify-center text-om-muted">
          <Plus size={22} />
        </button>
      ) : (
        <span className="h-10" />
      )}
    </div>
  );
}

function CategoryBadge({ id, size = "sm" }: { id: GoalCategory | null | undefined; size?: "sm" | "md" }) {
  const c = categoryOf(id);
  if (!c) return null;
  return (
    <span className={cn("inline-flex items-center gap-1 rounded-full font-medium", c.tone, size === "sm" ? "px-2 py-0.5 text-[11.5px]" : "px-2.5 py-1 text-[12.5px]")}>
      {c.icon({ size: size === "sm" ? 11 : 13 })} {c.label}
    </span>
  );
}

function progress(goal: Goal): { done: number; total: number } {
  return { done: goal.steps.filter((s) => s.status === "done").length, total: goal.steps.length };
}

/** One goal the way Muse lists it: a checkbox, the title, a two-line status, the cadence and progress. */
function GoalRow({ goal, onOpen, onToggle }: { goal: Goal; onOpen: () => void; onToggle: () => void }) {
  const { done: doneSteps, total } = progress(goal);
  const pct = total ? Math.round((doneSteps / total) * 100) : 0;
  const due = dueLabel(goal.due);
  const next = goal.steps.find((s) => s.status !== "done");
  const subtitle = next ? `下一步：${next.text}` : "";
  const cadence = goal.paused ? "已暂停" : goal.done ? "已完成" : goal.checkin ? `${describeCadence(goal.checkin)}问你` : "不定期跟进";
  return (
    <div className="flex items-start pb-2.5 pl-[18px] pr-1 pt-2">
      <button
        type="button"
        onClick={onToggle}
        aria-label={goal.done ? "标为没完成" : "标为完成"}
        className="mt-0.5 flex h-7 w-7 shrink-0 items-center justify-center text-om-muted"
      >
        {goal.done ? <SquareCheck size={22} /> : <Square size={22} />}
      </button>
      <button type="button" onClick={onOpen} className="ml-2.5 min-w-0 flex-1 pt-[3px] text-left">
        <span className={cn("line-clamp-2 block text-[15px] font-semibold leading-snug", goal.done && "text-om-muted")}>{goal.title}</span>
        {subtitle && !goal.done && <span className="mt-0.5 line-clamp-2 block text-[13px] leading-[18px] text-om-muted">{subtitle}</span>}
        <span className="mt-0.5 flex items-center gap-2 text-[12px] text-om-muted">
          <span className="truncate">
            {cadence}
            {due && !goal.done && !goal.paused ? ` · ${due}` : ""}
          </span>
          {pct > 0 && !goal.done && <span className="shrink-0 font-medium text-emerald-600 dark:text-emerald-400">{pct}%</span>}
        </span>
      </button>
      <button type="button" onClick={onOpen} aria-label={`「${goal.title}」详情`} className="flex h-10 w-10 shrink-0 items-center justify-center text-om-muted">
        <MoreVertical size={20} />
      </button>
    </div>
  );
}

/** "要调整计划吗" — the companion's proposal: 用新计划 swaps the open steps, 保持原样 keeps the plan. */
function ProposalCard({
  approval,
  name,
  onDecide,
}: {
  approval: ApprovalRecord;
  name: string;
  onDecide: (approval: ApprovalRecord, decision: "approve" | "reject") => Promise<void>;
}) {
  const [busy, setBusy] = useState(false);
  const goal = approval.context?.goal;
  const steps = Array.isArray(approval.arguments.steps) ? approval.arguments.steps.filter((s): s is string => typeof s === "string") : [];
  const reason = typeof approval.arguments.reason === "string" ? approval.arguments.reason : "";
  const kept = goal?.steps.filter((s) => s.status === "done").length ?? 0;
  const act = async (accept: boolean) => {
    setBusy(true);
    try {
      await onDecide(approval, accept ? "approve" : "reject");
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="rounded-3xl border border-om-border bg-om-accent-soft px-4 py-3.5">
      <div className="flex items-center gap-2 text-[12px] font-semibold text-om-accent">
        <Sparkles size={13} /> 要调整「{goal?.title ?? "目标"}」的计划吗？
      </div>
      {reason && <p className="mt-1.5 text-[14px] leading-snug">{reason}</p>}
      <div className="mt-2 text-[12px] text-om-muted">
        {kept > 0 && <span>已完成的 {kept} 步保留。</span>}
        新的剩余步骤：
      </div>
      <ol className="mt-1 list-decimal space-y-0.5 pl-5 text-[13.5px]">
        {steps.map((s, i) => (
          <li key={i}>{s}</li>
        ))}
      </ol>
      <div className="mt-3 flex gap-2">
        <button type="button" disabled={busy} onClick={() => void act(false)} className="flex-1 rounded-2xl border border-om-border py-2 text-[13.5px] font-medium disabled:opacity-50">
          保持原样
        </button>
        <button type="button" disabled={busy} onClick={() => void act(true)} className="flex-1 rounded-2xl bg-om-accent py-2 text-[13.5px] font-medium text-om-accent-fg disabled:opacity-50">
          用{name}的计划
        </button>
      </div>
    </div>
  );
}

function CategoryPicker({ value, onChange }: { value: GoalCategory | null; onChange: (v: GoalCategory | null) => void }) {
  return (
    <div className="flex flex-wrap gap-1.5">
      {CATEGORIES.map((c) => (
        <button
          key={c.id}
          type="button"
          aria-pressed={value === c.id}
          onClick={() => onChange(value === c.id ? null : c.id)}
          className={cn(
            "inline-flex items-center gap-1 rounded-full border px-2.5 py-1 text-[12.5px] font-medium transition",
            value === c.id ? cn(c.tone, "border-transparent ring-2 ring-om-accent") : "border-om-border text-om-muted",
          )}
        >
          {c.icon({ size: 12 })} {c.label}
        </button>
      ))}
    </div>
  );
}

function CadencePicker({ value, onChange }: { value: string; onChange: (spec: string) => void }) {
  const { cadence, anchor, time } = splitCadence(value);
  const set = (c: string, a: string, at: string) => onChange(joinCadence(c, a, at));
  return (
    <div className="space-y-2">
      <div className="flex flex-wrap gap-1.5">
        {CADENCES.map((c) => (
          <button
            key={c.id}
            type="button"
            aria-pressed={cadence === c.id}
            onClick={() => set(c.id, c.id === "weekly" ? anchor || "mon" : c.id === "monthly" ? anchor || "1" : "", time)}
            className={cn(
              "rounded-full border px-2.5 py-1 text-[12.5px] font-medium transition",
              cadence === c.id ? "border-transparent bg-om-accent text-om-accent-fg" : "border-om-border text-om-muted",
            )}
          >
            {c.label}
          </button>
        ))}
      </div>
      {cadence && (
        <div className="flex items-center gap-2 text-[13.5px]">
          {cadence === "weekly" && (
            <select value={anchor || "mon"} onChange={(e) => set(cadence, e.target.value, time)} aria-label="星期几" className="rounded-xl bg-om-surface-2 px-2.5 py-1.5 outline-none">
              {WEEKDAYS.map((d) => (
                <option key={d} value={d}>
                  {weekdayName(d)}
                </option>
              ))}
            </select>
          )}
          {cadence === "monthly" && (
            <select value={anchor || "1"} onChange={(e) => set(cadence, e.target.value, time)} aria-label="几号" className="rounded-xl bg-om-surface-2 px-2.5 py-1.5 outline-none">
              {Array.from({ length: 28 }, (_, i) => String(i + 1)).map((d) => (
                <option key={d} value={d}>
                  {d} 日
                </option>
              ))}
            </select>
          )}
          <input
            type="time"
            value={time}
            onChange={(e) => set(cadence, anchor, e.target.value || "09:00")}
            aria-label="时间"
            className="rounded-xl bg-om-surface-2 px-2.5 py-1.5 outline-none"
          />
        </div>
      )}
    </div>
  );
}

/** One line of a goal's work record: what happened, who did it, when. */
function LogRow({ entry, onOpenFile }: { entry: GoalLogEntry; onOpenFile: () => void }) {
  const icon =
    entry.kind === "helper" || entry.kind === "helper_done" ? (
      <span className="text-[15px] leading-none" aria-hidden="true">{entry.emoji || "🧑‍💼"}</span>
    ) : entry.kind === "file" ? (
      <FileText size={15} />
    ) : entry.status === "done" || entry.kind === "goal" ? (
      <CheckCircle2 size={15} />
    ) : entry.status === "blocked" ? (
      <AlertCircle size={15} className="text-om-danger" />
    ) : (
      <Circle size={15} />
    );
  return (
    <li className="flex gap-2.5 py-2">
      <span className="mt-0.5 grid w-5 shrink-0 place-items-center text-om-muted">{icon}</span>
      <div className="min-w-0 flex-1">
        {entry.kind === "file" ? (
          <button type="button" onClick={onOpenFile} className="text-left text-[14px] font-medium text-om-accent">
            做出了文件：{entry.text}
          </button>
        ) : (
          <p className="line-clamp-3 text-[14px] leading-snug">{entry.text}</p>
        )}
        {entry.note && <p className="mt-0.5 text-[12.5px] text-om-muted">{entry.note}</p>}
        <p className="mt-0.5 text-[12px] text-om-muted">{dayLabel(entry.at)}</p>
      </div>
    </li>
  );
}

function GoalDetail({
  goal,
  name,
  live,
  computer,
  onOpenCreations,
  onClose,
  onChange,
  onCheckIn,
  onRemove,
  onSend,
}: {
  goal: Goal | null;
  name: string;
  /** The companion is working on this goal right now. */
  live: { label: string | null; asked: string } | null;
  computer: ComputerStatus | null;
  onOpenCreations: () => void;
  onClose: () => void;
  onChange: (changes: GoalChanges) => Promise<void>;
  onCheckIn: () => Promise<void>;
  onRemove: () => Promise<void>;
  onSend: (text: string) => void;
}) {
  const [newStep, setNewStep] = useState("");
  const [editing, setEditing] = useState(false);
  const [watching, setWatching] = useState(false);
  const [allLog, setAllLog] = useState(false);
  const log = [...(goal?.log ?? [])].reverse();
  const due = goal ? dueLabel(goal.due) : "";
  const { done, total } = goal ? progress(goal) : { done: 0, total: 0 };

  const addStep = async () => {
    if (!newStep.trim()) return;
    await onChange({ add_step: newStep.trim() });
    setNewStep("");
  };

  return (
    <MuseSheet
      open={!!goal}
      onClose={() => {
        setEditing(false);
        onClose();
      }}
      title={goal?.title}
      footer={
        goal && (
          <div className="flex gap-2">
            {!goal.done && (
              <button
                type="button"
                onClick={() => void onChange({ paused: !goal.paused })}
                className="flex flex-1 items-center justify-center gap-1.5 rounded-2xl border border-om-border py-2.5 font-medium"
              >
                {goal.paused ? <Play size={16} /> : <Pause size={16} />} {goal.paused ? "继续" : "暂停"}
              </button>
            )}
            {!goal.done && !goal.paused && (
              <button
                type="button"
                onClick={() => onSend(`继续推进我的目标「${goal.title}」：看看下一步，能做的现在就做。`)}
                className="flex flex-1 items-center justify-center gap-1.5 rounded-2xl bg-om-accent py-2.5 font-medium text-om-accent-fg"
              >
                <Play size={16} /> 现在就推进
              </button>
            )}
            <button type="button" onClick={() => void onRemove()} aria-label="删除目标" className="rounded-2xl border border-om-border px-3 text-om-muted">
              <Trash2 size={18} />
            </button>
          </div>
        )
      }
    >
      {goal && (
        <div className="space-y-4">
          {live && (
            // Working on it now: what it is doing, and its browser when it is in one.
            <div className="rounded-2xl bg-om-surface-2 px-3.5 py-3">
              <div className="flex items-center gap-2 text-[13.5px] font-medium">
                <Loader2 size={14} className="animate-spin text-om-accent" />
                {name}正在推进{live.label ? ` · ${live.label}` : ""}
              </div>
              {computer?.running && (
                <div className="mt-2.5">
                  <BrowserPane name={name} status={computer} onOpen={() => setWatching(true)} />
                </div>
              )}
            </div>
          )}
          {watching && <BrowserFullscreen name={name} onClose={() => setWatching(false)} />}
          {/* area · due · reminders */}
          <div className="rounded-2xl bg-om-surface-2 px-3.5 py-3">
            {!editing ? (
              <button type="button" onClick={() => setEditing(true)} className="w-full text-left">
                <div className="flex flex-wrap items-center gap-1.5 text-[12.5px]">
                  {goal.category ? (
                    <CategoryBadge id={goal.category} size="md" />
                  ) : (
                    <span className="rounded-full border border-dashed border-om-border px-2.5 py-1 text-om-muted">没有分类</span>
                  )}
                  <span className="inline-flex items-center gap-1 rounded-full bg-om-surface px-2.5 py-1 text-om-muted">
                    <CalendarDays size={13} /> {due || "没有截止日期"}
                  </span>
                  <span className="inline-flex items-center gap-1 rounded-full bg-om-surface px-2.5 py-1 text-om-muted">
                    <Bell size={13} /> {goal.checkin ? describeCadence(goal.checkin) : "不提醒"}
                  </span>
                </div>
                {goal.next_checkin_ms && !goal.paused && (
                  <div className="mt-1.5 text-[12px] text-om-muted">下次跟进 {dayLabel(goal.next_checkin_ms)}</div>
                )}
                <div className="mt-1.5 text-[12px] font-medium text-om-accent">修改</div>
              </button>
            ) : (
              <div className="space-y-3">
                <div>
                  <div className="mb-1.5 text-[12px] font-semibold text-om-muted">生活的哪个方面</div>
                  <CategoryPicker value={goal.category ?? null} onChange={(v) => void onChange({ category: v })} />
                </div>
                <div>
                  <div className="mb-1.5 text-[12px] font-semibold text-om-muted">截止日期</div>
                  <div className="flex items-center gap-2">
                    <input
                      type="date"
                      value={goal.due ?? ""}
                      onChange={(e) => void onChange({ due: e.target.value || null })}
                      aria-label="截止日期"
                      className="rounded-xl bg-om-surface px-2.5 py-1.5 text-[13.5px] outline-none"
                    />
                    {goal.due && (
                      <button type="button" onClick={() => void onChange({ due: null })} className="text-[12.5px] text-om-muted">
                        清除
                      </button>
                    )}
                  </div>
                </div>
                <div>
                  <div className="mb-1.5 text-[12px] font-semibold text-om-muted">{name}多久问你一次</div>
                  <CadencePicker value={goal.checkin ?? ""} onChange={(spec) => void onChange({ checkin: spec || null })} />
                </div>
                <button type="button" onClick={() => setEditing(false)} className="text-[12.5px] font-medium text-om-accent">
                  完成
                </button>
              </div>
            )}
          </div>

          <div>
            <div className="mb-2 text-[12px] font-semibold text-om-muted">
              计划 · {done}/{total}
            </div>
            <ul className="space-y-1">
              {goal.steps.map((s, index) => {
                const Icon = STEP_ICON[s.status];
                const finished = s.status === "done";
                return (
                  <li key={`${index}-${s.text}`} className="flex items-start gap-2.5 rounded-2xl px-2 py-1.5">
                    <button
                      type="button"
                      aria-label={finished ? `第 ${index + 1} 步标为没做` : `第 ${index + 1} 步标为完成`}
                      onClick={() => void onChange({ step: index + 1, status: STEP_NEXT[s.status] })}
                      className="mt-0.5 text-om-muted"
                    >
                      <Icon size={20} />
                    </button>
                    <div className="min-w-0 flex-1">
                      <div className={cn("text-[14.5px] leading-snug", finished && "text-om-muted line-through")}>
                        {index + 1}. {s.text}
                      </div>
                      {s.note && <div className="mt-0.5 whitespace-pre-wrap text-[12.5px] text-om-muted">{s.note}</div>}
                    </div>
                  </li>
                );
              })}
            </ul>
            <div className="mt-2 flex gap-2">
              <input
                value={newStep}
                onChange={(e) => setNewStep(e.target.value)}
                onKeyDown={(e) => e.key === "Enter" && void addStep()}
                placeholder="加一步"
                aria-label="加一步"
                className="flex-1 rounded-2xl bg-om-surface-2 px-3.5 py-2 text-[16px] outline-none focus:ring-2 focus:ring-om-accent"
              />
              <button type="button" onClick={() => void addStep()} className="rounded-2xl bg-om-surface-2 px-3 text-om-accent" aria-label="添加这一步">
                <Plus size={18} />
              </button>
            </div>
          </div>
          {log.length > 0 && (
            <div>
              <div className="mb-1 text-[12px] font-semibold text-om-muted">工作记录</div>
              <ul className="divide-y divide-[color:var(--om-border)]">
                {(allLog ? log : log.slice(0, 6)).map((entry, i) => (
                  <LogRow key={`${entry.at}-${i}`} entry={entry} onOpenFile={onOpenCreations} />
                ))}
              </ul>
              {log.length > 6 && !allLog && (
                <button type="button" onClick={() => setAllLog(true)} className="mt-1 text-[13px] font-medium text-om-accent">
                  看全部 {log.length} 条
                </button>
              )}
            </div>
          )}
          <div className="flex flex-wrap gap-x-4 gap-y-1.5">
            <button
              type="button"
              onClick={() => onSend(`说说我的目标「${goal.title}」现在进展怎么样，接下来该做什么？`)}
              className="text-[13.5px] font-medium text-om-accent"
            >
              在对话里聊聊这个目标 →
            </button>
            {goal.checkin && !goal.done && !goal.paused && (
              <button type="button" onClick={() => void onCheckIn()} className="inline-flex items-center gap-1 text-[13.5px] font-medium text-om-accent">
                <BellRing size={14} /> 现在问问我
              </button>
            )}
          </div>
        </div>
      )}
    </MuseSheet>
  );
}

function NewGoalSheet({
  open,
  name,
  onClose,
  onCreated,
  onSend,
}: {
  open: boolean;
  name: string;
  onClose: () => void;
  onCreated: (goals: Goal[]) => void;
  onSend: (text: string) => void;
}) {
  const { client } = useClient();
  const [title, setTitle] = useState("");
  const [steps, setSteps] = useState("");
  const [category, setCategory] = useState<GoalCategory | null>(null);
  const [due, setDue] = useState("");
  const [checkIn, setCheckIn] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const reset = () => {
    setTitle("");
    setSteps("");
    setCategory(null);
    setDue("");
    setCheckIn("");
    setError(null);
  };

  const create = async () => {
    if (!title.trim()) return;
    setBusy(true);
    try {
      const next = await addGoal(client, {
        title: title.trim(),
        steps: steps.split("\n").map((s) => s.trim()).filter(Boolean),
        category,
        due: due || null,
        checkin: checkIn || null,
      });
      reset();
      onCreated(next);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const askCompanion = () => {
    if (!title.trim()) return;
    const extras = [
      category ? `方面：${categoryOf(category)?.label}` : "",
      due ? `截止：${due}` : "",
      checkIn ? `跟进：${describeCadence(checkIn)}` : "",
    ].filter(Boolean);
    onSend(`帮我定一个目标：「${title.trim()}」${extras.length ? `（${extras.join("，")}）` : ""}。用目标工具拆成具体的步骤，然后告诉我计划。`);
    reset();
  };

  return (
    <MuseSheet
      open={open}
      onClose={onClose}
      title="新目标"
      footer={
        <div className="flex gap-2">
          <button
            type="button"
            onClick={askCompanion}
            disabled={!title.trim()}
            className="flex flex-1 items-center justify-center gap-1.5 rounded-2xl border border-om-border py-2.5 font-medium disabled:opacity-50"
          >
            <Sparkles size={16} /> 让{name}来定计划
          </button>
          <button
            type="button"
            onClick={() => void create()}
            disabled={!title.trim() || busy}
            className="flex-1 rounded-2xl bg-om-accent py-2.5 font-medium text-om-accent-fg disabled:opacity-50"
          >
            创建
          </button>
        </div>
      }
    >
      <div className="space-y-3.5">
        <input
          value={title}
          onChange={(e) => setTitle(e.target.value)}
          placeholder="你想达成什么？"
          aria-label="你想达成什么？"
          className="w-full rounded-2xl bg-om-surface-2 px-3.5 py-2.5 text-[16px] outline-none focus:ring-2 focus:ring-om-accent"
        />
        <div>
          <div className="mb-1.5 text-[12px] font-semibold text-om-muted">生活的哪个方面</div>
          <CategoryPicker value={category} onChange={setCategory} />
        </div>
        <div className="flex items-center gap-3">
          <div className="flex-1 text-[12px] font-semibold text-om-muted">截止日期</div>
          <input type="date" value={due} onChange={(e) => setDue(e.target.value)} aria-label="截止日期" className="rounded-xl bg-om-surface-2 px-2.5 py-1.5 text-[13.5px] outline-none" />
        </div>
        <div>
          <div className="mb-1.5 text-[12px] font-semibold text-om-muted">{name}多久问你一次</div>
          <CadencePicker value={checkIn} onChange={setCheckIn} />
        </div>
        <textarea
          value={steps}
          onChange={(e) => setSteps(e.target.value)}
          placeholder={`步骤，一行一个（可以不填，让${name}来定）`}
          aria-label="步骤"
          rows={3}
          className="w-full resize-none rounded-2xl bg-om-surface-2 px-3.5 py-2.5 text-[16px] outline-none focus:ring-2 focus:ring-om-accent"
        />
        {error && (
          <p className="text-[13px] text-om-danger" role="alert">
            {error}
          </p>
        )}
      </div>
    </MuseSheet>
  );
}
