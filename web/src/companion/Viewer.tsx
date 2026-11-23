import { createContext, useContext, useEffect, useRef, useState, type ReactNode, type TouchEvent } from "react";
import { ChevronLeft, ChevronRight, Download, ExternalLink, FileText, Music, Share, Trash2, X } from "lucide-react";

import { MarkdownText } from "@/components/MarkdownText";

import { androidShell } from "./android-shell";
import type { CreationKind } from "./companion-api";
import { useBackKey, useDialogFocus } from "./Screen";

/** Something to look at full screen: a file in 创意 or media from the chat. */
export interface ViewerItem {
  key: string;
  kind: CreationKind;
  url: string;
  name: string;
  title: string;
  /** Under the title: when it was made, or what it was made from. */
  caption?: string;
  /** Saving uses this when set (e.g. a URL that asks the browser to download). */
  downloadUrl?: string;
}

const TEXT_SUFFIXES = [".md", ".markdown", ".txt", ".csv", ".json", ".log"];
/** Larger text files open elsewhere instead of in the preview. */
const MAX_PREVIEW_BYTES = 1024 * 1024;
const SWIPE_PX = 50;
const DOUBLE_TAP_MS = 300;

function isTextFile(name: string): boolean {
  const lower = name.toLowerCase();
  return TEXT_SUFFIXES.some((suffix) => lower.endsWith(suffix));
}

type OpenViewer = (items: ViewerItem[], index: number) => void;

const ViewerContext = createContext<OpenViewer | null>(null);

/** Chat media opens in the viewer when one is provided; otherwise in a new tab. */
export function useOpenViewer(): OpenViewer | null {
  return useContext(ViewerContext);
}

export function ViewerProvider({ children }: { children: ReactNode }) {
  const [open, setOpen] = useState<{ items: ViewerItem[]; index: number } | null>(null);
  return (
    <ViewerContext.Provider value={(items, index) => setOpen({ items, index })}>
      {children}
      {open && <Viewer items={open.items} start={open.index} onClose={() => setOpen(null)} />}
    </ViewerContext.Provider>
  );
}

async function shareItem(item: ViewerItem) {
  const android = androidShell();
  if (android?.shareFile) {
    // The Android app's WebView has no navigator.share: the system share sheet, from the app.
    android.shareFile(new URL(item.url, window.location.href).href, item.name);
    return;
  }
  try {
    const blob = await (await fetch(item.url, { credentials: "same-origin" })).blob();
    const file = new File([blob], item.name, { type: blob.type });
    if (navigator.canShare?.({ files: [file] })) {
      await navigator.share({ files: [file], title: item.title });
      return;
    }
    await navigator.share({ title: item.title, text: item.title });
  } catch {
    // Cancelled, or the system share sheet is unavailable: nothing to undo.
  }
}

/**
 * Full-screen look at one item at a time: images zoom with a double tap and
 * swipe to the next one, videos and audio play, text documents show inline.
 */
