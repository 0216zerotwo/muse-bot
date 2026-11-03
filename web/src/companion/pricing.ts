/**
 * A model's price in a line, whatever its style (see the server's model_check): per million
 * tokens, per second of video, per clip, per picture — always in 元. ``main`` is the headline (the
 * first, most common tier; "起" when other specs cost less), ``rows`` the specs underneath when there
 * is more than one, or a cache-hit price worth knowing.
 */
export type PriceUnit = "mtok" | "second" | "video" | "image";

export interface PriceRow {
  spec: string;
  /** mtok: 元 per million tokens in, out and on a cache hit. */
  input?: number;
  output?: number;
  cached?: number | null;
  /** second / video / image: 元 each. */
  price?: number;
}

export interface Price {
  unit: PriceUnit;
  rows: PriceRow[];
}

const EACH: Record<Exclude<PriceUnit, "mtok">, string> = { second: "秒", video: "条", image: "张" };

/** 2.1, 0.26, 0.025, 14 */
export function yuan(n: number): string {
  if (n >= 10) return String(Math.round(n * 10) / 10);
  if (n >= 1) return String(Math.round(n * 100) / 100);
  return String(Number(n.toPrecision(2)));
}

export function priceSummary(price: Price | null | undefined): { main: string; rows: string[] } | null {
  if (!price || price.rows.length === 0) return null;
  if (price.unit === "mtok") {
    const first = price.rows[0];
    const main = `¥${yuan(first.input ?? 0)} / ¥${yuan(first.output ?? 0)}`;
    const rows = price.rows.length > 1
      ? price.rows.map((r) => `${r.spec || "其他"} ¥${yuan(r.input ?? 0)} / ¥${yuan(r.output ?? 0)}${r.cached != null ? `，缓存 ¥${yuan(r.cached)}` : ""}`)
      : first.cached != null ? [`缓存命中 ¥${yuan(first.cached)}`] : [];
    return { main, rows };
  }
  const each = EACH[price.unit];
  const prices = price.rows.map((r) => r.price ?? 0);
  const lowest = Math.min(...prices);
  const main = price.rows.length > 1 && prices.some((p) => p !== lowest) ? `¥${yuan(lowest)}/${each}起` : `¥${yuan(prices[0])}/${each}`;
  const rows = price.rows.length > 1 ? price.rows.map((r) => `${r.spec || "标准"} ¥${yuan(r.price ?? 0)}/${each}`) : [];
  return { main, rows };
}

/** All of it in one line, for a model already added (where there is room): each spec's price. */
export function priceLine(price: Price | null | undefined): string | null {
  const summary = priceSummary(price);
  if (!summary) return null;
  if (price!.unit === "mtok") return `${summary.main} 每百万 token`;
  return summary.rows.length ? summary.rows.join(" · ") : summary.main;
}

/** Under 选择模型: what the prices are per. */
export function priceUnitHint(prices: Array<Price | null | undefined>): string | null {
  const units = new Set(prices.filter(Boolean).map((p) => p!.unit));
  if (units.size === 0) return null;
  if (units.size === 1 && units.has("mtok")) return "输入 / 输出 每百万 token";
  return "价格按官网，单位见每一行";
}
