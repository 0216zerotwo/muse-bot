import { useEffect, useMemo, useState } from "react";

import { useClient } from "@/providers/ClientProvider";

import { fetchUsage, type UsageDailyBucket, type UsagePayload, type UsagePeriod } from "./companion-api";
import { Skeleton } from "./Skeleton";

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

function shortDateLabel(isoDate: string): string {
  const parts = isoDate.split("-");
  if (parts.length === 3) return `${Number(parts[1])}/${Number(parts[2])}`;
  return isoDate;
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

function CacheSavingsCard({ hitRate, savedCost }: { hitRate?: number; savedCost?: number }) {
  const pct = typeof hitRate === "number" && Number.isFinite(hitRate)
    ? Math.max(0, Math.min(100, Math.round(hitRate * 100)))
    : null;
  const saved = typeof savedCost === "number" && Number.isFinite(savedCost) ? Math.max(0, savedCost) : null;
  if (pct === null && saved === null) return null;

  const savedLabel =
    saved === null
      ? "—"
      : saved === 0
        ? "¥0.00"
        : saved < 0.01
          ? "不到 1 分钱"
          : `约 ¥${saved.toFixed(2)}`;

  return (
    <section
      data-testid="usage-cache-savings-card"
      aria-label="Prompt Cache 命中与省钱估算"
      className="c-card rounded-[22px] p-4"
    >
      <div className="grid grid-cols-2 gap-3">
        <div className="flex flex-col gap-1">
          <span className="c-muted text-[12px]">近 7 天缓存命中率</span>
          <span className="text-[22px] font-bold leading-tight tabular-nums" style={{ color: "var(--c-online)" }}>
            {pct !== null ? `${pct}%` : "—"}
          </span>
        </div>
        <div className="flex flex-col gap-1">
          <span className="c-muted text-[12px]">累计省钱估算</span>
          <span className="text-[22px] font-bold leading-tight tabular-nums" style={{ color: "var(--c-primary)" }}>
            {savedLabel}
          </span>
        </div>
      </div>
      {pct !== null && (
        <div className="mt-3 h-2 w-full overflow-hidden rounded-full" style={{ background: "var(--c-fill)" }}>
          <div
            className="h-full rounded-full transition-all"
            style={{ width: `${Math.max(pct > 0 ? 3 : 0, pct)}%`, background: "var(--c-online)" }}
          />
        </div>
      )}
    </section>
  );
}

const CHART_WIDTH = 420;
const CHART_HEIGHT = 152;
const PLOT_TOP = 12;
const PLOT_BOTTOM = 124;
const PLOT_HEIGHT = PLOT_BOTTOM - PLOT_TOP;

/** Zero-dependency responsive SVG 14-day stacked token trend chart (fresh input / cached / output). */
export function DailyUsageChart({ daily }: { daily: UsageDailyBucket[] }) {
  const buckets = useMemo(() => daily.slice(-14), [daily]);
  const defaultIndex = useMemo(() => {
    for (let i = buckets.length - 1; i >= 0; i -= 1) {
      const b = buckets[i];
      if (b.input_tokens + b.output_tokens + b.requests > 0) return i;
    }
    return Math.max(0, buckets.length - 1);
  }, [buckets]);
  const [selectedIndex, setSelectedIndex] = useState<number>(defaultIndex);

  useEffect(() => {
    setSelectedIndex(defaultIndex);
  }, [defaultIndex]);

  if (buckets.length === 0) return null;

  const series = buckets.map((b) => {
    const cached = Math.max(0, b.cached_tokens);
    const fresh = Math.max(0, b.input_tokens - cached);
    const output = Math.max(0, b.output_tokens);
    const total = fresh + cached + output;
    return { bucket: b, fresh, cached, output, total };
  });
  const maxTotal = Math.max(1, ...series.map((s) => s.total));
  const safeIndex = Math.min(selectedIndex, series.length - 1);
  const active = series[safeIndex];
  const slotWidth = CHART_WIDTH / series.length;
  const barWidth = Math.max(8, Math.min(20, slotWidth * 0.62));
  // Backend models.py already adds media_cost into bucket.cost; avoid double-counting or showing "不到 1 分钱" on idle 0-cost days.
  const rawDailyCost = active.bucket.cost !== null ? active.bucket.cost : active.bucket.media_cost > 0 ? active.bucket.media_cost : null;
  const dailyCostLabel =
    rawDailyCost === null
      ? null
      : rawDailyCost > 0
        ? formatCost(rawDailyCost)
        : active.total > 0 || active.bucket.requests > 0
          ? "¥0.00"
          : null;

  return (
    <section
      data-testid="usage-daily-chart"
      aria-label="近 14 天每日用量趋势"
      className="c-card rounded-[22px] p-4"
    >
      <div className="mb-2.5 flex flex-wrap items-center justify-between gap-2">
        <h3 className="text-[14px] font-semibold">近 {buckets.length} 天 Token 趋势</h3>
        <div className="c-muted flex items-center gap-3 text-[11.5px]">
          <span className="inline-flex items-center gap-1">
            <span className="h-2.5 w-2.5 rounded-sm" style={{ background: "var(--c-primary)" }} aria-hidden="true" />
            新输入
          </span>
          <span className="inline-flex items-center gap-1">
            <span className="h-2.5 w-2.5 rounded-sm" style={{ background: "var(--c-online)" }} aria-hidden="true" />
            缓存命中
          </span>
          <span className="inline-flex items-center gap-1">
            <span className="h-2.5 w-2.5 rounded-sm" style={{ background: "#6366f1" }} aria-hidden="true" />
            输出
          </span>
        </div>
      </div>

      <svg
        viewBox={`0 0 ${CHART_WIDTH} ${CHART_HEIGHT}`}
        role="img"
        aria-label={`近 ${buckets.length} 天每日输入、缓存与输出 Token 堆叠柱状图`}
        className="h-auto w-full overflow-visible select-none"
      >
        {[0, 0.5, 1].map((ratio) => {
          const y = PLOT_TOP + PLOT_HEIGHT * (1 - ratio);
          return (
            <line
              key={ratio}
              x1={0}
              x2={CHART_WIDTH}
              y1={y}
              y2={y}
              stroke="var(--c-border)"
              strokeWidth="1"
              strokeDasharray={ratio === 0 ? undefined : "3 3"}
            />
          );
        })}

        {series.map((item, idx) => {
          const cx = idx * slotWidth + slotWidth / 2;
          const x = cx - barWidth / 2;
          const isSelected = idx === safeIndex;
          const totalH = item.total > 0 ? Math.max(3, (item.total / maxTotal) * PLOT_HEIGHT) : 0;
          const freshH = item.total > 0 ? (item.fresh / item.total) * totalH : 0;
          const cachedH = item.total > 0 ? (item.cached / item.total) * totalH : 0;
          const outputH = Math.max(0, totalH - freshH - cachedH);

          const freshY = PLOT_BOTTOM - freshH;
          const cachedY = freshY - cachedH;
          const outputY = cachedY - outputH;

          const showLabel =
            series.length <= 7 || idx === 0 || idx === series.length - 1 || idx % 3 === 0 || isSelected;

          return (
            <g
              key={item.bucket.date}
              data-testid={`usage-day-bar-${item.bucket.date}`}
              role="button"
              tabIndex={0}
              aria-pressed={isSelected}
              aria-label={`${item.bucket.date}：总计 ${formatTokens(item.total)} token，${item.bucket.requests} 次请求`}
              onMouseEnter={() => setSelectedIndex(idx)}
              onFocus={() => setSelectedIndex(idx)}
              onClick={() => setSelectedIndex(idx)}
              onKeyDown={(event) => {
                if (event.key === "Enter" || event.key === " ") {
                  event.preventDefault();
                  setSelectedIndex(idx);
                }
              }}
              className="cursor-pointer outline-none"
            >
              {isSelected && (
                <rect
                  x={idx * slotWidth + 1}
                  y={PLOT_TOP - 4}
                  width={Math.max(4, slotWidth - 2)}
                  height={PLOT_HEIGHT + 8}
                  rx={6}
                  fill="var(--c-fill)"
                  fillOpacity={0.75}
                />
              )}
              <rect
                x={idx * slotWidth}
                y={PLOT_TOP - 4}
                width={slotWidth}
                height={PLOT_HEIGHT + 26}
                fill="transparent"
              />
              {item.total === 0 ? (
                <rect
                  x={x}
                  y={PLOT_BOTTOM - 2}
                  width={barWidth}
                  height={2}
                  rx={1}
                  fill="var(--c-border)"
                />
              ) : (
                <>
                  {freshH > 0 && (
                    <rect
                      x={x}
                      y={freshY}
                      width={barWidth}
                      height={freshH}
                      rx={cachedH === 0 && outputH === 0 ? 3 : 1}
                      fill="var(--c-primary)"
                      fillOpacity={isSelected ? 1 : 0.82}
                    />
                  )}
                  {cachedH > 0 && (
                    <rect
                      x={x}
                      y={cachedY}
                      width={barWidth}
                      height={cachedH}
                      rx={outputH === 0 ? 3 : 1}
                      fill="var(--c-online)"
                      fillOpacity={isSelected ? 1 : 0.85}
                    />
                  )}
                  {outputH > 0 && (
                    <rect
                      x={x}
                      y={outputY}
                      width={barWidth}
                      height={outputH}
                      rx={3}
                      fill="#6366f1"
                      fillOpacity={isSelected ? 1 : 0.85}
                    />
                  )}
                </>
              )}
              {showLabel && (
                <text
                  x={cx}
                  y={CHART_HEIGHT - 6}
                  textAnchor="middle"
                  fontSize="10"
                  fill={isSelected ? "var(--c-text)" : "var(--c-text-muted)"}
                  fontWeight={isSelected ? 600 : 400}
                >
                  {shortDateLabel(item.bucket.date)}
                </text>
              )}
            </g>
          );
        })}
      </svg>

      <div
        data-testid="usage-day-detail"
        className="mt-2 flex flex-wrap items-center justify-between gap-x-3 gap-y-1 rounded-xl px-3 py-2 text-[12px]"
        style={{ background: "var(--c-fill)" }}
      >
        <span className="font-semibold tabular-nums">
          {active.bucket.date} · {active.bucket.requests} 次请求
        </span>
        <span className="c-muted tabular-nums">
          新输入 {formatTokens(active.fresh)} · 缓存 {formatTokens(active.cached)} · 输出 {formatTokens(active.output)}
          {dailyCostLabel ? ` · ${dailyCostLabel}` : ""}
        </span>
      </div>
    </section>
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
  const dailyBuckets = usage?.daily ?? [];
  const hasAnalyticsExtras =
    dailyBuckets.length > 0 ||
    usage?.cache_hit_rate !== undefined ||
    usage?.cache_saved_cost !== undefined;
  const periodEmpty = !period || (periodTotal(period) === 0 && (period.media ?? []).length === 0);

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
            className="min-h-9 rounded-[10px] text-[13px] transition-colors"
            style={range === key ? { background: "var(--c-segment-on)", fontWeight: 600, boxShadow: "0 1px 3px rgba(0,0,0,.15)" } : undefined}
          >
            {key === "today" ? "今天" : "近 7 天"}
          </button>
        ))}
      </div>
      {!period ? (
        !error && (
          <div role="status" aria-busy="true" aria-label="正在读取用量" className="flex flex-col gap-4">
            <div className="c-card rounded-[22px] p-4">
              <Skeleton className="h-7 w-36" />
              <Skeleton className="mt-2.5 h-3.5 w-48" />
              <Skeleton className="mt-4 h-2.5 w-full rounded-full" />
            </div>
            <div className="c-card divide-y overflow-hidden rounded-[22px] c-divider">
              {[0, 1].map((index) => (
                <div key={index} className="flex items-center justify-between gap-3 px-4 py-3.5">
                  <div className="min-w-0 flex-1 space-y-1.5">
                    <Skeleton className={index === 0 ? "h-3.5 w-32" : "h-3.5 w-24"} />
                    <Skeleton className="h-2.5 w-44" />
                  </div>
                  <Skeleton className="h-3.5 w-14 shrink-0" />
                </div>
              ))}
            </div>
          </div>
        )
      ) : periodEmpty && !hasAnalyticsExtras ? (
        <div className="c-card rounded-[22px] px-5 py-7 text-center">
          <p className="text-[15px] font-medium">这段时间还没有用量。</p>
          <p className="c-muted mt-1 text-[13px]">开始聊天、画图或做视频后，用量和缓存命中率会统计在这里。</p>
        </div>
      ) : (
        <div className="flex flex-col gap-4">
          {periodEmpty ? (
            <div className="c-card rounded-[22px] px-5 py-5 text-center">
              <p className="text-[15px] font-medium">这段时间还没有用量。</p>
              <p className="c-muted mt-1 text-[13px]">下方可查看近 14 天用量趋势与缓存节省情况。</p>
            </div>
          ) : (
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
          )}
          <CacheSavingsCard hitRate={usage?.cache_hit_rate} savedCost={usage?.cache_saved_cost} />
          {dailyBuckets.length > 0 && <DailyUsageChart daily={dailyBuckets} />}
          {period.by_model.length > 0 && (
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
          )}
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
