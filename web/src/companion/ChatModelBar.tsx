import { useEffect, useRef, useState } from "react";
import { Check, ChevronDown, Cpu, Gauge, Loader2, Minimize2, Sparkles } from "lucide-react";

import type { ChatSummary } from "@/lib/types";
import { useClient } from "@/providers/ClientProvider";

import {
  setChatModel,
  type ChatContextUsage,
  type ModelsPayload,
  type ReasoningEffort,
} from "./companion-api";
import { formatTokens } from "./UsagePanel";

const EFFORT_OPTIONS: Array<{ value: ReasoningEffort | null; label: string }> = [
  { value: null, label: "默认" },
  { value: "low", label: "低" },
  { value: "medium", label: "中" },
  { value: "high", label: "高" },
];

const EFFORT_LABEL: Record<string, string> = {
  none: "关",
  low: "低",
  medium: "中",
  high: "高",
  xhigh: "极高",
  max: "最高",
  adaptive: "自动",
};

/** Format a 0..1 usage ratio into a clamped whole percentage 0..100. */
export function contextPercent(usage: ChatContextUsage): number {
  if (!Number.isFinite(usage.usage_ratio) || usage.usage_ratio <= 0) return 0;
  return Math.min(100, Math.round(usage.usage_ratio * 100));
}

