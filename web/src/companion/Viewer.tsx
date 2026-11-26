import { createContext, lazy, Suspense, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode, type TouchEvent } from "react";
import {
  AlertCircle,
  Check,
  ChevronLeft,
  ChevronRight,
  Code2,
  Copy,
  Download,
  ExternalLink,
  Eye,
  FileText,
  ImageOff,
  Loader2,
  Music,
  RotateCcw,
  Share,
  Table2,
  Trash2,
  WrapText,
  X,
  ZoomIn,
  ZoomOut,
} from "lucide-react";

import { MarkdownText } from "@/components/MarkdownText";
import { useThemeValue } from "@/hooks/useTheme";
import { copyTextToClipboard } from "@/lib/clipboard";

import { androidShell } from "./android-shell";
import type { CreationKind } from "./companion-api";
import {
  artifactHtmlDocument,
  csvRows,
  fileBadge,
  fileEnding,
  fileKind,
  isPreviewableDocument,
  svgPageDocument,
} from "./fileKinds";
import { useBackKey, useDialogFocus } from "./Screen";

const LazyMermaidDiagram = lazy(() => import("@/components/MermaidDiagram"));

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

/** Larger text/web files open elsewhere instead of in the inline preview. */
const MAX_PREVIEW_BYTES = 2 * 1024 * 1024;
const SWIPE_PX = 50;
const DOUBLE_TAP_MS = 300;
export const MIN_ZOOM = 1;
export const MAX_ZOOM = 3;
export const ZOOM_STEP = 0.5;
const FOCUS_RING =
  "focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[color:var(--c-primary)]";

function isPdfFile(name: string): boolean {
  return fileKind(name) === "pdf";
}

