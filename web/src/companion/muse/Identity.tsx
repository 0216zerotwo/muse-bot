/*
 * Who the companion is, edited where it is shown: the pen on the face in the header (name, look)
 * and the 身份与记忆 pane of the status sheet — SOUL.md and MEMORY.md, edited in place. The pen and
 * the two file cards follow nanoMuse's MuseSheet (vendor/nanomuse-web, GPL-3.0-or-later).
 */
import { Check, Loader2, RefreshCw, ShieldCheck, Sparkles, Undo2 } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";

import { cn } from "@/lib/utils";
import { useClient } from "@/providers/ClientProvider";

import {
  fetchMemory,
  learnMemory,
  tidyMemory,
  fetchMemoryDoc,
  fetchSoul,
  saveMemoryDoc,
  saveSoul,
  undoMemoryChange,
  type CompanionChanges,
  type CompanionState,
  type MemoryChange,
  type MemoryItem,
} from "../companion-api";
import { dayLabel } from "../presentation";
import { MuseSheet } from "./Sheet";

/** 名字与形象, at the top of 身份与记忆: who it is, next to what it is like (SOUL.md) and remembers. */
export function NameLookRow({
  companion,
  onUpdate,
  onChangeLook,
}: {
  companion: CompanionState;
  onUpdate: (changes: CompanionChanges) => Promise<void>;
  /** 换形象: to the main chat, to describe the new look (it draws it). */
  onChangeLook: () => void;
}) {
  const [sheet, setSheet] = useState<"name" | null>(null);
  return (
    <div className="mb-3 flex items-center gap-2 rounded-2xl bg-om-surface-2 px-3.5 py-2.5">
      <span className="min-w-0 flex-1">
        <span className="block text-[12px] text-om-muted">名字与形象</span>
        <span className="block truncate text-[16px] font-semibold">{companion.name}</span>
      </span>
      <button type="button" onClick={() => setSheet("name")} className="rounded-full bg-om-surface px-3 py-1.5 text-[13.5px] font-medium">
        改名字
      </button>
      <button type="button" onClick={onChangeLook} className="rounded-full bg-om-surface px-3 py-1.5 text-[13.5px] font-medium">
        换形象
      </button>
      <RenameSheet open={sheet === "name"} name={companion.name} onClose={() => setSheet(null)} onSave={(name) => onUpdate({ name })} />
    </div>
  );
}

function RenameSheet({ open, name, onClose, onSave }: { open: boolean; name: string; onClose: () => void; onSave: (name: string) => Promise<void> }) {
  const [draft, setDraft] = useState(name);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    if (open) {
      setDraft(name);
      setError(null);
    }
  }, [open, name]);
  const save = async () => {
    const next = draft.trim();
    if (!next) return;
    setBusy(true);
    try {
      await onSave(next);
      onClose();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };
  return (
    <MuseSheet
      open={open}
      onClose={onClose}
      title="改名字"
      footer={
        <button
          type="button"
          disabled={!draft.trim() || busy}
          onClick={() => void save()}
          className="h-12 w-full rounded-full bg-om-accent text-[16px] font-semibold text-om-accent-fg disabled:opacity-50"
        >
          保存
        </button>
      }
    >
      <input
        value={draft}
        onChange={(e) => setDraft(e.target.value)}
        onKeyDown={(e) => e.key === "Enter" && void save()}
        maxLength={40}
        aria-label="名字"
        autoFocus
        className="w-full rounded-2xl bg-om-surface-2 px-4 py-3 text-[16px] outline-none focus:ring-2 focus:ring-om-accent"
      />
      {error && (
        <p className="mt-2 text-[13px] text-om-danger" role="alert">
          {error}
        </p>
      )}
    </MuseSheet>
  );
}

type Doc = "soul" | "memory";

