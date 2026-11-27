import { lazy, Suspense, useCallback, useEffect, useMemo, useRef, useState, type KeyboardEvent } from "react";
import { createPortal } from "react-dom";
import {
  AlertCircle,
  BookmarkPlus,
  Check,
  Code2,
  Columns2,
  Copy,
  Download,
  Eye,
  Loader2,
  RotateCcw,
  Sparkles,
  WrapText,
  X,
} from "lucide-react";

import { MarkdownText } from "@/components/MarkdownText";
import { useMediaQuery } from "@/hooks/useMediaQuery";
import { useThemeValue } from "@/hooks/useTheme";
import { copyTextToClipboard } from "@/lib/clipboard";
import { cn } from "@/lib/utils";
import { useClient } from "@/providers/ClientProvider";

import { saveCreationArtifact } from "../companion-api";
import {
  artifactFormatForLanguage,
  artifactHtmlDocument,
  downloadFilenameForCode,
  downloadTextFile,
  svgPageDocument,
} from "../fileKinds";
import { useBackKey, useDialogFocus } from "../Screen";
import { useArtifact, type ArtifactItem, type ArtifactType } from "./ArtifactContext";

const LazyMermaidDiagram = lazy(() => import("@/components/MermaidDiagram"));

const TYPE_BADGE: Record<ArtifactType, string> = {
  html: "HTML",
  svg: "SVG",
  mermaid: "Mermaid",
  markdown: "Markdown",
  code: "代码",
};

const PREVIEW_DEBOUNCE_MS = 250;

type PaneMode = "preview" | "edit" | "split";

type SaveState =
  | { status: "idle" }
  | { status: "saving" }
  | { status: "saved"; savedId: string; updated: boolean }
  | { status: "error"; message: string };

/**
 * Lazy-loaded interactive Artifact / Canvas pane.
 * - Desktop (`>= 1024px`): soft rounded side-pane next to the chat column (`>= 1280px` adds split edit/preview).
 * - Mobile / narrow (`< 1024px`): full-screen slide-up sheet overlay.
 */
export function ArtifactPane() {
  const ctx = useArtifact();
  const artifact = ctx?.artifact ?? null;
  if (!ctx || !artifact) return null;
  return (
    <ArtifactPaneContent
      artifact={artifact}
      onClose={ctx.closeArtifact}
      onRefine={ctx.refineArtifact}
    />
  );
}

/** Zero-dependency monospace live code editor with synchronized line numbers and 2-space Tab indentation. */
function LiveCodeEditor({
  filename,
  value,
  onChange,
  wrap,
  onToggleWrap,
}: {
  filename: string;
  value: string;
  onChange: (next: string) => void;
  wrap: boolean;
  onToggleWrap: () => void;
}) {
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const gutterRef = useRef<HTMLDivElement>(null);
  const lineCount = useMemo(() => Math.max(1, value.split("\n").length), [value]);

  const handleKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key !== "Tab") return;
    event.preventDefault();
    const box = event.currentTarget;
    const start = box.selectionStart ?? 0;
    const end = box.selectionEnd ?? 0;

    if (event.shiftKey) {
      const lineStart = value.lastIndexOf("\n", Math.max(0, start - 1)) + 1;
      if (value.slice(lineStart, lineStart + 2) === "  ") {
        const next = value.slice(0, lineStart) + value.slice(lineStart + 2);
        onChange(next);
        const nextStart = Math.max(lineStart, start - 2);
        const nextEnd = Math.max(lineStart, end - 2);
        window.requestAnimationFrame(() => {
          textareaRef.current?.setSelectionRange(nextStart, nextEnd);
        });
      }
      return;
    }

    const next = `${value.slice(0, start)}  ${value.slice(end)}`;
    onChange(next);
    const caret = start + 2;
    window.requestAnimationFrame(() => {
      textareaRef.current?.setSelectionRange(caret, caret);
    });
  };

  return (
    <div
      className="flex h-full min-h-0 flex-col overflow-hidden rounded-2xl border border-om-border"
      style={{ background: "var(--c-surface)" }}
    >
      <div className="flex shrink-0 items-center justify-between gap-2 border-b border-om-border px-3 py-1.5 text-[12px]">
        <span className="c-muted truncate font-mono">{filename}</span>
        <div className="flex shrink-0 items-center gap-2">
          <span className="c-muted hidden text-[11px] tabular-nums sm:inline">
            {lineCount} 行 · {value.length} 字
          </span>
          <button
            type="button"
            aria-pressed={wrap}
            onClick={onToggleWrap}
            className={cn(
              "c-pressable inline-flex items-center gap-1 rounded-full px-2.5 py-1 text-[11.5px] font-medium",
              wrap ? "text-om-fg" : "c-muted",
            )}
            style={wrap ? { background: "var(--c-fill)" } : undefined}
          >
            <WrapText className="h-3.5 w-3.5" aria-hidden="true" />
            自动换行
          </button>
        </div>
      </div>

      <div className="relative flex min-h-0 flex-1 overflow-hidden">
        <div
          ref={gutterRef}
          aria-hidden="true"
          data-testid="artifact-line-numbers"
          className="c-muted shrink-0 select-none overflow-hidden border-r border-om-border px-2.5 py-3 text-right font-mono text-[12px] leading-[1.6] tabular-nums"
          style={{ background: "color-mix(in srgb, var(--c-fill) 45%, transparent)", minWidth: "2.75rem" }}
        >
          {Array.from({ length: lineCount }, (_, i) => (
            <div key={i + 1}>{i + 1}</div>
          ))}
        </div>
        <textarea
          ref={textareaRef}
          value={value}
          onChange={(event) => onChange(event.target.value)}
          onKeyDown={handleKeyDown}
          onScroll={(event) => {
            if (gutterRef.current) {
              gutterRef.current.scrollTop = event.currentTarget.scrollTop;
            }
          }}
          spellCheck={false}
          autoCapitalize="off"
          autoCorrect="off"
          aria-label="编辑画布源码"
          data-testid="artifact-code-editor"
          className={cn(
            "min-h-0 flex-1 resize-none bg-transparent px-3 py-3 font-mono text-[12.5px] leading-[1.6] text-om-fg outline-none",
            wrap ? "whitespace-pre-wrap break-words" : "whitespace-pre overflow-x-auto",
          )}
        />
      </div>
    </div>
  );
}

