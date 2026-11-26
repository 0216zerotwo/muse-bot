import { useEffect, useId, useMemo, useState } from "react";
import { AlertCircle, Loader2, Maximize2, ZoomIn, ZoomOut } from "lucide-react";

import { sanitizeSvgMarkup } from "@/companion/fileKinds";
import { useThemeValue } from "@/hooks/useTheme";
import { cn } from "@/lib/utils";

export const MERMAID_MIN_ZOOM = 0.5;
export const MERMAID_MAX_ZOOM = 2.5;
export const MERMAID_ZOOM_STEP = 0.25;

interface MermaidModule {
  initialize: (config: Record<string, unknown>) => void;
  render: (id: string, text: string) => Promise<{ svg: string }>;
}

let mermaidLoaderPromise: Promise<MermaidModule> | null = null;

function loadMermaid(): Promise<MermaidModule> {
  if (!mermaidLoaderPromise) {
    mermaidLoaderPromise = import("mermaid")
      .then((mod) => {
        const candidate = (mod as { default?: MermaidModule }).default ?? (mod as unknown as MermaidModule);
        return candidate;
      })
      .catch((err) => {
        mermaidLoaderPromise = null;
        throw err;
      });
  }
  return mermaidLoaderPromise;
}

function clampMermaidZoom(value: number): number {
  return Math.max(MERMAID_MIN_ZOOM, Math.min(MERMAID_MAX_ZOOM, Math.round(value * 100) / 100));
}

export interface MermaidDiagramProps {
  code: string;
  /** Show zoom / fit-to-window toolbar controls. */
  interactive?: boolean;
  className?: string;
}

/**
 * Lazy-loaded Mermaid diagram renderer: dynamically imports `mermaid` only when rendered,
 * enforces `securityLevel: "strict"` + SVG sanitization, and provides zoom/fit controls.
 */