export function Viewer({
  items,
  start,
  onClose,
  onRemove,
}: {
  items: ViewerItem[];
  start: number;
  onClose: () => void;
  /** Offered for files in 创意: takes the item off the list (the file stays). */
  onRemove?: (item: ViewerItem) => Promise<void>;
}) {
  const [index, setIndex] = useState(start);
  const [zoomed, setZoomed] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const [removing, setRemoving] = useState(false);
  const panelRef = useRef<HTMLDivElement>(null);
  const item = items[Math.min(index, items.length - 1)];
  useBackKey(onClose);
  useDialogFocus(panelRef);

  useEffect(() => {
    setZoomed(false);
    setConfirming(false);
  }, [item?.key]);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "ArrowLeft") setIndex((i) => Math.max(0, i - 1));
      if (event.key === "ArrowRight") setIndex((i) => Math.min(items.length - 1, i + 1));
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [items.length]);

  /* Swipe sideways for the neighbours, down to close; double tap to zoom. */
  const touch = useRef<{ x: number; y: number } | null>(null);
  const lastTap = useRef(0);
  const onTouchStart = (event: TouchEvent) => {
    if (event.touches.length !== 1) return;
    touch.current = { x: event.touches[0].clientX, y: event.touches[0].clientY };
  };
  const onTouchEnd = (event: TouchEvent) => {
    const from = touch.current;
    touch.current = null;
    if (!from) return;
    const dx = event.changedTouches[0].clientX - from.x;
    const dy = event.changedTouches[0].clientY - from.y;
    if (Math.abs(dx) < 10 && Math.abs(dy) < 10 && item?.kind === "image") {
      const now = Date.now();
      if (now - lastTap.current < DOUBLE_TAP_MS) {
        event.preventDefault(); // no synthetic dblclick toggling it back
        setZoomed((z) => !z);
        lastTap.current = 0;
      } else {
        lastTap.current = now;
      }
      return;
    }
    if (zoomed) return;
    if (Math.abs(dx) > SWIPE_PX && Math.abs(dx) > Math.abs(dy)) {
      setIndex((i) => (dx < 0 ? Math.min(items.length - 1, i + 1) : Math.max(0, i - 1)));
    } else if (dy > SWIPE_PX * 2 && Math.abs(dy) > Math.abs(dx) && item?.kind !== "document") {
      onClose();
    }
  };

  if (!item) return null;
  const dark = item.kind !== "document";
  const remove = async () => {
    if (!onRemove) return;
    setRemoving(true);
    try {
      await onRemove(item);
      if (items.length <= 1) onClose();
      else setIndex((i) => Math.min(i, items.length - 2));
    } finally {
      setRemoving(false);
      setConfirming(false);
    }
  };

  return (
    <div
      ref={panelRef}
      tabIndex={-1}
      role="dialog"
      aria-modal="true"
      aria-label={item.title}
      className="c-screen fixed inset-0 z-50 flex flex-col outline-none"
      style={dark ? { background: "#000", color: "#fff" } : { background: "var(--c-bg)", color: "var(--c-text)" }}
    >
      <header className="flex min-h-14 shrink-0 items-center gap-1 px-2" style={{ paddingTop: "env(safe-area-inset-top)" }}>
        <button type="button" onClick={onClose} aria-label="关闭" className="c-pressable grid h-11 w-11 place-items-center rounded-full">
          <X className="h-6 w-6" aria-hidden="true" />
        </button>
        <div className="min-w-0 flex-1 text-center">
          <p className="truncate text-[15px] font-semibold">{item.title}</p>
          {item.caption && <p className="truncate text-[12px] opacity-70">{item.caption}</p>}
        </div>
        <span className="grid h-11 min-w-11 place-items-center text-[13px] opacity-70">
          {items.length > 1 ? `${index + 1}/${items.length}` : ""}
        </span>
      </header>

      <div className="relative min-h-0 flex-1" onTouchStart={onTouchStart} onTouchEnd={onTouchEnd}>
        <ViewerBody key={item.key} item={item} zoomed={zoomed} onToggleZoom={() => setZoomed((z) => !z)} />
        {items.length > 1 && !zoomed && (
          <>
            {index > 0 && (
              <button
                type="button"
                aria-label="上一个"
                onClick={() => setIndex(index - 1)}
                className="absolute left-2 top-1/2 hidden h-11 w-11 -translate-y-1/2 place-items-center rounded-full bg-black/40 text-white sm:grid"
              >
                <ChevronLeft className="h-6 w-6" aria-hidden="true" />
              </button>
            )}
            {index < items.length - 1 && (
              <button
                type="button"
                aria-label="下一个"
                onClick={() => setIndex(index + 1)}
                className="absolute right-2 top-1/2 hidden h-11 w-11 -translate-y-1/2 place-items-center rounded-full bg-black/40 text-white sm:grid"
              >
                <ChevronRight className="h-6 w-6" aria-hidden="true" />
              </button>
            )}
          </>
        )}
      </div>

      <footer className="shrink-0 px-4 pt-2" style={{ paddingBottom: "max(0.75rem, env(safe-area-inset-bottom))" }}>
        {confirming ? (
          <div className="mx-auto flex max-w-sm flex-col gap-2 text-center">
            <p className="text-[14px] opacity-80">从资源库移除？文件本身不会删除。</p>
            <div className="flex gap-2">
              <button type="button" onClick={() => setConfirming(false)} className="c-pressable min-h-11 flex-1 rounded-full text-[15px]" style={{ background: dark ? "rgb(255 255 255 / 0.14)" : "var(--c-fill)" }}>
                取消
              </button>
              <button type="button" disabled={removing} onClick={() => void remove()} className="min-h-11 flex-1 rounded-full text-[15px] font-semibold text-white disabled:opacity-50" style={{ background: "var(--c-danger-fill)" }}>
                {removing ? "移除中…" : "移除"}
              </button>
            </div>
          </div>
        ) : (
          <div className="mx-auto flex max-w-sm justify-around">
            <ViewerAction label="保存" href={item.downloadUrl ?? item.url} download={item.name} icon={<Download className="h-5 w-5" />} />
            {typeof navigator !== "undefined" && ("share" in navigator || !!androidShell()?.shareFile) && (
              <ViewerAction label="分享" onSelect={() => void shareItem(item)} icon={<Share className="h-5 w-5" />} />
            )}
            {onRemove && <ViewerAction label="移除" onSelect={() => setConfirming(true)} icon={<Trash2 className="h-5 w-5" />} />}
          </div>
        )}
      </footer>
    </div>
  );
}

