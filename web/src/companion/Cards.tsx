import { useCallback, useEffect, useRef, useState, type FormEvent, type ReactNode } from "react";
import { createPortal } from "react-dom";
import {
  Check,
  Cloud,
  CloudFog,
  CloudLightning,
  CloudRain,
  CloudSnow,
  CloudSun,
  Map as MapIcon,
  Maximize2,
  Moon,
  Sun,
  Wind,
  X,
  type LucideIcon,
} from "lucide-react";

import { ConnectCard } from "./ConnectCard";
import { CredentialCard } from "./CredentialCard";
import { formMessage, type Card, type CardButton, type FormField } from "./replyCards";

/** Taps send a message while the card is the latest thing it said; older cards only show. */
type Send = ((text: string) => void) | undefined;

/** A card's own answer (what was picked, ticked or submitted), kept across reloads. */
function useCardState<T>(key: string, initial: T): [T, (value: T) => void] {
  const storageKey = `companion.card.${key}`;
  const [value, setValue] = useState<T>(() => {
    try {
      const saved = localStorage.getItem(storageKey);
      return saved ? (JSON.parse(saved) as T) : initial;
    } catch {
      return initial;
    }
  });
  const save = useCallback(
    (next: T) => {
      setValue(next);
      try {
        localStorage.setItem(storageKey, JSON.stringify(next));
      } catch {
        // Private mode: the choice just isn't remembered after a reload.
      }
    },
    [storageKey],
  );
  return [value, save];
}

function Frame({ title, children, wide = false }: { title?: string; children: ReactNode; wide?: boolean }) {
  return (
    <section className={`c-card c-rise w-full rounded-[20px] p-3.5 ${wide ? "max-w-[92%]" : "max-w-[85%]"}`}>
      {title && <h3 className="c-muted mb-2.5 text-[13px] font-medium">{title}</h3>}
      {children}
    </section>
  );
}

function Chip({
  label,
  selected,
  primary,
  disabled,
  onClick,
}: {
  label: string;
  selected?: boolean;
  primary?: boolean;
  disabled?: boolean;
  onClick?: () => void;
}) {
  const filled = selected || primary;
  return (
    <button
      type="button"
      disabled={disabled}
      onClick={onClick}
      aria-pressed={selected}
      className={`c-pressable inline-flex min-h-9 items-center gap-1 rounded-full px-3.5 text-[14px] disabled:opacity-60 ${
        filled ? "c-primary font-medium" : ""
      }`}
      style={filled ? undefined : { background: "var(--c-fill)" }}
    >
      {selected && <Check className="h-3.5 w-3.5" aria-hidden="true" />}
      {label}
    </button>
  );
}

/* ---------- buttons and choices ---------- */

function PicturesCard({ card, send, stateKey }: { card: Extract<Card, { type: "pictures" }>; send: Send; stateKey: string }) {
  const [picked, setPicked] = useCardState<string | null>(stateKey, null);
  return (
    <Frame title={card.title}>
      <div className="grid grid-cols-2 gap-2">
        {card.items.map((item) => {
          const chosen = picked === item.label;
          return (
            <button
              key={item.label}
              type="button"
              aria-label={item.label}
              aria-pressed={chosen}
              disabled={!send || (picked !== null && !chosen)}
              onClick={() => {
                if (!send || picked) return;
                setPicked(item.label);
                send(item.send ?? item.label);
              }}
              className={`relative aspect-square overflow-hidden rounded-2xl border bg-white/70 transition disabled:opacity-40 ${
                chosen ? "border-[var(--c-accent)] ring-2 ring-[var(--c-accent)]" : "border-[var(--c-border)]"
              }`}
            >
              <img src={item.url} alt="" className="h-full w-full object-contain p-1" loading="lazy" />
              <span className="absolute left-1.5 top-1.5 rounded-full bg-black/45 px-2 text-[12px] font-semibold text-white">
                {item.label}
              </span>
              {chosen && <Check className="absolute right-1.5 top-1.5 h-5 w-5 rounded-full bg-[var(--c-accent)] p-0.5 text-white" aria-hidden="true" />}
            </button>
          );
        })}
      </div>
    </Frame>
  );
}

function ButtonsCard({ card, send, stateKey }: { card: Extract<Card, { type: "buttons" }>; send: Send; stateKey: string }) {
  const [picked, setPicked] = useCardState<string | null>(stateKey, null);
  const tap = (item: CardButton) => {
    if (!send || picked) return;
    setPicked(item.label);
    send(item.send ?? item.label);
  };
  return (
    <div className="c-rise flex max-w-[92%] flex-wrap gap-2">
      {card.items.map((item) => (
        <Chip
          key={item.label}
          label={item.label}
          primary={item.primary && !picked}
          selected={picked === item.label}
          disabled={!send || (picked !== null && picked !== item.label)}
          onClick={() => tap(item)}
        />
      ))}
    </div>
  );
}