const DOCS: Record<Doc, { title: string; caption: string; hint: string; className: string }> = {
  soul: {
    title: "SOUL.md",
    caption: "它是谁、怎么说话",
    hint: "写它的性格、说话方式、怎么称呼你、要注意什么。保存后从下一次对话开始生效。",
    className: "bg-om-accent text-om-accent-fg",
  },
  memory: {
    title: "MEMORY.md",
    caption: "它记得的你，每小时自己更新",
    hint: "空一行算一条。删掉一条它就忘了，加一条它就记住（你写的它不会改）。",
    className: "bg-om-fg text-om-bg",
  },
};

const CHANGE_LABEL: Record<MemoryChange["kind"], string> = {
  "learn-add": "从聊天里记下",
  "learn-update": "从聊天里更正",
  "learn-remove": "过时了，忘掉",
  merge: "合并重复",
  drop: "整理掉",
  fade: "很久没提，记个大概",
  forget: "很久没提，忘掉",
};

/** An entry as the user reads it: 【喜好与习惯】喜欢爬山 → 喜好与习惯 · 喜欢爬山. */
function plainEntry(entry: string): string {
  return entry.replace(/^【([^】]+)】/, "$1 · ");
}

export type MemoryProvenance = "user_written" | "learned" | "faded";

function normalizeMemoryLine(value: string): string {
  return value.replace(/\s+/g, " ").trim();
}

/** Classifies whether a memory entry was written by the user, learned from chat, or faded. */
export function classifyMemoryItem(
  item: MemoryItem & { user_written?: boolean },
  changes: MemoryChange[],
): MemoryProvenance {
  if (item.user_written === true) return "user_written";
  const targetRaw = normalizeMemoryLine(item.line);
  const targetPlain = normalizeMemoryLine(plainEntry(item.text));
  for (const change of changes) {
    if (change.undone || !change.after) continue;
    const afterRaw = normalizeMemoryLine(change.after);
    const afterPlain = normalizeMemoryLine(plainEntry(change.after));
    if (afterRaw === targetRaw || afterPlain === targetPlain) {
      if (change.kind === "fade") return "faded";
      if (change.kind === "learn-add" || change.kind === "learn-update" || change.kind === "merge") {
        return "learned";
      }
    }
  }
  if (item.user_written === false) return "learned";
  return "user_written";
}

const PROVENANCE_META: Record<MemoryProvenance, { label: string; className: string }> = {
  user_written: {
    label: "你写的",
    className: "bg-emerald-500/15 text-emerald-700 dark:text-emerald-300",
  },
  learned: {
    label: "聊天中学到",
    className: "bg-om-accent-soft text-om-accent",
  },
  faded: {
    label: "已淡化",
    className: "bg-amber-500/15 text-amber-700 dark:text-amber-300",
  },
};

function changeBadgeTone(kind: MemoryChange["kind"]): string {
  if (kind === "fade" || kind === "forget") return "bg-amber-500/15 text-amber-700 dark:text-amber-300";
  if (kind === "learn-add" || kind === "learn-update") return "bg-om-accent-soft text-om-accent";
  return "bg-om-surface text-om-muted";
}

