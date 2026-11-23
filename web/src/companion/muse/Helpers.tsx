/*
 * 帮手: the helpers the companion calls on (as OpenBot's coworkers). Each has a name, an emoji, a
 * one-line job and a role description; the companion hands them work on its own, or when the user
 * names one in a message (@记账 …). This page lists them with what they are doing, opens one to see
 * what it did and the notes it keeps, and makes new ones from scratch or from a template.
 */
import { Check, ChevronDown, Copy, Download, ImagePlus, Loader2, MessageCircle, Plus, Sparkles, Trash2, UserPlus, Users, X } from "lucide-react";
import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";

import { fetchAutomations, runAutomationAction } from "@/lib/api";
import { copyTextToClipboard } from "@/lib/clipboard";
import type { SessionAutomationJob } from "@/lib/types";
import { cn } from "@/lib/utils";
import { useClient } from "@/providers/ClientProvider";

import {
  changeGroup,
  changeHabit,
  changeHelper,
  drawHelperAvatar,
  fetchHelpers,
  helperShareText,
  parseHelperShare,
  fetchModels,
  type Helper,
  type HelperFields,
  type HelperGroup,
  type HelperHabit,
  type HelpersPayload,
} from "../companion-api";
import { MarkdownText } from "@/components/MarkdownText";
import { Switch } from "../Menu";
import { dayLabel } from "../presentation";
import { describeSchedule, PageTitle } from "../TabViews";
import { EmojiIcon } from "../EmojiIcon";
import { FACE_CHOICES } from "../icons";
import { SwipeRow } from "./Drawer";
import { TriggerList } from "./Shell";
import { MuseSheet } from "./Sheet";


const field = "c-card w-full rounded-2xl px-3.5 text-[16px] outline-none";

export function Avatar({ emoji, avatar, size = 44 }: { emoji: string; avatar?: string; size?: number }) {
  return (
    <span
      className="grid shrink-0 place-items-center overflow-hidden rounded-full"
      style={{ width: size, height: size, fontSize: size * 0.5, background: "var(--c-fill)" }}
      aria-hidden="true"
    >
      {avatar ? <img src={avatar} alt="" className="h-full w-full object-cover" /> : <EmojiIcon emoji={emoji || "🤖"} size={Math.round(size * 0.72)} />}
    </span>
  );
}

/** A group's face, as in a chat app: its members' faces in a small grid (up to four). */
export function GroupFace({ members, size = 48 }: { members: Pick<Helper, "emoji" | "avatar">[]; size?: number }) {
  const shown = members.slice(0, 4);
  const cell = shown.length <= 1 ? size : (size - 6) / 2;
  return (
    <span
      className="grid shrink-0 content-center justify-center gap-0.5 overflow-hidden rounded-[30%] p-0.5"
      style={{ width: size, height: size, background: "var(--c-surface)", boxShadow: "inset 0 0 0 0.5px var(--c-border)", gridTemplateColumns: shown.length > 1 ? `repeat(2, ${cell}px)` : undefined }}
      aria-hidden="true"
    >
      {shown.map((m, i) => (
        <Avatar key={i} emoji={m.emoji} avatar={m.avatar} size={cell} />
      ))}
    </span>
  );
}

/** When the user last looked at a conversation (this phone only): newer messages show a red dot. */
const SEEN_KEY = "companion.helper-seen";

function seenMap(): Record<string, number> {
  try {
    return JSON.parse(localStorage.getItem(SEEN_KEY) || "{}") as Record<string, number>;
  } catch {
    return {};
  }
}

export function seenAt(chatId: string): number {
  const seen = seenMap();
  if (seen["*"] === undefined) {
    // The first look at the list: what was there before counts as read.
    markSeen("*");
    return Date.now();
  }
  return Math.max(seen[chatId] ?? 0, seen["*"]);
}

export function markSeen(chatId: string): void {
  try {
    localStorage.setItem(SEEN_KEY, JSON.stringify({ ...seenMap(), [chatId]: Date.now() }));
  } catch {
    // Private mode: every conversation just looks read.
  }
}

/** A chat list's time: 21:09 today, 昨天, 周三 this week, else 10/23. */
export function chatTime(ms: number, now = new Date()): string {
  const at = new Date(ms);
  const day = (d: Date) => new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime();
  const days = Math.round((day(now) - day(at)) / 86_400_000);
  if (days <= 0) return `${String(at.getHours()).padStart(2, "0")}:${String(at.getMinutes()).padStart(2, "0")}`;
  if (days === 1) return "昨天";
  if (days < 7) return `周${"日一二三四五六"[at.getDay()]}`;
  return `${at.getMonth() + 1}/${at.getDate()}`;
}

function MenuItem({ icon, label, onClick }: { icon: ReactNode; label: string; onClick: () => void }) {
  return (
    <li>
      <button type="button" role="menuitem" onClick={onClick} className="c-pressable flex min-h-11 w-full items-center gap-2.5 px-3.5 text-left text-[15px]">
        {icon}
        {label}
      </button>
    </li>
  );
}

