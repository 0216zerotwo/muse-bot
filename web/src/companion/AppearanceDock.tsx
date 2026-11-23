/*
 * 设置 → 外观, done on the main chat itself: a small glass panel at the bottom with the choices,
 * and no shade over the chat, so the real thing behind it is the preview — the face in the header
 * grows or shrinks, the colors turn light or dark, as each choice is made. A tap anywhere else on
 * the chat, or pulling the panel down, closes it.
 */
import { Monitor, Moon, Sun } from "lucide-react";
import { useId, useRef, type ReactNode } from "react";

import { setPetSize, setThemeMode, usePetSize, useThemeMode, type PetSize, type ThemeMode } from "./appearance";
import { useBackKey, useDialogFocus } from "./Screen";
import { useSheetDrag } from "./sheetDrag";

export const MODE_LABEL: Record<ThemeMode, string> = { system: "跟随系统", light: "浅色", dark: "深色" };
export const SIZE_LABEL: Record<PetSize, string> = { small: "小", medium: "中", large: "大" };

const MODE_ICON: Record<ThemeMode, ReactNode> = {
  system: <Monitor className="h-4 w-4 shrink-0" aria-hidden="true" />,
  light: <Sun className="h-4 w-4 shrink-0" aria-hidden="true" />,
  dark: <Moon className="h-4 w-4 shrink-0" aria-hidden="true" />,
};

const FOCUS_RING =
  "focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[color:var(--c-primary)]";

function DockRadioGroup<T extends string>({
  label,
  value,
  options,
  onChange,
}: {
  label: string;
  value: T;
  options: { value: T; label: string; icon?: ReactNode }[];
  onChange: (value: T) => void;
}) {
  const labelId = useId();
  const groupRef = useRef<HTMLDivElement>(null);

  const move = (index: number, delta: number) => {
    const nextIndex = (index + delta + options.length) % options.length;
    onChange(options[nextIndex].value);
    const buttons = groupRef.current?.querySelectorAll<HTMLButtonElement>('[role="radio"]');
    buttons?.[nextIndex]?.focus();
  };

  return (
    <div className="flex flex-col gap-1.5">
      <span id={labelId} className="c-muted text-[13px] font-medium">
        {label}
      </span>
      <div
        ref={groupRef}
        role="radiogroup"
        aria-labelledby={labelId}
        aria-label={label}
        className="flex gap-1 rounded-2xl p-1"
        style={{ background: "var(--c-segment)", boxShadow: "inset 0 0 0 1px var(--c-border)" }}
      >
        {options.map((option, index) => {
          const selected = option.value === value;
          return (
            <button
              key={option.value}
              type="button"
              role="radio"
              aria-checked={selected}
              tabIndex={selected ? 0 : -1}
              onClick={() => onChange(option.value)}
              onKeyDown={(event) => {
                if (event.key === "ArrowRight" || event.key === "ArrowDown") {
                  event.preventDefault();
                  move(index, 1);
                } else if (event.key === "ArrowLeft" || event.key === "ArrowUp") {
                  event.preventDefault();
                  move(index, -1);
                }
              }}
              className={`inline-flex min-h-11 flex-1 items-center justify-center gap-1.5 rounded-xl px-2 text-[14px] transition-colors motion-reduce:transition-none ${FOCUS_RING}`}
              style={
                selected
                  ? { background: "var(--c-segment-on)", fontWeight: 600, boxShadow: "var(--c-shadow)" }
                  : undefined
              }
            >
              {option.icon}
              <span>{option.label}</span>
            </button>
          );
        })}
      </div>
    </div>
  );
}

export function AppearanceDock({ onDone }: { onDone: () => void }) {
  const mode = useThemeMode();
  const size = usePetSize();
  const panel = useRef<HTMLDivElement>(null);
  useBackKey(onDone);
  useDialogFocus(panel);
  useSheetDrag(panel, onDone);
  return (
    <>
      {/* Clear, not shaded: the chat behind stays the preview, and a tap on it closes the panel. */}
      <div className="fixed inset-0 z-40" onClick={onDone} data-testid="appearance-outside" aria-hidden="true" />
      <div
        data-sheet
        className="fixed inset-x-0 bottom-0 z-40 px-3"
        style={{ paddingBottom: "max(0.75rem, env(safe-area-inset-bottom))" }}
        role="dialog"
        aria-label="外观"
      >
        <div ref={panel} tabIndex={-1} className="c-float c-sheet relative mx-auto flex max-w-2xl flex-col gap-3 rounded-[26px] p-4 pt-5 outline-none">
          <div className="absolute left-1/2 top-2 h-1 w-10 -translate-x-1/2 rounded-full bg-om-border" aria-hidden="true" />
          <div data-sheet-grip className="flex items-center">
            <h2 className="flex-1 text-[17px] font-semibold">外观</h2>
            <button
              type="button"
              onClick={onDone}
              className={`c-primary min-h-11 rounded-full px-5 text-[14px] font-semibold transition active:scale-[0.98] motion-reduce:transition-none motion-reduce:active:scale-100 ${FOCUS_RING}`}
            >
              完成
            </button>
          </div>
          <DockRadioGroup
            label="模式"
            value={mode}
            options={(["system", "light", "dark"] as const).map((value) => ({
              value,
              label: MODE_LABEL[value],
              icon: MODE_ICON[value],
            }))}
            onChange={setThemeMode}
          />
          <DockRadioGroup
            label="形象大小"
            value={size}
            options={(["small", "medium", "large"] as const).map((value) => ({
              value,
              label: SIZE_LABEL[value],
            }))}
            onChange={setPetSize}
          />
          <p className="c-muted text-[12px] leading-snug">上面的主聊天就是预览：选了马上能看到效果。</p>
        </div>
      </div>
    </>
  );
}