export default function MermaidDiagram({ code, interactive = true, className }: MermaidDiagramProps) {
  const isDark = useThemeValue() === "dark";
  const rawId = useId();
  const safeDomId = useMemo(() => `muse-mmd-${rawId.replace(/[^a-zA-Z0-9_-]/g, "")}`, [rawId]);
  const [svg, setSvg] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [zoom, setZoom] = useState(1);
  const [fitMode, setFitMode] = useState(true);

  useEffect(() => {
    const trimmed = code.trim();
    if (!trimmed) {
      setSvg(null);
      setError("图表源码为空");
      setLoading(false);
      return;
    }

    let cancelled = false;
    setLoading(true);
    setError(null);

    const renderId = `${safeDomId}-${Date.now().toString(36)}`;
    void loadMermaid()
      .then(async (mermaid) => {
        if (cancelled) return;
        mermaid.initialize({
          startOnLoad: false,
          securityLevel: "strict",
          theme: isDark ? "dark" : "default",
          flowchart: { htmlLabels: false },
          suppressErrorRendering: true,
        });
        const rendered = await mermaid.render(renderId, trimmed);
        if (cancelled) return;
        const clean = sanitizeSvgMarkup(rendered.svg);
        if (!clean) {
          setError("未能生成有效的图表矢量输出");
          setSvg(null);
        } else {
          setSvg(clean);
          setError(null);
        }
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        const message = err instanceof Error ? err.message : String(err);
        setError(message || "Mermaid 语法解析失败");
        setSvg(null);
      })
      .finally(() => {
        // Mermaid may leave an error container in document.body on failed renders; remove it cleanly.
        if (typeof document !== "undefined") {
          document.getElementById(`d${renderId}`)?.remove();
        }
        if (!cancelled) setLoading(false);
      });

    return () => {
      cancelled = true;
    };
  }, [code, isDark, safeDomId]);

  const zoomOut = () => {
    setFitMode(false);
    setZoom((z) => clampMermaidZoom(z - MERMAID_ZOOM_STEP));
  };
  const zoomIn = () => {
    setFitMode(false);
    setZoom((z) => clampMermaidZoom(z + MERMAID_ZOOM_STEP));
  };
  const fitWindow = () => {
    setFitMode(true);
    setZoom(1);
  };

  return (
    <div
      data-testid="mermaid-diagram"
      className={cn(
        "flex flex-col overflow-hidden rounded-2xl border border-border/60",
        isDark ? "bg-[#1a1512]" : "bg-white/95",
        className,
      )}
    >
      {interactive && (
        <div
          role="toolbar"
          aria-label="图表缩放"
          className="flex items-center justify-between gap-2 border-b border-border/50 px-3 py-1.5 text-xs"
        >
          <span className="text-muted-foreground font-medium">Mermaid 实时图表</span>
          <div className="flex items-center gap-1">
            <button
              type="button"
              onClick={zoomOut}
              disabled={!svg || zoom <= MERMAID_MIN_ZOOM}
              aria-label="缩小图表"
              title="缩小"
              className="inline-flex h-7 w-7 items-center justify-center rounded-lg text-muted-foreground transition-colors hover:bg-muted hover:text-foreground disabled:opacity-40"
            >
              <ZoomOut className="h-3.5 w-3.5" aria-hidden="true" />
            </button>
            <span className="min-w-[44px] text-center tabular-nums text-[11.5px] text-muted-foreground">
              {fitMode ? "适应" : `${Math.round(zoom * 100)}%`}
            </span>
            <button
              type="button"
              onClick={zoomIn}
              disabled={!svg || zoom >= MERMAID_MAX_ZOOM}
              aria-label="放大图表"
              title="放大"
              className="inline-flex h-7 w-7 items-center justify-center rounded-lg text-muted-foreground transition-colors hover:bg-muted hover:text-foreground disabled:opacity-40"
            >
              <ZoomIn className="h-3.5 w-3.5" aria-hidden="true" />
            </button>
            <button
              type="button"
              onClick={fitWindow}
              disabled={!svg}
              aria-label="适应窗口"
              title="适应窗口"
              className="inline-flex h-7 items-center gap-1 rounded-lg px-2 text-[11.5px] font-medium text-muted-foreground transition-colors hover:bg-muted hover:text-foreground disabled:opacity-40"
            >
              <Maximize2 className="h-3 w-3" aria-hidden="true" />
              适应窗口
            </button>
          </div>
        </div>
      )}

      <div className="relative min-h-[160px] flex-1 overflow-auto p-4">
        {loading ? (
          <div className="flex min-h-[140px] flex-col items-center justify-center gap-2 text-muted-foreground" role="status">
            <Loader2 className="h-5 w-5 animate-spin motion-reduce:animate-none" aria-hidden="true" />
            <span className="text-xs">正在渲染图表…</span>
          </div>
        ) : error ? (
          <div className="flex flex-col gap-2 rounded-xl border border-destructive/30 bg-destructive/5 p-3 text-xs" role="alert">
            <div className="flex items-center gap-1.5 font-medium text-destructive">
              <AlertCircle className="h-4 w-4 shrink-0" aria-hidden="true" />
              <span>图表渲染失败：{error}</span>
            </div>
            <pre className="m-0 max-h-40 overflow-auto whitespace-pre-wrap rounded-lg bg-muted/40 p-2 font-mono text-[11.5px] text-foreground/80">
              {code}
            </pre>
          </div>
        ) : svg ? (
          <div
            data-testid="mermaid-svg-stage"
            data-zoom={zoom}
            data-fit={fitMode ? "true" : "false"}
            className={cn(
              "flex min-h-[140px] w-full items-center justify-center",
              fitMode && "[&_svg]:h-auto [&_svg]:max-w-full",
            )}
          >
            <div
              style={fitMode ? undefined : { transform: `scale(${zoom})`, transformOrigin: "top center" }}
              dangerouslySetInnerHTML={{ __html: svg }}
            />
          </div>
        ) : null}
      </div>
    </div>
  );
}