function clampZoom(value: number): number {
  return Math.max(MIN_ZOOM, Math.min(MAX_ZOOM, Math.round(value * 10) / 10));
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
 * swipe to the next one, videos and audio play, web/svg/mermaid/markdown/code/text documents show inline.
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
  const [zoom, setZoom] = useState(1);
  const [confirming, setConfirming] = useState(false);
  const [removing, setRemoving] = useState(false);
  const panelRef = useRef<HTMLDivElement>(null);
  const item = items[Math.min(index, items.length - 1)];
  const zoomed = zoom > 1;
  const isImage = item?.kind === "image";
  useBackKey(onClose);
  useDialogFocus(panelRef);

  const zoomIn = useCallback(() => setZoom((z) => clampZoom(z + ZOOM_STEP)), []);
  const zoomOut = useCallback(() => setZoom((z) => clampZoom(z - ZOOM_STEP)), []);
  const resetZoom = useCallback(() => setZoom(1), []);
  const toggleZoom = useCallback(() => setZoom((z) => (z > 1 ? 1 : 2)), []);

  useEffect(() => {
    setZoom(1);
    setConfirming(false);
  }, [item?.key]);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.ctrlKey || event.metaKey || event.altKey) return;
      const target = event.target as HTMLElement | null;
      if (
        target &&
        (target.tagName === "INPUT" || target.tagName === "TEXTAREA" || target.tagName === "SELECT" || target.isContentEditable)
      ) {
        return;
      }
      if (event.key === "ArrowLeft") setIndex((i) => Math.max(0, i - 1));
      else if (event.key === "ArrowRight") setIndex((i) => Math.min(items.length - 1, i + 1));
      else if (isImage && (event.key === "+" || event.key === "=")) {
        event.preventDefault();
        zoomIn();
      } else if (isImage && (event.key === "-" || event.key === "_")) {
        event.preventDefault();
        zoomOut();
      } else if (isImage && event.key === "0") {
        event.preventDefault();
        resetZoom();
      }
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [isImage, items.length, resetZoom, zoomIn, zoomOut]);

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
        toggleZoom();
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
        <button
          type="button"
          onClick={onClose}
          aria-label="关闭"
          className={`c-pressable grid min-h-11 min-w-11 place-items-center rounded-full motion-reduce:transition-none ${FOCUS_RING}`}
        >
          <X className="h-6 w-6" aria-hidden="true" />
        </button>
        <div className="min-w-0 flex-1 text-center">
          <p className="truncate text-[15px] font-semibold">{item.title}</p>
          {item.caption && <p className="truncate text-[12px] opacity-70">{item.caption}</p>}
        </div>
        <span className="grid min-h-11 min-w-11 place-items-center text-[13px] opacity-70">
          {items.length > 1 ? `${index + 1}/${items.length}` : ""}
        </span>
      </header>

      <div className="relative min-h-0 flex-1" onTouchStart={onTouchStart} onTouchEnd={onTouchEnd}>
        <ViewerBody key={`${item.key}:${item.url}`} item={item} zoom={zoom} onToggleZoom={toggleZoom} />
        {items.length > 1 && !zoomed && (
          <>
            {index > 0 && (
              <button
                type="button"
                aria-label="上一个"
                onClick={() => setIndex(index - 1)}
                className={`absolute left-2 top-1/2 hidden min-h-11 min-w-11 -translate-y-1/2 place-items-center rounded-full bg-black/50 text-white motion-reduce:transition-none sm:grid ${FOCUS_RING}`}
              >
                <ChevronLeft className="h-6 w-6" aria-hidden="true" />
              </button>
            )}
            {index < items.length - 1 && (
              <button
                type="button"
                aria-label="下一个"
                onClick={() => setIndex(index + 1)}
                className={`absolute right-2 top-1/2 hidden min-h-11 min-w-11 -translate-y-1/2 place-items-center rounded-full bg-black/50 text-white motion-reduce:transition-none sm:grid ${FOCUS_RING}`}
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
              <button
                type="button"
                onClick={() => setConfirming(false)}
                className={`c-pressable min-h-11 flex-1 rounded-full text-[15px] motion-reduce:transition-none ${FOCUS_RING}`}
                style={{ background: dark ? "rgb(255 255 255 / 0.14)" : "var(--c-fill)" }}
              >
                取消
              </button>
              <button
                type="button"
                disabled={removing}
                onClick={() => void remove()}
                className={`min-h-11 flex-1 rounded-full text-[15px] font-semibold text-white disabled:opacity-50 motion-reduce:transition-none ${FOCUS_RING}`}
                style={{ background: "var(--c-danger-fill)" }}
              >
                {removing ? "移除中…" : "移除"}
              </button>
            </div>
          </div>
        ) : (
          <div className="mx-auto flex max-w-sm flex-col gap-1.5">
            {isImage && (
              <div
                role="toolbar"
                aria-label="图片缩放"
                className="mx-auto inline-flex items-center gap-1 rounded-full px-1.5 py-0.5"
                style={{ background: "rgb(255 255 255 / 0.12)" }}
              >
                <button
                  type="button"
                  onClick={zoomOut}
                  disabled={zoom <= MIN_ZOOM}
                  aria-label="缩小"
                  className={`c-pressable grid min-h-11 min-w-11 place-items-center rounded-full disabled:opacity-35 motion-reduce:transition-none ${FOCUS_RING}`}
                >
                  <ZoomOut className="h-4 w-4" aria-hidden="true" />
                </button>
                <button
                  type="button"
                  onClick={resetZoom}
                  disabled={zoom === 1}
                  aria-label="重置缩放"
                  className={`c-pressable inline-flex min-h-11 min-w-[68px] items-center justify-center gap-1 rounded-full px-2.5 text-[13px] font-medium tabular-nums disabled:opacity-70 motion-reduce:transition-none ${FOCUS_RING}`}
                >
                  {zoom > 1 && <RotateCcw className="h-3.5 w-3.5" aria-hidden="true" />}
                  {Math.round(zoom * 100)}%
                </button>
                <button
                  type="button"
                  onClick={zoomIn}
                  disabled={zoom >= MAX_ZOOM}
                  aria-label="放大"
                  className={`c-pressable grid min-h-11 min-w-11 place-items-center rounded-full disabled:opacity-35 motion-reduce:transition-none ${FOCUS_RING}`}
                >
                  <ZoomIn className="h-4 w-4" aria-hidden="true" />
                </button>
              </div>
            )}
            <div className="flex justify-around">
              <ViewerAction label="保存" href={item.downloadUrl ?? item.url} download={item.name} icon={<Download className="h-5 w-5" />} />
              {typeof navigator !== "undefined" && ("share" in navigator || !!androidShell()?.shareFile) && (
                <ViewerAction label="分享" onSelect={() => void shareItem(item)} icon={<Share className="h-5 w-5" />} />
              )}
              {onRemove && <ViewerAction label="移除" onSelect={() => setConfirming(true)} icon={<Trash2 className="h-5 w-5" />} />}
            </div>
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
  const className = `c-pressable flex min-h-14 min-w-16 flex-col items-center justify-center gap-0.5 rounded-2xl px-3 text-[12px] motion-reduce:transition-none ${FOCUS_RING}`;
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

function ViewerBody({ item, zoom, onToggleZoom }: { item: ViewerItem; zoom: number; onToggleZoom: () => void }) {
  const scroller = useRef<HTMLDivElement>(null);
  const imgRef = useRef<HTMLImageElement>(null);
  const focus = useRef<{ x: number; y: number }>({ x: 0.5, y: 0.5 });
  const [imgState, setImgState] = useState<"loading" | "loaded" | "error">("loading");
  const [videoFailed, setVideoFailed] = useState(false);
  const [retryKey, setRetryKey] = useState(0);
  const zoomed = zoom > 1;

  useEffect(() => {
    setImgState("loading");
    setVideoFailed(false);
  }, [item.url]);

  // Cached images already loaded before mount may skip onLoad; clear the spinner immediately.
  useEffect(() => {
    if (item.kind !== "image") return;
    const node = imgRef.current;
    if (node && node.complete && node.naturalWidth > 0) {
      setImgState("loaded");
    }
  }, [item.kind, item.url, retryKey]);

  // Zooming in keeps the spot that was tapped in the middle.
  useEffect(() => {
    const el = scroller.current;
    if (!el || !zoomed) return;
    el.scrollLeft = focus.current.x * el.scrollWidth - el.clientWidth / 2;
    el.scrollTop = focus.current.y * el.scrollHeight - el.clientHeight / 2;
  }, [zoom, zoomed]);

  if (item.kind === "image") {
    if (imgState === "error") {
      return (
        <div className="absolute inset-0 flex flex-col items-center justify-center gap-3 px-6 text-center" role="alert">
          <span className="grid h-16 w-16 place-items-center rounded-3xl" style={{ background: "rgb(255 255 255 / 0.12)" }} aria-hidden="true">
            <ImageOff className="h-8 w-8 opacity-80" />
          </span>
          <p className="text-[16px] font-semibold">图片加载失败</p>
          <p className="max-w-xs text-[13px] opacity-70">可能是网络波动或文件已被移动，可以重试或直接保存查看。</p>
          <div className="mt-1 flex flex-wrap justify-center gap-2">
            <button
              type="button"
              onClick={() => {
                setImgState("loading");
                setRetryKey((k) => k + 1);
              }}
              className={`c-pressable min-h-11 rounded-full px-5 text-[14px] font-semibold text-white motion-reduce:transition-none ${FOCUS_RING}`}
              style={{ background: "rgb(255 255 255 / 0.18)" }}
            >
              重试
            </button>
            <a
              href={item.url}
              target="_blank"
              rel="noreferrer"
              className={`c-pressable inline-flex min-h-11 items-center gap-1.5 rounded-full px-4 text-[14px] text-white/90 motion-reduce:transition-none ${FOCUS_RING}`}
              style={{ background: "rgb(255 255 255 / 0.1)" }}
            >
              <ExternalLink className="h-4 w-4" aria-hidden="true" />
              在新标签打开
            </a>
          </div>
        </div>
      );
    }

    return (
      <div
        ref={scroller}
        className={`absolute inset-0 ${zoomed ? "cursor-zoom-out overflow-auto" : "grid cursor-zoom-in place-items-center overflow-hidden"}`}
        onDoubleClick={(event) => {
          const rect = event.currentTarget.getBoundingClientRect();
          if (rect.width > 0 && rect.height > 0) {
            focus.current = { x: (event.clientX - rect.left) / rect.width, y: (event.clientY - rect.top) / rect.height };
          }
          onToggleZoom();
        }}
      >
        {imgState === "loading" && (
          <div
            role="status"
            aria-label="正在加载图片"
            className="pointer-events-none absolute inset-0 grid place-items-center"
          >
            <Loader2 className="h-7 w-7 animate-spin opacity-75 motion-reduce:animate-none" aria-hidden="true" />
          </div>
        )}
        <img
          ref={imgRef}
          key={retryKey}
          src={item.url}
          alt={item.title}
          draggable={false}
          data-zoom={zoom}
          onLoad={() => setImgState("loaded")}
          onError={() => setImgState("error")}
          className={
            zoomed
              ? "max-w-none transition-[width] duration-150 motion-reduce:transition-none"
              : "max-h-full max-w-full object-contain transition-opacity duration-150 motion-reduce:transition-none"
          }
          style={zoomed ? { width: `${Math.round(zoom * 100)}%` } : undefined}
        />
      </div>
    );
  }
  if (item.kind === "video") {
    if (videoFailed) {
      return (
        <div className="absolute inset-0 flex flex-col items-center justify-center gap-3 px-6 text-center" role="alert">
          <AlertCircle className="h-8 w-8 opacity-80" aria-hidden="true" />
          <p className="text-[16px] font-semibold">视频无法播放</p>
          <button
            type="button"
            onClick={() => {
              setVideoFailed(false);
              setRetryKey((k) => k + 1);
            }}
            className={`c-pressable min-h-11 rounded-full px-5 text-[14px] font-semibold text-white motion-reduce:transition-none ${FOCUS_RING}`}
            style={{ background: "rgb(255 255 255 / 0.18)" }}
          >
            重试
          </button>
        </div>
      );
    }
    return (
      <div className="absolute inset-0 grid place-items-center">
        <video
          key={retryKey}
          src={item.url}
          controls
          autoPlay
          playsInline
          onError={() => setVideoFailed(true)}
          className="max-h-full max-w-full"
        />
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

function MonospaceSourceView({
  text,
  wrap,
  showLineNumbers,
}: {
  text: string;
  wrap: boolean;
  showLineNumbers: boolean;
}) {
  const lines = useMemo(() => text.split("\n"), [text]);
  return (
    <div
      className="c-card mx-auto max-w-3xl overflow-auto rounded-2xl p-4 font-mono text-[13px] leading-relaxed"
      data-testid="viewer-code-body"
      data-wrapped={wrap ? "true" : "false"}
    >
      <pre className={`m-0 ${wrap ? "whitespace-pre-wrap break-words" : "whitespace-pre"}`}>
        <code>
          {showLineNumbers
            ? lines.map((line, idx) => (
                <span key={idx} className={`flex ${wrap ? "min-w-0" : "min-w-max"}`}>
                  <span
                    aria-hidden="true"
                    className="c-muted w-10 shrink-0 select-none pr-3 text-right text-[12px] opacity-60 tabular-nums"
                  >
                    {idx + 1}
                  </span>
                  <span className={wrap ? "min-w-0 flex-1 whitespace-pre-wrap break-words" : "whitespace-pre"}>
                    {line || " "}
                  </span>
                  {idx < lines.length - 1 ? "\n" : null}
                </span>
              ))
            : text}
        </code>
      </pre>
    </div>
  );
}

function DocumentBody({ item }: { item: ViewerItem }) {
  const kind = fileKind(item.name);
  const previewable = isPreviewableDocument(item.name);
  const pdf = isPdfFile(item.name);
  const isDark = useThemeValue() === "dark";
  const hasDualMode = kind === "web" || kind === "svg" || kind === "mermaid" || kind === "markdown" || kind === "table";

  const [text, setText] = useState<string | null>(null);
  const [failed, setFailed] = useState(false);
  const [attempt, setAttempt] = useState(0);
  const [mode, setMode] = useState<"preview" | "source">("preview");
  const [wrap, setWrap] = useState(true);
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    setMode("preview");
    setCopied(false);
  }, [item.key, item.url]);

  useEffect(() => {
    if (!previewable) return;
    let cancelled = false;
    setText(null);
    setFailed(false);
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
  }, [attempt, item.url, previewable]);

  const handleCopy = useCallback(() => {
    if (text === null) return;
    void copyTextToClipboard(text).then((ok) => {
      if (!ok) return;
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1_500);
    });
  }, [text]);

  const htmlSrcDoc = useMemo(
    () => (kind === "web" && text !== null ? artifactHtmlDocument(text, isDark) : ""),
    [kind, text, isDark],
  );
  const svgSrcDoc = useMemo(
    () => (kind === "svg" && text !== null ? svgPageDocument(text, isDark) : ""),
    [kind, text, isDark],
  );
  const tableRows = useMemo(
    () => (kind === "table" && text !== null ? csvRows(text, fileEnding(item.name) === "tsv" ? "\t" : ",", 200, 30) : []),
    [kind, item.name, text],
  );

  if (previewable && !failed) {
    if (text === null) {
      return (
        <div className="flex flex-col items-center justify-center gap-2 pt-12 text-center" role="status">
          <Loader2 className="c-muted h-5 w-5 animate-spin motion-reduce:animate-none" aria-hidden="true" />
          <p className="c-muted text-sm">正在打开…</p>
        </div>
      );
    }

    const lineCount = text.split("\n").length;
    const showSource = !hasDualMode || mode === "source";

    return (
      <div className="absolute inset-0 flex flex-col overflow-hidden">
        <div className="mx-auto flex w-full max-w-3xl shrink-0 flex-wrap items-center justify-between gap-2 px-4 pb-2.5">
          <div className="flex items-center gap-2">
            <span
              className="rounded-full px-2.5 py-0.5 font-mono text-[11px] font-semibold"
              style={{ background: "var(--c-fill)", color: "var(--c-text-muted)" }}
            >
              {fileBadge(item.name)}
            </span>
            <span className="c-muted text-[12px] tabular-nums">{lineCount} 行</span>
          </div>

          <div className="flex items-center gap-1.5">
            {hasDualMode && (
              <div
                role="tablist"
                aria-label="预览模式切换"
                className="inline-flex items-center rounded-full p-0.5"
                style={{ background: "var(--c-segment)" }}
              >
                <button
                  type="button"
                  role="tab"
                  aria-selected={mode === "preview"}
                  onClick={() => setMode("preview")}
                  className={`inline-flex min-h-8 items-center gap-1 rounded-full px-3 text-[12.5px] font-medium ${FOCUS_RING}`}
                  style={mode === "preview" ? { background: "var(--c-segment-on)", color: "var(--c-text)" } : undefined}
                >
                  {kind === "table" ? <Table2 className="h-3.5 w-3.5" aria-hidden="true" /> : <Eye className="h-3.5 w-3.5" aria-hidden="true" />}
                  {kind === "table" ? "表格" : "预览"}
                </button>
                <button
                  type="button"
                  role="tab"
                  aria-selected={mode === "source"}
                  onClick={() => setMode("source")}
                  className={`inline-flex min-h-8 items-center gap-1 rounded-full px-3 text-[12.5px] font-medium ${FOCUS_RING}`}
                  style={mode === "source" ? { background: "var(--c-segment-on)", color: "var(--c-text)" } : undefined}
                >
                  <Code2 className="h-3.5 w-3.5" aria-hidden="true" />
                  源码
                </button>
              </div>
            )}

            {showSource && (
              <button
                type="button"
                aria-pressed={wrap}
                onClick={() => setWrap((v) => !v)}
                className={`c-pressable inline-flex min-h-8 items-center gap-1 rounded-full px-2.5 text-[12px] font-medium ${FOCUS_RING}`}
                style={{ background: wrap ? "var(--c-fill)" : "transparent" }}
              >
                <WrapText className="h-3.5 w-3.5" aria-hidden="true" />
                自动换行
              </button>
            )}

            <button
              type="button"
              onClick={handleCopy}
              className={`c-pressable inline-flex min-h-8 items-center gap-1 rounded-full px-3 text-[12.5px] font-medium ${FOCUS_RING}`}
              style={{ background: "var(--c-fill)" }}
            >
              {copied ? <Check className="h-3.5 w-3.5" aria-hidden="true" /> : <Copy className="h-3.5 w-3.5" aria-hidden="true" />}
              {copied ? "已复制" : "复制"}
            </button>
          </div>
        </div>

        <div className="min-h-0 flex-1 overflow-y-auto overscroll-contain px-4 pb-6">
          {!showSource && kind === "web" ? (
            <div className="mx-auto h-full max-w-4xl">
              <iframe
                title={`预览：${item.title}`}
                sandbox="allow-scripts"
                referrerPolicy="no-referrer"
                srcDoc={htmlSrcDoc}
                className="h-full min-h-[360px] w-full rounded-2xl border border-om-border bg-white"
              />
            </div>
          ) : !showSource && kind === "svg" ? (
            <div className="mx-auto h-full max-w-4xl">
              <iframe
                title={`预览：${item.title}`}
                sandbox=""
                referrerPolicy="no-referrer"
                srcDoc={svgSrcDoc}
                className="h-full min-h-[360px] w-full rounded-2xl border border-om-border bg-transparent"
              />
            </div>
          ) : !showSource && kind === "mermaid" ? (
            <div className="mx-auto max-w-4xl">
              <Suspense
                fallback={
                  <div className="flex min-h-[200px] items-center justify-center gap-2 text-sm c-muted" role="status">
                    <Loader2 className="h-5 w-5 animate-spin motion-reduce:animate-none" aria-hidden="true" />
                    正在加载图表引擎…
                  </div>
                }
              >
                <LazyMermaidDiagram code={text} interactive />
              </Suspense>
            </div>
          ) : !showSource && kind === "markdown" ? (
            <div className="c-bubble-text mx-auto max-w-2xl text-[15px] leading-relaxed">
              <MarkdownText>{text}</MarkdownText>
            </div>
          ) : !showSource && kind === "table" ? (
            <div className="c-card mx-auto max-w-4xl overflow-x-auto rounded-2xl">
              <table className="w-full border-collapse text-left text-[13px]">
                <tbody>
                  {tableRows.map((row, r) => (
                    <tr
                      key={r}
                      className={r === 0 ? "font-semibold" : ""}
                      style={{ borderBottom: "0.5px solid var(--c-border)", background: r === 0 ? "var(--c-fill)" : undefined }}
                    >
                      {row.map((cell, c) => (
                        <td key={c} className="whitespace-nowrap px-3 py-2">
                          {cell}
                        </td>
                      ))}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <MonospaceSourceView text={text} wrap={wrap} showLineNumbers={kind !== "text"} />
          )}
        </div>
      </div>
    );
  }
  return (
    <div
      className="absolute inset-0 flex flex-col items-center justify-center gap-4 px-8 text-center"
      role={previewable && failed ? "alert" : undefined}
    >
      <span className="c-primary grid h-20 w-20 place-items-center rounded-3xl" aria-hidden="true">
        {previewable && failed ? <AlertCircle className="h-9 w-9" /> : <FileText className="h-9 w-9" />}
      </span>
      <p className="max-w-full break-all text-[16px] font-medium">{item.name}</p>
      <p className="c-muted max-w-xs text-[13px] leading-relaxed">
        {previewable && failed
          ? "文件预览加载失败或体积过大，可以重试或用其他应用打开。"
          : pdf
            ? "PDF 文件暂不支持页内直接预览，可保存到本地或用其他应用打开。"
            : "当前格式暂不支持页内直接预览，可用其他应用打开查看。"}
      </p>
      <div className="flex flex-wrap items-center justify-center gap-2">
        {previewable && failed && (
          <button
            type="button"
            onClick={() => setAttempt((n) => n + 1)}
            className={`c-primary min-h-11 rounded-full px-5 text-[15px] font-semibold motion-reduce:transition-none ${FOCUS_RING}`}
          >
            重试
          </button>
        )}
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
          className={`c-card c-pressable inline-flex min-h-11 items-center gap-2 rounded-full px-4 text-[15px] motion-reduce:transition-none ${FOCUS_RING}`}
        >
          <ExternalLink className="h-4 w-4" aria-hidden="true" />
          用其他应用打开
        </a>
      </div>
    </div>
  );
}
