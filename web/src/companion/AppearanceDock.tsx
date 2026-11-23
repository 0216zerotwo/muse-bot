/*
 * 设置 → 外观, done on the main chat itself: a small glass panel at the bottom with the choices,
 * and no shade over the chat, so the real thing behind it is the preview — the face in the header
 * grows or shrinks, the colors turn light or dark, as each choice is made. A tap anywhere else on
 * the chat, or pulling the panel down, closes it.
 */
import { useRef } from "react";

import { setPetSize, setThemeMode, usePetSize, useThemeMode, type PetSize, type ThemeMode } from "./appearance";
import { Segmented } from "./Menu";
import { useBackKey, useDialogFocus } from "./Screen";
import { useSheetDrag } from "./sheetDrag";

export const MODE_LABEL: Record<ThemeMode, string> = { system: "跟随系统", light: "浅色", dark: "深色" };
export const SIZE_LABEL: Record<PetSize, string> = { small: "小", medium: "中", large: "大" };

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
          <button type="button" onClick={onDone} className="c-primary min-h-9 rounded-full px-4 text-[14px] font-semibold">
            完成
          </button>
        </div>
        <div className="flex flex-col gap-1.5">
          <span className="c-muted text-[13px]">模式</span>
          <Segmented
            label="模式"
            value={mode}
            options={(["system", "light", "dark"] as const).map((value) => ({ value, label: MODE_LABEL[value] }))}
            onChange={setThemeMode}
          />
        </div>
        <div className="flex flex-col gap-1.5">
          <span className="c-muted text-[13px]">形象大小</span>
          <Segmented
            label="形象大小"
            value={size}
            options={(["small", "medium", "large"] as const).map((value) => ({ value, label: SIZE_LABEL[value] }))}
            onChange={setPetSize}
          />
        </div>
        <p className="c-muted text-[12px] leading-snug">上面的主聊天就是预览：选了马上能看到效果。</p>
      </div>
    </div>
    </>
  );
}
