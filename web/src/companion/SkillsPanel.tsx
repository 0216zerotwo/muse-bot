/*
 * 技能 (in 设置): what it knows how to do step by step. Ones it wrote itself after getting
 * something done, or that the user taught it by doing a task once in the browser (教它做), come
 * first; the ones that ship with it are folded below. Each opens to its steps, with 用一次 /
 * 定时做 (both start a message in the chat), 编辑 and 删除.
 */
import { ChevronDown, ChevronRight, GraduationCap } from "lucide-react";
import { useCallback, useEffect, useState } from "react";

import { MarkdownText } from "@/components/MarkdownText";
import { cn } from "@/lib/utils";
import { useClient } from "@/providers/ClientProvider";

import { deleteSkill, fetchSkills, readSkill, saveSkill, type SkillInfo, type SkillsPayload } from "./companion-api";
import { MenuGroup } from "./Menu";
import { MuseSheet } from "./muse/Sheet";
import { dayLabel } from "./presentation";

/** "market-data-comparison" → "market data comparison". */
export function skillTitle(name: string): string {
  return name.replace(/[-_]+/g, " ");
}

/** The steps without the header block (name, description) at the top. */
function body(content: string): string {
  return content.replace(/^---\s*\n[\s\S]*?\n---\s*\n?/, "").trim();
}

/** How "用一次" and "定时做" word the message they start. */
export const skillMessage = (name: string) => `用「${name}」技能：`;
const scheduleSkillText = (name: string) => `每天早上 9 点用「${name}」技能做一遍`;