function ArtifactPreviewStage({
  type,
  title,
  code,
  isDark,
}: {
  type: ArtifactType;
  title: string;
  code: string;
  isDark: boolean;
}) {
  const htmlSrcDoc = useMemo(
    () => (type === "html" ? artifactHtmlDocument(code, isDark) : ""),
    [type, code, isDark],
  );
  const svgSrcDoc = useMemo(
    () => (type === "svg" ? svgPageDocument(code, isDark) : ""),
    [type, code, isDark],
  );

  if (type === "html") {
    return (
      <iframe
        title={`HTML 预览：${title}`}
        sandbox="allow-scripts"
        referrerPolicy="no-referrer"
        srcDoc={htmlSrcDoc}
        className="h-full w-full rounded-2xl border border-om-border bg-white"
      />
    );
  }
  if (type === "svg") {
    return (
      <iframe
        title={`SVG 预览：${title}`}
        sandbox=""
        referrerPolicy="no-referrer"
        srcDoc={svgSrcDoc}
        className="h-full w-full rounded-2xl border border-om-border bg-transparent"
      />
    );
  }
  if (type === "mermaid") {
    return (
      <Suspense
        fallback={
          <div className="flex h-full items-center justify-center gap-2 text-sm text-om-muted" role="status">
            <Loader2 className="h-5 w-5 animate-spin motion-reduce:animate-none" aria-hidden="true" />
            正在加载图表引擎…
          </div>
        }
      >
        <LazyMermaidDiagram code={code} interactive className="h-full" />
      </Suspense>
    );
  }
  return (
    <div
      className="c-bubble-text h-full overflow-y-auto overscroll-contain rounded-2xl border border-om-border px-5 py-4"
      style={{ background: "var(--c-surface)" }}
    >
      <MarkdownText>{code}</MarkdownText>
    </div>
  );
}

