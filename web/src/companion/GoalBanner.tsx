import { useState } from "react";
import { ChevronDown, Square, Target } from "lucide-react";

import type { GoalStateWsPayload } from "@/lib/types";

/** Prefix the gateway expects for an explicit, user-started sustained goal. */
export const GOAL_COMMAND = "/goal ";

export function isGoalMessage(content: string): boolean {
  return content.startsWith(GOAL_COMMAND);
}

export function goalText(content: string): string {
  return content.slice(GOAL_COMMAND.length).trim();
}

/**
 * Floating card while a goal is in progress: what it is working toward, where
 * it is now, and a way to stop it. Blocked goals ask the user to step in.
 */
export function GoalBanner({
  goal,
  onStop,
}: {
  goal: GoalStateWsPayload;
  onStop: () => void;
}) {
  const [open, setOpen] = useState(false);
  const blocked = goal.status === "blocked";
  const objective = goal.objective?.trim() || "进行中的目标";
  const summary = goal.ui_summary?.trim();

  return (
    <section
      className="c-float c-rise pointer-events-auto mx-auto w-full max-w-sm rounded-[20px] px-3 py-2.5"
      aria-label="进行中的目标"
    >
      <div className="flex items-center gap-2.5">
        <span
          className={`grid h-8 w-8 shrink-0 place-items-center rounded-full ${blocked ? "" : "c-primary"}`}
          style={blocked ? { background: "var(--c-danger-fill)", color: "#fff" } : undefined}
          aria-hidden="true"
        >
          <Target className="h-4 w-4" />
        </span>
        <button
          type="button"
          onClick={() => setOpen((value) => !value)}
          aria-expanded={open}
          className="flex min-h-11 min-w-0 flex-1 flex-col items-start justify-center text-left"
        >
          <span className="flex w-full items-center gap-1 text-[14px] font-semibold">
            <span className="truncate">{objective}</span>
            <ChevronDown
              className={`h-3.5 w-3.5 shrink-0 transition-transform motion-reduce:transition-none ${open ? "rotate-180" : ""}`}
              aria-hidden="true"
            />
          </span>
          <span className={`w-full truncate text-[12px] ${blocked ? "c-danger" : "c-muted"}`}>
            {blocked ? "卡住了，需要你看一下" : summary || "正在推进…"}
          </span>
        </button>
        <button
          type="button"
          onClick={onStop}
          aria-label="停止这个目标"
          className="c-pressable grid h-11 w-11 shrink-0 place-items-center rounded-full transition-transform active:scale-95 motion-reduce:transition-none"
        >
          <Square className="h-3.5 w-3.5" style={{ fill: "currentColor" }} aria-hidden="true" />
        </button>
      </div>
      {open && (
        <div className="mt-2 border-t pt-2 c-divider">
          <p className="whitespace-pre-wrap text-[14px] leading-relaxed">{goal.objective}</p>
          {summary && <p className="c-muted mt-2 text-[13px]">当前：{summary}</p>}
          {goal.recap?.trim() && <p className="c-muted mt-1 text-[13px]">回顾：{goal.recap}</p>}
        </div>
      )}
    </section>
  );
}
