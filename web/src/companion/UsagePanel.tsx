import { useEffect, useState } from "react";

import { useClient } from "@/providers/ClientProvider";

import { fetchUsage, type UsagePayload, type UsagePeriod } from "./companion-api";

/** Usage sources in plain words; anything else counts as 其他. */
const SOURCES: { key: string; label: string }[] = [
  { key: "user", label: "聊天" },
  { key: "cron", label: "后台任务" },
  { key: "router", label: "挑模型" },
  { key: "dream", label: "整理记忆" },
];

export function formatTokens(n: number): string {
  if (n >= 100_000_000) return `${(n / 100_000_000).toFixed(1)} 亿`;
  if (n >= 10_000) return `${(n / 10_000).toFixed(1)} 万`;
  return String(n);
}

export function formatCost(cost: number | null): string | null {
  if (cost === null) return null;
  return cost < 0.01 ? "不到 1 分钱" : `约 ¥${cost.toFixed(2)}`;
}

/** How much of the input was a cache hit, as a whole percent (null when there was no input). */
export function cachedShare(period: UsagePeriod): number | null {
  if (!period.input_tokens) return null;
  return Math.round(((period.cached_tokens ?? 0) / period.input_tokens) * 100);
}

function periodTotal(period: UsagePeriod): number {
  return Object.values(period.by_source).reduce((sum, n) => sum + n, 0);
}

function SourceBar({ period }: { period: UsagePeriod }) {
  const total = periodTotal(period);
  if (total === 0) return null;
  const known = new Set(SOURCES.map((s) => s.key));
  const other = Object.entries(period.by_source)
    .filter(([key]) => !known.has(key))
    .reduce((sum, [, n]) => sum + n, 0);
  const rows = [...SOURCES.map((s) => ({ label: s.label, value: period.by_source[s.key] ?? 0 })), { label: "其他", value: other }].filter(
    (row) => row.value > 0,
  );
  return (
    <ul className="flex flex-col gap-2">
      {rows.map((row) => (
        <li key={row.label} className="flex items-center gap-3 text-[14px]">
          <span className="w-16 shrink-0">{row.label}</span>
          <span className="h-2 flex-1 overflow-hidden rounded-full" style={{ background: "var(--c-fill)" }}>
            <span
              className="block h-full rounded-full"
              style={{ width: `${Math.max(2, (row.value / total) * 100)}%`, background: "var(--c-primary)" }}
            />
          </span>
          <span className="c-muted w-16 shrink-0 text-right text-[13px]">{formatTokens(row.value)}</span>
        </li>
      ))}
    </ul>
  );
}

