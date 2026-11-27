import { Sparkles } from "lucide-react";
import { useEffect, useState } from "react";

import { useClient } from "@/providers/ClientProvider";

import { fetchFollowups } from "./companion-api";

export const MAX_FOLLOWUPS_CACHE = 100;
const FOLLOWUPS_CACHE = new Map<string, string[]>();

export function clearFollowupsCache(): void {
  FOLLOWUPS_CACHE.clear();
}

/** Drop cached follow-ups for a chat (or one regenerated turn) whose underlying reply changed. */
export function invalidateFollowupsCacheFor(chatId: string, turnId?: string | null): void {
  if (!chatId) return;
  const prefix = turnId ? `${chatId}:${turnId}:` : `${chatId}:`;
  const exact = turnId ? `${chatId}:${turnId}` : "";
  for (const key of Array.from(FOLLOWUPS_CACHE.keys())) {
    if (key.startsWith(prefix) || (exact && key === exact)) {
      FOLLOWUPS_CACHE.delete(key);
    }
  }
}

/** Compact deterministic digest so same-turn regenerations miss stale client cache entries. */
export function followupReplyFingerprint(text: string): string {
  const trimmed = text.trim();
  let hash = 0x811c9dc5;
  for (let i = 0; i < trimmed.length; i += 1) {
    hash ^= trimmed.charCodeAt(i);
    hash = Math.imul(hash, 0x01000193) >>> 0;
  }
  return `${trimmed.length}:${hash.toString(16)}`;
}

function writeFollowupsCache(key: string, suggestions: string[]): void {
  if (FOLLOWUPS_CACHE.has(key)) FOLLOWUPS_CACHE.delete(key);
  FOLLOWUPS_CACHE.set(key, suggestions);
  if (FOLLOWUPS_CACHE.size > MAX_FOLLOWUPS_CACHE) {
    const oldest = FOLLOWUPS_CACHE.keys().next().value;
    if (oldest) FOLLOWUPS_CACHE.delete(oldest);
  }
}

/**
 * Lazy-loaded contextual follow-up prompt chips displayed at the bottom of an idle thread.
 * Results are cached per ``(chatId, turnId, fingerprint)`` to avoid repeat fetches on re-renders
 * while still refreshing immediately if the reply for the same ``turnId`` is regenerated.
 */
export function FollowUpChips({
  chatId,
  turnId,
  fingerprint,
  enabled,
  onSelect,
}: {
  chatId: string;
  turnId: string | null;
  fingerprint?: string | null;
  enabled: boolean;
  onSelect: (prompt: string) => void;
}) {
  const { token } = useClient();
  const cacheKey = chatId && turnId ? (fingerprint ? `${chatId}:${turnId}:${fingerprint}` : `${chatId}:${turnId}`) : "";
  const [suggestions, setSuggestions] = useState<string[]>(() =>
    cacheKey ? (FOLLOWUPS_CACHE.get(cacheKey) ?? []) : [],
  );

  useEffect(() => {
    if (!enabled || !chatId || !turnId || !token) {
      setSuggestions([]);
      return;
    }
    const cached = FOLLOWUPS_CACHE.get(cacheKey);
    if (cached) {
      setSuggestions(cached);
      return;
    }

    let cancelled = false;
    const controller = new AbortController();
    setSuggestions([]);
    fetchFollowups(token, chatId, turnId, controller.signal)
      .then((payload) => {
        if (cancelled) return;
        const next = (payload.suggestions ?? [])
          .map((item) => item.trim())
          .filter(Boolean)
          .slice(0, 3);
        writeFollowupsCache(cacheKey, next);
        setSuggestions(next);
      })
      .catch(() => {
        if (!cancelled) setSuggestions([]);
      });

    return () => {
      cancelled = true;
      controller.abort();
    };
  }, [cacheKey, chatId, enabled, token, turnId]);

  if (!enabled || !turnId || suggestions.length === 0) return null;

  return (
    <div className="c-rise mt-2 flex flex-wrap items-center gap-1.5 px-4 pb-2" aria-label="继续追问">
      {suggestions.map((item) => (
        <button
          key={item}
          type="button"
          onClick={() => onSelect(item)}
          className="c-card c-pressable inline-flex min-h-9 items-center gap-1.5 rounded-full px-3.5 py-1.5 text-left text-[13px] leading-snug transition-transform active:scale-95 motion-reduce:transition-none"
        >
          <Sparkles className="h-3.5 w-3.5 shrink-0" style={{ color: "var(--c-primary)" }} aria-hidden="true" />
          <span>{item}</span>
        </button>
      ))}
    </div>
  );
}
