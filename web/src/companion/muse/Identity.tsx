/*
 * Who the companion is, edited where it is shown: the pen on the face in the header (name, look)
 * and the 身份与记忆 pane of the status sheet — SOUL.md and MEMORY.md, edited in place. The pen and
 * the two file cards follow nanoMuse's MuseSheet (vendor/nanomuse-web, GPL-3.0-or-later).
 */
import { Check, Loader2, RefreshCw, Undo2 } from "lucide-react";
import { useCallback, useEffect, useState, type ReactNode } from "react";

import { cn } from "@/lib/utils";
import { useClient } from "@/providers/ClientProvider";

import {
  fetchMemory,
  learnMemory,
  fetchMemoryDoc,
  fetchSoul,
  saveMemoryDoc,
  saveSoul,
  undoMemoryChange,
  type CompanionChanges,
  type CompanionState,
  type MemoryChange,
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

/** The 身份与记忆 pane: SOUL.md and MEMORY.md, opened and edited right here. */
export function SoulMemoryPane({ identity }: { identity?: ReactNode } = {}) {
  const { client, token } = useClient();
  const [open, setOpen] = useState<Doc | null>(null);
  const [text, setText] = useState("");
  const [saved, setSaved] = useState("");
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<{ ok: boolean; text: string } | null>(null);
  const [changes, setChanges] = useState<MemoryChange[]>([]);
  const [learnedAt, setLearnedAt] = useState<number | null>(null);
  const [learning, setLearning] = useState(false);

  const loadChanges = useCallback(() => {
    fetchMemory(token)
      .then((view) => {
        setChanges(view.changes ?? []);
        setLearnedAt(view.learned_at ?? null);
      })
      .catch(() => setChanges([]));
  }, [token]);

  /* 现在更新: read the new chats now instead of waiting for the hour. */
  const learnNow = async () => {
    setLearning(true);
    setNote(null);
    try {
      const view = await learnMemory(client);
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

  const openDoc = async (doc: Doc) => {
    if (open === doc) {
      setOpen(null);
      return;
    }
    setOpen(doc);
    setNote(null);
    setText("");
    try {
      const body = doc === "soul" ? await fetchSoul(token) : await fetchMemoryDoc(token);
      setText(body.text);
      setSaved(body.text);
      if (doc === "memory") loadChanges();
    } catch (e) {
      setNote({ ok: false, text: e instanceof Error ? e.message : String(e) });
    }
  };

  const save = async () => {
    if (!open) return;
    setBusy(true);
    setNote(null);
    try {
      const body = open === "soul" ? await saveSoul(client, text) : await saveMemoryDoc(client, text);
      setText(body.text);
      setSaved(body.text);
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

  const dirty = text !== saved;
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
          <p className="mb-2 text-[12.5px] leading-snug text-om-muted">{DOCS[open].hint}</p>
          {open === "memory" && (
            <div className="mb-2 flex items-center gap-2 text-[12.5px] text-om-muted">
              <span className="min-w-0 flex-1">{learnedAt ? `上次更新：${dayLabel(learnedAt * 1000)}` : "还没自己更新过"}</span>
              <button
                type="button"
                disabled={learning || dirty}
                onClick={() => void learnNow()}
                className="inline-flex items-center gap-1 rounded-full bg-om-surface-2 px-3 py-1.5 font-medium text-om-fg disabled:opacity-40"
              >
                {learning ? <Loader2 size={13} className="animate-spin" /> : <RefreshCw size={13} />}
                {learning ? "正在读聊天…" : "现在更新"}
              </button>
            </div>
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
          {open === "memory" && changes.some((c) => !c.undone) && (
            <div className="mt-4">
              <div className="mb-1.5 text-[12px] font-semibold text-om-muted">最近记下和改过的</div>
              <ul className="divide-y divide-[color:var(--om-border)] overflow-hidden rounded-2xl bg-om-surface-2">
                {changes
                  .filter((c) => !c.undone)
                  .slice(0, 5)
                  .map((change) => (
                    <li key={change.id} className="flex items-start gap-2 py-2 pl-3.5 pr-1 text-[13px]">
                      <span className="min-w-0 flex-1">
                        <span className="block text-[11.5px] text-om-muted">{CHANGE_LABEL[change.kind] ?? "改动"}</span>
                        {change.before.length > 0 && (
                          <span className="block text-om-muted line-through">{change.before.map(plainEntry).join(" / ")}</span>
                        )}
                        {change.after && <span className="block">{plainEntry(change.after)}</span>}
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
