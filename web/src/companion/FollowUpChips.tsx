import { Sparkles } from "lucide-react";
import { useEffect, useState } from "react";

import { useClient } from "@/providers/ClientProvider";

import { fetchFollowups } from "./companion-api";

export const MAX_FOLLOWUPS_CACHE = 100;
const FOLLOWUPS_CACHE = new Map<string, string[]>();

export function clearFollowupsCache(): void {
  FOLLOWUPS_CACHE.clear();
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
 * Results are cached per ``(chatId, turnId)`` to avoid repeat network requests on re-renders.
 */
export function FollowUpChips({
  chatId,
  turnId,
  enabled,
  onSelect,
}: {
  chatId: string;
  turnId: string | null;
  enabled: boolean;
  onSelect: (prompt: string) => void;
}) {
  const { token } = useClient();
  const cacheKey = chatId && turnId ? `${chatId}:${turnId}` : "";
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