function ChoicesCard({ card, send, stateKey }: { card: Extract<Card, { type: "choices" }>; send: Send; stateKey: string }) {
  const [sent, setSent] = useCardState<string[] | null>(stateKey, null);
  const [chosen, setChosen] = useState<string[]>([]);
  const done = sent !== null;
  const shown = sent ?? chosen;
  const pick = (option: CardButton) => {
    if (!send || done) return;
    if (!card.multiple) {
      setSent([option.label]);
      send(option.send ?? option.label);
      return;
    }
    setChosen((current) => (current.includes(option.label) ? current.filter((l) => l !== option.label) : [...current, option.label]));
  };
  return (
    <Frame title={card.title}>
      <div className="flex flex-wrap gap-2">
        {card.options.map((option) => (
          <Chip
            key={option.label}
            label={option.primary ? `${option.label} · 推荐` : option.label}
            selected={shown.includes(option.label)}
            disabled={!send || (done && !shown.includes(option.label))}
            onClick={() => pick(option)}
          />
        ))}
      </div>
      {card.multiple && !done && send && (
        <button
          type="button"
          disabled={chosen.length === 0}
          onClick={() => {
            setSent(chosen);
            // Each option's own text, comma-separated: the form both the model and its
            // multiple-choice questions read back.
            send(card.options.filter((o) => chosen.includes(o.label)).map((o) => o.send ?? o.label).join(", "));
          }}
          className="c-primary mt-3 min-h-10 w-full rounded-full text-[14px] font-semibold disabled:opacity-50"
        >
          {card.submit ?? "就这些"}
        </button>
      )}
    </Frame>
  );
}

/* ---------- 出行 ---------- */

const WEATHER_ICONS: [RegExp, LucideIcon][] = [
  [/thunder|storm|雷/i, CloudLightning],
  [/snow|雪/i, CloudSnow],
  [/rain|shower|drizzle|雨/i, CloudRain],
  [/fog|mist|haze|雾|霾/i, CloudFog],
  [/wind|风/i, Wind],
  [/night|moon|夜|晚/i, Moon],
  [/partly|cloud.*sun|sun.*cloud|多云|间/i, CloudSun],
  [/cloud|overcast|阴|云/i, Cloud],
  [/sun|clear|晴/i, Sun],
];

function weatherIcon(hint: string | undefined): LucideIcon {
  return WEATHER_ICONS.find(([pattern]) => pattern.test(hint ?? ""))?.[1] ?? CloudSun;
}

function WeatherCard({ card }: { card: Extract<Card, { type: "weather" }> }) {
  const Icon = weatherIcon(card.icon ?? card.summary);
  return (
    <Frame>
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <p className="c-muted truncate text-[13px]">
            {card.place}
            {card.when ? ` · ${card.when}` : ""}
          </p>
          <p className="mt-0.5 text-[34px] font-semibold leading-none tracking-tight">{card.temp}</p>
          {card.summary && <p className="mt-1.5 text-[14px]">{card.summary}</p>}
        </div>
        <Icon className="c-accent mt-1 h-9 w-9 shrink-0" strokeWidth={1.6} aria-hidden="true" />
      </div>
      {(card.high || card.low) && (
        <p className="c-muted mt-1 text-[13px]">
          {card.high && `最高 ${card.high}`}
          {card.high && card.low && " · "}
          {card.low && `最低 ${card.low}`}
        </p>
      )}
      {card.hours && card.hours.length > 0 && (
        <div className="mt-3 flex gap-3 overflow-x-auto border-t pt-3 c-divider">
          {card.hours.map((hour) => {
            const HourIcon = weatherIcon(hour.icon);
            return (
              <div key={hour.time} className="flex min-w-11 flex-col items-center gap-1 text-[12px]">
                <span className="c-muted">{hour.time}</span>
                <HourIcon className="h-4 w-4" aria-hidden="true" />
                <span className="font-medium">{hour.temp}</span>
              </div>
            );
          })}
        </div>
      )}
      {card.details && card.details.length > 0 && (
        <dl className="mt-3 grid grid-cols-3 gap-2 border-t pt-3 c-divider">
          {card.details.map((detail) => (
            <div key={detail.label}>
              <dt className="c-muted text-[12px]">{detail.label}</dt>
              <dd className="text-[14px] font-medium">{detail.value}</dd>
            </div>
          ))}
        </dl>
      )}
    </Frame>
  );
}