/** A photo from the phone, cut square and made small enough to keep with the helper. */
function shrinkPhoto(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const url = URL.createObjectURL(file);
    const image = new Image();
    image.onload = () => {
      const side = Math.min(image.width, image.height);
      const canvas = document.createElement("canvas");
      canvas.width = canvas.height = 192;
      canvas.getContext("2d")?.drawImage(image, (image.width - side) / 2, (image.height - side) / 2, side, side, 0, 0, 192, 192);
      URL.revokeObjectURL(url);
      resolve(canvas.toDataURL("image/jpeg", 0.85));
    };
    image.onerror = () => {
      URL.revokeObjectURL(url);
      reject(new Error("这张图片打不开"));
    };
    image.src = url;
  });
}

/** A labelled field with a line under the label saying what it is for. */
function Field({ label, hint, children }: { label: string; hint: string; children: ReactNode }) {
  return (
    <div className="flex flex-col gap-1.5">
      <span className="text-[14px] font-semibold">{label}</span>
      {children}
      <span className="c-muted text-[12.5px] leading-snug">{hint}</span>
    </div>
  );
}

/** Name, avatar, what it is for, how it works and its model: for a new helper or an existing one. */
function HelperEditor({
  open,
  start,
  helperId,
  title,
  models,
  canReset,
  onClose,
  onSave,
  onReset,
}: {
  open: boolean;
  start: HelperFields | null;
  /** An existing helper (it can have a face drawn for it). */
  helperId?: string;
  title: string;
  /** The models on the 模型 page it can run on. */
  models: string[];
  /** A default helper that was changed: it can go back as it came. */
  canReset: boolean;
  onClose: () => void;
  onSave: (fields: HelperFields) => Promise<void>;
  onReset: () => Promise<void>;
}) {
  const { client } = useClient();
  const [emoji, setEmoji] = useState("🤖");
  const [avatar, setAvatar] = useState("");
  const [choosing, setChoosing] = useState(false);
  const [drawing, setDrawing] = useState(false);
  const photo = useRef<HTMLInputElement>(null);
  const [name, setName] = useState("");
  const [job, setJob] = useState("");
  const [instructions, setInstructions] = useState("");
  const [model, setModel] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    if (!open) return;
    setEmoji(start?.emoji || "🤖");
    setAvatar(start?.avatar ?? "");
    setName(start?.name ?? "");
    setJob(start?.title ?? "");
    setInstructions(start?.instructions ?? "");
    setModel(start?.model ?? "");
    setChoosing(false);
    setError(null);
  }, [open, start]);

  const draw = async () => {
    if (!helperId) return;
    setDrawing(true);
    setError(null);
    try {
      const next = await drawHelperAvatar(client, helperId, `${name.trim()}，${job.trim()}`);
      setAvatar(next.helpers.find((h) => h.id === helperId)?.avatar ?? "");
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setDrawing(false);
    }
  };

  const run = async (task: () => Promise<void>) => {
    setBusy(true);
    setError(null);
    try {
      await task();
      onClose();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <MuseSheet open={open} onClose={onClose} title={title}>
      <div className="flex flex-col gap-5 pb-2">
        <Field label="名字" hint="在聊天里输入「@名字」就能叫它。">
          <div className="flex items-center gap-2.5">
            <button
              type="button"
              aria-label="换头像"
              aria-expanded={choosing}
              onClick={() => setChoosing((v) => !v)}
              className="relative shrink-0 rounded-full text-[22px]"
            >
              <Avatar emoji={emoji} avatar={avatar} size={44} />
              {drawing && (
                <span className="absolute inset-0 grid place-items-center rounded-full bg-black/40 text-white">
                  <Loader2 className="h-4 w-4 animate-spin" />
                </span>
              )}
            </button>
            <input value={name} onChange={(e) => setName(e.target.value)} maxLength={12} placeholder="比如：记账" aria-label="名字" className={`${field} min-h-11`} />
          </div>
          {choosing && (
            <div className="flex flex-wrap gap-2">
              <input
                ref={photo}
                type="file"
                accept="image/*"
                className="hidden"
                onChange={(e) => {
                  const file = e.target.files?.[0];
                  e.target.value = "";
                  if (file)
                    void shrinkPhoto(file)
                      .then((url) => {
                        setAvatar(url);
                        setChoosing(false);
                      })
                      .catch((err: unknown) => setError(err instanceof Error ? err.message : String(err)));
                }}
              />
              <button type="button" onClick={() => photo.current?.click()} className="c-pressable flex min-h-9 items-center gap-1.5 rounded-full px-3 text-[13.5px]" style={{ background: "var(--c-fill)" }}>
                <ImagePlus className="h-4 w-4" /> 用照片
              </button>
              {helperId && (
                <button
                  type="button"
                  disabled={drawing}
                  onClick={() => void draw()}
                  className="c-pressable flex min-h-9 items-center gap-1.5 rounded-full px-3 text-[13.5px] disabled:opacity-50"
                  style={{ background: "var(--c-fill)" }}
                >
                  <Sparkles className="h-4 w-4" /> {drawing ? "画着呢，一分钟左右…" : "让 AI 画一个"}
                </button>
              )}
              {avatar && (
                <button type="button" onClick={() => setAvatar("")} className="c-muted min-h-9 px-2 text-[13.5px]">
                  不用图片
                </button>
              )}
            </div>
          )}
          {choosing && (
            <div className="flex flex-wrap gap-1.5" role="radiogroup" aria-label="头像">
              {FACE_CHOICES.map((e) => (
                <button
                  key={e}
                  type="button"
                  role="radio"
                  aria-checked={!avatar && emoji === e}
                  aria-label={e}
                  onClick={() => {
                    setEmoji(e);
                    setAvatar("");
                    setChoosing(false);
                  }}
                  className={cn("grid h-11 w-11 place-items-center rounded-full", !avatar && emoji === e && "ring-2")}
                  style={{ background: "var(--c-fill)", ["--tw-ring-color" as string]: "var(--c-primary)" }}
                >
                  <EmojiIcon emoji={e} size={32} />
                </button>
              ))}
            </div>
          )}
        </Field>

        <Field label="它负责什么" hint="一句话。显示在帮手列表里；主助理也看这句话，决定什么事交给它。">
          <input value={job} onChange={(e) => setJob(e.target.value)} maxLength={16} placeholder="比如：记下每笔收支，按月算账" aria-label="它负责什么" className={`${field} min-h-11`} />
        </Field>

        <Field label="做事要求" hint="它每次做事都会照着这里来：怎么做、交出什么样的结果、哪些事不能做。一行写一条就好。">
          <textarea
            value={instructions}
            onChange={(e) => setInstructions(e.target.value)}
            rows={6}
            maxLength={2000}
            placeholder={"比如：\n- 每笔记下日期、金额、类别\n- 每月给我一个汇总\n- 不要替我付款"}
            aria-label="做事要求"
            className={`${field} resize-none py-3 leading-relaxed`}
          />
        </Field>

        <Field label="用哪个模型" hint="难的活可以给它配更强的模型，简单的用便宜快的。模型在「设置 → 模型」里添加。">
          <select value={model} onChange={(e) => setModel(e.target.value)} aria-label="用哪个模型" className={`${field} min-h-11 appearance-none`}>
            <option value="">跟主助理一样</option>
            {models.map((m) => (
              <option key={m} value={m}>
                {m}
              </option>
            ))}
          </select>
        </Field>

        {error && <p className="c-danger text-[14px]">{error}</p>}
        <button
          type="button"
          disabled={busy || drawing || !name.trim()}
          onClick={() => void run(() => onSave({ emoji, avatar, name: name.trim(), title: job.trim(), instructions: instructions.trim(), model }))}
          className="c-primary min-h-11 rounded-full text-[15px] font-semibold disabled:opacity-40"
        >
          {busy ? "保存中…" : "保存"}
        </button>
        {canReset && (
          <button type="button" disabled={busy} onClick={() => void run(onReset)} className="c-muted -mt-2 min-h-10 text-[14px]">
            恢复默认设置
          </button>
        )}
      </div>
    </MuseSheet>
  );
}

