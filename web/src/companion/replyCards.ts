/**
 * Interactive cards in replies. The companion writes a fenced block
 *
 *     ```card
 *     {"type": "choices", "options": ["周六", "周日"]}
 *     ```
 *
 * inside an ordinary reply; the app renders it as a card (weather, an itinerary, a form…)
 * whose buttons send the user's choice back as a message. Anything that does not parse as
 * a known card is left out rather than shown as code.
 */

export interface CardButton {
  label: string;
  /** What tapping sends; defaults to the label. */
  send?: string;
  primary?: boolean;
}

export type FieldKind = "text" | "number" | "select" | "chips" | "multi" | "slider" | "date" | "time";

export interface FormField {
  name: string;
  label: string;
  kind: FieldKind;
  options?: string[];
  min?: number;
  max?: number;
  step?: number;
  unit?: string;
  placeholder?: string;
  value?: string | number | string[];
  required?: boolean;
}

export type Card =
  | { type: "buttons"; items: CardButton[] }
  | { type: "choices"; title?: string; multiple?: boolean; options: CardButton[]; submit?: string }
  /** Pictures to pick one of (the candidate looks drawn for 换形象); tapping one sends its ``send``. */
  | { type: "pictures"; title?: string; items: { url: string; label: string; send?: string }[] }
  | {
      type: "weather";
      place: string;
      when?: string;
      temp: string;
      summary?: string;
      icon?: string;
      high?: string;
      low?: string;
      details?: { label: string; value: string }[];
      hours?: { time: string; temp: string; icon?: string }[];
    }
  | { type: "timeline"; title?: string; items: { time?: string; title: string; detail?: string }[] }
  | {
      type: "places";
      title?: string;
      items: { name: string; address?: string; note?: string; rating?: string; lat?: number; lng?: number; url?: string }[];
    }
  | {
      type: "trains";
      title?: string;
      from?: string;
      to?: string;
      date?: string;
      items: {
        code: string;
        depart: string;
        arrive: string;
        duration?: string;
        price?: string;
        seats?: { name: string; left: string }[];
        send?: string;
      }[];
    }
  | { type: "compare"; title?: string; options: string[]; rows: { label: string; values: string[] }[]; best?: number }
  | { type: "checklist"; title?: string; items: { text: string; done?: boolean }[] }
  | { type: "table"; title?: string; columns: string[]; rows: string[][] }
  | { type: "form"; title?: string; fields: FormField[]; submit?: string }
  | { type: "widget"; title: string; url: string; height?: number; summary?: string }
  /** A small app written right in the reply (```ui): HTML using the companion's UI kit. */
  | { type: "ui"; html: string }
  /** A service the task needs connected first (按需连接). */
  | { type: "connect"; service: string; reason?: string }
  /** A login or an API key the task needs; the user types it into the card, not the chat. */
  | { type: "credential"; kind: "login" | "api_key"; service: string; origin?: string; env?: string; reason?: string };

export type ReplyPart =
  | { type: "text"; text: string }
  | { type: "card"; card: Card }
  | { type: "pending"; kind?: "ui" };

// Models sometimes label the block ``json``: a json block that is a known card counts too.
const FENCE = /```(card|json|ui)[ \t]*\n([\s\S]*?)(?:\n```|$)/g;

const str = (value: unknown): string | undefined =>
  typeof value === "string" && value.trim() ? value.trim() : typeof value === "number" ? String(value) : undefined;
const list = (value: unknown): unknown[] => (Array.isArray(value) ? value : []);
const record = (value: unknown): Record<string, unknown> =>
  value && typeof value === "object" && !Array.isArray(value) ? (value as Record<string, unknown>) : {};
const num = (value: unknown): number | undefined =>
  typeof value === "number" && Number.isFinite(value) ? value : typeof value === "string" && value.trim() && Number.isFinite(Number(value)) ? Number(value) : undefined;

function button(value: unknown): CardButton | null {
  if (typeof value === "string" && value.trim()) return { label: value.trim() };
  const item = record(value);
  const label = str(item.label);
  if (!label) return null;
  return { label, send: str(item.send), primary: item.primary === true };
}