function TimelineCard({ card }: { card: Extract<Card, { type: "timeline" }> }) {
  return (
    <Frame title={card.title}>
      <ol className="flex flex-col">
        {card.items.map((item, index) => (
          <li key={index} className="relative flex gap-3 pb-3 last:pb-0">
            <span className="c-accent w-12 shrink-0 pt-px text-right text-[13px] font-semibold tabular-nums">{item.time}</span>
            <span className="relative flex w-3 shrink-0 justify-center" aria-hidden="true">
              <span className="mt-1.5 h-2 w-2 rounded-full" style={{ background: "var(--c-accent)" }} />
              {index < card.items.length - 1 && (
                <span className="absolute top-4 bottom-[-0.5rem] w-px" style={{ background: "var(--c-border)" }} />
              )}
            </span>
            <span className="min-w-0 flex-1">
              <span className="block text-[15px] leading-snug">{item.title}</span>
              {item.detail && <span className="c-muted block text-[13px] leading-snug">{item.detail}</span>}
            </span>
          </li>
        ))}
      </ol>
    </Frame>
  );
}

function mapLink(place: Extract<Card, { type: "places" }>["items"][number]): string {
  if (place.url && /^https?:\/\//.test(place.url)) return place.url;
  if (place.lat !== undefined && place.lng !== undefined) {
    return `https://uri.amap.com/marker?position=${place.lng},${place.lat}&name=${encodeURIComponent(place.name)}`;
  }
  return `https://uri.amap.com/search?keyword=${encodeURIComponent([place.name, place.address].filter(Boolean).join(" "))}`;
}

function PlacesCard({ card }: { card: Extract<Card, { type: "places" }> }) {
  return (
    <Frame title={card.title}>
      <ul className="flex flex-col divide-y c-divider">
        {card.items.map((place) => (
          <li key={place.name}>
            <a
              href={mapLink(place)}
              target="_blank"
              rel="noreferrer"
              className="c-pressable -mx-1.5 flex min-h-12 items-center gap-3 rounded-xl px-1.5 py-2"
            >
              <span className="grid h-9 w-9 shrink-0 place-items-center rounded-xl" style={{ background: "var(--c-accent-soft)" }}>
                <MapIcon className="c-accent h-4 w-4" aria-hidden="true" />
              </span>
              <span className="min-w-0 flex-1">
                <span className="flex items-baseline gap-2">
                  <span className="truncate text-[15px] font-medium">{place.name}</span>
                  {place.rating && <span className="c-accent shrink-0 text-[12px]">★ {place.rating}</span>}
                </span>
                {(place.address || place.note) && (
                  <span className="c-muted block truncate text-[13px]">{[place.note, place.address].filter(Boolean).join(" · ")}</span>
                )}
              </span>
            </a>
          </li>
        ))}
      </ul>
    </Frame>
  );
}

function TrainsCard({ card, send, stateKey }: { card: Extract<Card, { type: "trains" }>; send: Send; stateKey: string }) {
  const [picked, setPicked] = useCardState<string | null>(stateKey, null);
  const title = card.title ?? [card.from && card.to ? `${card.from} → ${card.to}` : null, card.date].filter(Boolean).join(" · ");
  return (
    <Frame title={title || undefined} wide>
      <ul className="flex flex-col gap-2">
        {card.items.map((train) => {
          const chosen = picked === train.code;
          return (
            <li key={train.code}>
              <button
                type="button"
                disabled={!send || (picked !== null && !chosen)}
                onClick={() => {
                  if (!send || picked) return;
                  setPicked(train.code);
                  send(train.send ?? `就选 ${train.code}`);
                }}
                className="c-pressable w-full rounded-2xl px-3 py-2.5 text-left disabled:opacity-60"
                style={{ background: chosen ? "var(--c-accent-soft)" : "var(--c-fill)" }}
              >
                <span className="flex items-center gap-3">
                  <span className="text-[17px] font-semibold tabular-nums">{train.depart}</span>
                  <span className="c-muted flex flex-1 flex-col items-center text-[11px] leading-tight">
                    <span>{train.code}</span>
                    <span className="my-0.5 h-px w-full" style={{ background: "var(--c-border)" }} aria-hidden="true" />
                    <span>{train.duration}</span>
                  </span>
                  <span className="text-[17px] font-semibold tabular-nums">{train.arrive}</span>
                </span>
                {(train.seats?.length || train.price) && (
                  <span className="c-muted mt-1.5 flex flex-wrap gap-x-3 text-[12px]">
                    {train.price && <span className="c-accent font-medium">{train.price}</span>}
                    {train.seats?.map((seat) => (
                      <span key={seat.name}>
                        {seat.name} {seat.left}
                      </span>
                    ))}
                  </span>
                )}
              </button>
            </li>
          );
        })}
      </ul>
    </Frame>
  );
}

/* ---------- 对比和清单 ---------- */

function CompareCard({ card }: { card: Extract<Card, { type: "compare" }> }) {
  return (
    <Frame title={card.title} wide>
      <div className="-mx-1 overflow-x-auto px-1">
        <table className="w-full border-collapse text-[14px]">
          <thead>
            <tr>
              <th className="w-[28%]" />
              {card.options.map((option, index) => (
                <th
                  key={option}
                  scope="col"
                  className={`rounded-t-xl px-2 pb-2 pt-1 text-left align-bottom text-[14px] font-semibold ${index === card.best ? "c-accent" : ""}`}
                  style={index === card.best ? { background: "var(--c-accent-soft)" } : undefined}
                >
                  {index === card.best && <span className="block text-[11px] font-medium">推荐</span>}
                  {option}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {card.rows.map((row) => (
              <tr key={row.label} className="border-t c-divider">
                <th scope="row" className="c-muted py-2 pr-2 text-left align-top text-[13px] font-normal">
                  {row.label}
                </th>
                {card.options.map((option, index) => (
                  <td
                    key={option}
                    className="px-2 py-2 align-top"
                    style={index === card.best ? { background: "var(--c-accent-soft)" } : undefined}
                  >
                    {row.values[index] ?? "—"}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Frame>
  );
}

function ChecklistCard({ card, stateKey }: { card: Extract<Card, { type: "checklist" }>; stateKey: string }) {
  const [done, setDone] = useCardState<boolean[]>(stateKey, card.items.map((item) => item.done === true));
  const count = done.filter(Boolean).length;
  return (
    <Frame title={card.title ? `${card.title} · ${count}/${card.items.length}` : `${count}/${card.items.length}`}>
      <ul className="flex flex-col">
        {card.items.map((item, index) => (
          <li key={index}>
            <button
              type="button"
              role="checkbox"
              aria-checked={done[index] === true}
              onClick={() => setDone(card.items.map((_, i) => (i === index ? !done[i] : done[i] === true)))}
              className="c-pressable -mx-1.5 flex min-h-11 w-[calc(100%+0.75rem)] items-center gap-3 rounded-xl px-1.5 text-left"
            >
              <span
                className={`grid h-5 w-5 shrink-0 place-items-center rounded-full ${done[index] ? "c-primary" : ""}`}
                style={done[index] ? undefined : { boxShadow: "inset 0 0 0 1.5px var(--c-border)" }}
                aria-hidden="true"
              >
                {done[index] && <Check className="h-3 w-3" strokeWidth={3} />}
              </span>
              <span className={`text-[15px] ${done[index] ? "c-muted line-through" : ""}`}>{item.text}</span>
            </button>
          </li>
        ))}
      </ul>
    </Frame>
  );
}

function TableCard({ card }: { card: Extract<Card, { type: "table" }> }) {
  // A first column of short labels (职责, 做法…) stays on one line; on a phone the table would
  // otherwise squeeze it to one character per line.
  const labels = card.rows.every((row) => (row[0] ?? "").length <= 8);
  return (
    <Frame title={card.title} wide>
      <div className="-mx-1 overflow-x-auto px-1">
        <table className="w-full border-collapse text-[14px]">
          <thead>
            <tr>
              {card.columns.map((column) => (
                <th key={column} scope="col" className="c-muted whitespace-nowrap px-2 pb-2 text-left text-[12px] font-medium first:pl-0">
                  {column}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {card.rows.map((row, rowIndex) => (
              <tr key={rowIndex} className="border-t c-divider">
                {card.columns.map((_, index) => (
                  <td key={index} className={`px-2 py-2 align-top first:pl-0 ${labels && index === 0 ? "whitespace-nowrap" : ""}`}>
                    {row[index] ?? ""}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Frame>
  );
}

/* ---------- 表单 ---------- */

const INPUT =
  "c-card min-h-11 w-full rounded-xl px-3 text-[15px] outline-none focus-visible:outline-2 focus-visible:outline-[color:var(--c-primary)]";

function initialValue(field: FormField): string | string[] {
  if (Array.isArray(field.value)) return field.value;
  if (field.value !== undefined) return String(field.value);
  if (field.kind === "multi") return [];
  if (field.kind === "slider") return String(field.min ?? 0);
  return "";
}

function FieldInput({
  field,
  value,
  disabled,
  onChange,
}: {
  field: FormField;
  value: string | string[];
  disabled: boolean;
  onChange: (value: string | string[]) => void;
}) {
  const text = Array.isArray(value) ? "" : value;
  switch (field.kind) {
    case "chips":
    case "multi": {
      const chosen = Array.isArray(value) ? value : value ? [value] : [];
      return (
        <div className="flex flex-wrap gap-2">
          {field.options?.map((option) => (
            <Chip
              key={option}
              label={option}
              selected={chosen.includes(option)}
              disabled={disabled}
              onClick={() =>
                onChange(
                  field.kind === "chips"
                    ? option
                    : chosen.includes(option)
                      ? chosen.filter((o) => o !== option)
                      : [...chosen, option],
                )
              }
            />
          ))}
        </div>
      );
    }
    case "select":
      return (
        <select value={text} disabled={disabled} onChange={(e) => onChange(e.target.value)} className={INPUT}>
          <option value="">请选择</option>
          {field.options?.map((option) => (
            <option key={option} value={option}>
              {option}
            </option>
          ))}
        </select>
      );
    case "slider":
      return (
        <div className="flex items-center gap-3">
          <input
            type="range"
            min={field.min ?? 0}
            max={field.max ?? 100}
            step={field.step ?? 1}
            value={text}
            disabled={disabled}
            onChange={(e) => onChange(e.target.value)}
            className="flex-1"
            style={{ accentColor: "var(--c-primary)" }}
            aria-label={field.label}
          />
          <span className="min-w-14 text-right text-[14px] font-medium tabular-nums">
            {text}
            {field.unit}
          </span>
        </div>
      );
    default:
      return (
        <input
          type={field.kind === "number" ? "number" : field.kind === "date" ? "date" : field.kind === "time" ? "time" : "text"}
          value={text}
          placeholder={field.placeholder}
          min={field.min}
          max={field.max}
          disabled={disabled}
          onChange={(e) => onChange(e.target.value)}
          className={INPUT}
        />
      );
  }
}

function FormCard({ card, send, stateKey }: { card: Extract<Card, { type: "form" }>; send: Send; stateKey: string }) {
  const [submitted, setSubmitted] = useCardState<Record<string, string | string[]> | null>(stateKey, null);
  const [values, setValues] = useState<Record<string, string | string[]>>(() =>
    Object.fromEntries(card.fields.map((f) => [f.name, initialValue(f)])),
  );
  const [error, setError] = useState<string | null>(null);
  const shown = submitted ?? values;
  const locked = submitted !== null || !send;

  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (locked || !send) return;
    const missing = card.fields.find((f) => f.required && (Array.isArray(values[f.name]) ? !values[f.name].length : !values[f.name]));
    if (missing) {
      setError(`还没填「${missing.label}」`);
      return;
    }
    setSubmitted(values);
    send(formMessage(card, values));
  };

  return (
    <Frame title={card.title}>
      <form onSubmit={submit} className="flex flex-col gap-3.5">
        {card.fields.map((field) => {
          const caption = (
            <>
              {field.label}
              {field.required && <span className="c-accent"> *</span>}
            </>
          );
          const input = (
            <FieldInput
              field={field}
              value={shown[field.name] ?? initialValue(field)}
              disabled={locked}
              onChange={(value) => {
                setError(null);
                setValues((current) => ({ ...current, [field.name]: value }));
              }}
            />
          );
          // A group of chips is a fieldset; a <label> would name only its first button.
          return field.kind === "chips" || field.kind === "multi" ? (
            <fieldset key={field.name} className="flex flex-col gap-1.5">
              <legend className="mb-1.5 text-[13px] font-medium">{caption}</legend>
              {input}
            </fieldset>
          ) : (
            <label key={field.name} className="flex flex-col gap-1.5">
              <span className="text-[13px] font-medium">{caption}</span>
              {input}
            </label>
          );
        })}
        {error && (
          <p className="c-danger text-[13px]" role="alert">
            {error}
          </p>
        )}
        {submitted ? (
          <p className="c-muted flex items-center gap-1.5 text-[13px]">
            <Check className="h-4 w-4" aria-hidden="true" />
            已提交
          </p>
        ) : (
          send && (
            <button type="submit" className="c-primary min-h-11 rounded-full text-[15px] font-semibold">
              {card.submit ?? "提交"}
            </button>
          )
        )}
      </form>
    </Frame>
  );
}

/* ---------- 生成的界面 ---------- */

const THEME_TOKENS = [
  "--c-bg", "--c-surface", "--c-text", "--c-text-muted", "--c-border", "--c-primary", "--c-primary-text",
  "--c-accent", "--c-accent-soft", "--c-fill",
];

function themeTokens(from: Element | null): Record<string, string> {
  const styles = getComputedStyle(from?.closest(".companion") ?? document.querySelector(".companion") ?? document.body);
  return Object.fromEntries(THEME_TOKENS.map((name) => [name, styles.getPropertyValue(name).trim()]));
}

/** The UI kit's files, fetched once by the app and written into each generated page. */
export interface UiKit {
  css: string;
  js: string;
  chart: string;
  leaflet: string;
  leafletCss: string;
}

const kitFiles = new Map<string, Promise<string>>();

/** One kit file, fetched once (and again after a failure). */
function kitFile(path: string): Promise<string> {
  let request = kitFiles.get(path);
  if (!request) {
    request = fetch(path)
      .then((response) => {
        if (!response.ok) throw new Error(`${path}: HTTP ${response.status}`);
        return response.text();
      })
      .catch((error) => {
        kitFiles.delete(path); // try again next time
        throw error;
      });
    kitFiles.set(path, request);
  }
  return request;
}

const usesChart = (html: string) => /ui\.chart|new Chart\b/.test(html);
const usesMap = (html: string) => /ui\.map|\bL\.map\b/.test(html);

/**
 * Generated pages run in a sandbox with an opaque origin, which browsers won't let load files
 * from a local address (127.0.0.1 on the Android shell, a LAN server…). So the app fetches the kit
 * itself and inlines it. The chart and map libraries (some 350 KB) are fetched only for a page
 * that uses them: a reply with a plain card costs the phone the small kit alone.
 */
export function loadUiKit(html: string): Promise<UiKit> {
  const chart = usesChart(html);
  const map = usesMap(html);
  return Promise.all([
    kitFile("/ui-kit/kit.css"),
    kitFile("/ui-kit/kit.js"),
    chart ? kitFile("/ui-kit/vendor/chart.umd.js") : "",
    map ? kitFile("/ui-kit/vendor/leaflet/leaflet.js") : "",
    map ? kitFile("/ui-kit/vendor/leaflet/leaflet.css") : "",
  ]).then(([css, js, chartJs, leaflet, leafletCss]) => ({ css, js, chart: chartJs, leaflet, leafletCss }));
}

const inlineScript = (code: string) => `<script>${code.replace(/<\/script/gi, "<\\/script")}</script>`;
const inlineStyle = (code: string) => `<style>${code.replace(/<\/style/gi, "<\\/style")}</style>`;

/**
 * The page around a ```ui answer: the kit inline, the theme inline (no flash), and a policy that
 * lets it load nothing and send nothing anywhere (map tiles excepted) — its only way out is asking
 * the app to send a message.
 */
export function uiDocument(html: string, kit: UiKit, tokens: Record<string, string>): string {
  const policy = [
    "default-src 'none'",
    "script-src 'unsafe-inline'",
    "style-src 'unsafe-inline'",
    "img-src data: blob: https://*.is.autonavi.com",
    "font-src data:",
    "connect-src 'none'",
    "form-action 'none'",
    "base-uri 'none'",
  ].join("; ");
  const theme = Object.entries(tokens)
    .filter(([, value]) => value)
    .map(([name, value]) => `${name}:${value}`)
    .join(";");
  const chart = usesChart(html);
  const map = usesMap(html);
  return [
    '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">',
    '<meta name="viewport" content="width=device-width,initial-scale=1">',
    // The app's light/dark, not the phone's: native controls (sliders, selects) follow it.
    `<meta name="color-scheme" content="${document.documentElement.classList.contains("dark") ? "dark" : "light"}">`,
    `<meta http-equiv="Content-Security-Policy" content="${policy}">`,
    inlineStyle(kit.css),
    map ? inlineStyle(kit.leafletCss) : "",
    `<style>:root{${theme}}</style>`,
    chart ? inlineScript(kit.chart) : "",
    map ? inlineScript(kit.leaflet) : "",
    inlineScript(kit.js),
    `</head><body>${html}</body></html>`,
  ].join("\n");
}

const SANDBOX_SEND_COOLDOWN_MS = 800;

/** Neutralize leading slash commands from sandboxed UI/widget frames and cap message size. */
export function sanitizeSandboxSendText(raw: string): string {
  const trimmed = raw.trim().slice(0, 2000);
  if (!trimmed) return "";
  return trimmed.startsWith("/") ? trimmed.replace(/^\/+/, "／") : trimmed;
}

/**
 * A sandboxed answer page (a ```ui block, or a page file the server signed). Its scripts run in
 * their own origin with no access to the app; it talks back only through postMessage: its height,
 * and "send this as my message" (honoured only on the latest reply). About a screen tall in the
 * thread; 放大 opens it full screen.
 */
function SandboxFrame({
  title,
  src,
  html,
  send,
  fixedHeight,
}: {
  title: string;
  src?: string;
  html?: string;
  send: Send;
  fixedHeight?: number;
}) {
  const frame = useRef<HTMLIFrameElement>(null);
  const lastSendAt = useRef(0);
  const [height, setHeight] = useState(fixedHeight ?? 240);
  const [expanded, setExpanded] = useState(false);
  const [srcDoc, setSrcDoc] = useState<string | undefined>(undefined);
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    if (html === undefined) return;
    let cancelled = false;
    loadUiKit(html)
      .then((kit) => !cancelled && setSrcDoc(uiDocument(html, kit, themeTokens(frame.current))))
      .catch(() => !cancelled && setFailed(true));
    return () => {
      cancelled = true;
    };
  }, [html]);

  useEffect(() => {
    const onMessage = (event: MessageEvent) => {
      if (event.source !== frame.current?.contentWindow) return;
      const data = event.data as { type?: string; text?: unknown; height?: unknown };
      if (data?.type === "companion:send" && typeof data.text === "string" && send) {
        const clean = sanitizeSandboxSendText(data.text);
        if (!clean) return;
        const now = Date.now();
        if (lastSendAt.current > 0 && now - lastSendAt.current < SANDBOX_SEND_COOLDOWN_MS) return;
        lastSendAt.current = now;
        setExpanded(false);
        send(clean);
      } else if (data?.type === "companion:height" && typeof data.height === "number" && !fixedHeight) {
        setHeight(Math.min(Math.max(data.height, 80), 2000));
      }
    };
    window.addEventListener("message", onMessage);
    return () => window.removeEventListener("message", onMessage);
  }, [send, fixedHeight]);

  // Follow the app's theme (light/dark, the companion's color) after it loaded.
  useEffect(() => {
    const push = () => frame.current?.contentWindow?.postMessage({ type: "companion:theme", tokens: themeTokens(frame.current) }, "*");
    const observer = new MutationObserver(push);
    observer.observe(document.documentElement, { attributes: true, attributeFilter: ["class"] });
    return () => observer.disconnect();
  }, []);

  if (html !== undefined && !srcDoc) {
    return failed ? (
      <p className="c-muted c-card w-full max-w-[94%] rounded-[20px] p-3.5 text-[13px]">这个界面没加载出来，刷新一下再看。</p>
    ) : (
      <CardPlaceholder kind="ui" />
    );
  }
  const screen = typeof window === "undefined" ? 700 : window.innerHeight;
  const inline = Math.min(height, Math.round(screen * 0.68));
  // Taller than fits in the chat: it scrolls inside, and says there is more below.
  const clipped = !fixedHeight && height > inline + 4;
  const iframe = (
    <iframe
      ref={frame}
      title={title}
      src={src}
      srcDoc={srcDoc}
      sandbox="allow-scripts allow-forms"
      referrerPolicy="no-referrer"
      onLoad={() => frame.current?.contentWindow?.postMessage({ type: "companion:theme", tokens: themeTokens(frame.current) }, "*")}
      className="block w-full border-0"
      style={{ height: expanded ? "100%" : inline, background: "transparent" }}
    />
  );

  if (expanded) {
    return createPortal(
      <div className="companion fixed inset-0 z-50 flex flex-col" style={{ background: "var(--c-bg)" }} role="dialog" aria-label={title}>
        <header className="flex items-center gap-2 px-3 pb-1 pt-[max(env(safe-area-inset-top),12px)]">
          <span className="min-w-0 flex-1 truncate text-[15px] font-semibold">{title}</span>
          <button
            type="button"
            onClick={() => setExpanded(false)}
            className="c-pressable grid h-10 w-10 place-items-center rounded-full"
            style={{ background: "var(--c-fill)" }}
            aria-label="收起"
          >
            <X className="h-5 w-5" aria-hidden="true" />
          </button>
        </header>
        <div className="min-h-0 flex-1 px-3 pb-[max(env(safe-area-inset-bottom),12px)]">{iframe}</div>
      </div>,
      document.body,
    );
  }
  return (
    <section className="c-card c-rise relative w-full max-w-[94%] overflow-hidden rounded-[20px] p-2.5">
      {iframe}
      {clipped && (
        <div className="pointer-events-none absolute inset-x-0 bottom-0 flex justify-center pb-2 pt-10" style={{ background: "linear-gradient(transparent, var(--c-surface))" }}>
          <button
            type="button"
            onClick={() => setExpanded(true)}
            className="c-float c-pressable pointer-events-auto rounded-full px-3.5 py-1.5 text-[13px] font-medium"
          >
            查看全部
          </button>
        </div>
      )}
      <button
        type="button"
        onClick={() => setExpanded(true)}
        className="c-pressable absolute right-2 top-2 grid h-8 w-8 place-items-center rounded-full"
        style={{ background: "var(--c-glass)", boxShadow: "inset 0 0 0 0.5px var(--c-border)" }}
        aria-label={`放大 ${title}`}
      >
        <Maximize2 className="h-3.5 w-3.5" aria-hidden="true" />
      </button>
    </section>
  );
}

/** A title for a generated page: its <title>, else its first heading or .title. */
export function uiTitle(html: string): string {
  const match =
    /<title>([^<]{1,40})<\/title>/i.exec(html) ??
    /<(?:h1|h2|h3)[^>]*>([^<]{1,40})</i.exec(html) ??
    /class="title"[^>]*>([^<]{1,40})</i.exec(html);
  return match?.[1].trim() || "互动结果";
}

function UiCard({ card, send }: { card: Extract<Card, { type: "ui" }>; send: Send }) {
  return <SandboxFrame title={uiTitle(card.html)} html={card.html} send={send} />;
}

function WidgetCard({ card, send }: { card: Extract<Card, { type: "widget" }>; send: Send }) {
  return <SandboxFrame title={card.title} src={card.url} send={send} fixedHeight={card.height} />;
}

/** One card in a reply. ``send`` is set while the card can still be answered. */
export function CardView({ card, send, stateKey }: { card: Card; send: Send; stateKey: string }) {
  switch (card.type) {
    case "buttons":
      return <ButtonsCard card={card} send={send} stateKey={stateKey} />;
    case "choices":
      return <ChoicesCard card={card} send={send} stateKey={stateKey} />;
    case "pictures":
      return <PicturesCard card={card} send={send} stateKey={stateKey} />;
    case "weather":
      return <WeatherCard card={card} />;
    case "timeline":
      return <TimelineCard card={card} />;
    case "places":
      return <PlacesCard card={card} />;
    case "trains":
      return <TrainsCard card={card} send={send} stateKey={stateKey} />;
    case "compare":
      return <CompareCard card={card} />;
    case "checklist":
      return <ChecklistCard card={card} stateKey={stateKey} />;
    case "table":
      return <TableCard card={card} />;
    case "form":
      return <FormCard card={card} send={send} stateKey={stateKey} />;
    case "widget":
      return <WidgetCard card={card} send={send} />;
    case "ui":
      return <UiCard card={card} send={send} />;
    case "connect":
      return <ConnectCard service={card.service} reason={card.reason} send={send} />;
    case "credential":
      return <CredentialCard card={card} send={send} />;
  }
}

/** Where a card (or a generated page) is still being written. */
export function CardPlaceholder({ kind }: { kind?: "ui" }) {
  if (kind === "ui") {
    return (
      <div className="c-card w-full max-w-[94%] rounded-[20px] p-3.5" aria-label="正在生成界面">
        <p className="c-muted flex items-center gap-2 text-[13px]">
          <span className="c-dot h-1.5 w-1.5 rounded-full" style={{ background: "var(--c-accent)" }} aria-hidden="true" />
          正在做一个可以直接操作的界面…
        </p>
        <div className="mt-3 h-24 animate-pulse rounded-2xl" style={{ background: "var(--c-fill)" }} />
        <div className="mt-2 grid grid-cols-3 gap-2">
          {[0, 1, 2].map((i) => (
            <div key={i} className="h-10 animate-pulse rounded-xl" style={{ background: "var(--c-fill)" }} />
          ))}
        </div>
      </div>
    );
  }
  return (
    <div className="c-card w-full max-w-[85%] animate-pulse rounded-[20px] p-3.5" aria-label="正在准备卡片">
      <div className="h-3 w-1/3 rounded-full" style={{ background: "var(--c-fill)" }} />
      <div className="mt-3 h-3 w-2/3 rounded-full" style={{ background: "var(--c-fill)" }} />
      <div className="mt-2 h-3 w-1/2 rounded-full" style={{ background: "var(--c-fill)" }} />
    </div>
  );
}
