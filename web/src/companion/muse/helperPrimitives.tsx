import type { Helper } from "../companion-api";
import { EmojiIcon } from "../EmojiIcon";

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
      style={{
        width: size,
        height: size,
        background: "var(--c-surface)",
        boxShadow: "inset 0 0 0 0.5px var(--c-border)",
        gridTemplateColumns: shown.length > 1 ? `repeat(2, ${cell}px)` : undefined,
      }}
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
