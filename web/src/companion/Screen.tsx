import {
  Component,
  lazy,
  Suspense,
  useEffect,
  useRef,
  type ComponentProps,
  type ComponentType,
  type ReactNode,
  type RefObject,
} from "react";
import { ChevronLeft, X } from "lucide-react";

import { useSheetDrag } from "./sheetDrag";

const lazyResetters = new Set<() => void>();

/**
 * Wraps ``React.lazy`` so a rejected dynamic import (offline or stale deploy hash) can be reset
 * and retried cleanly inside ``LazyBoundary`` without reloading the active chat session.
 */
// eslint-disable-next-line @typescript-eslint/no-explicit-any
export function lazyScreen<T extends ComponentType<any>>(
  factory: () => Promise<{ default: T }>,
): ComponentType<ComponentProps<T>> {
  let Inner = lazy(factory);
  lazyResetters.add(() => {
    Inner = lazy(factory);
  });
  const RetryableLazy = (props: ComponentProps<T>) => <Inner {...props} />;
  return RetryableLazy;
}

export class LazyBoundary extends Component<
  { children: ReactNode; fallback?: ReactNode; onClose?: () => void },
  { failed: boolean; retryKey: number }
> {
  state = { failed: false, retryKey: 0 };

  static getDerivedStateFromError() {
    return { failed: true };
  }

  private retry = () => {
    for (const reset of lazyResetters) reset();
    this.setState((prev) => ({ failed: false, retryKey: prev.retryKey + 1 }));
  };

  render() {
    if (this.state.failed) {
      const card = (
        <div
          className="c-card flex max-w-xs flex-col items-center justify-center gap-3 rounded-3xl px-6 py-6 text-center"
          role="alert"
        >
          <p className="c-muted text-[14px]">这一页没加载出来，检查一下网络或刷新试试。</p>
          <div className="flex flex-wrap items-center justify-center gap-2">
            <button
              type="button"
              onClick={this.retry}
              className="c-primary min-h-10 rounded-full px-4 text-[14px] font-semibold"
            >
              重试
            </button>
            <button
              type="button"
              onClick={() => window.location.reload()}
              className="c-pressable min-h-10 rounded-full px-4 text-[14px]"
              style={{ background: "var(--c-fill)" }}
            >
              刷新页面
            </button>
            {this.props.onClose && (
              <button
                type="button"
                onClick={this.props.onClose}
                className="c-muted c-pressable min-h-10 rounded-full px-3 text-[14px]"
              >
                关闭
              </button>
            )}
          </div>
        </div>
      );
      if (this.props.onClose) {
        return (
          <div className="fixed inset-0 z-50 grid place-items-center bg-black/45 p-6" onClick={this.props.onClose}>
            <div onClick={(e) => e.stopPropagation()}>{card}</div>
          </div>
        );
      }
      return <div className="flex justify-center px-6 py-10">{card}</div>;
    }
    return (
      <Suspense key={this.state.retryKey} fallback={this.props.fallback ?? null}>
        {this.props.children}
      </Suspense>
    );
  }
}

/**
 * Open overlays/pages, numbered when they enter the active stack: a layer opened on top
 * (a nested sheet, viewer, fullscreen browser, or dock) has a higher number and answers
 * Escape / Android Back / edge-swipe alone without dismissing layers underneath it.
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

/** A Radix dialog is open over everything: it owns its own Escape handling. */
const radixDialogOnTop = () => !!document.querySelector('[role="dialog"][data-state="open"]');
const sheetOnTop = () => !!document.querySelector('[role="dialog"][data-state="open"], [data-sheet]');

/** Move focus into a modal surface on open (without stealing from autoFocus inputs or higher layers) and restore on close. */
export function useDialogFocus(panelRef: RefObject<HTMLElement | null>, enabled = true, screenId?: number) {
  useEffect(() => {
    if (!enabled) return;
    const opener = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const panel = panelRef.current;
    const topped = screenId !== undefined && openScreens.size > 0 && Math.max(...openScreens) > screenId;
    if (!topped && panel && !panel.contains(document.activeElement)) {
      panel.focus({ preventScroll: true });
    }
    return () => {
      if (opener && opener.isConnected) {
        opener.focus({ preventScroll: true });
      }
    };
  }, [enabled, panelRef, screenId]);
}

/**
 * Escape (and the phone's back key, which the Android app sends as Escape), or a swipe in from the
 * left edge, runs ``onBack`` for the topmost active overlay/page only.
 */
export function useBackKey(onBack: () => void, enabled = true): number {
  const latest = useRef(onBack);
  latest.current = onBack;
  const activeId = useRef(0);
  if (enabled && activeId.current === 0) {
    activeId.current = ++renderedScreens;
  } else if (!enabled) {
    activeId.current = 0;
  }
  const id = activeId.current;
  useEffect(() => {
    if (!enabled || !id) return;
    openScreens.add(id);
    const onKey = (event: KeyboardEvent) => {
      if (event.key !== "Escape" || event.defaultPrevented || Math.max(...openScreens) !== id) return;
      if (radixDialogOnTop()) return;
      event.preventDefault();
      latest.current();
    };
    document.addEventListener("keydown", onKey);
    const stopSwipe = onEdgeSwipe(() => {
      if (Math.max(...openScreens) === id && !radixDialogOnTop()) latest.current();
    });
    return () => {
      document.removeEventListener("keydown", onKey);
      stopSwipe();
      openScreens.delete(id);
    };
  }, [enabled, id]);
  return id;
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
  const panel = useRef<HTMLDivElement>(null);
  const screenId = useBackKey(onBack);
  useDialogFocus(panel, true, screenId);
  return (
    <div
      ref={panel}
      tabIndex={-1}
      role="dialog"
      aria-modal="true"
      aria-label={title}
      className="c-screen fixed inset-0 z-40 flex flex-col outline-none"
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
  const screenId = useBackKey(onBack);
  const panel = useRef<HTMLDivElement>(null);
  useDialogFocus(panel, true, screenId);
  useSheetDrag(panel, onClose);
  return (
    <div className="fixed inset-0 z-40 flex items-end sm:items-center sm:justify-center">
      <div className="absolute inset-0 bg-black/45 backdrop-blur-[2px]" onClick={onClose} aria-hidden="true" />
      <div
        ref={panel}
        tabIndex={-1}
        role="dialog"
        aria-modal="true"
        aria-label={title}
        className="sheet-in c-glass-panel relative flex h-[90dvh] w-full flex-col rounded-t-3xl border border-om-border text-om-fg shadow-2xl outline-none sm:h-[80dvh] sm:max-w-lg sm:rounded-3xl"
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