/** Tokens per model and per purpose, with a cost estimate from the prices entered in 模型. */
export function UsagePanel({ active }: { /** Loads when shown. */ active: boolean }) {
  const { token } = useClient();
  const [usage, setUsage] = useState<UsagePayload | null>(null);
  const [range, setRange] = useState<"today" | "week">("today");
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!active) return;
    let cancelled = false;
    setError(null);
    fetchUsage(token)
      .then((next) => {
        if (cancelled) return;
        setUsage(next);
      })
      .catch((e) => {
        if (!cancelled) setError(e instanceof Error ? e.message : String(e));
      });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [active, token]);

  const period = usage?.[range];
  const cost = period ? formatCost(period.cost) : null;
  // Models used without a price: their share isn't in the estimate, so it says so.
  const unpriced = period
    ? [...new Set([
        ...period.by_model.filter((row) => row.cost === null).map((row) => row.preset || row.model),
        ...(period.media ?? []).filter((row) => row.cost === null).map((row) => row.name),
      ])]
    : [];

  return (
    <div>
      <p className="c-muted mb-3 text-[13px] leading-snug">
        每次对话都会把说明、工具和之前的聊天一起发给模型；和上一次相同的开头会命中缓存，便宜得多。费用按「模型」里的价格估算（可以点「检测价格」从官网读取），图片和视频按张、按秒算。
      </p>
      <div
        className="mb-4 ml-auto grid w-48 grid-cols-2 gap-0.5 rounded-xl p-0.5"
        style={{ background: "var(--c-segment)" }}
        role="radiogroup"
        aria-label="时间范围"
      >
        {(["today", "week"] as const).map((key) => (
          <button
            key={key}
            type="button"
            role="radio"
            aria-checked={range === key}
            onClick={() => setRange(key)}
            className="min-h-8 rounded-[10px] text-[13px]"
            style={range === key ? { background: "var(--c-segment-on)", boxShadow: "0 1px 3px rgba(0,0,0,.15)" } : undefined}
          >
            {key === "today" ? "今天" : "近 7 天"}
          </button>
        ))}
      </div>
      {!period ? (
        !error && <p className="c-muted text-sm">正在读取…</p>
      ) : periodTotal(period) === 0 && (period.media ?? []).length === 0 ? (
        <p className="c-muted text-sm">这段时间还没有用量。</p>
      ) : (
        <div className="flex flex-col gap-4">
          <section className="c-card rounded-[22px] p-4">
            <p className="text-[28px] font-bold leading-tight">{formatTokens(periodTotal(period))} token</p>
            {cachedShare(period) !== null && (
              <p className="mt-1 text-[14px]">
                其中 {cachedShare(period)}% 是缓存命中，真正新发出去的约 {formatTokens(periodTotal(period) - (period.cached_tokens ?? 0))}
              </p>
            )}
            <p className="c-muted mt-1 text-[14px]">
              {!cost
                ? "填上模型价格后可以估算费用"
                : unpriced.length > 0
                  ? `${cost}，不含 ${unpriced.join("、")}（没填价格）`
                  : cost}
            </p>
            <div className="mt-4">
              <SourceBar period={period} />
            </div>
          </section>
          <ul className="c-card overflow-hidden rounded-[22px]">
            {period.by_model.map((row, i) => (
              <li key={`${row.provider}/${row.model}`} className={`flex items-center gap-3 px-4 py-3 ${i > 0 ? "border-t c-divider" : ""}`}>
                <div className="min-w-0 flex-1">
                  <p className="truncate text-[15px] font-medium">{row.preset ?? row.model}</p>
                  <p className="c-muted text-[12px]">
                    {row.calls} 次 · 输入 {formatTokens(row.input_tokens)}
                    {row.cached_tokens ? `（缓存 ${formatTokens(row.cached_tokens)}）` : ""} · 输出 {formatTokens(row.output_tokens)}
                  </p>
                </div>
                {row.subscription ? (
                  <span className="c-muted shrink-0 text-[14px]">会员额度</span>
                ) : (
                  formatCost(row.cost) && <span className="shrink-0 text-[14px]">{formatCost(row.cost)}</span>
                )}
              </li>
            ))}
          </ul>
          {(period.media ?? []).length > 0 && (
            // Pictures and clips: counted as they are made, priced from 模型 (检测价格 fills it in).
            <ul className="c-card overflow-hidden rounded-[22px]" aria-label="图像和视频">
              {(period.media ?? []).map((row, i) => (
                <li key={row.name} className={`flex items-center gap-3 px-4 py-3 ${i > 0 ? "border-t c-divider" : ""}`}>
                  <div className="min-w-0 flex-1">
                    <p className="truncate text-[15px] font-medium">{row.name}</p>
                    <p className="c-muted text-[12px]">
                      {row.kind === "image" ? `画了 ${row.count} 张` : `做了 ${row.count} 段视频，共 ${Math.round(row.seconds)} 秒`}
                    </p>
                  </div>
                  <span className={`shrink-0 text-[14px] ${row.cost === null || row.subscription ? "c-muted" : ""}`}>
                    {row.subscription ? "会员额度" : row.cost === null ? "没有价格" : formatCost(row.cost)}
                  </span>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
      {error && (
        <p className="c-danger mt-3 text-sm" role="alert">
          {error}
        </p>
      )}
    </div>
  );
}
