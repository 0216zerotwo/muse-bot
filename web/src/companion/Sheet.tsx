import * as DialogPrimitive from "@radix-ui/react-dialog";
import { X } from "lucide-react";
import { useRef, type ReactNode } from "react";

import { useSheetDrag } from "./sheetDrag";

/** iOS-style bottom sheet (pull it down to close). Radix Dialog supplies focus trapping, Escape and labelling. */
export function Sheet({
  open,
  onOpenChange,
  title,
  description,
  children,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title: string;
  description?: string;
  children: ReactNode;
}) {
  const panel = useRef<HTMLDivElement>(null);
  useSheetDrag(panel, () => onOpenChange(false), open);
  return (
    <DialogPrimitive.Root open={open} onOpenChange={onOpenChange}>
      <DialogPrimitive.Portal>
        {/* Portaled outside the page, so it carries the companion tokens itself. */}
        <div className="companion">
          <DialogPrimitive.Overlay className="fixed inset-0 z-40" style={{ background: "var(--c-scrim)" }} />
          <DialogPrimitive.Content
            ref={panel}
            className="c-sheet fixed inset-x-0 bottom-0 z-50 mx-auto max-h-[85dvh] max-w-lg overflow-y-auto rounded-t-[28px] px-5 pt-3 outline-none"
            style={{
              background: "var(--c-surface)",
              color: "var(--c-text)",
              paddingBottom: "max(1.5rem, env(safe-area-inset-bottom))",
            }}
          >
            <div data-sheet-grip className="-mx-5 -mt-3 px-5 pt-3">
            <div className="mx-auto mb-3 h-1.5 w-10 rounded-full" style={{ background: "var(--c-border)" }} aria-hidden="true" />
            <div className="mb-4 flex items-start justify-between gap-3">
              <div>
                <DialogPrimitive.Title className="text-[17px] font-semibold">{title}</DialogPrimitive.Title>
                {description ? (
                  <DialogPrimitive.Description className="c-muted mt-1 text-sm">{description}</DialogPrimitive.Description>
                ) : (
                  <DialogPrimitive.Description className="sr-only">{title}</DialogPrimitive.Description>
                )}
              </div>
              <DialogPrimitive.Close
                className="c-pressable -mr-2 -mt-1 grid h-11 w-11 shrink-0 place-items-center rounded-full"
                aria-label="关闭"
              >
                <X className="h-5 w-5" aria-hidden="true" />
              </DialogPrimitive.Close>
            </div>
            </div>
            {children}
          </DialogPrimitive.Content>
        </div>
      </DialogPrimitive.Portal>
    </DialogPrimitive.Root>
  );
}

/** One tappable row in a sheet's action list. */
export function SheetAction({
  icon,
  label,
  detail,
  onSelect,
  tone,
}: {
  icon: ReactNode;
  label: string;
  detail?: string;
  onSelect: () => void;
  tone?: "danger";
}) {
  return (
    <button
      type="button"
      onClick={onSelect}
      className={`c-pressable flex min-h-[52px] w-full items-center gap-3 rounded-2xl px-3 text-left ${tone === "danger" ? "c-danger" : ""}`}
    >
      <span className="grid h-9 w-9 shrink-0 place-items-center rounded-full" style={{ background: "var(--c-fill)" }} aria-hidden="true">
        {icon}
      </span>
      <span className="min-w-0 flex-1">
        <span className="block text-[15px] font-medium">{label}</span>
        {detail && <span className="c-muted block truncate text-[13px]">{detail}</span>}
      </span>
    </button>
  );
}
