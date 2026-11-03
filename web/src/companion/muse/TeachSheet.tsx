/*
 * 教它做, after 做完了: the steps the user did, in words; what the task was; then the skill the
 * companion wrote from them, to read and change before it is saved (nothing is kept unless saved).
 */
import { Loader2 } from "lucide-react";
import { useEffect, useState } from "react";

import { useClient } from "@/providers/ClientProvider";

import { draftSkill, saveSkill, type TaughtStep } from "../companion-api";
import { MuseSheet } from "./Sheet";

export function stepText(step: TaughtStep): string {
  switch (step.do) {
    case "open":
      return `打开 ${step.url ?? ""}`;
    case "click":
      return `点 ${step.target ?? "页面"}`;
    case "type":
      return `在${step.target ?? "输入框"}里输入「${step.text ?? ""}」`;
    case "key":
      return `按 ${step.key === "Enter" ? "回车" : step.key}`;
    case "back":
      return "返回上一页";
  }
}

const field = "c-card w-full rounded-2xl px-3.5 outline-none";

export function TeachSheet({ steps, name, onClose }: { steps: TaughtStep[] | null; name: string; onClose: () => void }) {
  const { client } = useClient();
  const [goal, setGoal] = useState("");
  const [draft, setDraft] = useState<{ name: string; content: string } | null>(null);
  const [saved, setSaved] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    setGoal("");
    setDraft(null);
    setSaved(null);
    setError(null);
  }, [steps]);

  const run = async (task: () => Promise<void>) => {
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
    <MuseSheet open={steps !== null} onClose={onClose} title={saved ? "学会了" : draft ? "看看这个技能" : `记下了 ${steps?.length ?? 0} 步`}>
      {steps && (
        <div className="flex flex-col gap-4 pb-2">
          {saved ? (
            <>
              <p className="text-[15px] leading-relaxed">
                {name}学会了「{saved}」。以后在聊天里输入 / 就能选它，或者说「照「{saved}」做一遍」；在「设置 → 技能」里可以改。
              </p>
              <button type="button" onClick={onClose} className="c-primary min-h-11 rounded-full text-[15px] font-semibold">
                好
              </button>
            </>
          ) : draft ? (
            <>
              <label className="flex flex-col gap-1.5">
                <span className="text-[14px] font-semibold">技能名</span>
                <input
                  value={draft.name}
                  onChange={(e) => setDraft({ ...draft, name: e.target.value })}
                  className={`${field} min-h-11 text-[16px]`}
                  aria-label="技能名"
                />
                <span className="c-muted text-[12.5px]">英文小写和短横线，比如 book-train-ticket</span>
              </label>
              <label className="flex flex-col gap-1.5">
                <span className="text-[14px] font-semibold">步骤</span>
                <textarea
                  value={draft.content}
                  onChange={(e) => setDraft({ ...draft, content: e.target.value })}
                  rows={14}
                  spellCheck={false}
                  aria-label="技能内容"
                  className={`${field} resize-y py-3 font-mono text-[13.5px] leading-relaxed`}
                />
                <span className="c-muted text-[12.5px]">它以后照这里做。哪步不对直接改。</span>
              </label>
              {error && <p className="c-danger text-[14px]">{error}</p>}
              <button
                type="button"
                disabled={busy || !draft.name.trim() || !draft.content.trim()}
                onClick={() =>
                  void run(async () => {
                    await saveSkill(client, draft.name.trim(), draft.content, true);
                    setSaved(draft.name.trim());
                  })
                }
                className="c-primary min-h-11 rounded-full text-[15px] font-semibold disabled:opacity-40"
              >
                {busy ? "保存中…" : "保存技能"}
              </button>
            </>
          ) : (
            <>
              <ol className="c-card max-h-[35vh] list-decimal overflow-y-auto rounded-2xl py-2.5 pl-9 pr-3.5 text-[14px] leading-relaxed">
                {steps.map((step, i) => (
                  <li key={i} className="break-all">
                    {stepText(step)}
                  </li>
                ))}
              </ol>
              <label className="flex flex-col gap-1.5">
                <span className="text-[14px] font-semibold">这是在做什么？</span>
                <input
                  value={goal}
                  onChange={(e) => setGoal(e.target.value)}
                  maxLength={200}
                  placeholder="比如：在 12306 查明天去上海的高铁"
                  aria-label="这是在做什么"
                  className={`${field} min-h-11 text-[16px]`}
                />
                <span className="c-muted text-[12.5px]">说清楚，它才知道下次什么时候用、哪些地方要换成新的内容。</span>
              </label>
              {error && <p className="c-danger text-[14px]">{error}</p>}
              <button
                type="button"
                disabled={busy}
                onClick={() => void run(async () => setDraft(await draftSkill(client, goal, steps)))}
                className="c-primary flex min-h-11 items-center justify-center gap-2 rounded-full text-[15px] font-semibold disabled:opacity-60"
              >
                {busy && <Loader2 className="h-4 w-4 animate-spin" />}
                {busy ? "正在整理…" : "整理成技能"}
              </button>
            </>
          )}
        </div>
      )}
    </MuseSheet>
  );
}