function field(value: unknown, index: number): FormField | null {
  const item = record(value);
  const label = str(item.label);
  if (!label) return null;
  const kinds: FieldKind[] = ["text", "number", "select", "chips", "multi", "slider", "date", "time"];
  const kind = kinds.includes(item.kind as FieldKind) ? (item.kind as FieldKind) : "text";
  const options = list(item.options).map(str).filter((o): o is string => Boolean(o));
  if ((kind === "select" || kind === "chips" || kind === "multi") && options.length === 0) return null;
  const preset = item.value;
  return {
    name: str(item.name) ?? `field${index}`,
    label,
    kind,
    options,
    min: num(item.min),
    max: num(item.max),
    step: num(item.step),
    unit: str(item.unit),
    placeholder: str(item.placeholder),
    value: Array.isArray(preset) ? preset.map(String) : typeof preset === "number" || typeof preset === "string" ? preset : undefined,
    required: item.required === true,
  };
}

/** A card from its JSON, or null when it is not one the app knows how to show. */
export function parseCard(raw: unknown): Card | null {
  const data = record(raw);
  switch (data.type) {
    case "buttons": {
      const items = list(data.items).map(button).filter((b): b is CardButton => b !== null);
      return items.length ? { type: "buttons", items: items.slice(0, 6) } : null;
    }
    case "choices": {
      const options = list(data.options).map(button).filter((b): b is CardButton => b !== null);
      return options.length
        ? { type: "choices", title: str(data.title), multiple: data.multiple === true, options: options.slice(0, 12), submit: str(data.submit) }
        : null;
    }
    case "pictures": {
      const items = list(data.items)
        .map((i) => ({ url: str(record(i).url) ?? "", label: str(record(i).label) ?? "", send: str(record(i).send) }))
        .filter((i) => i.label && /^(\/|https:\/\/)/.test(i.url));
      return items.length ? { type: "pictures", title: str(data.title), items: items.slice(0, 6) } : null;
    }
    case "weather": {
      const place = str(data.place);
      const temp = str(data.temp);
      if (!place || !temp) return null;
      return {
        type: "weather",
        place,
        temp,
        when: str(data.when),
        summary: str(data.summary),
        icon: str(data.icon),
        high: str(data.high),
        low: str(data.low),
        details: list(data.details)
          .map((d) => ({ label: str(record(d).label) ?? "", value: str(record(d).value) ?? "" }))
          .filter((d) => d.label && d.value)
          .slice(0, 6),
        hours: list(data.hours)
          .map((h) => ({ time: str(record(h).time) ?? "", temp: str(record(h).temp) ?? "", icon: str(record(h).icon) }))
          .filter((h) => h.time && h.temp)
          .slice(0, 8),
      };
    }
    case "timeline": {
      const items = list(data.items)
        .map((i) => ({ time: str(record(i).time), title: str(record(i).title) ?? "", detail: str(record(i).detail) }))
        .filter((i) => i.title);
      return items.length ? { type: "timeline", title: str(data.title), items } : null;
    }
    case "places": {
      const items = list(data.items)
        .map((i) => {
          const r = record(i);
          return {
            name: str(r.name) ?? "",
            address: str(r.address),
            note: str(r.note),
            rating: str(r.rating),
            lat: num(r.lat),
            lng: num(r.lng),
            url: str(r.url),
          };
        })
        .filter((i) => i.name);
      return items.length ? { type: "places", title: str(data.title), items: items.slice(0, 8) } : null;
    }
    case "trains": {
      const items = list(data.items)
        .map((i) => {
          const r = record(i);
          return {
            code: str(r.code) ?? "",
            depart: str(r.depart) ?? "",
            arrive: str(r.arrive) ?? "",
            duration: str(r.duration),
            price: str(r.price),
            send: str(r.send),
            seats: list(r.seats)
              .map((s) => ({ name: str(record(s).name) ?? "", left: str(record(s).left) ?? "" }))
              .filter((s) => s.name),
          };
        })
        .filter((i) => i.code && i.depart && i.arrive);
      return items.length
        ? { type: "trains", title: str(data.title), from: str(data.from), to: str(data.to), date: str(data.date), items: items.slice(0, 10) }
        : null;
    }
    case "compare": {
      const options = list(data.options).map(str).filter((o): o is string => Boolean(o));
      const rows = list(data.rows)
        .map((r) => ({ label: str(record(r).label) ?? "", values: list(record(r).values).map((v) => str(v) ?? "—") }))
        .filter((r) => r.label);
      if (options.length < 2 || !rows.length) return null;
      const best = num(data.best);
      return { type: "compare", title: str(data.title), options: options.slice(0, 4), rows, best: best !== undefined && best < options.length ? best : undefined };
    }
    case "checklist": {
      const items = list(data.items)
        .map((i) => (typeof i === "string" ? { text: i } : { text: str(record(i).text) ?? "", done: record(i).done === true }))
        .filter((i) => i.text);
      return items.length ? { type: "checklist", title: str(data.title), items } : null;
    }
    case "table": {
      const columns = list(data.columns).map((c) => str(c) ?? "");
      const rows = list(data.rows).map((r) => list(r).map((v) => str(v) ?? ""));
      return columns.length && rows.length ? { type: "table", title: str(data.title), columns, rows: rows.slice(0, 30) } : null;
    }
    case "form": {
      const fields = list(data.fields).map(field).filter((f): f is FormField => f !== null);
      return fields.length ? { type: "form", title: str(data.title), fields: fields.slice(0, 8), submit: str(data.submit) } : null;
    }
    case "widget": {
      const title = str(data.title);
      const url = str(data.url);
      // Only pages the server signed for the chat; never an arbitrary site.
      if (!title || !url || !url.startsWith("/api/webui/companion/widgets/")) return null;
      const height = num(data.height);
      return { type: "widget", title, url, summary: str(data.summary), height: height ? Math.min(Math.max(height, 160), 720) : undefined };
    }
    case "credential": {
      const kind = data.kind === "login" || data.kind === "api_key" ? data.kind : null;
      const service = str(data.service);
      const env = str(data.env);
      if (!kind || !service || (kind === "api_key" && !(env && /^[A-Z][A-Z0-9_]{2,63}$/.test(env)))) return null;
      return { type: "credential", kind, service, origin: str(data.origin), env, reason: str(data.reason) };
    }
    case "connect": {
      const service = str(data.service);
      return service && /^[a-z0-9-]{1,40}$/.test(service) ? { type: "connect", service, reason: str(data.reason) } : null;
    }
    default:
      return null;
  }
}