/** The 身份与记忆 pane: SOUL.md and MEMORY.md, opened and edited right here. */
export function SoulMemoryPane({ identity }: { identity?: ReactNode } = {}) {
  const { client, token } = useClient();
  const [open, setOpen] = useState<Doc | null>(null);
  const [text, setText] = useState("");
  const [saved, setSaved] = useState("");
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<{ ok: boolean; text: string } | null>(null);
  const [sections, setSections] = useState<{ file: string; title: string; items: MemoryItem[] }[]>([]);
  const [changes, setChanges] = useState<MemoryChange[]>([]);
  const [learnedAt, setLearnedAt] = useState<number | null>(null);
  const [learning, setLearning] = useState(false);
  const [tidying, setTidying] = useState(false);
  const [showAllItems, setShowAllItems] = useState(false);
  const [pendingSwitch, setPendingSwitch] = useState<Doc | "close" | null>(null);
  const openSeq = useRef(0);

  const loadChanges = useCallback(() => {
    fetchMemory(token)
      .then((view) => {
        setSections(view.sections ?? []);
        setChanges(view.changes ?? []);
        setLearnedAt(view.learned_at ?? null);
      })
      .catch(() => {
        setSections([]);
        setChanges([]);
      });
  }, [token]);

  /* 现在更新: read the new chats now instead of waiting for the hour. */
  const learnNow = async () => {
    setLearning(true);
    setNote(null);
    try {
      const view = await learnMemory(client);
      setSections(view.sections ?? []);
      setChanges(view.changes ?? []);
      setLearnedAt(view.learned_at ?? null);
      const body = await fetchMemoryDoc(token);
      setText(body.text);
      setSaved(body.text);
      const count = view.learned?.learned ?? 0;
      setNote({ ok: !view.learned?.error, text: view.learned?.error ? "这会儿没连上模型，稍后再试" : count ? `记下了 ${count} 处` : "没有新的要记" });
    } catch (e) {
      setNote({ ok: false, text: e instanceof Error ? e.message : String(e) });
    } finally {
      setLearning(false);
    }
  };

  /* 整理一下: deduplicate and consolidate memory entries via tidyMemory. */
  const tidyNow = async () => {
    setTidying(true);
    setNote(null);
    try {
      const view = await tidyMemory(client);
      setSections(view.sections ?? []);
      setChanges(view.changes ?? []);
      const body = await fetchMemoryDoc(token);
      setText(body.text);
      setSaved(body.text);
      const changed = view.tidied?.changed ?? 0;
      setNote({ ok: true, text: changed ? `整理了 ${changed} 处` : "已经很整齐了" });
    } catch (e) {
      setNote({ ok: false, text: e instanceof Error ? e.message : String(e) });
    } finally {
      setTidying(false);
    }
  };

  const classifiedItems = useMemo(() => {
    const list: { item: MemoryItem; provenance: MemoryProvenance }[] = [];
    for (const section of sections) {
      for (const item of section.items) {
        list.push({ item, provenance: classifyMemoryItem(item, changes) });
      }
    }
    return list;
  }, [sections, changes]);

  const dirty = text !== saved;

  const loadDoc = async (doc: Doc | "close") => {
    setPendingSwitch(null);
    const seq = ++openSeq.current;
    if (doc === "close") {
      setOpen(null);
      setText(saved);
      setNote(null);
      return;
    }
    setOpen(doc);
    setNote(null);
    setText("");
    setSaved("");
    try {
      const body = doc === "soul" ? await fetchSoul(token) : await fetchMemoryDoc(token);
      if (seq !== openSeq.current) return;
      setText(body.text);
      setSaved(body.text);
      if (doc === "memory") loadChanges();
    } catch (e) {
      if (seq !== openSeq.current) return;
      setNote({ ok: false, text: e instanceof Error ? e.message : String(e) });
    }
  };

  const openDoc = async (doc: Doc) => {
    const target: Doc | "close" = open === doc ? "close" : doc;
    if (open && dirty) {
      setPendingSwitch(target);
      return;
    }
    await loadDoc(target);
  };

  const save = async () => {
    if (!open) return;
    setBusy(true);
    setNote(null);
    try {
      const body = open === "soul" ? await saveSoul(client, text) : await saveMemoryDoc(client, text);
      setText(body.text);
      setSaved(body.text);
      setPendingSwitch(null);
      if (open === "memory") loadChanges();
      setNote({ ok: true, text: open === "soul" ? "已保存，下次对话生效" : "已保存" });
    } catch (e) {
      setNote({ ok: false, text: e instanceof Error ? e.message : String(e) });
    } finally {
      setBusy(false);
    }
  };

  const undo = async (change: MemoryChange) => {
    try {
      await undoMemoryChange(client, change.id);
      const body = await fetchMemoryDoc(token);
      setText(body.text);
      setSaved(body.text);
      loadChanges();
    } catch (e) {
      setNote({ ok: false, text: e instanceof Error ? e.message : String(e) });
    }
  };

  return (
    <div className="pb-2">
      {identity}
      <div className="grid grid-cols-2 gap-3">
        {(Object.keys(DOCS) as Doc[]).map((doc) => (
          <button
            key={doc}
            type="button"
            aria-expanded={open === doc}
            onClick={() => void openDoc(doc)}
            className={cn(
              "flex h-[110px] flex-col items-start rounded-[18px] p-3.5 text-left transition active:scale-[0.98]",
              DOCS[doc].className,
              open && open !== doc && "opacity-50",
            )}
          >
            <span className="text-[17px] font-semibold">{DOCS[doc].title}</span>
            <span className="mt-auto text-[12px] opacity-85">{DOCS[doc].caption}</span>
          </button>
        ))}
      </div>

      {open && (
        <div className="om-rise mt-4">
          {pendingSwitch && dirty && (
            <div className="mb-3 flex items-center gap-2 rounded-2xl border border-om-border bg-om-surface-2 px-3.5 py-2.5 text-[13px]" role="alert">
              <span className="min-w-0 flex-1">还有没保存的修改，要放弃吗？</span>
              <button
                type="button"
                onClick={() => setPendingSwitch(null)}
                className="rounded-full px-3 py-1 font-medium text-om-muted"
              >
                继续编辑
              </button>
              <button
                type="button"
                onClick={() => void loadDoc(pendingSwitch)}
                className="rounded-full bg-om-danger px-3 py-1 font-semibold text-white"
              >
                放弃修改
              </button>
            </div>
          )}
          <p className="mb-2 text-[12.5px] leading-snug text-om-muted">{DOCS[open].hint}</p>
          {open === "memory" && (
            <>
              <div className="mb-2.5 rounded-2xl border border-om-border bg-om-surface-2 px-3.5 py-2.5 text-[12px] leading-relaxed">
                <div className="flex items-start gap-2">
                  <ShieldCheck size={15} className="mt-0.5 shrink-0 text-emerald-600 dark:text-emerald-400" aria-hidden="true" />
                  <div>
                    <span className="font-semibold text-om-fg">记忆来源与自然淡化：</span>
                    标为
                    <span className="mx-1 inline-block rounded-full bg-emerald-500/15 px-1.5 py-0.5 text-[11px] font-semibold text-emerald-700 dark:text-emerald-300">
                      你写的
                    </span>
                    的内容永久保留，不会被自动合并、改写或淡化；伙伴从聊天中学到的细节若很久不再提起，会先淡化成大概印象，再久才慢慢忘掉，每次自动改动都可在下方撤销。
                  </div>
                </div>
              </div>
              <div className="mb-2 flex flex-wrap items-center gap-2 text-[12.5px] text-om-muted">
                <span className="min-w-0 flex-1">{learnedAt ? `上次更新：${dayLabel(learnedAt * 1000)}` : "还没自己更新过"}</span>
                <button
                  type="button"
                  disabled={learning || tidying || dirty}
                  onClick={() => void tidyNow()}
                  className="inline-flex items-center gap-1 rounded-full bg-om-surface-2 px-3 py-1.5 font-medium text-om-fg disabled:opacity-40"
                >
                  {tidying ? <Loader2 size={13} className="animate-spin" /> : <Sparkles size={13} />}
                  {tidying ? "整理中…" : "整理一下"}
                </button>
                <button
                  type="button"
                  disabled={learning || tidying || dirty}
                  onClick={() => void learnNow()}
                  className="inline-flex items-center gap-1 rounded-full bg-om-surface-2 px-3 py-1.5 font-medium text-om-fg disabled:opacity-40"
                >
                  {learning ? <Loader2 size={13} className="animate-spin" /> : <RefreshCw size={13} />}
                  {learning ? "正在读聊天…" : "现在更新"}
                </button>
              </div>
            </>
          )}
          <textarea
            value={text}
            onChange={(e) => setText(e.target.value)}
            aria-label={DOCS[open].title}
            rows={12}
            spellCheck={false}
            className="w-full resize-y rounded-2xl bg-om-surface-2 px-3.5 py-3 font-mono text-[14px] leading-relaxed outline-none focus:ring-2 focus:ring-om-accent"
          />
          <div className="mt-2 flex items-center gap-2">
            <span className={cn("min-w-0 flex-1 truncate text-[12.5px]", note?.ok ? "text-emerald-600 dark:text-emerald-400" : "text-om-danger")} role="status">
              {note && (
                <>
                  {note.ok && <Check size={13} className="mr-1 inline" aria-hidden="true" />}
                  {note.text}
                </>
              )}
            </span>
            {dirty && (
              <button type="button" onClick={() => setText(saved)} className="rounded-full px-3 py-2 text-[13.5px] text-om-muted">
                还原
              </button>
            )}
            <button
              type="button"
              disabled={!dirty || busy}
              onClick={() => void save()}
              className="rounded-full bg-om-accent px-4 py-2 text-[13.5px] font-semibold text-om-accent-fg disabled:opacity-40"
            >
              {busy ? "保存中…" : "保存"}
            </button>
          </div>
          {open === "memory" && classifiedItems.length > 0 && (
            <div className="mt-4">
              <div className="mb-1.5 flex items-center justify-between text-[12px]">
                <span className="font-semibold text-om-muted">
                  当前记忆来源（{classifiedItems.filter((x) => x.provenance === "user_written").length} 条你写的 ·{" "}
                  {classifiedItems.filter((x) => x.provenance !== "user_written").length} 条自动提炼）
                </span>
                {classifiedItems.length > 5 && (
                  <button
                    type="button"
                    onClick={() => setShowAllItems((v) => !v)}
                    className="font-medium text-om-accent"
                  >
                    {showAllItems ? "收起" : `展开全部 ${classifiedItems.length} 条`}
                  </button>
                )}
              </div>
              <ul className="divide-y divide-[color:var(--om-border)] overflow-hidden rounded-2xl bg-om-surface-2">
                {(showAllItems ? classifiedItems : classifiedItems.slice(0, 5)).map(({ item, provenance }) => {
                  const badge = PROVENANCE_META[provenance];
                  return (
                    <li key={item.id} className="flex items-start gap-2 px-3.5 py-2 text-[13px]">
                      <span className={cn("mt-0.5 shrink-0 rounded-full px-2 py-0.5 text-[11px] font-semibold", badge.className)}>
                        {badge.label}
                      </span>
                      <span className="min-w-0 flex-1 leading-snug">{plainEntry(item.text)}</span>
                    </li>
                  );
                })}
              </ul>
            </div>
          )}
          {open === "memory" && changes.some((c) => !c.undone) && (
            <div className="mt-4">
              <div className="mb-1.5 text-[12px] font-semibold text-om-muted">最近记下和改过的</div>
              <ul className="divide-y divide-[color:var(--om-border)] overflow-hidden rounded-2xl bg-om-surface-2">
                {changes
                  .filter((c) => !c.undone)
                  .slice(0, 5)
                  .map((change) => (
                    <li key={change.id} className="flex items-start gap-2 py-2.5 pl-3.5 pr-1 text-[13px]">
                      <span className="min-w-0 flex-1">
                        <span className="mb-1 flex flex-wrap items-center gap-1.5 text-[11.5px]">
                          <span className={cn("rounded-full px-2 py-0.5 font-semibold", changeBadgeTone(change.kind))}>
                            {CHANGE_LABEL[change.kind] ?? "改动"}
                          </span>
                          {change.at > 0 && <span className="text-om-muted">{dayLabel(change.at * 1000)}</span>}
                        </span>
                        {change.before.length > 0 && (
                          <span className="block text-om-muted line-through">{change.before.map(plainEntry).join(" / ")}</span>
                        )}
                        {change.after && <span className="block">{plainEntry(change.after)}</span>}
                        {change.why && <span className="mt-0.5 block text-[12px] text-om-muted">{change.why}</span>}
                      </span>
                      <button type="button" aria-label="撤销这处改动" onClick={() => void undo(change)} className="flex h-9 w-9 shrink-0 items-center justify-center text-om-muted">
                        <Undo2 size={15} />
                      </button>
                    </li>
                  ))}
              </ul>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
