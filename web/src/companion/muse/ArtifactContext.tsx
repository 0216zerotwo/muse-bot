import { createContext, useCallback, useContext, useMemo, useState, type ReactNode } from "react";

export type ArtifactType = "html" | "svg" | "mermaid" | "markdown" | "code";

export interface ArtifactItem {
  id: string;
  title: string;
  type: ArtifactType;
  language: string;
  code: string;
  initialTab?: "preview" | "source";
}

export interface OpenArtifactInput {
  id?: string;
  title?: string;
  type?: ArtifactType;
  language?: string;
  code: string;
  initialTab?: "preview" | "source";
}

export interface ArtifactContextValue {
  artifact: ArtifactItem | null;
  openArtifact: (input: OpenArtifactInput) => void;
  closeArtifact: () => void;
  refineArtifact: (artifact: ArtifactItem) => void;
}

const PREVIEWABLE_LANGUAGES = new Set(["html", "htm", "svg", "mermaid", "mmd", "md", "markdown", "mdx"]);

/** Whether a fenced code block language supports live visual preview in the Artifact canvas. */
export function isPreviewableArtifactLanguage(language?: string | null): boolean {
  const norm = (language ?? "").trim().toLowerCase();
  return PREVIEWABLE_LANGUAGES.has(norm);
}

/** Whether a fenced code block language is a Mermaid diagram. */
export function isMermaidLanguage(language?: string | null): boolean {
  const norm = (language ?? "").trim().toLowerCase();
  return norm === "mermaid" || norm === "mmd";
}

/** Infer the Artifact rendering mode from a language tag. */
export function inferArtifactType(language?: string | null): ArtifactType {
  const norm = (language ?? "").trim().toLowerCase();
  if (norm === "html" || norm === "htm") return "html";
  if (norm === "svg") return "svg";
  if (norm === "mermaid" || norm === "mmd") return "mermaid";
  if (norm === "md" || norm === "markdown" || norm === "mdx") return "markdown";
  return "code";
}

/** Extract a sensible default Chinese title from artifact source when none was provided. */
export function inferArtifactTitle(code: string, type: ArtifactType, language?: string | null): string {
  const text = code.trim();
  if (type === "html") {
    const titleMatch = /<title[^>]*>([\s\S]*?)<\/title>/i.exec(text) ?? /<h1[^>]*>([\s\S]*?)<\/h1>/i.exec(text);
    const cleaned = titleMatch?.[1]?.replace(/<[^>]+>/g, "").replace(/\s+/g, " ").trim();
    if (cleaned) return cleaned.slice(0, 40);
    return "网页预览";
  }
  if (type === "svg") {
    const titleMatch = /<title[^>]*>([\s\S]*?)<\/title>/i.exec(text);
    const cleaned = titleMatch?.[1]?.replace(/\s+/g, " ").trim();
    if (cleaned) return cleaned.slice(0, 40);
    return "SVG 图形";
  }
  if (type === "mermaid") {
    const first = text.split(/\r?\n/).find((line) => {
      const s = line.trim();
      return s && !s.startsWith("%%");
    })?.trim() ?? "";
    if (/^sequenceDiagram\b/i.test(first)) return "时序图";
    if (/^(?:flowchart|graph)\b/i.test(first)) return "流程图";
    if (/^classDiagram\b/i.test(first)) return "类图";
    if (/^erDiagram\b/i.test(first)) return "ER 关系图";
    if (/^gantt\b/i.test(first)) return "甘特图";
    if (/^pie\b/i.test(first)) return "饼图";
    if (/^mindmap\b/i.test(first)) return "思维导图";
    if (/^stateDiagram/i.test(first)) return "状态图";
    return "Mermaid 图表";
  }
  if (type === "markdown") {
    const heading = /^#{1,3}\s+(.+)$/m.exec(text)?.[1]?.replace(/[*_`#]/g, "").trim();
    if (heading) return heading.slice(0, 40);
    return "Markdown 文档";
  }
  const lang = (language ?? "").trim().toUpperCase();
  return lang ? `${lang} 代码` : "代码画布";
}

const ArtifactContext = createContext<ArtifactContextValue | null>(null);

/** Access the Artifact side-pane controller when mounted inside Companion mode (null otherwise). */
export function useArtifact(): ArtifactContextValue | null {
  return useContext(ArtifactContext);
}

let artifactSeq = 0;

export function ArtifactProvider({
  onRefine,
  children,
}: {
  /** Called when the user clicks 「让伙伴继续完善」; fills the chat composer draft and focuses it. */
  onRefine?: (draftText: string) => void;
  children: ReactNode;
}) {
  const [artifact, setArtifact] = useState<ArtifactItem | null>(null);

  const openArtifact = useCallback((input: OpenArtifactInput) => {
    const type = input.type ?? inferArtifactType(input.language);
    const language = (input.language ?? "").trim() || (type === "code" ? "text" : type);
    const title = input.title?.trim() || inferArtifactTitle(input.code, type, language);
    const id = input.id?.trim() || `artifact-${++artifactSeq}`;
    setArtifact({
      id,
      title,
      type,
      language,
      code: input.code,
      initialTab: input.initialTab ?? (type === "code" ? "source" : "preview"),
    });
  }, []);

  const closeArtifact = useCallback(() => {
    setArtifact(null);
  }, []);

  const refineArtifact = useCallback(
    (item: ArtifactItem) => {
      const prompt = `请帮我继续完善「${item.title}」：`;
      onRefine?.(prompt);
    },
    [onRefine],
  );

  const value = useMemo<ArtifactContextValue>(
    () => ({ artifact, openArtifact, closeArtifact, refineArtifact }),
    [artifact, openArtifact, closeArtifact, refineArtifact],
  );

  return <ArtifactContext.Provider value={value}>{children}</ArtifactContext.Provider>;
}