/**
 * A reply as text and cards, in order. A card block still being written (no closing fence
 * yet, while the reply streams) becomes a placeholder; a block that isn't a known card is dropped.
 */
export function splitReply(content: string, streaming = false): ReplyPart[] {
  const parts: ReplyPart[] = [];
  let last = 0;
  for (const match of content.matchAll(FENCE)) {
    const before = content.slice(last, match.index).trim();
    if (before) parts.push({ type: "text", text: before });
    last = (match.index ?? 0) + match[0].length;
    if (!match[0].endsWith("\n```")) {
      if (streaming) parts.push(match[1] === "ui" ? { type: "pending", kind: "ui" } : { type: "pending" });
      continue;
    }
    if (match[1] === "ui") {
      if (match[2].trim()) parts.push({ type: "card", card: { type: "ui", html: match[2] } });
      continue;
    }
    let card: Card | null = null;
    try {
      card = parseCard(JSON.parse(match[2]));
    } catch {
      // Malformed JSON: not a card.
    }
    if (card) parts.push({ type: "card", card });
    // Ordinary JSON stays the code block it was.
    else if (match[1] === "json") parts.push({ type: "text", text: match[0] });
  }
  const rest = content.slice(last).trim();
  if (rest) parts.push({ type: "text", text: rest });
  return parts;
}

/** The reply without its card blocks (for previews, titles and the bubble splitter). */
export function textWithoutCards(content: string): string {
  return splitReply(content)
    .flatMap((part) => (part.type === "text" ? [part.text] : []))
    .join("\n\n");
}

/** What a submitted form sends back: one line per answer. */
export function formMessage(card: Extract<Card, { type: "form" }>, values: Record<string, string | string[]>): string {
  const lines = card.fields
    .map((f) => {
      const value = values[f.name];
      const text = Array.isArray(value) ? value.join("、") : value;
      return text ? `${f.label}：${text}${f.unit && f.kind !== "multi" ? f.unit : ""}` : null;
    })
    .filter(Boolean);
  return [card.title ?? "我填好了", ...lines].join("\n");
}
