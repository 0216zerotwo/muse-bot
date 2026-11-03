import { useEffect, useRef, useState, type ReactNode } from "react";
import { ChevronLeft, X } from "lucide-react";

import { useSheetDrag } from "./sheetDrag";

/**
 * Open full-screen pages, numbered when first rendered: a page rendered later
 * (opened on top, or nested inside) has a higher number and answers the back key.
 */
const openScreens = new Set<number>();
let renderedScreens = 0;

const EDGE_PX = 24;
const SWIPE_PX = 70;

/**
 * A swipe to the right that starts at the screen's left edge, as iOS goes back. An app added to the
 * home screen gets no such gesture from Safari, so the app gives it itself.
 */
function onEdgeSwipe(run: () => void): () => void {
  let start: { x: number; y: number } | null = null;
  const onStart = (event: TouchEvent) => {
    const touch = event.touches[0];
    start = event.touches.length === 1 && touch.clientX <= EDGE_PX ? { x: touch.clientX, y: touch.clientY } : null;
  };
  const onEnd = (event: TouchEvent) => {
    if (!start) return;
    const touch = event.changedTouches[0];
    const dx = touch.clientX - start.x;
    const dy = Math.abs(touch.clientY - start.y);
    start = null;
    if (dx > SWIPE_PX && dx > dy * 1.5) run();
  };
  document.addEventListener("touchstart", onStart, { passive: true });
  document.addEventListener("touchend", onEnd);
  return () => {
    document.removeEventListener("touchstart", onStart);
    document.removeEventListener("touchend", onEnd);
  };
}

/** A sheet is open over everything: gestures and the back key are its own. */
const sheetOnTop = () => !!document.querySelector('[role="dialog"][data-state="open"], [data-sheet]');

/**
 * Escape (and the phone's back key, which the Android app sends as Escape), or a swipe in from the
 * left edge, runs ``onBack`` for the topmost full-screen page. Sheets opened on top of it are
 * dialogs that handle their own Escape.
 */
export function useBackKey(onBack: () => void) {
  const latest = useRef(onBack);
  latest.current = onBack;
  const [id] = useState(() => ++renderedScreens);
  useEffect(() => {
    openScreens.add(id);
    const onKey = (event: KeyboardEvent) => {
      if (event.key !== "Escape" || Math.max(...openScreens) !== id) return;
      if (document.querySelector('[role="dialog"][data-state="open"]')) return;
      event.preventDefault();
      latest.current();
    };
    document.addEventListener("keydown", onKey);
    const stopSwipe = onEdgeSwipe(() => {
      if (Math.max(...openScreens) === id && !sheetOnTop()) latest.current();
    });
    return () => {
      document.removeEventListener("keydown", onKey);
      stopSwipe();
      openScreens.delete(id);
    };
  }, [id]);
}

/** The same swipe on the main screen, with no page open over it (it opens the side menu). */
export function useEdgeSwipe(run: () => void, enabled: boolean) {
  const latest = useRef(run);
  latest.current = run;
  useEffect(() => {
    if (!enabled) return;
    return onEdgeSwipe(() => {
      if (openScreens.size === 0 && !sheetOnTop()) latest.current();
    });
  }, [enabled]);
}

/** A full-screen page like Muse's settings: back on the left, the title centred. */
export function FullScreen({
  title,
  backLabel,
  onBack,
  pageKey,
  flush = false,
  children,
}: {
  title: string;
  backLabel: string;
  onBack: () => void;
  /** Changing it replays the push animation (a new page in the same screen). */
  pageKey?: string;
  /** Content runs edge to edge (it brings its own side padding). */
  flush?: boolean;
  children: ReactNode;
}) {
  useBackKey(onBack);
  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-label={title}
      className="c-screen fixed inset-0 z-40 flex flex-col"
      style={{ background: "var(--c-bg)", color: "var(--c-text)" }}
    >
      <header className="relative flex min-h-14 shrink-0 items-center px-2" style={{ paddingTop: "env(safe-area-inset-top)" }}>
        <button
          type="button"
          onClick={onBack}
          className="c-pressable z-10 grid h-11 w-11 place-items-center rounded-full"
          aria-label={backLabel}
        >
          <ChevronLeft className="h-6 w-6" aria-hidden="true" />
        </button>
        <h1 className="pointer-events-none absolute inset-x-0 text-center text-[17px] font-semibold">{title}</h1>
      </header>
      <main
        key={pageKey}
        className={`c-push min-h-0 flex-1 overflow-y-auto overscroll-contain pt-2 ${flush ? "" : "px-4"}`}
        style={{ paddingBottom: "max(2rem, env(safe-area-inset-bottom))" }}
      >
        {children}
      </main>
    </div>
  );
}

/**
 * A page that rises from the bottom like the companion's own sheet (设置): it leaves the top of the
 * chat showing, pulls down (or a tap above it) to close, and pages deeper inside push in place —
 * back on the left when there is somewhere to go back to, close on the right.
 */
export function PageSheet({
  title,
  backLabel,
  onBack,
  onClose,
  pageKey,
  flush = false,
  children,
}: {
  title: string;
  /** Set when there is a page to go back to inside the sheet. */
  backLabel?: string;
  onBack: () => void;
  onClose: () => void;
  pageKey?: string;
  flush?: boolean;
  children: ReactNode;
}) {
  useBackKey(onBack);
  const panel = useRef<HTMLDivElement>(null);
  useSheetDrag(panel, onClose);
  return (
    <div className="fixed inset-0 z-40 flex items-end sm:items-center sm:justify-center">
      <div className="absolute inset-0 bg-black/45 backdrop-blur-[2px]" onClick={onClose} aria-hidden="true" />
      <div
        ref={panel}
        role="dialog"
        aria-modal="true"
        aria-label={title}
        className="sheet-in c-glass-panel relative flex h-[90dvh] w-full flex-col rounded-t-3xl border border-om-border text-om-fg shadow-2xl sm:h-[80dvh] sm:max-w-lg sm:rounded-3xl"
      >
        <header data-sheet-grip className="relative flex min-h-14 shrink-0 items-center px-2 pt-2">
          <div className="absolute left-1/2 top-2 h-1 w-10 -translate-x-1/2 rounded-full bg-om-border sm:hidden" aria-hidden="true" />
          {backLabel ? (
            <button
              type="button"
              onClick={onBack}
              className="c-pressable z-10 grid h-11 w-11 place-items-center rounded-full"
              aria-label={backLabel}
            >
              <ChevronLeft className="h-6 w-6" aria-hidden="true" />
            </button>
          ) : (
            <span className="h-11 w-11" />
          )}
          <h1 className="pointer-events-none absolute inset-x-0 text-center text-[17px] font-semibold">{title}</h1>
          <button
            type="button"
            onClick={onClose}
            className="c-pressable z-10 ml-auto grid h-11 w-11 place-items-center rounded-full text-om-muted"
            aria-label="关闭"
          >
            <X className="h-5 w-5" aria-hidden="true" />
          </button>
        </header>
        <main
          key={pageKey}
          className={`c-push min-h-0 flex-1 overflow-y-auto overscroll-contain pt-1 ${flush ? "" : "px-4"}`}
          style={{ paddingBottom: "max(2rem, env(safe-area-inset-bottom))" }}
        >
          {children}
        </main>
      </div>
    </div>
  );
}
