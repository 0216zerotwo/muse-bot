/*
 * Ported from nanoMuse (vendor/nanomuse-web/web/src/components/Sheet.tsx, GPL-3.0-or-later):
 * a bottom sheet on phones, a centred dialog on wide screens, with an optional footer.
 * Portaled into its own `.companion` scope so it keeps the companion's colors. Pull it down to close
 * it (useSheetDrag), or tap outside it.
 */
import { X } from "lucide-react";
import { useId, useRef, type ReactNode } from "react";
import { createPortal } from "react-dom";

import { useBackKey, useDialogFocus } from "../Screen";
import { useSheetDrag } from "../sheetDrag";

export function MuseSheet({
  open,
  onClose,
  title,
  children,
  footer,
}: {
  open: boolean;
  onClose: () => void;
  title?: ReactNode;
  children: ReactNode;
  footer?: ReactNode;
}) {
  const panel = useRef<HTMLDivElement>(null);
  const titleId = useId();
  useSheetDrag(panel, onClose, open);
  useBackKey(onClose, open);
  useDialogFocus(panel, open);

  if (!open) return null;
  return createPortal(
    <div className="companion">
      <div className="fixed inset-0 z-50 flex items-end sm:items-center sm:justify-center">
        <div className="absolute inset-0 bg-black/45 backdrop-blur-[2px]" onClick={onClose} />
        <div
          ref={panel}
          data-sheet
          tabIndex={-1}
          role="dialog"
          aria-modal="true"
          aria-labelledby={title ? titleId : undefined}
          className="sheet-in relative flex max-h-[92dvh] w-full flex-col c-glass-panel rounded-t-3xl border border-om-border text-om-fg shadow-2xl outline-none sm:max-w-lg sm:rounded-3xl"
        >
          <div data-sheet-grip className="flex items-center gap-3 px-5 pb-2 pt-4">
            <div className="absolute left-1/2 top-2 h-1 w-10 -translate-x-1/2 rounded-full bg-om-border sm:hidden" />
            <div id={titleId} className="mt-1 flex-1 text-[17px] font-semibold">
              {title}
            </div>
            <button type="button" onClick={onClose} aria-label="关闭" className="-mr-2 rounded-full p-2 text-om-muted active:bg-om-surface-2">
              <X size={20} />
            </button>
          </div>
          <div className="flex-1 overflow-y-auto px-5 pb-4">{children}</div>
          {footer && (
            <div className="border-t border-om-border px-5 pt-2" style={{ paddingBottom: "max(1rem, env(safe-area-inset-bottom))" }}>
              {footer}
            </div>
          )}
        </div>
      </div>
    </div>,
    document.body,
  );
}
