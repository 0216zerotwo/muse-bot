import { Children, Fragment, type ReactNode } from "react";
import { ChevronRight, ChevronsUpDown } from "lucide-react";

/** iOS-style grouped list: rows in one rounded card, hairlines between them. */
export function MenuGroup({ children, footnote }: { children: ReactNode; footnote?: ReactNode }) {
  const rows = Children.toArray(children).filter(Boolean);
  if (rows.length === 0) return null;
  return (
    <div>
      <div className="c-card overflow-hidden rounded-2xl">
        {rows.map((row, index) => (
          <Fragment key={index}>
            {index > 0 && <div className="ml-[52px] border-t c-divider" aria-hidden="true" />}
            {row}
          </Fragment>
        ))}
      </div>
      {footnote && <p className="c-muted px-3.5 pt-1.5 text-[12px] leading-snug">{footnote}</p>}
    </div>
  );
}

/**
 * One line: icon, label, an optional short value on the right, then a chevron
 * when the row opens something, or a custom control (a switch) instead.
 */
export function MenuRow({
  icon,
  label,
  detail,
  value,
  trailing,
  onSelect,
  ariaLabel,
  danger,
}: {
  /** A lucide icon (boxed here), or a ready-made 28px logo when ``detail`` is not set. */
  icon: ReactNode;
  label: string;
  /** A muted second line. */
  detail?: string;
  value?: string;
  trailing?: ReactNode;
  onSelect?: () => void;
  ariaLabel?: string;
  /** A destructive action (delete, disconnect). */
  danger?: boolean;
}) {
  const main = (
    <>
      <span
        className="grid h-7 w-7 shrink-0 place-items-center rounded-lg"
        style={{ background: "var(--c-fill)" }}
        aria-hidden="true"
      >
        {icon}
      </span>
      {detail ? (
        <span className="min-w-0 flex-1 py-2">
          <span className="block truncate text-[15px]">{label}</span>
          <span className="c-muted block truncate text-[12px]">{detail}</span>
        </span>
      ) : (
        <span className={`min-w-0 flex-1 truncate text-[15px] ${danger ? "c-danger" : ""}`}>{label}</span>
      )}
      {value && <span className="c-muted max-w-[50%] shrink-0 truncate text-[14px]">{value}</span>}
    </>
  );
  const content = (
    <>
      {main}
      {trailing ?? (onSelect && <ChevronRight className="c-muted h-4 w-4 shrink-0" aria-hidden="true" />)}
    </>
  );
  if (!onSelect) return <div className="flex min-h-12 items-center gap-3 px-3.5">{content}</div>;
  if (trailing) {
    // Its own controls (删除, 检测价格) sit beside the tappable part, not inside it: a button
    // within a button is invalid, and a tap on the inner one can reach the outer one.
    return (
      <div className="flex min-h-12 items-center gap-2 pr-3.5">
        <button
          type="button"
          onClick={onSelect}
          aria-label={ariaLabel}
          className="c-pressable flex min-h-12 min-w-0 flex-1 items-center gap-3 pl-3.5 text-left"
        >
          {main}
        </button>
        {trailing}
      </div>
    );
  }
  return (
    <button
      type="button"
      onClick={onSelect}
      aria-label={ariaLabel}
      className="c-pressable flex min-h-12 w-full items-center gap-3 px-3.5 text-left"
    >
      {content}
    </button>
  );
}

export function Switch({
  checked,
  onChange,
  label,
  disabled,
}: {
  checked: boolean;
  onChange: (checked: boolean) => void;
  label: string;
  disabled?: boolean;
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={label}
      disabled={disabled}
      onClick={() => onChange(!checked)}
      className="relative h-8 w-[52px] shrink-0 rounded-full transition-colors disabled:opacity-50"
      style={{ background: checked ? "var(--c-primary)" : "var(--c-border)" }}
    >
      <span
        className="absolute top-1 h-6 w-6 rounded-full bg-white shadow transition-[left]"
        style={{ left: checked ? 24 : 4 }}
        aria-hidden="true"
      />
    </button>
  );
}

/** A row whose value is picked with the phone's own picker (a native select laid over it). */
export function SelectRow<T extends string>({
  icon,
  label,
  value,
  options,
  onChange,
  disabled,
}: {
  icon?: ReactNode;
  label: string;
  value: T;
  options: { value: T; label: string }[];
  onChange: (value: T) => void;
  disabled?: boolean;
}) {
  const current = options.find((option) => option.value === value)?.label ?? value;
  return (
    <label className="c-pressable relative flex min-h-12 items-center gap-3 px-3.5">
      {icon && (
        <span
          className="grid h-7 w-7 shrink-0 place-items-center rounded-lg"
          style={{ background: "var(--c-fill)" }}
          aria-hidden="true"
        >
          {icon}
        </span>
      )}
      <span className="min-w-0 flex-1 truncate text-[15px]">{label}</span>
      <span className="c-muted max-w-[55%] truncate text-[14px]">{current}</span>
      <ChevronsUpDown className="c-muted h-3.5 w-3.5 shrink-0" aria-hidden="true" />
      <select
        aria-label={label}
        value={value}
        disabled={disabled}
        onChange={(event) => onChange(event.target.value as T)}
        className="absolute inset-0 h-full w-full cursor-pointer opacity-0"
      >
        {options.map((option) => (
          <option key={option.value} value={option.value}>
            {option.label}
          </option>
        ))}
      </select>
    </label>
  );
}

/** A compact segmented control (one choice out of a few). */
export function Segmented<T extends string>({
  label,
  value,
  options,
  onChange,
  disabled,
}: {
  label: string;
  value: T;
  options: { value: T; label: string }[];
  onChange: (value: T) => void;
  disabled?: boolean;
}) {
  return (
    <div
      role="radiogroup"
      aria-label={label}
      className="flex rounded-xl p-0.5"
      // The outline keeps the track visible where it sits straight on the page (dark mode).
      style={{ background: "var(--c-segment)", boxShadow: "inset 0 0 0 1px var(--c-border)" }}
    >
      {options.map((option) => {
        const selected = option.value === value;
        return (
          <button
            key={option.value}
            type="button"
            role="radio"
            aria-checked={selected}
            disabled={disabled}
            onClick={() => onChange(option.value)}
            className="min-h-8 flex-1 rounded-[10px] px-1 text-[13px] transition-colors disabled:opacity-50"
            style={selected ? { background: "var(--c-segment-on)", fontWeight: 600, boxShadow: "var(--c-shadow)" } : undefined}
          >
            {option.label}
          </button>
        );
      })}
    </div>
  );
}