/** One thing it was asked, with its answer folded until tapped. */
function WorkRow({ work }: { work: Helper["work"][number] }) {
  const [open, setOpen] = useState(false);
  return (
    <li>
      <button type="button" onClick={() => setOpen((v) => !v)} aria-expanded={open} className="flex w-full items-start gap-2 py-3 text-left">
        <span className="min-w-0 flex-1">
          <span className="c-muted block text-[12.5px]">
            {work.from} · {dayLabel(work.at)}
            {!work.done && " · 在做"}
          </span>
          <span className={cn("mt-0.5 block text-[15px] leading-snug", !open && "line-clamp-2")}>{work.task}</span>
        </span>
        <ChevronDown size={16} className={cn("c-muted mt-1 shrink-0 transition-transform", open && "rotate-180")} />
      </button>
      {open && work.answer && (
        <div className="c-card mb-3 rounded-2xl px-3.5 py-2.5 text-[14.5px] leading-relaxed">
          <MarkdownText>{work.answer}</MarkdownText>
        </div>
      )}
    </li>
  );
}

/** What runs on its own for this helper: the scheduled jobs it set up from its chat. */
function HelperRoutines({ helper }: { helper: Helper }) {
  const { client, token } = useClient();
  const [jobs, setJobs] = useState<SessionAutomationJob[] | null>(null);
  const mine = (all: SessionAutomationJob[]) => all.filter((job) => job.owner?.id === helper.id);
  useEffect(() => {
    fetchAutomations(token).then((p) => setJobs(mine(p.jobs))).catch(() => setJobs([]));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [token, helper.id]);
  const act = (action: "enable" | "disable" | "delete", job: SessionAutomationJob) =>
    runAutomationAction(client, action, job.id).then((p) => setJobs(mine(p.jobs))).catch(() => undefined);

  return (
    <>
      <h4 className="c-muted mt-5 text-[13px] font-medium">例行</h4>
      {jobs && jobs.length === 0 ? (
        <p className="c-muted py-2 text-[13.5px] leading-snug">没有。在它的聊天里说一声（比如“每晚九点提醒我记账”），它就会自己定好。</p>
      ) : (
        <ul className="c-divider divide-y">
          {(jobs ?? []).map((job) => (
            <li key={job.id} className="flex items-center gap-2 py-2">
              <span className="min-w-0 flex-1">
                <span className="block truncate text-[14.5px] font-medium">{job.name}</span>
                <span className="c-muted block truncate text-[12.5px]">
                  {describeSchedule(job)}
                  {job.enabled && job.state.next_run_at_ms ? ` · 下次 ${dayLabel(job.state.next_run_at_ms)}` : ""}
                </span>
              </span>
              <Switch checked={job.enabled} label={job.name} onChange={() => void act(job.enabled ? "disable" : "enable", job)} />
              <button type="button" aria-label={`删除 ${job.name}`} onClick={() => void act("delete", job)} className="c-muted grid h-9 w-9 shrink-0 place-items-center">
                <Trash2 className="h-4 w-4" />
              </button>
            </li>
          ))}
        </ul>
      )}
    </>
  );
}

/** 分享设定: its settings copied as text, for someone to paste into 新建帮手 → 导入. */
function ShareButton({ helper }: { helper: Helper }) {
  const [copied, setCopied] = useState<boolean | null>(null);
  const text = helperShareText(helper);
  const copy = async () => {
    if (await copyTextToClipboard(text)) {
      setCopied(true);
      setTimeout(() => setCopied(null), 2000);
    } else {
      setCopied(false); // no clipboard here: show the text to copy by hand
    }
  };
  return (
    <>
      <button type="button" onClick={() => void copy()} className="c-pressable flex min-h-11 flex-1 items-center justify-center gap-1.5 rounded-full text-[15px]" style={{ background: "var(--c-fill)" }}>
        {copied ? <Check className="h-4 w-4" /> : <Copy className="h-4 w-4" />} {copied ? "已复制" : "分享设定"}
      </button>
      <MuseSheet open={copied === false} onClose={() => setCopied(null)} title="分享设定">
        <p className="c-muted pb-2 text-[13px]">长按全选复制，发给别人；对方在「新建帮手 → 导入」里粘贴。</p>
        <textarea readOnly value={text} rows={8} aria-label="帮手设定" className={`${field} resize-none py-3 font-mono text-[13px]`} onFocus={(e) => e.target.select()} />
      </MuseSheet>
    </>
  );
}

const HABIT_SOURCE: Record<HelperHabit["by"], string> = { you: "你告诉它的", chat: "聊天里你说过", learned: "慢慢摸清的" };

/** 它摸清的你的习惯: what it does your way by default, learned from the chats or told here. */
function HelperHabits({ helper, onChanged }: { helper: Helper; onChanged: (payload: HelpersPayload) => void }) {
  const { client } = useClient();
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const change = async (what: { text: string } | { habit: string }) => {
    setBusy(true);
    try {
      onChanged(await changeHabit(client, helper.id, what));
      if ("text" in what) setText("");
    } catch {
      // Stays as it was; the next try shows the real state.
    } finally {
      setBusy(false);
    }
  };
  return (
    <>
      <h4 className="c-muted mt-5 text-[13px] font-medium">它摸清的你的习惯</h4>
      {helper.habits.length === 0 ? (
        <p className="c-muted py-2 text-[13.5px] leading-snug">还没有。你在聊天里表达过的偏好（比如“别把时间排太紧”），它会慢慢记住，以后默认照着来。</p>
      ) : (
        <ul className="c-divider divide-y">
          {helper.habits.map((habit) => (
            <li key={habit.id} className="flex items-start gap-2 py-2">
              <span className="min-w-0 flex-1 pt-0.5">
                <span className="block text-[14.5px] leading-snug">{habit.text}</span>
                <span className="c-muted block text-[12px]">
                  {HABIT_SOURCE[habit.by] ?? ""} · {dayLabel(habit.at)}
                </span>
              </span>
              <button
                type="button"
                aria-label={`忘掉：${habit.text}`}
                disabled={busy}
                onClick={() => void change({ habit: habit.id })}
                className="c-muted grid h-8 w-8 shrink-0 place-items-center"
              >
                <X className="h-4 w-4" />
              </button>
            </li>
          ))}
        </ul>
      )}
      <form
        className="mt-1.5 flex gap-2"
        onSubmit={(event) => {
          event.preventDefault();
          if (text.trim() && !busy) void change({ text: text.trim() });
        }}
      >
        <input
          value={text}
          onChange={(e) => setText(e.target.value)}
          maxLength={120}
          placeholder="告诉它一个习惯"
          aria-label="告诉它一个习惯"
          className={`${field} min-h-10 flex-1 text-[14.5px]`}
        />
        <button type="submit" disabled={!text.trim() || busy} className="c-primary min-h-10 shrink-0 rounded-full px-4 text-[14px] font-semibold disabled:opacity-40">
          记住
        </button>
      </form>
    </>
  );
}

function HelperDetail({
  helper,
  onClose,
  onEdit,
  onChanged,
}: {
  helper: Helper | null;
  onClose: () => void;
  onEdit: (helper: Helper) => void;
  onChanged: (payload: HelpersPayload) => void;
}) {
  const { client } = useClient();
  const [confirmDelete, setConfirmDelete] = useState(false);
  useEffect(() => setConfirmDelete(false), [helper]);
  if (!helper) return <MuseSheet open={false} onClose={onClose}>{null}</MuseSheet>;
  const dropNote = (note: string) =>
    changeHelper(client, "update", { id: helper.id, notes: helper.notes.filter((n) => n !== note) }).then(onChanged).catch(() => undefined);

  return (
    <MuseSheet open onClose={onClose}>
      <div className="flex flex-col pb-2">
        <div className="flex flex-col items-center text-center">
          <Avatar emoji={helper.emoji} avatar={helper.avatar} size={64} />
          <h3 className="mt-2 text-[21px] font-semibold">{helper.name}</h3>
          {helper.title && <p className="c-muted text-[14px]">{helper.title}</p>}
        </div>
        <div className="mt-4 flex gap-2">
          <button type="button" onClick={() => onEdit(helper)} className="c-pressable min-h-11 flex-1 rounded-full text-[15px]" style={{ background: "var(--c-fill)" }}>
            编辑资料
          </button>
          <ShareButton helper={helper} />
        </div>

        {helper.instructions && <p className="c-muted mt-4 whitespace-pre-wrap text-[14px] leading-relaxed">{helper.instructions}</p>}
        <p className="c-muted mt-3 text-[13px]">模型：{helper.model || "跟主助理一样"}</p>

        <HelperHabits helper={helper} onChanged={onChanged} />

        <h4 className="c-muted mt-5 text-[13px] font-medium">最近做的事</h4>
        {helper.work.length === 0 ? (
          <p className="c-muted py-2 text-[14px]">还没做过事</p>
        ) : (
          <ul className="c-divider divide-y">{helper.work.map((w) => <WorkRow key={w.turn_id} work={w} />)}</ul>
        )}

        <HelperRoutines helper={helper} />
        <TriggerList name={helper.name} owner={helper.id} />

        {helper.notes.length > 0 && (
          <>
            <h4 className="c-muted mt-4 text-[13px] font-medium">工作笔记</h4>
            <ul className="c-divider divide-y">
              {helper.notes.map((note) => (
                <li key={note} className="flex items-start gap-2 py-2 text-[14px] leading-snug">
                  <span className="min-w-0 flex-1 pt-1">{note}</span>
                  <button type="button" aria-label="删掉这条笔记" onClick={() => void dropNote(note)} className="c-muted grid h-8 w-8 shrink-0 place-items-center">
                    <X className="h-4 w-4" />
                  </button>
                </li>
              ))}
            </ul>
          </>
        )}

        {confirmDelete ? (
          <div className="mt-5 flex gap-2">
            <button type="button" onClick={() => setConfirmDelete(false)} className="c-pressable min-h-11 flex-1 rounded-full text-[15px]" style={{ background: "var(--c-fill)" }}>
              取消
            </button>
            <button
              type="button"
              onClick={() => {
                onClose();
                void changeHelper(client, "delete", { id: helper.id }).then(onChanged).catch(() => undefined);
              }}
              className="min-h-11 flex-1 rounded-full text-[15px] font-semibold text-white"
              style={{ background: "var(--c-danger-fill)" }}
            >
              删除
            </button>
          </div>
        ) : (
          <button type="button" onClick={() => setConfirmDelete(true)} className="c-danger mt-5 min-h-10 text-[14px]">
            删除这个帮手
          </button>
        )}
      </div>
    </MuseSheet>
  );
}

/** The helpers and models, loaded for a sheet that is opened from elsewhere (a helper's chat). */
function useHelpersData(open: boolean) {
  const { token } = useClient();
  const [data, setData] = useState<HelpersPayload | null>(null);
  const [models, setModels] = useState<string[]>([]);
  useEffect(() => {
    if (!open) return;
    fetchHelpers(token).then(setData).catch(() => undefined);
    fetchModels(token).then((m) => setModels(m.presets.map((p) => p.name))).catch(() => undefined);
  }, [open, token]);
  return { data, setData, models };
}

/**
 * A helper's profile, from the top of its chat (as Grok Bot's conversation details): what it does,
 * what it did, its notes, and 编辑 (name, avatar, job, how it works, model) or 删除.
 */
export function HelperProfileSheet({
  chatId,
  onClose,
  onChanged,
  onDeleted,
}: {
  /** The helper's chat; null when closed. */
  chatId: string | null;
  onClose: () => void;
  onChanged: () => void;
  onDeleted: () => void;
}) {
  const { client } = useClient();
  const { data, setData, models } = useHelpersData(chatId !== null);
  const [editing, setEditing] = useState(false);
  useEffect(() => setEditing(false), [chatId]);
  const helper = data?.helpers.find((h) => h.chat_id === chatId) ?? null;
  const changed = (next: HelpersPayload) => {
    setData(next);
    onChanged();
  };
  return (
    <>
      <HelperDetail
        helper={chatId && !editing ? helper : null}
        onClose={onClose}
        onEdit={() => setEditing(true)}
        onChanged={(next) => {
          changed(next);
          if (!next.helpers.some((h) => h.chat_id === chatId)) onDeleted();
        }}
      />
      <HelperEditor
        open={editing && helper !== null}
        start={helper}
        helperId={helper?.id}
        title="编辑帮手"
        models={models}
        canReset={Boolean(helper?.preset && !helper.is_default)}
        onReset={async () => {
          if (helper) changed(await changeHelper(client, "reset", { id: helper.id }));
        }}
        onClose={() => setEditing(false)}
        onSave={async (fields) => {
          if (helper) changed(await changeHelper(client, "update", { id: helper.id, ...fields }));
        }}
      />
    </>
  );
}

/** 群聊: a name and which helpers are in it (2~6); for a new one, or an existing one with 删除. */
export function GroupSheet({
  open,
  group,
  onClose,
  onSaved,
  onDeleted,
}: {
  open: boolean;
  /** The group to change; null for a new one. */
  group: HelperGroup | null;
  onClose: () => void;
  /** Saved; for a new group, its chat to open. */
  onSaved: (opened?: string) => void;
  onDeleted?: () => void;
}) {
  const { client } = useClient();
  const { data } = useHelpersData(open);
  const [name, setName] = useState("");
  const [members, setMembers] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [confirmDelete, setConfirmDelete] = useState(false);
  useEffect(() => {
    if (!open) return;
    setName(group?.name ?? "");
    setMembers(group?.members ?? []);
    setError(null);
    setConfirmDelete(false);
  }, [open, group]);
  const max = data?.max_group ?? 6;
  const toggle = (id: string) =>
    setMembers((current) => (current.includes(id) ? current.filter((m) => m !== id) : current.length < max ? [...current, id] : current));

  return (
    <MuseSheet open={open} onClose={onClose} title={group ? "群聊设置" : "新建群聊"}>
      <div className="flex flex-col gap-4 pb-2">
        <Field label="群名" hint="不填就用成员的名字。">
          <input value={name} onChange={(e) => setName(e.target.value)} maxLength={20} placeholder="比如：买房小组" aria-label="群名" className={`${field} min-h-11`} />
        </Field>
        <Field label={`成员 · ${members.length}/${max}`} hint="选 2~6 个帮手。在群里 @ 谁就交给谁，不 @ 由主助理来派。">
          <ul className="c-card divide-y overflow-hidden rounded-2xl" style={{ borderColor: "var(--c-border)" }}>
            {(data?.helpers ?? []).map((h) => {
              const on = members.includes(h.id);
              return (
                <li key={h.id}>
                  <button
                    type="button"
                    role="checkbox"
                    aria-checked={on}
                    onClick={() => toggle(h.id)}
                    className="flex w-full items-center gap-3 px-3.5 py-2.5 text-left"
                  >
                    <Avatar emoji={h.emoji} avatar={h.avatar} size={34} />
                    <span className="min-w-0 flex-1">
                      <span className="block text-[15px] font-medium">{h.name}</span>
                      {h.title && <span className="c-muted block truncate text-[12.5px]">{h.title}</span>}
                    </span>
                    <span
                      className="grid h-6 w-6 shrink-0 place-items-center rounded-full text-[13px] font-bold"
                      style={on ? { background: "var(--c-primary)", color: "var(--c-primary-text)" } : { boxShadow: "inset 0 0 0 1.5px var(--c-border)" }}
                      aria-hidden="true"
                    >
                      {on ? "✓" : ""}
                    </span>
                  </button>
                </li>
              );
            })}
          </ul>
        </Field>
        {error && <p className="c-danger text-[14px]">{error}</p>}
        <button
          type="button"
          disabled={busy || members.length < 2}
          onClick={async () => {
            setBusy(true);
            setError(null);
            try {
              const next = group
                ? await changeGroup(client, "update", { id: group.id, name: name.trim() || group.name, members })
                : await changeGroup(client, "create", { name: name.trim(), members });
              onSaved(next.opened);
              onClose();
            } catch (e) {
              setError(e instanceof Error ? e.message : String(e));
            } finally {
              setBusy(false);
            }
          }}
          className="c-primary min-h-11 rounded-full text-[15px] font-semibold disabled:opacity-40"
        >
          {busy ? "保存中…" : group ? "保存" : "建好，开始聊"}
        </button>
        {group &&
          (confirmDelete ? (
            <div className="flex gap-2">
              <button type="button" onClick={() => setConfirmDelete(false)} className="c-pressable min-h-11 flex-1 rounded-full text-[15px]" style={{ background: "var(--c-fill)" }}>
                取消
              </button>
              <button
                type="button"
                disabled={busy}
                onClick={async () => {
                  setBusy(true);
                  setError(null);
                  try {
                    await changeGroup(client, "delete", { id: group.id });
                    onClose();
                    onDeleted?.();
                  } catch (e) {
                    setError(e instanceof Error ? e.message : String(e));
                  } finally {
                    setBusy(false);
                  }
                }}
                className="min-h-11 flex-1 rounded-full text-[15px] font-semibold text-white disabled:opacity-50"
                style={{ background: "var(--c-danger-fill)" }}
              >
                {busy ? "删除中…" : "删除"}
              </button>
            </div>
          ) : (
            <button type="button" onClick={() => setConfirmDelete(true)} className="c-danger min-h-10 text-[14px]">
              删除这个群聊
            </button>
          ))}
      </div>
    </MuseSheet>
  );
}

export function HelpersScreen({ active, onOpenChat }: { active: boolean; onOpenChat: (chatId: string) => void }) {
  const { client, token } = useClient();
  const [data, setData] = useState<HelpersPayload | null>(null);
  const [picking, setPicking] = useState(false);
  const [grouping, setGrouping] = useState(false);
  const [importing, setImporting] = useState(false);
  const [pasted, setPasted] = useState("");
  const [imported, setImported] = useState<HelperFields | null>(null);
  const [importModels, setImportModels] = useState<string[]>([]);
  useEffect(() => {
    if (!imported) return;
    fetchModels(token)
      .then((payload) => setImportModels(payload.presets.map((p) => p.name)))
      .catch(() => setImportModels([]));
  }, [imported, token]);
  // The one row swiped open to show 删除, the one asked about, and the one held for its profile.
  const [swiped, setSwiped] = useState<string | null>(null);
  const [deleting, setDeleting] = useState<{ kind: "helper" | "group"; id: string; name: string } | null>(null);
  const [profile, setProfile] = useState<string | null>(null);
  const [editingGroup, setEditingGroup] = useState<HelperGroup | null>(null);
  const [menu, setMenu] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(() => {
    fetchHelpers(token).then(setData).catch(() => undefined);
  }, [token]);
  useEffect(() => {
    if (active) load();
  }, [active, load]);
  // While one is working, look again now and then: it finishes on its own.
  const busy = data?.helpers.some((h) => h.working) ?? false;
  useEffect(() => {
    if (!active || !busy) return;
    const timer = setInterval(load, 5_000);
    return () => clearInterval(timer);
  }, [active, busy, load]);

  const helpers = data?.helpers ?? [];
  const unused = (data?.templates ?? []).filter((t) => !helpers.some((h) => h.name === t.name));
  const full = data ? helpers.length >= data.max : false;
  // A new helper opens straight into its chat: a blank one asks what it should do.
  const [idea, setIdea] = useState("");
  const make = async (task: () => Promise<HelpersPayload>) => {
    setPicking(false);
    setIdea("");
    setError(null);
    try {
      const next = await task();
      setData(next);
      if (next.opened) onOpenChat(next.opened);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  // One list of conversations, as a chat app's: helpers and groups together, the latest on top.
  const conversations = [
    ...helpers.map((h) => ({ kind: "helper" as const, id: h.id, chat: h.chat_id, at: h.last?.at || h.created_at || 0, helper: h })),
    ...(data?.groups ?? []).map((g) => ({ kind: "group" as const, id: g.id, chat: g.chat_id, at: g.last?.at || g.created_at || 0, group: g })),
  ].sort((a, b) => b.at - a.at);
  const memberList = (g: HelperGroup) => g.members.map((id) => helpers.find((h) => h.id === id)).filter((h): h is Helper => h !== undefined);

  return (
    <div className="pb-4">
      <PageTitle
        title="帮手"
        trailing={
          <div className="relative">
            <button
              type="button"
              aria-label="新建"
              aria-expanded={menu}
              onClick={() => setMenu((v) => !v)}
              className="c-pressable grid h-11 w-11 shrink-0 place-items-center rounded-full"
              style={{ background: "var(--c-fill)" }}
            >
              <Plus className="h-5 w-5" />
            </button>
            {menu && (
              <>
                <button type="button" aria-label="关闭菜单" className="fixed inset-0 z-30 cursor-default" onClick={() => setMenu(false)} />
                <ul className="c-float absolute right-0 top-full z-40 mt-2 w-44 overflow-hidden rounded-2xl py-1 shadow-lg" role="menu">
                  {!full && (
                    <MenuItem icon={<UserPlus className="h-4 w-4" />} label="新建帮手" onClick={() => { setMenu(false); setPicking(true); }} />
                  )}
                  {helpers.length >= 2 && (
                    <MenuItem icon={<Users className="h-4 w-4" />} label="发起群聊" onClick={() => { setMenu(false); setGrouping(true); }} />
                  )}
                  {!full && (
                    <MenuItem
                      icon={<Download className="h-4 w-4" />}
                      label="导入设定"
                      onClick={() => {
                        setMenu(false);
                        setPasted("");
                        setImporting(true);
                      }}
                    />
                  )}
                </ul>
              </>
            )}
          </div>
        }
      />
      {error && <p className="c-danger px-5 pb-2 text-[14px]">{error}</p>}

      {data && conversations.length === 0 && (
        <p className="c-muted px-5 text-[15px] leading-relaxed">点右上角的 + 建一个帮手：跟它聊几句，它就知道自己该干什么。</p>
      )}
      <ul className="c-divider divide-y">
        {conversations.map((row) => {
          const last = row.kind === "helper" ? row.helper.last : row.group.last;
          const unread = !!last && last.who !== "我" && last.at > seenAt(row.chat);
          const working = row.kind === "helper" && row.helper.working;
          const preview = working
            ? "正在做事…"
            : row.kind === "helper" && row.helper.setup
              ? "还没设定好，点进去跟它聊"
              : last
                ? `${last.who ? `${last.who}：` : ""}${last.text}`
                : row.kind === "helper"
                  ? row.helper.title || "还没聊过"
                  : // The name is usually the members already: say what to do instead.
                    `${row.group.members.length} 位帮手，还没聊过，说一句大家都能看到`;
          return (
            <SwipeRow
              key={row.id}
              rounded=""
              className="c-pressable items-center gap-3 px-4 py-3"
              swiped={swiped === row.id}
              onSwipe={(open) => setSwiped(open ? row.id : null)}
              onOpen={() => onOpenChat(row.chat)}
              onEdit={() => (row.kind === "helper" ? setProfile(row.chat) : setEditingGroup(row.group))}
              onDelete={() => {
                setSwiped(null);
                setDeleting(row.kind === "helper" ? { kind: "helper", id: row.id, name: row.helper.name } : { kind: "group", id: row.id, name: row.group.name });
              }}
            >
              <span className="relative shrink-0">
                {row.kind === "helper" ? (
                  <Avatar emoji={row.helper.emoji} avatar={row.helper.avatar} size={48} />
                ) : (
                  <GroupFace members={memberList(row.group)} size={48} />
                )}
                {unread && (
                  <span
                    className="absolute -right-0.5 -top-0.5 h-3 w-3 rounded-full"
                    style={{ background: "var(--c-danger)", boxShadow: "0 0 0 2px var(--c-bg)" }}
                    aria-label="有新消息"
                  />
                )}
              </span>
              <span className="min-w-0 flex-1">
                <span className="flex items-baseline gap-2">
                  <span className="min-w-0 flex-1 truncate text-[16.5px] font-semibold">
                    {row.kind === "helper" ? row.helper.name : row.group.name}
                    {row.kind === "group" && <span className="c-muted font-normal">（{row.group.members.length + 1}）</span>}
                  </span>
                  {last && <span className="c-muted shrink-0 text-[12px]">{chatTime(last.at)}</span>}
                </span>
                <span
                  className={cn("block truncate text-[14px]", !working && "c-muted")}
                  style={working ? { color: "var(--c-primary)" } : undefined}
                >
                  {preview}
                </span>
              </span>
            </SwipeRow>
          );
        })}
      </ul>

      {/* New: set one up by talking, or start from a template. */}
      <MuseSheet open={picking} onClose={() => setPicking(false)} title="新建帮手">
        <div className="flex flex-col gap-2 pb-2">
          {/* A name or a sentence is enough: it works out the rest and proposes it in its chat. */}
          <input
            value={idea}
            onChange={(e) => setIdea(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && void make(() => changeHelper(client, "blank", { idea: idea.trim() }))}
            maxLength={60}
            placeholder="给它起个名字，或说说它负责什么"
            aria-label="新帮手负责什么"
            className={field}
          />
          <p className="c-muted px-1 text-[12.5px] leading-snug">比如「邮件分拣」「盯着我的快递」「健身教练」，它会自己想好怎么做，给你过目。</p>
          <button
            type="button"
            onClick={() => void make(() => changeHelper(client, "blank", { idea: idea.trim() }))}
            className="c-primary flex min-h-12 items-center justify-center gap-2 rounded-full text-[15px] font-semibold"
          >
            <MessageCircle className="h-4 w-4" /> {idea.trim() ? "建好，让它出个方案" : "聊着设定一个新帮手"}
          </button>
          {unused.length > 0 && <p className="c-muted px-1 pt-2 text-[13px]">或者从这些开始</p>}
          {unused.map((t) => (
            <button key={t.name} type="button" onClick={() => void make(() => changeHelper(client, "create", t))} className="c-card c-pressable flex items-center gap-3 rounded-2xl p-3 text-left">
              <Avatar emoji={t.emoji} size={40} />
              <span className="min-w-0 flex-1">
                <span className="block text-[15px] font-semibold">{t.name}</span>
                <span className="c-muted block truncate text-[13px]">{t.title}</span>
              </span>
            </button>
          ))}
        </div>
      </MuseSheet>

      <MuseSheet open={deleting !== null} onClose={() => setDeleting(null)} title={`删除「${deleting?.name ?? ""}」？`}>
        <div className="flex flex-col gap-3 pb-2">
          <p className="c-muted text-[14px] leading-snug">
            {deleting?.kind === "group" ? "群里的聊天记录会一起删掉，帮手们还在。" : "它的聊天记录、工作笔记、摸清的习惯和它定的例行会一起删掉，删了找不回来。"}
          </p>
          <div className="flex gap-2">
            <button type="button" onClick={() => setDeleting(null)} className="c-pressable min-h-11 flex-1 rounded-full text-[15px]" style={{ background: "var(--c-fill)" }}>
              取消
            </button>
            <button
              type="button"
              onClick={() => {
                const target = deleting;
                setDeleting(null);
                if (target)
                  void (target.kind === "group" ? changeGroup(client, "delete", { id: target.id }) : changeHelper(client, "delete", { id: target.id }))
                    .then(setData)
                    .catch((e: unknown) => setError(e instanceof Error ? e.message : String(e)));
              }}
              className="min-h-11 flex-1 rounded-full text-[15px] font-semibold text-white"
              style={{ background: "var(--c-danger-fill)" }}
            >
              删除
            </button>
          </div>
        </div>
      </MuseSheet>
      <HelperProfileSheet
        chatId={profile}
        onClose={() => setProfile(null)}
        onChanged={load}
        onDeleted={() => setProfile(null)}
      />

      <MuseSheet open={importing} onClose={() => setImporting(false)} title="导入设定">
        <div className="flex flex-col gap-3 pb-2">
          <textarea
            value={pasted}
            onChange={(e) => setPasted(e.target.value)}
            rows={7}
            placeholder="把别人「分享设定」给你的内容粘贴在这里"
            aria-label="帮手设定"
            className={`${field} resize-none py-3 text-[14px]`}
          />
          {pasted.trim() && !parseHelperShare(pasted) && <p className="c-danger text-[13.5px]">这不像是一份帮手设定。</p>}
          <button
            type="button"
            disabled={!parseHelperShare(pasted)}
            onClick={() => {
              setImporting(false);
              setImported(parseHelperShare(pasted));
            }}
            className="c-primary min-h-11 rounded-full text-[15px] font-semibold disabled:opacity-40"
          >
            下一步：看一眼再保存
          </button>
        </div>
      </MuseSheet>
      <HelperEditor
        open={imported !== null}
        start={imported}
        title="导入的帮手"
        models={importModels}
        canReset={false}
        onReset={async () => undefined}
        onClose={() => setImported(null)}
        onSave={async (fields) => {
          setData(await changeHelper(client, "create", fields));
        }}
      />

      <GroupSheet
        open={editingGroup !== null}
        group={editingGroup}
        onClose={() => setEditingGroup(null)}
        onSaved={load}
        onDeleted={load}
      />
      <GroupSheet
        open={grouping}
        group={null}
        onClose={() => setGrouping(false)}
        onSaved={(opened) => {
          load();
          if (opened) onOpenChat(opened);
        }}
      />
    </div>
  );
}
