import { useSyncExternalStore } from "react";

export const DRAFT_STORE_KEY = "companion.drafts.v1";
export const MAX_DRAFT_CHATS = 30;

export interface ChatDraft {
  text: string;
  quote: string | null;
  updatedAt: number;
}

type DraftMap = Record<string, ChatDraft>;

const listeners = new Set<() => void>();
const EMPTY_SET: ReadonlySet<string> = new Set();

let cachedRaw: string | null | undefined;
let cachedIds: ReadonlySet<string> = EMPTY_SET;

function notify(): void {
  cachedRaw = undefined;
  for (const listener of listeners) {
    listener();
  }
}

function readRaw(): string | null {
  try {
    return typeof window !== "undefined" ? window.localStorage.getItem(DRAFT_STORE_KEY) : null;
  } catch {
    return null;
  }
}

function parseMap(raw: string | null): DraftMap {
  if (!raw) return {};
  try {
    const parsed = JSON.parse(raw) as unknown;
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return {};
    const out: DraftMap = {};
    for (const [chatId, entry] of Object.entries(parsed as Record<string, unknown>)) {
      if (!chatId || !entry || typeof entry !== "object") continue;
      const rec = entry as Record<string, unknown>;
      const text = typeof rec.text === "string" ? rec.text : "";
      const quote = typeof rec.quote === "string" && rec.quote.trim() ? rec.quote : null;
      const updatedAt = typeof rec.updatedAt === "number" && Number.isFinite(rec.updatedAt) ? rec.updatedAt : 0;
      if (!text.trim() && !quote) continue;
      out[chatId] = { text, quote, updatedAt };
    }
    return out;
  } catch {
    return {};
  }
}

function writeMap(map: DraftMap): void {
  try {
    if (typeof window === "undefined") return;
    const entries = Object.entries(map);
    if (entries.length === 0) {
      window.localStorage.removeItem(DRAFT_STORE_KEY);
    } else {
      entries.sort((a, b) => b[1].updatedAt - a[1].updatedAt);
      const pruned: DraftMap = {};
      for (const [chatId, draft] of entries.slice(0, MAX_DRAFT_CHATS)) {
        pruned[chatId] = draft;
      }
      window.localStorage.setItem(DRAFT_STORE_KEY, JSON.stringify(pruned));
    }
  } catch {
    // Ignore storage quota or private-mode errors.
  }
  notify();
}

/** Read a saved non-empty draft for ``chatId``, or null when none exists. */
export function loadDraft(chatId: string): ChatDraft | null {
  if (!chatId) return null;
  const map = parseMap(readRaw());
  return map[chatId] ?? null;
}

/** Persist unsent text and quote for ``chatId`` (LRU-capped at 30 sessions); empty clears it. */
export function saveDraft(chatId: string, text: string, quote: string | null, now = Date.now()): void {
  if (!chatId) return;
  const cleanQuote = quote && quote.trim() ? quote : null;
  const map = parseMap(readRaw());
  if (!text.trim() && !cleanQuote) {
    if (chatId in map) {
      delete map[chatId];
      writeMap(map);
    }
    return;
  }
  const existing = map[chatId];
  if (existing && existing.text === text && existing.quote === cleanQuote) return;
  map[chatId] = { text, quote: cleanQuote, updatedAt: now };
  writeMap(map);
}

/** Remove the persisted draft for ``chatId`` (e.g., immediately after successful send). */
export function clearDraft(chatId: string): void {
  if (!chatId) return;
  const map = parseMap(readRaw());
  if (!(chatId in map)) return;
  delete map[chatId];
  writeMap(map);
}

/** Whether ``chatId`` currently has a non-empty unsent draft or quote. */
export function hasDraft(chatId: string): boolean {
  return loadDraft(chatId) !== null;
}

/** Return a stable Set of chat IDs that currently hold non-empty drafts. */
export function listDraftChatIds(): ReadonlySet<string> {
  const raw = readRaw();
  if (raw === cachedRaw) return cachedIds;
  cachedRaw = raw;
  const keys = Object.keys(parseMap(raw));
  cachedIds = keys.length > 0 ? new Set(keys) : EMPTY_SET;
  return cachedIds;
}

/** Subscribe to draft updates across this tab and cross-tab localStorage changes. */
export function subscribeDrafts(listener: () => void): () => void {
  listeners.add(listener);
  const onStorage = (event: StorageEvent) => {
    if (event.key === null || event.key === DRAFT_STORE_KEY) {
      cachedRaw = undefined;
      listener();
    }
  };
  if (typeof window !== "undefined") {
    window.addEventListener("storage", onStorage);
  }
  return () => {
    listeners.delete(listener);
    if (typeof window !== "undefined") {
      window.removeEventListener("storage", onStorage);
    }
  };
}

/** Reactive set of chat IDs with unsent drafts (used by the sidebar drawer). */
export function useDraftChatIds(): ReadonlySet<string> {
  return useSyncExternalStore(subscribeDrafts, listDraftChatIds, () => EMPTY_SET);
}