function ViewerAction({
  label,
  icon,
  href,
  download,
  onSelect,
}: {
  label: string;
  icon: ReactNode;
  href?: string;
  download?: string;
  onSelect?: () => void;
}) {
  const className = "c-pressable flex min-h-14 min-w-16 flex-col items-center justify-center gap-0.5 rounded-2xl px-3 text-[12px]";
  const content = (
    <>
      <span aria-hidden="true">{icon}</span>
      {label}
    </>
  );
  return href ? (
    <a href={href} download={download} className={className}>
      {content}
    </a>
  ) : (
    <button type="button" onClick={onSelect} className={className}>
      {content}
    </button>
  );
}

function ViewerBody({ item, zoomed, onToggleZoom }: { item: ViewerItem; zoomed: boolean; onToggleZoom: () => void }) {
  const scroller = useRef<HTMLDivElement>(null);
  const focus = useRef<{ x: number; y: number }>({ x: 0.5, y: 0.5 });

  // Zooming in keeps the spot that was tapped in the middle.
  useEffect(() => {
    const el = scroller.current;
    if (!el || !zoomed) return;
    el.scrollLeft = focus.current.x * el.scrollWidth - el.clientWidth / 2;
    el.scrollTop = focus.current.y * el.scrollHeight - el.clientHeight / 2;
  }, [zoomed]);

  if (item.kind === "image") {
    return (
      <div
        ref={scroller}
        className={`absolute inset-0 ${zoomed ? "overflow-auto" : "grid place-items-center overflow-hidden"}`}
        onDoubleClick={(event) => {
          const rect = event.currentTarget.getBoundingClientRect();
          focus.current = { x: (event.clientX - rect.left) / rect.width, y: (event.clientY - rect.top) / rect.height };
          onToggleZoom();
        }}
      >
        <img
          src={item.url}
          alt={item.title}
          draggable={false}
          className={zoomed ? "max-w-none" : "max-h-full max-w-full object-contain"}
          style={zoomed ? { width: "200%" } : undefined}
        />
      </div>
    );
  }
  if (item.kind === "video") {
    return (
      <div className="absolute inset-0 grid place-items-center">
        <video src={item.url} controls autoPlay playsInline className="max-h-full max-w-full" />
      </div>
    );
  }
  if (item.kind === "audio") {
    return (
      <div className="absolute inset-0 flex flex-col items-center justify-center gap-6 px-6">
        <span className="grid h-28 w-28 place-items-center rounded-3xl" style={{ background: "rgb(255 255 255 / 0.12)" }}>
          <Music className="h-12 w-12" aria-hidden="true" />
        </span>
        <audio src={item.url} controls autoPlay className="w-full max-w-sm" />
      </div>
    );
  }
  return <DocumentBody item={item} />;
}

function DocumentBody({ item }: { item: ViewerItem }) {
  const previewable = isTextFile(item.name);
  const [text, setText] = useState<string | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    if (!previewable) return;
    let cancelled = false;
    fetch(item.url, { credentials: "same-origin" })
      .then(async (res) => {
        const size = Number(res.headers.get("Content-Length") || 0);
        if (!res.ok || size > MAX_PREVIEW_BYTES) throw new Error("not previewable");
        return res.text();
      })
      .then((body) => !cancelled && setText(body))
      .catch(() => !cancelled && setFailed(true));
    return () => {
      cancelled = true;
    };
  }, [item.url, previewable]);

  if (previewable && !failed) {
    return (
      <div className="absolute inset-0 overflow-y-auto overscroll-contain px-5 pb-6">
        {text === null ? (
          <p className="c-muted pt-8 text-center text-sm">正在打开…</p>
        ) : item.name.toLowerCase().endsWith(".md") || item.name.toLowerCase().endsWith(".markdown") ? (
          <div className="mx-auto max-w-2xl text-[15px] leading-relaxed">
            <MarkdownText>{text}</MarkdownText>
          </div>
        ) : (
          <pre className="mx-auto max-w-2xl whitespace-pre-wrap break-words text-[14px] leading-relaxed">{text}</pre>
        )}
      </div>
    );
  }
  return (
    <div className="absolute inset-0 flex flex-col items-center justify-center gap-4 px-8 text-center">
      <span className="c-primary grid h-20 w-20 place-items-center rounded-3xl" aria-hidden="true">
        <FileText className="h-9 w-9" />
      </span>
      <p className="max-w-full break-all text-[16px] font-medium">{item.name}</p>
      <a
        href={item.url}
        target="_blank"
        rel="noreferrer"
        onClick={(event) => {
          const android = androidShell();
          if (android?.openFile) {
            event.preventDefault();
            android.openFile(new URL(item.url, window.location.href).href, item.name);
          }
        }}
        className="c-card c-pressable inline-flex min-h-11 items-center gap-2 rounded-full px-4 text-[15px]"
      >
        <ExternalLink className="h-4 w-4" aria-hidden="true" />
        用其他应用打开
      </a>
    </div>
  );
}
