/*
 * Ported from nanoMuse (vendor/nanomuse-web/web/src/goals.tsx, GPL-3.0-or-later): the life
 * areas, the cadence spec ("weekly mon 09:00") and its words, the due-date line — in Chinese.
 */
import { Briefcase, Compass, GraduationCap, Heart, HeartHandshake, House, Palette, PiggyBank, Tag, Users } from "lucide-react";
import type { ReactNode } from "react";

import type { GoalCategory } from "../companion-api";

/** The name of a scheduled job that asks about a goal: the goal shows it, task lists leave it out. */
export const GOAL_CHECKIN_PREFIX = "目标跟进：";

/** Life areas. Colors are Tailwind classes so the badge and the picker agree. */
export const CATEGORIES: Array<{ id: GoalCategory; label: string; icon: (p: { size: number }) => ReactNode; tone: string }> = [
  { id: "health", label: "健康", icon: (p) => <Heart {...p} />, tone: "bg-rose-500/10 text-rose-600 dark:text-rose-300" },
  { id: "finance", label: "财务", icon: (p) => <PiggyBank {...p} />, tone: "bg-emerald-500/10 text-emerald-600 dark:text-emerald-300" },
  { id: "career", label: "事业", icon: (p) => <Briefcase {...p} />, tone: "bg-sky-500/10 text-sky-600 dark:text-sky-300" },
  { id: "learning", label: "学习", icon: (p) => <GraduationCap {...p} />, tone: "bg-violet-500/10 text-violet-600 dark:text-violet-300" },
  { id: "relationships", label: "人际", icon: (p) => <HeartHandshake {...p} />, tone: "bg-pink-500/10 text-pink-600 dark:text-pink-300" },
  { id: "family", label: "家人", icon: (p) => <Users {...p} />, tone: "bg-amber-500/10 text-amber-600 dark:text-amber-300" },
  { id: "home", label: "居家", icon: (p) => <House {...p} />, tone: "bg-orange-500/10 text-orange-600 dark:text-orange-300" },
  { id: "travel", label: "出行", icon: (p) => <Compass {...p} />, tone: "bg-cyan-500/10 text-cyan-600 dark:text-cyan-300" },
  { id: "creative", label: "创作", icon: (p) => <Palette {...p} />, tone: "bg-fuchsia-500/10 text-fuchsia-600 dark:text-fuchsia-300" },
  { id: "other", label: "其他", icon: (p) => <Tag {...p} />, tone: "bg-om-surface-2 text-om-muted" },
];

export function categoryOf(id: GoalCategory | null | undefined) {
  return CATEGORIES.find((c) => c.id === id) ?? null;
}

export const CADENCES = [
  { id: "", label: "不提醒" },
  { id: "daily", label: "每天" },
  { id: "weekdays", label: "工作日" },
  { id: "weekly", label: "每周" },
  { id: "monthly", label: "每月" },
];
export const WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"];
const WEEKDAY_NAMES: Record<string, string> = { mon: "周一", tue: "周二", wed: "周三", thu: "周四", fri: "周五", sat: "周六", sun: "周日" };

/** "weekly mon 09:00" → parts, and back. */
export function splitCadence(spec: string | null | undefined): { cadence: string; anchor: string; time: string } {
  const m = /^(daily|weekdays|weekly|monthly)(?:\s+(\S+?))?(?:\s+(\d{1,2}:\d{2}))?$/.exec((spec ?? "").trim());
  if (!m) return { cadence: "", anchor: "", time: "09:00" };
  let anchor = m[2] ?? "";
  let time = m[3] ?? "09:00";
  if (anchor && /^\d{1,2}:\d{2}$/.test(anchor)) {
    time = anchor;
    anchor = "";
  }
  return { cadence: m[1], anchor, time: time.padStart(5, "0") };
}

export function joinCadence(cadence: string, anchor: string, time: string): string {
  if (!cadence) return "";
  return [cadence, anchor, time].filter(Boolean).join(" ");
}

export function weekdayName(short: string): string {
  return WEEKDAY_NAMES[short] ?? short;
}

/** "每天 08:00", "工作日 07:30", "每周一 09:00", "每月 1 日 09:00". */
export function describeCadence(spec: string | null | undefined): string {
  const { cadence, anchor, time } = splitCadence(spec);
  switch (cadence) {
    case "daily":
      return `每天 ${time}`;
    case "weekdays":
      return `工作日 ${time}`;
    case "weekly":
      return `每${weekdayName(anchor || "mon")} ${time}`;
    case "monthly":
      return `每月 ${anchor || 1} 日 ${time}`;
    default:
      return "";
  }
}

/** "今天到期", "还有 5 天", "12月31日前", "已过期 2 天". */
export function dueLabel(due: string | null | undefined): string {
  if (!due) return "";
  const d = new Date(`${due}T00:00:00`);
  const days = Math.round((d.getTime() - new Date().setHours(0, 0, 0, 0)) / 86_400_000);
  if (days < 0) return `已过期 ${-days} 天`;
  if (days === 0) return "今天到期";
  if (days === 1) return "明天到期";
  if (days <= 14) return `还有 ${days} 天`;
  return `${d.getMonth() + 1}月${d.getDate()}日前`;
}

const CRON_DAYS: Record<string, string> = { "0": "周日", "1": "周一", "2": "周二", "3": "周三", "4": "周四", "5": "周五", "6": "周六", "7": "周日" };

/** A scheduled job's cron line in words: "每天 08:00", "工作日 07:30", "每天 9/12/15 点", or the raw line. */
export function cronLabel(expr: string): string {
  const parts = expr.trim().split(/\s+/);
  if (parts.length !== 5) return `定时 · ${expr}`;
  const [minute, hour, dom, month, dow] = parts;
  if (month !== "*" || !/^\d+$/.test(minute)) return `定时 · ${expr}`;
  const at = /^\d+$/.test(hour)
    ? `${hour.padStart(2, "0")}:${minute.padStart(2, "0")}`
    : /^\d+(,\d+)+$/.test(hour)
      ? `${hour.split(",").join("/")} 点${minute === "0" ? "" : ` ${minute} 分`}`
      : null;
  if (!at) return `定时 · ${expr}`;
  if (dom === "*" && dow === "*") return `每天 ${at}`;
  if (dom === "*" && dow === "1-5") return `工作日 ${at}`;
  if (dom === "*" && CRON_DAYS[dow]) return `每${CRON_DAYS[dow]} ${at}`;
  if (/^\d+$/.test(dom) && dow === "*") return `每月 ${dom} 日 ${at}`;
  return `定时 · ${expr}`;
}