/** Compact per-chat model/reasoning capsule and context watermark meter above the Composer. */
export function ChatModelBar({
  chatId,
  session,
  models,
  contextUsage,
  hasMessages,
  streaming,
  connected,
  onModelChanged,
  onCompact,
}: {
  chatId: string;
  session: ChatSummary;
  models: ModelsPayload | null;
  contextUsage: ChatContextUsage | null;
  hasMessages: boolean;
  streaming: boolean;
  connected: boolean;
  onModelChanged?: (next: { model_preset: string | null; reasoning_effort: string | null }) => void;
  onCompact?: () => void;
}) {
  const { client } = useClient();
  const rootRef = useRef<HTMLDivElement>(null);
  const [openPopover, setOpenPopover] = useState<"model" | "context" | null>(null);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [localOverride, setLocalOverride] = useState<{
    chatId: string;
    model_preset: string | null;
    reasoning_effort: string | null;
  } | null>(null);

  useEffect(() => {
    setLocalOverride(null);
    setOpenPopover(null);
    setError(null);
  }, [chatId, session.modelPreset, session.reasoningEffort]);

  useEffect(() => {
    if (!openPopover) return;
    const onPointerDown = (event: MouseEvent) => {
      if (rootRef.current && !rootRef.current.contains(event.target as Node)) {
        setOpenPopover(null);
      }
    };
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.stopPropagation();
        setOpenPopover(null);
      }
    };
    document.addEventListener("mousedown", onPointerDown);
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("mousedown", onPointerDown);
      document.removeEventListener("keydown", onKeyDown);
    };
  }, [openPopover]);

  const activeOverride = localOverride?.chatId === chatId ? localOverride : null;
  const effectivePreset = activeOverride
    ? activeOverride.model_preset
    : (session.modelPreset ?? contextUsage?.model_preset ?? null);
  const effectiveEffort = activeOverride
    ? activeOverride.reasoning_effort
    : (session.reasoningEffort ?? contextUsage?.reasoning_effort ?? null);

  const presets = Array.isArray(models?.presets) ? models.presets : [];
  const globalDefault = models?.settings?.fixed ?? models?.default ?? presets[0]?.name ?? null;
  const hasModelPicker = presets.length > 0;
  const hasContextMeter = Boolean(hasMessages && contextUsage && contextUsage.context_window > 0);

  if (!hasModelPicker && !hasContextMeter) return null;

  const isOverridden = Boolean(effectivePreset || effectiveEffort);
  const displayModel = effectivePreset || globalDefault || contextUsage?.model || "默认模型";
  const effortText = effectiveEffort ? (EFFORT_LABEL[effectiveEffort] ?? effectiveEffort) : null;

  const applySelection = async (nextPreset: string | null, nextEffort: ReasoningEffort | null) => {
    if (saving) return;
    setSaving(true);
    setError(null);
    try {
      const res = await setChatModel(client, chatId, {
        model_preset: nextPreset,
        reasoning_effort: nextEffort,
      });
      const resolvedPreset = res?.model_preset ?? nextPreset;
      const resolvedEffort = res?.reasoning_effort ?? nextEffort;
      setLocalOverride({
        chatId,
        model_preset: resolvedPreset,
        reasoning_effort: resolvedEffort,
      });
      onModelChanged?.({
        model_preset: resolvedPreset,
        reasoning_effort: resolvedEffort,
      });
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "切换模型失败");
    } finally {
      setSaving(false);
    }
  };

  const pct = contextUsage ? contextPercent(contextUsage) : 0;
  const ringRadius = 5.5;
  const ringCircumference = 2 * Math.PI * ringRadius;
  const ringDashOffset = ringCircumference * (1 - pct / 100);
  const ctxTone = !contextUsage
    ? "normal"
    : contextUsage.estimated_tokens >= contextUsage.tier2_threshold || pct >= 85
      ? "danger"
      : contextUsage.estimated_tokens >= contextUsage.tier1_threshold || pct >= 65
        ? "warn"
        : "normal";
  const ctxColor =
    ctxTone === "danger"
      ? "var(--c-danger)"
      : ctxTone === "warn"
        ? "#d97706"
        : "var(--c-online)";

  return (
    <div ref={rootRef} className="relative flex flex-wrap items-center gap-1.5 px-1">
      {hasModelPicker && (
        <button
          type="button"
          onClick={() => setOpenPopover((prev) => (prev === "model" ? null : "model"))}
          aria-expanded={openPopover === "model"}
          aria-haspopup="dialog"
          aria-label={`当前会话模型：${displayModel}${effortText ? `，思考强度${effortText}` : ""}`}
          data-testid="chat-model-capsule"
          className="c-pressable inline-flex h-7 max-w-[230px] items-center gap-1.5 rounded-full px-2.5 text-[12px] font-medium transition-colors"
          style={
            isOverridden
              ? { background: "var(--c-accent-soft)", color: "var(--c-primary)" }
              : { background: "var(--c-fill)", color: "var(--c-text-muted)" }
          }
        >
          {saving ? (
            <Loader2 className="h-3 w-3 shrink-0 animate-spin motion-reduce:animate-none" aria-hidden="true" />
          ) : isOverridden ? (
            <Sparkles className="h-3 w-3 shrink-0" aria-hidden="true" />
          ) : (
            <Cpu className="h-3 w-3 shrink-0" aria-hidden="true" />
          )}
          <span className="truncate">{displayModel}</span>
          {effortText && <span className="shrink-0 opacity-80">· {effortText}</span>}
          {!isOverridden && <span className="shrink-0 opacity-65">· 默认</span>}
          <ChevronDown className="h-3 w-3 shrink-0 opacity-70" aria-hidden="true" />
        </button>
      )}

      {hasContextMeter && contextUsage && (
        <button
          type="button"
          onClick={() => setOpenPopover((prev) => (prev === "context" ? null : "context"))}
          aria-expanded={openPopover === "context"}
          aria-haspopup="dialog"
          aria-label={`上下文占用 ${pct}%`}
          data-testid="chat-context-capsule"
          className="c-pressable inline-flex h-7 items-center gap-1.5 rounded-full px-2.5 text-[12px] font-medium tabular-nums transition-colors"
          style={{
            background: "var(--c-fill)",
            color: ctxTone === "normal" ? "var(--c-text-muted)" : ctxColor,
          }}
        >
          <svg className="h-3.5 w-3.5 shrink-0 -rotate-90" viewBox="0 0 14 14" aria-hidden="true">
            <circle
              cx="7"
              cy="7"
              r={ringRadius}
              fill="none"
              stroke="currentColor"
              strokeOpacity="0.22"
              strokeWidth="1.8"
            />
            <circle
              cx="7"
              cy="7"
              r={ringRadius}
              fill="none"
              stroke={ctxColor}
              strokeWidth="1.8"
              strokeLinecap="round"
              strokeDasharray={ringCircumference}
              strokeDashoffset={ringDashOffset}
            />
          </svg>
          <span>{pct}%</span>
        </button>
      )}

      {openPopover === "model" && hasModelPicker && (
        <div
          role="dialog"
          aria-label="当前会话模型与思考强度"
          data-testid="chat-model-popover"
          className="c-card c-rise absolute bottom-full left-0 z-30 mb-2 w-[280px] max-w-[calc(100vw-2rem)] rounded-[20px] p-3 shadow-xl"
        >
          <div className="mb-2 flex items-center justify-between">
            <span className="text-[13px] font-semibold">本会话模型预设</span>
            {isOverridden && (
              <button
                type="button"
                disabled={saving}
                onClick={() => void applySelection(null, null)}
                className="c-pressable rounded-full px-2 py-0.5 text-[11.5px] font-medium"
                style={{ background: "var(--c-fill)", color: "var(--c-primary)" }}
              >
                恢复默认
              </button>
            )}
          </div>

          <div className="flex max-h-48 flex-col gap-1 overflow-y-auto">
            <button
              type="button"
              disabled={saving}
              aria-pressed={!effectivePreset}
              onClick={() => void applySelection(null, (effectiveEffort as ReasoningEffort | null) ?? null)}
              className="c-pressable flex w-full items-center gap-2 rounded-xl px-2.5 py-2 text-left text-[13px]"
              style={!effectivePreset ? { background: "var(--c-accent-soft)", color: "var(--c-primary)" } : undefined}
            >
              <span className="min-w-0 flex-1">
                <span className="block font-medium">跟随全局默认</span>
                {globalDefault && <span className="c-muted block truncate text-[11.5px]">{globalDefault}</span>}
              </span>
              {!effectivePreset && <Check className="h-3.5 w-3.5 shrink-0" aria-hidden="true" />}
            </button>

            {presets.map((preset) => {
              const selected = effectivePreset === preset.name;
              const health = models?.health?.[preset.name];
              const dotColor = health
                ? health.ok
                  ? "var(--c-online)"
                  : "var(--c-danger)"
                : "var(--c-text-muted)";
              return (
                <button
                  key={preset.name}
                  type="button"
                  disabled={saving}
                  aria-pressed={selected}
                  onClick={() =>
                    void applySelection(preset.name, (effectiveEffort as ReasoningEffort | null) ?? null)
                  }
                  className="c-pressable flex w-full items-center gap-2 rounded-xl px-2.5 py-2 text-left text-[13px]"
                  style={selected ? { background: "var(--c-accent-soft)", color: "var(--c-primary)" } : undefined}
                >
                  <span
                    className="h-2 w-2 shrink-0 rounded-full"
                    style={{ background: dotColor, opacity: health ? 1 : 0.45 }}
                    aria-hidden="true"
                  />
                  <span className="min-w-0 flex-1">
                    <span className="block truncate font-medium">{preset.name}</span>
                    <span className="c-muted block truncate text-[11.5px]">{preset.model}</span>
                  </span>
                  {selected && <Check className="h-3.5 w-3.5 shrink-0" aria-hidden="true" />}
                </button>
              );
            })}
          </div>

          <div className="c-divider mt-2.5 border-t pt-2.5">
            <span className="c-muted mb-1.5 block text-[11.5px] font-medium">思考深度</span>
            <div
              role="radiogroup"
              aria-label="思考深度"
              className="grid grid-cols-4 gap-1 rounded-xl p-0.5"
              style={{ background: "var(--c-segment)" }}
            >
              {EFFORT_OPTIONS.map((opt) => {
                const checked = (effectiveEffort ?? null) === opt.value;
                return (
                  <button
                    key={opt.label}
                    type="button"
                    role="radio"
                    aria-checked={checked}
                    disabled={saving}
                    onClick={() => void applySelection(effectivePreset, opt.value)}
                    className="min-h-8 rounded-[9px] text-[12px] font-medium transition-colors"
                    style={
                      checked
                        ? { background: "var(--c-segment-on)", color: "var(--c-text)", boxShadow: "0 1px 2px rgba(0,0,0,0.12)" }
                        : { color: "var(--c-text-muted)" }
                    }
                  >
                    {opt.label}
                  </button>
                );
              })}
            </div>
          </div>

          {error && (
            <p className="c-danger mt-2 text-[12px]" role="alert">
              {error}
            </p>
          )}
        </div>
      )}

      {openPopover === "context" && hasContextMeter && contextUsage && (
        <div
          role="dialog"
          aria-label="上下文占用详情"
          data-testid="chat-context-popover"
          className="c-card c-rise absolute bottom-full left-0 z-30 mb-2 w-[276px] max-w-[calc(100vw-2rem)] rounded-[20px] p-3.5 shadow-xl"
        >
          <div className="flex items-center justify-between gap-2">
            <span className="inline-flex items-center gap-1.5 text-[13px] font-semibold">
              <Gauge className="h-4 w-4" style={{ color: ctxColor }} aria-hidden="true" />
              上下文水位
            </span>
            <span className="text-[13px] font-semibold tabular-nums" style={{ color: ctxColor }}>
              {pct}%
            </span>
          </div>

          <div className="mt-2 h-2 w-full overflow-hidden rounded-full" style={{ background: "var(--c-fill)" }}>
            <div
              className="h-full rounded-full transition-all"
              style={{ width: `${Math.max(2, pct)}%`, background: ctxColor }}
            />
          </div>

          <p className="mt-2 text-[12.5px] tabular-nums">
            已用约 <strong>{formatTokens(contextUsage.estimated_tokens)}</strong> /{" "}
            {formatTokens(contextUsage.context_window)} token
          </p>
          <p className="c-muted mt-1 text-[11.5px] leading-snug">
            模型：{contextUsage.model || displayModel}
            {contextUsage.tier2_threshold > 0 ? ` · 自动压缩阈值 ${formatTokens(contextUsage.tier2_threshold)}` : ""}
          </p>
          {contextUsage.has_summary_checkpoint && (
            <span
              className="mt-1.5 inline-block rounded-full px-2 py-0.5 text-[11px] font-medium"
              style={{ background: "var(--c-accent-soft)", color: "var(--c-primary)" }}
            >
              已有摘要检查点
            </span>
          )}

          {onCompact && (
            <button
              type="button"
              disabled={!connected || streaming}
              onClick={() => {
                setOpenPopover(null);
                onCompact();
              }}
              className="c-primary mt-3 flex min-h-9 w-full items-center justify-center gap-1.5 rounded-full text-[12.5px] font-semibold disabled:opacity-45"
            >
              <Minimize2 className="h-3.5 w-3.5" aria-hidden="true" />
              立即压缩上下文 (/compact)
            </button>
          )}
        </div>
      )}
    </div>
  );
}
