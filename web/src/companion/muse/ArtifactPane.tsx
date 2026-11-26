import { lazy, Suspense, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import {
  AlertCircle,
  BookmarkPlus,
  Check,
  Code2,
  Copy,
  Download,
  Eye,
  Loader2,
  Sparkles,
  WrapText,
  X,
} from "lucide-react";

import { CodeBlock } from "@/components/CodeBlock";
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

type SaveState =
  | { status: "idle" }
  | { status: "saving" }
  | { status: "saved"; savedId: string }
  | { status: "error"; message: string };

/**
 * Lazy-loaded interactive Artifact / Canvas pane.
 * - Desktop (`>= 1024px`): soft rounded side-pane next to the chat column.
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

function ArtifactPaneContent({
  artifact,
  onClose,
  onRefine,
}: {
  artifact: ArtifactItem;
  onClose: () => void;
  onRefine: (item: ArtifactItem) => void;
}) {
  const { client } = useClient();
  const isDesktop = useMediaQuery("(min-width: 1024px)", false);
  const isDark = useThemeValue() === "dark";
  const panelRef = useRef<HTMLDivElement>(null);

  const canPreview = artifact.type !== "code";
  const [mode, setMode] = useState<"preview" | "source">(() =>
    canPreview ? (artifact.initialTab ?? "preview") : "source",
  );
  const [wrapSource, setWrapSource] = useState(true);
  const [copied, setCopied] = useState(false);
  const [saveState, setSaveState] = useState<SaveState>({ status: "idle" });

  useBackKey(onClose);
  useDialogFocus(panelRef, !isDesktop);

  useEffect(() => {
    setMode(artifact.type !== "code" ? (artifact.initialTab ?? "preview") : "source");
    setCopied(false);
    setSaveState({ status: "idle" });
  }, [artifact.id, artifact.code, artifact.type, artifact.initialTab]);

  const htmlSrcDoc = useMemo(
    () => (artifact.type === "html" ? artifactHtmlDocument(artifact.code, isDark) : ""),
    [artifact.type, artifact.code, isDark],
  );

  const svgSrcDoc = useMemo(
    () => (artifact.type === "svg" ? svgPageDocument(artifact.code, isDark) : ""),
    [artifact.type, artifact.code, isDark],
  );

  const handleCopy = useCallback(() => {
    void copyTextToClipboard(artifact.code).then((ok) => {
      if (!ok) return;
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1_500);
    });
  }, [artifact.code]);

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
    downloadTextFile(artifact.code, filename, mime);
  }, [artifact]);

  const handleSave = useCallback(async () => {
    if (saveState.status === "saving") return;
    setSaveState({ status: "saving" });
    try {
      const res = await saveCreationArtifact(client, {
        title: artifact.title,
        content: artifact.code,
        format: artifactFormatForLanguage(artifact.language || artifact.type),
      });
      setSaveState({ status: "saved", savedId: res.saved_id });
    } catch (err: unknown) {
      setSaveState({
        status: "error",
        message: err instanceof Error ? err.message : String(err),
      });
    }
  }, [artifact, client, saveState.status]);

  const handleRefine = useCallback(() => {
    onRefine(artifact);
    if (!isDesktop) onClose();
  }, [artifact, isDesktop, onClose, onRefine]);

  const badgeText = artifact.type === "code" ? (artifact.language || "TEXT").toUpperCase() : TYPE_BADGE[artifact.type];

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
          </div>
        </div>

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
              aria-selected={mode === "preview"}
              onClick={() => setMode("preview")}
              className={cn(
                "inline-flex min-h-8 items-center gap-1 rounded-full px-3 text-[12.5px] font-medium transition-colors",
                mode === "preview" ? "shadow-sm" : "c-muted",
              )}
              style={mode === "preview" ? { background: "var(--c-segment-on)", color: "var(--c-text)" } : undefined}
            >
              <Eye className="h-3.5 w-3.5" aria-hidden="true" />
              预览
            </button>
            <button
              type="button"
              role="tab"
              aria-selected={mode === "source"}
              onClick={() => setMode("source")}
              className={cn(
                "inline-flex min-h-8 items-center gap-1 rounded-full px-3 text-[12.5px] font-medium transition-colors",
                mode === "source" ? "shadow-sm" : "c-muted",
              )}
              style={mode === "source" ? { background: "var(--c-segment-on)", color: "var(--c-text)" } : undefined}
            >
              <Code2 className="h-3.5 w-3.5" aria-hidden="true" />
              源码
            </button>
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
        {mode === "preview" && canPreview ? (
          artifact.type === "html" ? (
            <iframe
              title={`HTML 预览：${artifact.title}`}
              sandbox="allow-scripts"
              referrerPolicy="no-referrer"
              srcDoc={htmlSrcDoc}
              className="h-full w-full rounded-2xl border border-om-border bg-white"
            />
          ) : artifact.type === "svg" ? (
            <iframe
              title={`SVG 预览：${artifact.title}`}
              sandbox=""
              referrerPolicy="no-referrer"
              srcDoc={svgSrcDoc}
              className="h-full w-full rounded-2xl border border-om-border bg-transparent"
            />
          ) : artifact.type === "mermaid" ? (
            <Suspense
              fallback={
                <div className="flex h-full items-center justify-center gap-2 text-sm text-om-muted" role="status">
                  <Loader2 className="h-5 w-5 animate-spin motion-reduce:animate-none" aria-hidden="true" />
                  正在加载图表引擎…
                </div>
              }
            >
              <LazyMermaidDiagram code={artifact.code} interactive className="h-full" />
            </Suspense>
          ) : (
            <div
              className="c-bubble-text h-full overflow-y-auto overscroll-contain rounded-2xl border border-om-border px-5 py-4"
              style={{ background: "var(--c-surface)" }}
            >
              <MarkdownText>{artifact.code}</MarkdownText>
            </div>
          )
        ) : (
          <div
            className="flex h-full flex-col overflow-hidden rounded-2xl border border-om-border"
            style={{ background: "var(--c-surface)" }}
          >
            <div className="flex shrink-0 items-center justify-between border-b border-om-border px-3 py-1.5 text-[12px]">
              <span className="c-muted font-mono">{downloadFilenameForCode(artifact.language, artifact.title)}</span>
              <button
                type="button"
                aria-pressed={wrapSource}
                onClick={() => setWrapSource((v) => !v)}
                className={cn(
                  "c-pressable inline-flex items-center gap-1 rounded-full px-2.5 py-1 text-[11.5px] font-medium",
                  wrapSource ? "text-om-fg" : "c-muted",
                )}
                style={wrapSource ? { background: "var(--c-fill)" } : undefined}
              >
                <WrapText className="h-3.5 w-3.5" aria-hidden="true" />
                自动换行
              </button>
            </div>
            <div className="min-h-0 flex-1 overflow-auto">
              <CodeBlock
                language={artifact.language}
                code={artifact.code}
                chrome="none"
                showLineNumbers
                wrapLongLines={wrapSource}
              />
            </div>
          </div>
        )}
      </div>

      {/* Feedback Banner (save error or success) */}
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
          {saveState.status === "saving"
            ? "保存中…"
            : saveState.status === "saved"
              ? "已存入资源库"
              : "存入资源库"}
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
      <div className="h-full w-[46%] min-w-[380px] max-w-[680px] shrink-0 p-3 pl-0">
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
