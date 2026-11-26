import { BookOpen, ExternalLink, Globe } from "lucide-react";
import { useState } from "react";

import type { CitationSource } from "@/lib/types";

import { MuseSheet } from "./muse/Sheet";

const MAX_INLINE_CHIPS = 4;

export function isKnowledgeSource(source: CitationSource): boolean {
  return source.url.startsWith("#knowledge:") || (source.domain?.startsWith("知识库") ?? false);
}

export function isSafeHttpUrl(url: string): boolean {
  return /^https?:\/\//i.test(url.trim());
}

export function sourceDomainLabel(source: CitationSource): string {
  if (source.domain?.trim()) return source.domain.trim();
  if (isKnowledgeSource(source)) return "知识库";
  if (isSafeHttpUrl(source.url)) {
    try {
      return new URL(source.url).hostname.replace(/^www\./, "");
    } catch {
      return source.title;
    }
  }
  return source.title;
}

function sourceBadgeNumber(source: CitationSource, index: number): number {
  return typeof source.id === "number" && Number.isFinite(source.id) && source.id > 0 ? source.id : index + 1;
}

/**
 * Perplexity-style compact source citation pills rendered under a reply,
 * paired with a full details drawer grouped by Knowledge Vault vs Web sources.
 */
export function SourcesBar({ sources }: { sources: CitationSource[] }) {
  const [sheetOpen, setSheetOpen] = useState(false);
  const [focusedIndex, setFocusedIndex] = useState<number | null>(null);

  if (!sources.length) return null;

  const openDetails = (index: number | null = null) => {
    setFocusedIndex(index);
    setSheetOpen(true);
  };

  const inlineSources = sources.slice(0, MAX_INLINE_CHIPS);
  const knowledgeItems = sources
    .map((source, index) => ({ source, index }))
    .filter(({ source }) => isKnowledgeSource(source));
  const webItems = sources
    .map((source, index) => ({ source, index }))
    .filter(({ source }) => !isKnowledgeSource(source));

  const renderDetailGroup = (
    heading: string,
    items: Array<{ source: CitationSource; index: number }>,
    knowledge: boolean,
  ) => {
    if (!items.length) return null;
    return (
      <section className="flex flex-col gap-2">
        <div className="c-muted flex items-center gap-1.5 px-1 text-[12px] font-semibold">
          {knowledge ? <BookOpen className="h-3.5 w-3.5" aria-hidden="true" /> : <Globe className="h-3.5 w-3.5" aria-hidden="true" />}
          <span>{heading}</span>
        </div>
        <ul className="flex flex-col gap-2">
          {items.map(({ source, index }) => {
            const num = sourceBadgeNumber(source, index);
            const domain = sourceDomainLabel(source);
            const safeUrl = isSafeHttpUrl(source.url) ? source.url : null;
            const focused = focusedIndex === index;
            return (
              <li
                key={`${source.url || source.title}-${index}`}
                className={`rounded-2xl bg-om-surface-2 px-3.5 py-2.5 ${
                  focused ? "ring-2 ring-[var(--c-primary)]" : ""
                }`}
              >
                <div className="flex items-start gap-2">
                  <span
                    className="mt-0.5 inline-flex h-5 min-w-5 shrink-0 items-center justify-center rounded-full px-1.5 text-[11px] font-semibold tabular-nums"
                    style={{
                      background: "color-mix(in srgb, var(--c-primary) 14%, transparent)",
                      color: "var(--c-primary)",
                    }}
                  >
                    {num}
                  </span>
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center gap-2">
                      <span className="c-muted truncate text-[11.5px] font-medium">{domain}</span>
                      {safeUrl && (
                        <a
                          href={safeUrl}
                          target="_blank"
                          rel="noopener noreferrer"
                          className="ml-auto inline-flex shrink-0 items-center gap-1 text-[12px] font-medium"
                          style={{ color: "var(--c-primary)" }}
                        >
                          打开
                          <ExternalLink className="h-3 w-3" aria-hidden="true" />
                        </a>
                      )}
                    </div>
                    <p className="mt-0.5 text-[14px] font-medium leading-snug">{source.title}</p>
                    {source.snippet && (
                      <p className="c-muted mt-1 line-clamp-4 text-[12.5px] leading-relaxed">{source.snippet}</p>
                    )}
                  </div>
                </div>
              </li>
            );
          })}
        </ul>
      </section>
    );
  };

  return (
    <div className="mt-0.5 flex max-w-[88%] flex-wrap items-center gap-1.5 px-0.5" aria-label="参考来源">
      {inlineSources.map((source, index) => {
        const num = sourceBadgeNumber(source, index);
        const domain = sourceDomainLabel(source);
        const pillClass =
          "c-card c-pressable inline-flex max-w-[180px] items-center gap-1.5 rounded-full px-2.5 py-1 text-[12px] leading-none transition-transform active:scale-95 motion-reduce:transition-none";
        const content = (
          <>
            <span
              className="inline-flex h-4 min-w-4 shrink-0 items-center justify-center rounded-full px-1 text-[10.5px] font-semibold tabular-nums"
              style={{
                background: "color-mix(in srgb, var(--c-primary) 14%, transparent)",
                color: "var(--c-primary)",
              }}
            >
              {num}
            </span>
            <span className="truncate">{domain}</span>
          </>
        );
        if (!isKnowledgeSource(source) && isSafeHttpUrl(source.url)) {
          return (
            <a
              key={`${source.url}-${index}`}
              href={source.url}
              target="_blank"
              rel="noopener noreferrer"
              title={source.title}
              aria-label={`[${num}] ${domain}：${source.title}`}
              className={pillClass}
            >
              {content}
            </a>
          );
        }
        return (
          <button
            key={`${source.url || source.title}-${index}`}
            type="button"
            title={source.title}
            aria-label={`[${num}] ${domain}：${source.title}`}
            onClick={() => openDetails(index)}
            className={pillClass}
          >
            {content}
          </button>
        );
      })}
      <button
        type="button"
        onClick={() => openDetails(null)}
        className="c-muted c-pressable inline-flex items-center rounded-full px-2.5 py-1 text-[12px] font-medium"
        style={{ background: "var(--c-fill)" }}
      >
        查看全部 {sources.length} 个来源
      </button>

      <MuseSheet open={sheetOpen} onClose={() => setSheetOpen(false)} title={`参考来源（${sources.length}）`}>
        <div className="flex flex-col gap-4 pb-2">
          {renderDetailGroup("知识库条目", knowledgeItems, true)}
          {renderDetailGroup("网页来源", webItems, false)}
        </div>
      </MuseSheet>
    </div>
  );
}
