import { Suspense, lazy, memo, startTransition, useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { Check, Copy, Download, Eye, GitBranch, Loader2, WrapText } from "lucide-react";
import { useTranslation } from "react-i18next";

import { downloadFilenameForCode, downloadTextFile } from "@/companion/fileKinds";
import {
  isMermaidLanguage,
  isPreviewableArtifactLanguage,
  useArtifact,
  type OpenArtifactInput,
} from "@/companion/muse/ArtifactContext";
import { useThemeValue } from "@/hooks/useTheme";
import { hasAnsi, parseAnsiSegments, stripAnsi } from "@/lib/ansi";
import { copyTextToClipboard } from "@/lib/clipboard";
import { normalizeCodeLanguage } from "@/lib/code-language";
import { cn } from "@/lib/utils";

interface CodeBlockProps {
  language?: string;
  code: string;
  className?: string;
  chrome?: "default" | "none";
  highlight?: boolean;
  showLineNumbers?: boolean;
  wrapLongLines?: boolean;
  viewportHighlight?: boolean;
  onPreview?: (input: OpenArtifactInput) => void;
}

interface HighlightedCodeProps {
  language?: string;
  code: string;
  chrome: "default" | "none";
  showLineNumbers: boolean;
  wrapLongLines: boolean;
  viewportHighlight: boolean;
}

const CODE_FONT_STACK = [
  '"JetBrains Mono"',
  '"SFMono-Regular"',
  '"SF Mono"',
  '"Fira Code"',
  '"Cascadia Code"',
  '"Source Code Pro"',
  "Menlo",
  "Consolas",
  "monospace",
].join(", ");

const ANSI_LANGUAGES = new Set(["ansi", "ansi-output"]);

const LazyMermaidDiagram = lazy(() => import("@/components/MermaidDiagram"));

const LazyHighlightedCode = lazy(async () => {
  const [
    { default: SyntaxHighlighter },
    { ViewportCodeRows },
  ] = await Promise.all([
    import("react-syntax-highlighter/dist/esm/prism-async-light"),
    import("@/components/ViewportCodeRows"),
  ]);

  return {
    default({
      language,
      code,
      chrome,
      showLineNumbers,
      wrapLongLines,
      viewportHighlight,
    }: HighlightedCodeProps) {
      return (
        <SyntaxHighlighter
          language={language || "text"}
          useInlineStyles={false}
          style={{}}
          customStyle={{
            background: "transparent",
            margin: 0,
            padding: chrome === "none" ? "0.75rem 1rem" : "0.875rem 1rem",
            fontFamily: CODE_FONT_STACK,
            fontSize: "13px",
            lineHeight: chrome === "none" ? 1.55 : 1.6,
            tabSize: 2,
            // FilePreviewPanel owns both scroll axes. Replacing this <pre>
            // during progressive highlighting must not reset horizontal scroll.
            ...(viewportHighlight ? { overflow: "visible" } : {}),
          }}
          codeTagProps={{
            style: {
              background: "transparent",
              fontFamily: CODE_FONT_STACK,
              ...(viewportHighlight ? { lineHeight: "inherit" } : {}),
            },
          }}
          lineNumberStyle={{
            minWidth: "2.6em",
            paddingRight: "1.15rem",
            color: "var(--code-comment)",
            fontFamily: CODE_FONT_STACK,
            userSelect: "none",
          }}
          PreTag="pre"
          showLineNumbers={showLineNumbers && !viewportHighlight}
          wrapLongLines={wrapLongLines}
          renderer={viewportHighlight ? props => <ViewportCodeRows {...props} /> : undefined}
        >
          {code}
        </SyntaxHighlighter>
      );
    },
  };
});

function renderPlainText(value: string): ReactNode {
  return value;
}

function renderAnsiText(value: string, theme: "light" | "dark"): ReactNode {
  return parseAnsiSegments(value, theme).map((segment, index) => (
    <span key={index} style={segment.style}>
      {segment.text}
    </span>
  ));
}

function CodeTextBlock({
  code,
  chrome,
  showLineNumbers,
  wrapLongLines,
  testId,
  className,
  renderText = renderPlainText,
  compact = false,
}: {
  code: string;
  chrome: "default" | "none";
  showLineNumbers: boolean;
  wrapLongLines: boolean;
  testId: string;
  className?: string;
  renderText?: (value: string) => ReactNode;
  compact?: boolean;
}) {
  const lines = showLineNumbers ? code.split("\n") : [];
  return (
    <pre
      className={cn(
        "m-0 bg-transparent font-mono text-[13px] text-foreground/90",
        compact ? "overflow-visible" : "overflow-x-auto",
        wrapLongLines ? "whitespace-pre-wrap break-words" : "whitespace-pre",
        chrome === "default"
          ? "px-4 py-3.5 leading-[1.6]"
          : compact ? "px-4 py-3 leading-[1.55]" : "p-3 leading-[1.55]",
        className,
      )}
      data-testid={testId}
      data-wrapped={wrapLongLines ? "true" : "false"}
      style={compact ? { fontFamily: CODE_FONT_STACK } : undefined}
    >
      <code className="text-inherit">
        {showLineNumbers && compact ? (
          <span className="flex min-w-max">
            <span aria-hidden className="shrink-0 select-none pr-[1.15rem] text-right text-muted-foreground"
              style={{ minWidth: "4.25em" }}>{lines.map((_, index) => index + 1).join("\n")}</span>
            <span>{renderText(code)}</span>
          </span>
        ) : showLineNumbers ? (
          lines.map((line, index) => (
            <span key={index} className={cn("flex", wrapLongLines ? "min-w-0" : "min-w-max")}>
              <span className="w-10 shrink-0 select-none pr-4 text-right text-muted-foreground">
                {index + 1}
              </span>
              <span className={wrapLongLines ? "min-w-0 flex-1 whitespace-pre-wrap break-words" : "whitespace-pre"}>
                {renderText(line || " ")}
              </span>
              {index < lines.length - 1 ? "\n" : null}
            </span>
          ))
        ) : renderText(code)}
      </code>
    </pre>
  );
}

function shouldRenderAnsi(language: string | undefined, code: string): boolean {
  const normalized = language?.trim().toLowerCase();
  return Boolean((normalized && ANSI_LANGUAGES.has(normalized)) || hasAnsi(code));
}

export const CodeBlock = memo(function CodeBlock({
  language,
  code,
  className,
  chrome = "default",
  highlight = true,
  showLineNumbers = false,
  wrapLongLines = true,
  viewportHighlight = false,
  onPreview,
}: CodeBlockProps) {
  const { t } = useTranslation();
  const artifactContext = useArtifact();
  const rootRef = useRef<HTMLDivElement>(null);
  const [copied, setCopied] = useState(false);
  const [wrapped, setWrapped] = useState(wrapLongLines);
  const [inlineMermaidOpen, setInlineMermaidOpen] = useState(false);
  const [highlightedSource, setHighlightedSource] = useState<string | null>(null);
  const renderAnsi = useMemo(() => shouldRenderAnsi(language, code), [language, code]);
  const plainCode = useMemo(() => renderAnsi ? stripAnsi(code) : code, [renderAnsi, code]);
  const isDark = useThemeValue() === "dark";
  const hasChrome = chrome === "default";
  const syntaxLanguage = normalizeCodeLanguage(language);
  const previewable = isPreviewableArtifactLanguage(language);
  const isMermaid = isMermaidLanguage(language);
  const effectiveWrap = hasChrome ? wrapped : wrapLongLines;
  const useViewportHighlight = viewportHighlight && showLineNumbers && !effectiveWrap;

  useEffect(() => {
    setWrapped(wrapLongLines);
  }, [wrapLongLines]);

  // Minified/very large previews must not monopolize the main thread in Prism.
  // Plain text still contains the entire preview, with normal selection and find.
  const withinHighlightBudget = useMemo(() => {
    if (!useViewportHighlight) return true;
    if (code.length > 100_000) return false;
    const lines = code.split("\n");
    return lines.length <= 2_000 && !lines.some(line => line.length > 2_000);
  }, [code, useViewportHighlight]);
  const deferHighlight = highlight && !renderAnsi && useViewportHighlight
    && withinHighlightBudget && code.length > 8_000;
  useEffect(() => {
    if (!deferHighlight) return;
    // Paint the tab and full plain source first. Color is a progressive enhancement,
    // not a reason to hold up switching tabs (including an already cached file).
    let readyToEnhance = false;
    let frame = requestAnimationFrame(() => {
      frame = requestAnimationFrame(() => {
        readyToEnhance = true;
        enhance();
      });
    });
    function enhance() {
      // A tab click may clear a selection before the first frame. Only resume
      // selection-blocked work here after the initial paint opportunity.
      if (!readyToEnhance) return;
      const selection = document.getSelection();
      if (rootRef.current && selection && !selection.isCollapsed && selection.rangeCount
        && selection.getRangeAt(0).intersectsNode(rootRef.current)) return;
      document.removeEventListener("selectionchange", enhance);
      startTransition(() => setHighlightedSource(code));
    }
    document.addEventListener("selectionchange", enhance);
    return () => {
      cancelAnimationFrame(frame);
      document.removeEventListener("selectionchange", enhance);
    };
  }, [code, deferHighlight]);
  const copyLabel = copied ? t("code.copied") : t("code.copyAria");

  const onCopy = useCallback(() => {
    void copyTextToClipboard(plainCode).then((ok) => {
      if (!ok) return;
      setCopied(true);
      setTimeout(() => setCopied(false), 1_500);
    });
  }, [plainCode]);

  const onDownload = useCallback(() => {
    const filename = downloadFilenameForCode(language);
    downloadTextFile(plainCode, filename);
  }, [language, plainCode]);

  const onOpenPreview = useCallback(() => {
    const payload: OpenArtifactInput = { language, code: plainCode };
    if (onPreview) {
      onPreview(payload);
    } else if (artifactContext) {
      artifactContext.openArtifact(payload);
    } else if (isMermaid) {
      setInlineMermaidOpen(true);
    }
  }, [artifactContext, isMermaid, language, onPreview, plainCode]);

  const toolbarBtnClass = cn(
    "inline-flex h-7 items-center gap-1 rounded-md px-2 text-[11.5px] font-medium",
    "text-muted-foreground transition-colors hover:bg-muted/70 hover:text-foreground",
    "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60",
  );

  return (
    <div
      ref={rootRef}
      className={cn(
        "not-prose relative code-content",
        useViewportHighlight ? "overflow-visible" : "overflow-hidden",
        hasChrome && "rounded-floating bg-secondary/70",
        className,
      )}
      data-language={language || t("code.fallbackLanguage")}
      data-wrapped={effectiveWrap ? "true" : "false"}
    >
      {hasChrome ? (
        <div
          className="flex flex-wrap items-center justify-between gap-1.5 border-b border-border/50 px-3 py-1.5"
          role="toolbar"
          aria-label="代码块工具栏"
        >
          <span className="font-mono text-[11.5px] font-medium uppercase tracking-wide text-muted-foreground">
            {language || t("code.fallbackLanguage")}
          </span>
          <div className="flex flex-wrap items-center gap-1">
            {isMermaid && (
              <button
                type="button"
                onClick={() => setInlineMermaidOpen((open) => !open)}
                aria-expanded={inlineMermaidOpen}
                aria-label={inlineMermaidOpen ? "收起内联图表" : "展开内联图表"}
                title={inlineMermaidOpen ? "收起图表" : "消息内预览图表"}
                className={cn(toolbarBtnClass, inlineMermaidOpen && "bg-muted text-foreground")}
              >
                <GitBranch className="h-3.5 w-3.5" aria-hidden />
                <span>{inlineMermaidOpen ? "收起图表" : "图表"}</span>
              </button>
            )}
            {previewable && (
              <button
                type="button"
                onClick={onOpenPreview}
                aria-label="在画布中预览"
                title="预览"
                className={cn(toolbarBtnClass, "text-foreground/90")}
              >
                <Eye className="h-3.5 w-3.5" aria-hidden />
                <span>预览</span>
              </button>
            )}
            <button
              type="button"
              onClick={() => setWrapped((v) => !v)}
              aria-pressed={wrapped}
              aria-label={wrapped ? "关闭自动换行" : "开启自动换行"}
              title="自动换行"
              className={cn(toolbarBtnClass, wrapped && "bg-muted/80 text-foreground")}
            >
              <WrapText className="h-3.5 w-3.5" aria-hidden />
              <span className="hidden sm:inline">自动换行</span>
            </button>
            <button
              type="button"
              onClick={onDownload}
              aria-label="下载文件"
              title={`下载 ${downloadFilenameForCode(language)}`}
              className={toolbarBtnClass}
            >
              <Download className="h-3.5 w-3.5" aria-hidden />
              <span className="hidden sm:inline">下载</span>
            </button>
            <button
              type="button"
              onClick={onCopy}
              className={toolbarBtnClass}
              aria-label={copyLabel}
              title={copyLabel}
            >
              {copied ? (
                <Check className="h-3.5 w-3.5" aria-hidden />
              ) : (
                <Copy className="h-3.5 w-3.5" aria-hidden />
              )}
              <span className="hidden sm:inline">{copied ? "已复制" : "复制"}</span>
            </button>
          </div>
        </div>
      ) : null}

      {hasChrome && isMermaid && inlineMermaidOpen ? (
        <div className="border-b border-border/50 p-2.5">
          <Suspense
            fallback={
              <div className="flex min-h-[120px] items-center justify-center gap-2 text-xs text-muted-foreground" role="status">
                <Loader2 className="h-4 w-4 animate-spin motion-reduce:animate-none" aria-hidden />
                正在加载 Mermaid 图表…
              </div>
            }
          >
            <LazyMermaidDiagram code={plainCode} interactive />
          </Suspense>
        </div>
      ) : null}

      {renderAnsi ? (
        <CodeTextBlock
          code={code}
          chrome={chrome}
          showLineNumbers={showLineNumbers}
          wrapLongLines={effectiveWrap}
          testId="ansi-code"
          renderText={(value) => renderAnsiText(value, isDark ? "dark" : "light")}
          compact={useViewportHighlight}
        />
      ) : highlight && withinHighlightBudget && (!deferHighlight || highlightedSource === code) ? (
        <Suspense
          fallback={
            <CodeTextBlock
              code={code}
              chrome={chrome}
              showLineNumbers={showLineNumbers}
              wrapLongLines={effectiveWrap}
              testId="plain-code-fallback"
              compact={useViewportHighlight}
            />
          }
        >
          <LazyHighlightedCode
            language={syntaxLanguage}
            code={code}
            chrome={chrome}
            showLineNumbers={showLineNumbers}
            wrapLongLines={effectiveWrap}
            viewportHighlight={useViewportHighlight}
          />
        </Suspense>
      ) : (
        <CodeTextBlock
          code={code}
          chrome={chrome}
          showLineNumbers={showLineNumbers}
          wrapLongLines={effectiveWrap}
          testId="plain-code-fallback"
          compact={useViewportHighlight}
        />
      )}
    </div>
  );
});
