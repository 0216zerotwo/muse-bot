import type { ActivityKind } from "./presentation";

/**
 * Animated companion pets.
 *
 * A pet is a Codex pet sprite atlas (8 columns × 9 rows of 192×208 cells,
 * see ~/.codex/skills/hatch-pet) plus, for our own pets, a second atlas with
 * the errands it runs for you: typing on a laptop, reading, drawing… Codex
 * pets work as they are; errands they lack fall back to the nearest base row
 * with a small badge saying what it is doing.
 */

export const CELL_WIDTH = 192;
export const CELL_HEIGHT = 208;
export const ATLAS_COLUMNS = 8;

/** Rows of the Codex atlas (``spritesheet.webp``), in order. */
export type BaseAnimation =
  | "idle"
  | "running-right"
  | "running-left"
  | "waving"
  | "jumping"
  | "failed"
  | "waiting"
  | "running"
  | "review";

/** Rows of our errands atlas (``actions.webp``), in order. */
export type ActionAnimation = "typing" | "reading" | "drawing" | "writing" | "sleeping" | "searching";

export type PetAnimation = BaseAnimation | ActionAnimation;

interface Row {
  sheet: "base" | "actions";
  row: number;
  /** One duration per frame (ms); the frame count is its length. */
  durations: number[];
}

function frames(count: number, each: number, last: number): number[] {
  return [...Array.from({ length: count - 1 }, () => each), last];
}

/** Frame timings from hatch-pet's references/animation-rows.md, plus our errand rows. */
export const ROWS: Record<PetAnimation, Row> = {
  idle: { sheet: "base", row: 0, durations: [280, 110, 110, 140, 140, 320] },
  "running-right": { sheet: "base", row: 1, durations: frames(8, 120, 220) },
  "running-left": { sheet: "base", row: 2, durations: frames(8, 120, 220) },
  waving: { sheet: "base", row: 3, durations: frames(4, 140, 280) },
  jumping: { sheet: "base", row: 4, durations: frames(5, 140, 280) },
  failed: { sheet: "base", row: 5, durations: frames(8, 140, 240) },
  waiting: { sheet: "base", row: 6, durations: frames(6, 150, 260) },
  running: { sheet: "base", row: 7, durations: frames(6, 120, 220) },
  review: { sheet: "base", row: 8, durations: frames(6, 150, 280) },
  typing: { sheet: "actions", row: 0, durations: frames(6, 120, 200) },
  reading: { sheet: "actions", row: 1, durations: frames(6, 180, 320) },
  drawing: { sheet: "actions", row: 2, durations: frames(6, 150, 260) },
  writing: { sheet: "actions", row: 3, durations: frames(6, 150, 260) },
  sleeping: { sheet: "actions", row: 4, durations: frames(6, 320, 480) },
  searching: { sheet: "actions", row: 5, durations: frames(6, 160, 300) },
};

export const ACTION_ANIMATIONS: ActionAnimation[] = ["typing", "reading", "drawing", "writing", "sleeping", "searching"];

export function isAction(animation: PetAnimation): animation is ActionAnimation {
  return ROWS[animation].sheet === "actions";
}

/** What a pet without the errands atlas plays instead (with a badge). */
export const ACTION_FALLBACK: Record<ActionAnimation, BaseAnimation> = {
  typing: "running",
  reading: "review",
  drawing: "running",
  writing: "running",
  sleeping: "idle",
  searching: "review",
};

/** Names for previews and accessible labels. */
export const ANIMATION_LABELS: Record<PetAnimation, string> = {
  idle: "待机",
  "running-right": "向右走",
  "running-left": "向左走",
  waving: "招手",
  jumping: "跳起来",
  failed: "沮丧",
  waiting: "等待",
  running: "忙碌",
  review: "思考",
  typing: "敲电脑",
  reading: "翻书",
  drawing: "画画",
  writing: "写信",
  sleeping: "睡觉",
  searching: "拿放大镜",
};

/** What it does while working on each kind of step. */
const ACTIVITY_ANIMATIONS: Record<ActivityKind, PetAnimation> = {
  thinking: "review",
  typing: "writing",
  search: "searching",
  read: "reading",
  files: "typing",
  run: "typing",
  image: "drawing",
  schedule: "writing",
  send: "writing",
  recall: "review",
  delegate: "running",
  app: "running",
  working: "running",
};

export function activityAnimation(kind: ActivityKind): PetAnimation {
  return ACTIVITY_ANIMATIONS[kind];
}

/** Quiet hours, matching the check-ins: it sleeps from 22:00 to 9:00 when nothing is going on. */
export function isNight(date = new Date()): boolean {
  const hour = date.getHours();
  return hour >= 22 || hour < 9;
}

/** How long it must be left alone at night before it dozes off. */
export const DOZE_AFTER_MS = 2 * 60 * 1000;

export interface PetMood {
  connected: boolean;
  activity: ActivityKind | null;
  /** Approvals waiting for the user. */
  pendingApprovals: number;
  /** Milliseconds since the user last did something in the app. */
  idleForMs: number;
  now?: Date;
}

/** The looping animation for the current situation (one-off reactions play on top of it). */
export function moodAnimation({ connected, activity, pendingApprovals, idleForMs, now }: PetMood): PetAnimation {
  if (!connected) return "sleeping";
  if (activity) return activityAnimation(activity);
  if (pendingApprovals > 0) return "waving";
  if (isNight(now) && idleForMs >= DOZE_AFTER_MS) return "sleeping";
  return "idle";
}

/** Total length of one pass through a row, for one-off reactions. */
export function rowDuration(animation: PetAnimation): number {
  return ROWS[animation].durations.reduce((sum, ms) => sum + ms, 0);
}