function SkillSheet({
  skill,
  bundled,
  onClose,
  onChanged,
  onDraft,
}: {
  skill: SkillInfo | null;
  bundled: boolean;
  onClose: () => void;
  onChanged: (payload: SkillsPayload) => void;
  onDraft: (text: string) => void;
}) {
  const { client } = useClient();
  const [content, setContent] = useState<string | null>(null);
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [confirmDelete, setConfirmDelete] = useState(false);

  useEffect(() => {
    setContent(null);
    setEditing(false);
    setError(null);
    setConfirmDelete(false);
    if (!skill) return;
    readSkill(client, skill.name)
      .then((s) => setContent(s.content))
      .catch((e: unknown) => setError(e instanceof Error ? e.message : String(e)));
  }, [client, skill]);

  const act = async (task: () => Promise<void>) => {
    setBusy(true);
    setError(null);
    try {
      await task();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <MuseSheet open={skill !== null} onClose={onClose} title={skill ? skillTitle(skill.name) : ""}>
      {skill && (
        <div className="flex flex-col gap-3 pb-2">
          {skill.description && <p className="c-muted text-[14px] leading-relaxed">{skill.description}</p>}
          <div className="flex gap-2">
            <button type="button" onClick={() => onDraft(skillMessage(skill.name))} className="c-primary min-h-11 flex-1 rounded-full text-[15px] font-semibold">
              用一次
            </button>
            <button type="button" onClick={() => onDraft(scheduleSkillText(skill.name))} className="c-pressable min-h-11 flex-1 rounded-full text-[15px]" style={{ background: "var(--c-fill)" }}>
              定时做
            </button>
          </div>

          {content === null && !error && <div className="c-card h-32 animate-pulse rounded-2xl" aria-hidden="true" />}
          {content !== null && !editing && (
            <div className="c-card max-h-[45vh] overflow-y-auto rounded-2xl px-4 py-3 text-[14.5px] leading-relaxed">
              <MarkdownText>{body(content)}</MarkdownText>
            </div>
          )}
          {editing && (
            <textarea
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              rows={14}
              spellCheck={false}
              aria-label="技能内容"
              className="c-card w-full resize-y rounded-2xl px-3.5 py-3 font-mono text-[13.5px] leading-relaxed outline-none"
            />
          )}
          {error && <p className="c-danger text-[14px]">{error}</p>}

          {!bundled && content !== null && (
            editing ? (
              <div className="flex gap-2">
                <button type="button" onClick={() => setEditing(false)} className="c-pressable min-h-11 flex-1 rounded-full text-[15px]" style={{ background: "var(--c-fill)" }}>
                  取消
                </button>
                <button
                  type="button"
                  disabled={busy || draft === content}
                  onClick={() =>
                    void act(async () => {
                      onChanged(await saveSkill(client, skill.name, draft));
                      setContent(draft);
                      setEditing(false);
                    })
                  }
                  className="c-primary min-h-11 flex-1 rounded-full text-[15px] font-semibold disabled:opacity-40"
                >
                  {busy ? "保存中…" : "保存"}
                </button>
              </div>
            ) : (
              <button
                type="button"
                onClick={() => {
                  setDraft(content);
                  setEditing(true);
                }}
                className="c-pressable min-h-11 rounded-full text-[15px]"
                style={{ background: "var(--c-fill)" }}
              >
                编辑步骤
              </button>
            )
          )}
          {!bundled && !editing && (
            confirmDelete ? (
              <div className="flex gap-2">
                <button type="button" onClick={() => setConfirmDelete(false)} className="c-pressable min-h-11 flex-1 rounded-full text-[15px]" style={{ background: "var(--c-fill)" }}>
                  取消
                </button>
                <button
                  type="button"
                  disabled={busy}
                  onClick={() =>
                    void act(async () => {
                      onChanged(await deleteSkill(client, skill.name));
                      onClose();
                    })
                  }
                  className="min-h-11 flex-1 rounded-full text-[15px] font-semibold text-white"
                  style={{ background: "var(--c-danger-fill)" }}
                >
                  删除
                </button>
              </div>
            ) : (
              <button type="button" onClick={() => setConfirmDelete(true)} className="c-danger min-h-10 text-[14px]">
                删除这个技能
              </button>
            )
          )}
        </div>
      )}
    </MuseSheet>
  );
}

function SkillRow({ skill, onOpen, showDate }: { skill: SkillInfo; onOpen: () => void; showDate: boolean }) {
  return (
    <li>
      <button type="button" onClick={onOpen} className="c-pressable flex w-full items-center gap-3 px-4 py-3 text-left">
        <span className="min-w-0 flex-1">
          <span className="block truncate text-[15.5px] font-medium">{skillTitle(skill.name)}</span>
          {skill.description && <span className="c-muted line-clamp-2 block text-[13px] leading-snug">{skill.description}</span>}
        </span>
        {showDate && <span className="c-muted shrink-0 text-[12px]">{dayLabel(skill.updated_at)}</span>}
        <ChevronRight className="c-muted h-4 w-4 shrink-0" aria-hidden="true" />
      </button>
    </li>
  );
}

export function SkillsPanel({ onDraft, onTeach }: { onDraft: (text: string) => void; onTeach: () => void }) {
  const { token } = useClient();
  const [data, setData] = useState<SkillsPayload | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [showBundled, setShowBundled] = useState(false);
  const [open, setOpen] = useState<{ skill: SkillInfo; bundled: boolean } | null>(null);

  const load = useCallback(() => {
    fetchSkills(token)
      .then(setData)
      .catch((e: unknown) => setError(e instanceof Error ? e.message : String(e)));
  }, [token]);
  useEffect(load, [load]);

  return (
    <div className="flex flex-col gap-6">
      <button type="button" onClick={onTeach} className="c-card c-pressable flex items-center gap-3 rounded-2xl p-4 text-left">
        <span className="grid h-10 w-10 shrink-0 place-items-center rounded-full" style={{ background: "var(--c-fill)" }} aria-hidden="true">
          <GraduationCap className="h-5 w-5" />
        </span>
        <span className="min-w-0 flex-1">
          <span className="block text-[16px] font-semibold">教它一个新技能</span>
          <span className="c-muted block text-[13px] leading-snug">在浏览器里亲手做一遍，它记下步骤，以后照着做</span>
        </span>
        <ChevronRight className="c-muted h-4 w-4 shrink-0" aria-hidden="true" />
      </button>

      {error && <p className="c-danger px-1 text-[14px]">{error}</p>}

      <section className="flex flex-col gap-1.5">
        <h2 className="c-muted px-3.5 text-[13px]">它学会的</h2>
        {data && data.learned.length === 0 ? (
          <p className="c-muted px-3.5 text-[14px] leading-relaxed">还没有。它把一件事做成后会自己记成技能，你也可以亲手教它。</p>
        ) : (
          <MenuGroup>
            <ul className="c-divider divide-y">
              {(data?.learned ?? []).map((skill) => (
                <SkillRow key={skill.name} skill={skill} showDate onOpen={() => setOpen({ skill, bundled: false })} />
              ))}
            </ul>
          </MenuGroup>
        )}
      </section>

      {data && data.bundled.length > 0 && (
        <section className="flex flex-col gap-1.5">
          <button
            type="button"
            aria-expanded={showBundled}
            onClick={() => setShowBundled((v) => !v)}
            className="c-muted flex items-center gap-1 px-3.5 text-left text-[13px]"
          >
            自带的 {data.bundled.length} 个
            <ChevronDown className={cn("h-3.5 w-3.5 transition-transform", showBundled && "rotate-180")} aria-hidden="true" />
          </button>
          {showBundled && (
            <MenuGroup>
              <ul className="c-divider divide-y">
                {data.bundled.map((skill) => (
                  <SkillRow key={skill.name} skill={skill} showDate={false} onOpen={() => setOpen({ skill, bundled: true })} />
                ))}
              </ul>
            </MenuGroup>
          )}
        </section>
      )}

      <SkillSheet
        skill={open?.skill ?? null}
        bundled={open?.bundled ?? false}
        onClose={() => setOpen(null)}
        onChanged={setData}
        onDraft={(text) => {
          setOpen(null);
          onDraft(text);
        }}
      />
    </div>
  );
}
