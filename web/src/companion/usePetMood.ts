import { useCallback, useEffect, useRef, useState } from "react";

import type { ApprovalRecord } from "./companion-api";
import { moodAnimation, type PetAnimation } from "./petAnimations";
import type { ActivityKind } from "./presentation";

/** Between idle fidgets (a glance around while nothing happens). */
const FIDGET_MIN_MS = 18_000;
const FIDGET_SPREAD_MS = 14_000;
/** Re-check the clock for night-time dozing. */
const CLOCK_TICK_MS = 30_000;

export interface PetMoodState {
  animation: PetAnimation;
  /** False for one-off reactions, which end by calling ``onDone``. */
  loop: boolean;
  onDone: () => void;
  /** Play a one-off reaction now (e.g. jumping when tapped). */
  react: (animation: PetAnimation) => void;
}

/**
 * What the pet is doing: the errand in progress, waving for approvals,
 * dozing at night or while offline, otherwise idling with the odd fidget;
 * plus reactions: a wave hello, a slump when something fails, a jump when
 * an approved action went through.
 */
export function usePetMood({
  connected,
  activity,
  approvals,
  failed,
}: {
  connected: boolean;
  activity: ActivityKind | null;
  approvals: ApprovalRecord[];
  /** Becomes true when a turn fails. */
  failed: boolean;
}): PetMoodState {
  const [reaction, setReaction] = useState<PetAnimation | null>("waving");
  const react = useCallback((animation: PetAnimation) => setReaction(animation), []);
  const onDone = useCallback(() => setReaction(null), []);

  /* Being left alone (for night-time dozing). */
  const [lastTouch, setLastTouch] = useState(() => Date.now());
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const touch = () => setLastTouch(Date.now());
    document.addEventListener("pointerdown", touch);
    document.addEventListener("keydown", touch);
    const clock = window.setInterval(() => setNow(Date.now()), CLOCK_TICK_MS);
    return () => {
      document.removeEventListener("pointerdown", touch);
      document.removeEventListener("keydown", touch);
      window.clearInterval(clock);
    };
  }, []);

  /* A slump when a turn fails. */
  const wasFailed = useRef(failed);
  useEffect(() => {
    if (failed && !wasFailed.current) setReaction("failed");
    wasFailed.current = failed;
  }, [failed]);

  /* A jump when an action it asked about went through. */
  const statuses = useRef(new Map(approvals.map((approval) => [approval.id, approval.status])));
  useEffect(() => {
    const previous = statuses.current;
    if (approvals.some((approval) => approval.status === "executed" && previous.get(approval.id) === "pending")) {
      setReaction("jumping");
    }
    statuses.current = new Map(approvals.map((approval) => [approval.id, approval.status]));
  }, [approvals]);

  const pendingApprovals = approvals.filter((approval) => approval.status === "pending").length;
  const mood = moodAnimation({
    connected,
    activity,
    pendingApprovals,
    idleForMs: Math.max(0, now - lastTouch),
    now: new Date(now),
  });

  /* Now and then, while idle, it glances around; work cancels a pending glance. */
  useEffect(() => {
    if (reaction === "waiting" && mood !== "idle") setReaction(null);
  }, [mood, reaction]);
  useEffect(() => {
    if (mood !== "idle" || reaction) return;
    const timer = window.setTimeout(() => setReaction("waiting"), FIDGET_MIN_MS + Math.random() * FIDGET_SPREAD_MS);
    return () => window.clearTimeout(timer);
  }, [mood, reaction]);

  // Work in progress wins over an idle glance; other reactions play out first.
  if (reaction && (reaction !== "waiting" || mood === "idle")) {
    return { animation: reaction, loop: false, onDone, react };
  }
  return { animation: mood, loop: true, onDone, react };
}