function ArtifactPaneContent({
  artifact,
  onClose,
  onRefine,
}: {
  artifact: ArtifactItem;
  onClose: () => void;
  onRefine: (item: ArtifactItem, editedCode?: string) => void;
}) {
  const { client } = useClient();
  const isDesktop = useMediaQuery("(min-width: 1024px)", false);
  const isWideDesktop = useMediaQuery("(min-width: 1280px)", false);
  const isDark = useThemeValue() === "dark";
  const panelRef = useRef<HTMLDivElement>(null);

  const canPreview = artifact.type !== "code";
  const [mode, setMode] = useState<PaneMode>(() =>
    canPreview ? (artifact.initialTab === "source" ? "edit" : "preview") : "edit",
  );
  const [draftCode, setDraftCode] = useState(artifact.code);
  const [debouncedCode, setDebouncedCode] = useState(artifact.code);
  const [savedId, setSavedId] = useState<string | null>(null);
  const [wrapSource, setWrapSource] = useState(true);
  const [copied, setCopied] = useState(false);
  const [saveState, setSaveState] = useState<SaveState>({ status: "idle" });

  useBackKey(onClose);
  useDialogFocus(panelRef, !isDesktop);

  useEffect(() => {
    setMode(artifact.type !== "code" ? (artifact.initialTab === "source" ? "edit" : "preview") : "edit");
    setDraftCode(artifact.code);
    setDebouncedCode(artifact.code);
    setSavedId(null);
    setCopied(false);
    setSaveState({ status: "idle" });
  }, [artifact.id, artifact.code, artifact.type, artifact.initialTab]);

  useEffect(() => {
    if (!isWideDesktop && mode === "split") {
      setMode("preview");
    }
  }, [isWideDesktop, mode]);

  useEffect(() => {
    if (draftCode === debouncedCode) return;
    const timer = window.setTimeout(() => {
      setDebouncedCode(draftCode);
    }, PREVIEW_DEBOUNCE_MS);
    return () => window.clearTimeout(timer);
  }, [draftCode, debouncedCode]);

  const isDirty = draftCode !== artifact.code;

  const handleCodeChange = useCallback((next: string) => {
    setDraftCode(next);
    setSaveState((prev) => (prev.status === "saved" || prev.status === "error" ? { status: "idle" } : prev));
  }, []);

  const handleRevert = useCallback(() => {
    setDraftCode(artifact.code);
    setDebouncedCode(artifact.code);
    setSaveState((prev) => (prev.status === "saved" || prev.status === "error" ? { status: "idle" } : prev));
  }, [artifact.code]);

  const handleCopy = useCallback(() => {
    void copyTextToClipboard(draftCode).then((ok) => {
      if (!ok) return;
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1_500);
    });
  }, [draftCode]);

  const handleDownload = useCallback(() => {
    const filename = downloadFilenameForCode(artifact.language, artifact.title);
    const mime =
      artifact.type === "html"
        ? "text/html;charset=utf-8"
        : artifact.type === "svg"
          ? "image/svg+xml;charset=utf-8"
          : artifact.type === "markdown"
            ? "text/markdown;charset=utf-8"
            : "text/plain;charset=utf-8";
    downloadTextFile(draftCode, filename, mime);
  }, [artifact.language, artifact.title, artifact.type, draftCode]);

  const handleSave = useCallback(async () => {
    if (saveState.status === "saving") return;
    setSaveState({ status: "saving" });
    try {
      const res = await saveCreationArtifact(client, {
        ...(savedId ? { id: savedId } : {}),
        title: artifact.title,
        content: draftCode,
        format: artifactFormatForLanguage(artifact.language || artifact.type),
      });
      const nextId = res.saved_id || savedId || "";
      setSavedId(nextId || null);
      setSaveState({
        status: "saved",
        savedId: nextId,
        updated: Boolean(res.updated ?? savedId),
      });
    } catch (err: unknown) {
      setSaveState({
        status: "error",
        message: err instanceof Error ? err.message : String(err),
      });
    }
  }, [artifact.language, artifact.title, artifact.type, client, draftCode, saveState.status, savedId]);

  const handleRefine = useCallback(() => {
    onRefine(artifact, isDirty ? draftCode : undefined);
    if (!isDesktop) onClose();
  }, [artifact, draftCode, isDesktop, isDirty, onClose, onRefine]);

  const badgeText = artifact.type === "code" ? (artifact.language || "TEXT").toUpperCase() : TYPE_BADGE[artifact.type];
  const filename = downloadFilenameForCode(artifact.language, artifact.title);
  const effectiveMode: PaneMode = !canPreview ? "edit" : mode === "split" && !isWideDesktop ? "preview" : mode;

  const saveLabel =
    saveState.status === "saving"
      ? "保存中…"
      : saveState.status === "saved"
        ? saveState.updated
          ? "已更新资源库"
          : "已存入资源库"
        : savedId
          ? "更新资源库"
          : "存入资源库";

  const inner = (
    <div
      ref={panelRef}
      tabIndex={isDesktop ? undefined : -1}
      role={isDesktop ? "complementary" : "dialog"}
      aria-modal={isDesktop ? undefined : true}
      aria-label={`画布：${artifact.title}`}
      data-testid={isDesktop ? "artifact-pane-desktop" : "artifact-pane-sheet"}
      className={cn(
        "c-glass-panel flex min-h-0 flex-col overflow-hidden border border-om-border text-om-fg outline-none",
        isDesktop
          ? "h-full w-full rounded-[24px] shadow-sm"
          : "sheet-in relative h-[94dvh] w-full rounded-t-[28px] shadow-2xl",
      )}
    >
      {/* Header */}
      <header
        className="flex shrink-0 items-center gap-2 border-b border-om-border px-4 py-3"
        style={!isDesktop ? { paddingTop: "max(0.75rem, env(safe-area-inset-top))" } : undefined}
      >
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <span
              className="shrink-0 rounded-full px-2 py-0.5 text-[11px] font-semibold tracking-wide"
              style={{ background: "var(--c-accent-soft)", color: "var(--c-primary)" }}
            >
              {badgeText}
            </span>
            <h2 className="truncate text-[15px] font-semibold leading-tight">{artifact.title}</h2>
            {isDirty && (
              <span
                data-testid="artifact-dirty-badge"
                className="shrink-0 rounded-full px-2 py-0.5 text-[11px] font-medium"
                style={{ background: "var(--c-fill)", color: "var(--c-primary)" }}
              >
                已修改
              </span>
            )}
          </div>
        </div>

        {isDirty && (
          <button
            type="button"
            onClick={handleRevert}
            className="c-pressable inline-flex min-h-8 shrink-0 items-center gap-1 rounded-full px-2.5 text-[12px] font-medium"
            style={{ background: "var(--c-fill)" }}
          >
            <RotateCcw className="h-3.5 w-3.5" aria-hidden="true" />
            还原
          </button>
        )}

        {canPreview && (
          <div
            role="tablist"
            aria-label="画布视图切换"
            className="inline-flex shrink-0 items-center rounded-full p-0.5"
            style={{ background: "var(--c-segment)" }}
          >
            <button
              type="button"
              role="tab"
              aria-selected={effectiveMode === "preview"}
              onClick={() => setMode("preview")}
              className={cn(
                "inline-flex min-h-8 items-center gap-1 rounded-full px-3 text-[12.5px] font-medium transition-colors",
                effectiveMode === "preview" ? "shadow-sm" : "c-muted",
              )}
              style={effectiveMode === "preview" ? { background: "var(--c-segment-on)", color: "var(--c-text)" } : undefined}
            >
              <Eye className="h-3.5 w-3.5" aria-hidden="true" />
              预览
            </button>
            <button
              type="button"
              role="tab"
              aria-selected={effectiveMode === "edit"}
              onClick={() => setMode("edit")}
              className={cn(
                "inline-flex min-h-8 items-center gap-1 rounded-full px-3 text-[12.5px] font-medium transition-colors",
                effectiveMode === "edit" ? "shadow-sm" : "c-muted",
              )}
              style={effectiveMode === "edit" ? { background: "var(--c-segment-on)", color: "var(--c-text)" } : undefined}
            >
              <Code2 className="h-3.5 w-3.5" aria-hidden="true" />
              编辑
            </button>
            {isWideDesktop && (
              <button
                type="button"
                role="tab"
                aria-selected={effectiveMode === "split"}
                onClick={() => setMode("split")}
                className={cn(
                  "inline-flex min-h-8 items-center gap-1 rounded-full px-3 text-[12.5px] font-medium transition-colors",
                  effectiveMode === "split" ? "shadow-sm" : "c-muted",
                )}
                style={effectiveMode === "split" ? { background: "var(--c-segment-on)", color: "var(--c-text)" } : undefined}
              >
                <Columns2 className="h-3.5 w-3.5" aria-hidden="true" />
                分屏
              </button>
            )}
          </div>
        )}

        <button
          type="button"
          onClick={onClose}
          aria-label="关闭画布"
          className="c-pressable grid h-9 w-9 shrink-0 place-items-center rounded-full text-om-muted hover:text-om-fg"
        >
          <X className="h-5 w-5" aria-hidden="true" />
        </button>
      </header>

      {/* Canvas Body */}
      <div className="relative min-h-0 flex-1 overflow-hidden p-3">
        {effectiveMode === "split" && canPreview ? (
          <div data-testid="artifact-split-view" className="grid h-full min-h-0 grid-cols-2 gap-3">
            <LiveCodeEditor
              filename={filename}
              value={draftCode}
              onChange={handleCodeChange}
              wrap={wrapSource}
              onToggleWrap={() => setWrapSource((v) => !v)}
            />
            <div className="min-h-0 h-full overflow-hidden">
              <ArtifactPreviewStage
                type={artifact.type}
                title={artifact.title}
                code={debouncedCode}
                isDark={isDark}
              />
            </div>
          </div>
        ) : effectiveMode === "preview" && canPreview ? (
          <ArtifactPreviewStage
            type={artifact.type}
            title={artifact.title}
            code={debouncedCode}
            isDark={isDark}
          />
        ) : (
          <LiveCodeEditor
            filename={filename}
            value={draftCode}
            onChange={handleCodeChange}
            wrap={wrapSource}
            onToggleWrap={() => setWrapSource((v) => !v)}
          />
        )}
      </div>

      {/* Feedback Banner (save error) */}
      {saveState.status === "error" && (
        <div
          role="alert"
          className="mx-3 mb-2 flex items-center gap-2 rounded-2xl border border-destructive/30 bg-destructive/10 px-3 py-2 text-[12.5px] c-danger"
        >
          <AlertCircle className="h-4 w-4 shrink-0" aria-hidden="true" />
          <span className="min-w-0 flex-1 truncate">保存失败：{saveState.message}</span>
        </div>
      )}

      {/* Footer Actions */}
      <footer
        className="flex shrink-0 flex-wrap items-center gap-2 border-t border-om-border px-3 py-2.5"
        style={!isDesktop ? { paddingBottom: "max(0.75rem, env(safe-area-inset-bottom))" } : undefined}
      >
        <button
          type="button"
          onClick={handleCopy}
          className="c-pressable inline-flex min-h-9 items-center gap-1.5 rounded-full px-3 text-[13px] font-medium"
          style={{ background: "var(--c-fill)" }}
        >
          {copied ? <Check className="h-3.5 w-3.5" aria-hidden="true" /> : <Copy className="h-3.5 w-3.5" aria-hidden="true" />}
          {copied ? "已复制" : "复制源码"}
        </button>

        <button
          type="button"
          onClick={handleDownload}
          className="c-pressable inline-flex min-h-9 items-center gap-1.5 rounded-full px-3 text-[13px] font-medium"
          style={{ background: "var(--c-fill)" }}
        >
          <Download className="h-3.5 w-3.5" aria-hidden="true" />
          下载
        </button>

        <button
          type="button"
          disabled={saveState.status === "saving"}
          onClick={() => void handleSave()}
          className="c-pressable inline-flex min-h-9 items-center gap-1.5 rounded-full px-3 text-[13px] font-medium disabled:opacity-60"
          style={
            saveState.status === "saved"
              ? { background: "var(--c-accent-soft)", color: "var(--c-primary)" }
              : { background: "var(--c-fill)" }
          }
        >
          {saveState.status === "saving" ? (
            <Loader2 className="h-3.5 w-3.5 animate-spin motion-reduce:animate-none" aria-hidden="true" />
          ) : saveState.status === "saved" ? (
            <Check className="h-3.5 w-3.5" aria-hidden="true" />
          ) : (
            <BookmarkPlus className="h-3.5 w-3.5" aria-hidden="true" />
          )}
          {saveLabel}
        </button>

        <button
          type="button"
          onClick={handleRefine}
          className="c-primary ml-auto inline-flex min-h-9 items-center gap-1.5 rounded-full px-3.5 text-[13px] font-semibold"
        >
          <Sparkles className="h-3.5 w-3.5" aria-hidden="true" />
          让伙伴继续完善
        </button>
      </footer>
    </div>
  );

  if (isDesktop) {
    return (
      <div
        className={cn(
          "h-full shrink-0 p-3 pl-0 transition-[width] duration-200",
          effectiveMode === "split" ? "w-[56%] min-w-[540px] max-w-[920px]" : "w-[46%] min-w-[380px] max-w-[680px]",
        )}
      >
        {inner}
      </div>
    );
  }

  return createPortal(
    <div className="companion">
      <div data-sheet className="fixed inset-0 z-50 flex items-end">
        <div className="absolute inset-0 bg-black/45 backdrop-blur-[2px]" onClick={onClose} aria-hidden="true" />
        {inner}
      </div>
    </div>,
    document.body,
  );
}
