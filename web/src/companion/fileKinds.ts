/*
 * Files in the chat, as Muse shows a document (its first page, then its name and kind), and the
 * small things around a reply the chat apps have: its words as read out (朗读), and what it saved
 * about the user on the way (已记住). Pure, so the thread and the tests share it.
 */
import type { ToolProgressEvent, UIMessage } from "@/lib/types";
import type { ArtifactSaveFormat } from "./companion-api";

export type FileKind =
  | "pdf"
  | "markdown"
  | "text"
  | "code"
  | "table"
  | "web"
  | "svg"
  | "mermaid"
  | "word"
  | "sheet"
  | "slides"
  | "audio"
  | "archive"
  | "other";

const KINDS: Record<string, FileKind> = {};
const add = (kind: FileKind, endings: string) => endings.split(" ").forEach((ending) => (KINDS[ending] = kind));
add("pdf", "pdf");
add("markdown", "md markdown mdown mkd mdx");
add("text", "txt text log rst");
add("table", "csv tsv");
add("web", "html htm xhtml");
add("svg", "svg");
add("mermaid", "mmd mermaid");
add("word", "doc docx rtf pages odt");
add("sheet", "xls xlsx numbers ods");
add("slides", "ppt pptx key odp");
add("audio", "mp3 m4a aac wav aif aiff caf flac ogg opus amr");
add("archive", "zip rar 7z tar gz tgz");
add(
  "code",
  "json jsonl py pyi js mjs cjs ts tsx jsx swift sh bash zsh ps1 bat yaml yml toml ini cfg conf xml css scss less sass sql c h cc cpp hpp java kt kts go rs rb php lua r cs vue svelte graphql proto dockerfile",
);

export function fileEnding(name: string): string {
  const clean = name.split(/[?#]/)[0];
  const dot = clean.lastIndexOf(".");
  return dot > 0 && dot > clean.lastIndexOf("/") ? clean.slice(dot + 1).toLowerCase() : "";
}

export function fileKind(name: string): FileKind {
  return KINDS[fileEnding(name)] ?? "other";
}

const WORDS: Record<FileKind, string> = {
  pdf: "PDF",
  markdown: "文档",
  word: "文档",
  text: "文本",
  code: "代码",
  table: "表格",
  sheet: "表格",
  web: "网页",
  svg: "矢量图",
  mermaid: "流程图",
  slides: "演示文稿",
  audio: "音频",
  archive: "压缩包",
  other: "文件",
};

/** The words under its name. */
export function fileWords(kind: FileKind): string {
  return WORDS[kind];
}

/** Drawn from its own text. */
export function isTextKind(kind: FileKind): boolean {
  return kind === "markdown" || kind === "text" || kind === "code" || kind === "table" || kind === "mermaid";
}

/** Whether the file can be previewed inline inside the companion Viewer. */
export function isPreviewableDocument(name: string): boolean {
  const kind = fileKind(name);
  return kind === "markdown" || kind === "text" || kind === "code" || kind === "table" || kind === "web" || kind === "svg" || kind === "mermaid";
}

const KNOWLEDGE_IMPORT_EXTS = new Set([
  "pdf", "docx", "xlsx", "pptx", "odt",
  "html", "htm", "xhtml",
  "md", "markdown", "mdx",
  "txt", "csv", "tsv", "json", "jsonl", "yaml", "yml", "xml", "svg", "rst", "log", "ini", "toml", "conf", "cfg",
  "py", "ts", "tsx", "js", "jsx", "mjs", "cjs", "swift", "java", "kt", "kts", "go", "rs",
  "sh", "bash", "zsh", "css", "scss", "sql", "c", "h", "cpp", "hpp", "rb", "php",
  "mmd", "mermaid",
]);

const EXTENSIONLESS_KNOWLEDGE_NAMES = new Set(["readme", "license", "makefile", "dockerfile", "changelog"]);

/** Whether the backend knowledge vault accepts this file name for ingestion. */
export function isKnowledgeImportable(name: string): boolean {
  const ext = fileEnding(name);
  if (ext) return KNOWLEDGE_IMPORT_EXTS.has(ext);
  const base = (name.split("/").pop() ?? name).trim().toLowerCase();
  return EXTENSIONLESS_KNOWLEDGE_NAMES.has(base);
}

const LANG_TO_EXT: Record<string, string> = {
  html: "html",
  htm: "html",
  markup: "html",
  svg: "svg",
  mermaid: "mmd",
  mmd: "mmd",
  md: "md",
  markdown: "md",
  mdx: "md",
  py: "py",
  python: "py",
  ts: "ts",
  typescript: "ts",
  tsx: "tsx",
  js: "js",
  javascript: "js",
  jsx: "jsx",
  json: "json",
  jsonl: "jsonl",
  csv: "csv",
  tsv: "tsv",
  yaml: "yaml",
  yml: "yaml",
  toml: "toml",
  ini: "ini",
  xml: "xml",
  css: "css",
  scss: "scss",
  less: "less",
  sql: "sql",
  sh: "sh",
  bash: "sh",
  shell: "sh",
  zsh: "sh",
  powershell: "ps1",
  ps1: "ps1",
  go: "go",
  golang: "go",
  rs: "rs",
  rust: "rs",
  java: "java",
  kt: "kt",
  kotlin: "kt",
  swift: "swift",
  c: "c",
  cpp: "cpp",
  "c++": "cpp",
  cs: "cs",
  csharp: "cs",
  rb: "rb",
  ruby: "rb",
  php: "php",
  vue: "vue",
  svelte: "svelte",
  dockerfile: "dockerfile",
  docker: "dockerfile",
  txt: "txt",
  text: "txt",
  plain: "txt",
};

const LANG_TO_ARTIFACT_FORMAT: Record<string, ArtifactSaveFormat> = {
  html: "html",
  htm: "html",
  svg: "svg",
  md: "md",
  markdown: "md",
  mdx: "md",
  mermaid: "mermaid",
  mmd: "mermaid",
  txt: "txt",
  text: "txt",
  json: "json",
  csv: "csv",
  py: "py",
  python: "py",
  js: "js",
  javascript: "js",
  jsx: "js",
  ts: "ts",
  typescript: "ts",
  tsx: "tsx",
  css: "css",
  scss: "css",
  sh: "sh",
  bash: "sh",
  shell: "sh",
  zsh: "sh",
  yaml: "yaml",
  yml: "yaml",
};

/** Infer a clean file extension (without leading dot) from a fenced code language tag. */
export function fileExtensionForLanguage(language?: string | null): string {
  const key = (language ?? "").trim().toLowerCase();
  if (!key) return "txt";
  return LANG_TO_EXT[key] ?? (/^[a-z0-9]{1,8}$/.test(key) ? key : "txt");
}

/** Map any code language to a server-accepted `companion.creations.save` format. */
export function artifactFormatForLanguage(language?: string | null): ArtifactSaveFormat {
  const key = (language ?? "").trim().toLowerCase();
  return LANG_TO_ARTIFACT_FORMAT[key] ?? "txt";
}

/** Build a safe download filename for a code block or artifact. */
export function downloadFilenameForCode(language?: string | null, title?: string | null): string {
  const ext = fileExtensionForLanguage(language);
  const rawStem = (title ?? "").trim().replace(/[\\/:*?"<>|\r\n\t]+/g, " ").replace(/\s+/g, "-").replace(/^-+|-+$/g, "");
  const defaultStem =
    ext === "html" ? "page"
      : ext === "svg" ? "graphic"
        : ext === "mmd" ? "diagram"
          : ext === "md" ? "document"
            : "snippet";
  const stem = (rawStem || defaultStem).slice(0, 48);
  return stem.toLowerCase().endsWith(`.${ext}`) ? stem : `${stem}.${ext}`;
}

/** Trigger a client-side file download from a UTF-8 text string. */
export function downloadTextFile(content: string, filename: string, mime = "text/plain;charset=utf-8"): boolean {
  if (typeof document === "undefined" || typeof URL === "undefined" || typeof URL.createObjectURL !== "function") {
    return false;
  }
  const blob = new Blob([content], { type: mime });
  const href = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = href;
  anchor.download = filename;
  anchor.rel = "noopener";
  anchor.style.display = "none";
  document.body.appendChild(anchor);
  try {
    anchor.click();
  } finally {
    anchor.remove();
    URL.revokeObjectURL(href);
  }
  return true;
}

/** The mark on its icon: "PDF", "MD", "XLSX" (four letters at most). */
export function fileBadge(name: string): string {
  return fileEnding(name).toUpperCase().slice(0, 4) || "FILE";
}

/** Its name without the ending. */
export function fileTitle(name: string): string {
  const base = name.split("/").pop() ?? name;
  const dot = base.lastIndexOf(".");
  return dot > 0 ? base.slice(0, dot) : base;
}

export function fileSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(bytes < 10 * 1024 ? 1 : 0)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

/** The first rows of a CSV or TSV (quoted cells understood), all the same length. */
export function csvRows(text: string, separator = ",", rows = 10, columns = 5): string[][] {
  const out: string[][] = [];
  let row: string[] = [];
  let cell = "";
  let quoted = false;
  const endRow = () => {
    row.push(cell);
    cell = "";
    if (!(row.length === 1 && row[0] === "")) out.push(row.map((value) => value.trim()));
    row = [];
  };
  for (let i = 0; i < text.length && out.length < rows; i += 1) {
    const ch = text[i];
    if (quoted) {
      if (ch === '"' && text[i + 1] === '"') {
        cell += '"';
        i += 1;
      } else if (ch === '"') quoted = false;
      else cell += ch;
    } else if (ch === '"' && cell === "") quoted = true;
    else if (ch === separator) {
      row.push(cell);
      cell = "";
    } else if (ch === "\n") endRow();
    else if (ch !== "\r") cell += ch;
  }
  if (out.length < rows && (cell !== "" || row.length)) endRow();
  const width = Math.min(columns, Math.max(0, ...out.map((r) => r.length)));
  return out.map((r) => [...r, ...Array(Math.max(0, width - r.length)).fill("")].slice(0, width));
}

/** Markdown for a page preview: pictures left out (they would load from the web). */
export function markdownPreview(text: string): string {
  return text.replace(/!\[[^\]]*\]\([^)]*\)/g, "").replace(/^\s*<img\b[^>]*>\s*$/gm, "");
}

/**
 * A web page file in a preview frame: the same rules as the chat's cards (no network, no forms),
 * added to its own head, so a page's scripts can draw but reach nothing.
 */
export function filePageDocument(html: string): string {
  const policy =
    "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data: blob:; font-src data:; " +
    "media-src data: blob:; connect-src 'none'; form-action 'none'; base-uri 'none'";
  if (typeof DOMParser !== "undefined") {
    const doc = new DOMParser().parseFromString(html, "text/html");
    // Strip declarative meta http-equiv (e.g. refresh redirects, which CSP connect-src/default-src
    // do not block) and base tags before injecting our strict CSP first in <head>.
    doc.querySelectorAll("meta[http-equiv], base").forEach((el) => el.remove());
    let head = doc.head;
    if (!head) {
      head = doc.createElement("head");
      doc.documentElement.insertBefore(head, doc.documentElement.firstChild);
    } else if (doc.documentElement.firstChild !== head) {
      doc.documentElement.insertBefore(head, doc.documentElement.firstChild);
    }
    const meta = doc.createElement("meta");
    meta.setAttribute("http-equiv", "Content-Security-Policy");
    meta.setAttribute("content", policy);
    head.insertBefore(meta, head.firstChild);
    return `<!doctype html>${doc.documentElement.outerHTML}`;
  }
  const meta = `<meta http-equiv="Content-Security-Policy" content="${policy}">`;
  const safeBody = html.replace(
    /<\/?(?:html|head|body)\b[^>]*>|<meta\b[^>]*http-equiv\b[^>]*>|<base\b[^>]*>|<!doctype[^>]*>/gi,
    "",
  );
  return `<!doctype html><html><head>${meta}</head><body>${safeBody}</body></html>`;
}

/**
 * Sandboxed HTML document for Artifact Canvas & Viewer: strict offline CSP first in `<head>`,
 * plus gentle light/dark theme defaults placed before author styles so author CSS overrides freely.
 */
export function artifactHtmlDocument(html: string, dark = false): string {
  const baseCss =
    `:root{color-scheme:${dark ? "dark" : "light"};}` +
    `html,body{margin:0;padding:16px;font-family:-apple-system,BlinkMacSystemFont,"PingFang SC",system-ui,sans-serif;` +
    `background:${dark ? "#17120f" : "#ffffff"};color:${dark ? "#f3edea" : "#1e1814"};box-sizing:border-box;}`;
  const policy =
    "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data: blob:; font-src data:; " +
    "media-src data: blob:; connect-src 'none'; form-action 'none'; base-uri 'none'";
  if (typeof DOMParser !== "undefined") {
    const doc = new DOMParser().parseFromString(html, "text/html");
    doc.querySelectorAll("meta[http-equiv], base").forEach((el) => el.remove());
    let head = doc.head;
    if (!head) {
      head = doc.createElement("head");
      doc.documentElement.insertBefore(head, doc.documentElement.firstChild);
    } else if (doc.documentElement.firstChild !== head) {
      doc.documentElement.insertBefore(head, doc.documentElement.firstChild);
    }
    const style = doc.createElement("style");
    style.setAttribute("data-muse-base", "1");
    style.textContent = baseCss;
    head.insertBefore(style, head.firstChild);
    const meta = doc.createElement("meta");
    meta.setAttribute("http-equiv", "Content-Security-Policy");
    meta.setAttribute("content", policy);
    head.insertBefore(meta, head.firstChild);
    return `<!doctype html>${doc.documentElement.outerHTML}`;
  }
  const meta = `<meta http-equiv="Content-Security-Policy" content="${policy}">`;
  const style = `<style data-muse-base="1">${baseCss}</style>`;
  const safeBody = html.replace(
    /<\/?(?:html|head|body)\b[^>]*>|<meta\b[^>]*http-equiv\b[^>]*>|<base\b[^>]*>|<!doctype[^>]*>/gi,
    "",
  );
  return `<!doctype html><html><head>${meta}${style}</head><body>${safeBody}</body></html>`;
}

const SVG_FORBIDDEN_TAGS = new Set([
  "script",
  "iframe",
  "object",
  "embed",
  "applet",
  "meta",
  "base",
  "link",
  "form",
  "handler",
  "set",
  "animate",
  "animatemotion",
  "animatetransform",
]);

const URL_ATTRS = new Set(["href", "xlink:href", "src", "action", "formaction", "poster", "data"]);

function isUnsafeUri(raw: string): boolean {
  let compact = "";
  for (let i = 0; i < raw.length; i += 1) {
    if (raw.charCodeAt(i) > 32) compact += raw[i];
  }
  compact = compact.toLowerCase();
  if (!compact) return false;
  if (compact.startsWith("#")) return false;
  if (compact.startsWith("data:image/") && !compact.startsWith("data:image/svg")) return false;
  return /^(?:javascript:|vbscript:|data:|https?:|\/\/)/i.test(compact);
}

function scrubStyleValue(css: string): string {
  return css
    .replace(/@import\b[^;]*;?/gi, "")
    .replace(/expression\s*\([^)]*\)/gi, "")
    .replace(/url\s*\(\s*(['"]?)\s*(?:javascript:|vbscript:|data:text\/html|https?:|\/\/)[^)]*\1\s*\)/gi, "none");
}

/**
 * Strip active/scriptable/networked vectors from SVG markup before rendering (defense-in-depth
 * alongside iframe `sandbox=""` or Mermaid's `securityLevel: "strict"`).
 */
export function sanitizeSvgMarkup(rawSvg: string, options: { allowForeignObject?: boolean } = {}): string {
  const source = rawSvg.trim();
  if (!source) return "";
  if (typeof DOMParser !== "undefined") {
    const parser = new DOMParser();
    let doc = parser.parseFromString(source, "image/svg+xml");
    let svgEl: Element | null =
      doc.documentElement && doc.documentElement.tagName.toLowerCase() === "svg" && !doc.querySelector("parsererror")
        ? doc.documentElement
        : null;
    if (!svgEl) {
      doc = parser.parseFromString(source, "text/html");
      svgEl = doc.querySelector("svg");
    }
    if (svgEl) {
      const nodes = [svgEl, ...Array.from(svgEl.querySelectorAll("*"))];
      for (const el of nodes) {
        const tag = el.tagName.toLowerCase();
        if (SVG_FORBIDDEN_TAGS.has(tag) || (!options.allowForeignObject && tag === "foreignobject")) {
          el.remove();
          continue;
        }
        if (tag === "style" && el.textContent) {
          el.textContent = scrubStyleValue(el.textContent);
        }
        for (const attr of Array.from(el.attributes)) {
          const name = attr.name.toLowerCase();
          if (name.startsWith("on")) {
            el.removeAttribute(attr.name);
            continue;
          }
          if (URL_ATTRS.has(name) && isUnsafeUri(attr.value)) {
            el.removeAttribute(attr.name);
            continue;
          }
          if (name === "style") {
            el.setAttribute(attr.name, scrubStyleValue(attr.value));
          }
        }
      }
      if (!svgEl.getAttribute("xmlns")) {
        svgEl.setAttribute("xmlns", "http://www.w3.org/2000/svg");
      }
      if (typeof XMLSerializer !== "undefined") {
        return new XMLSerializer().serializeToString(svgEl);
      }
      return svgEl.outerHTML;
    }
  }
  return source
    .replace(/<script\b[\s\S]*?<\/script>/gi, "")
    .replace(/<foreignObject\b[\s\S]*?<\/foreignObject>/gi, "")
    .replace(/\son[a-z]+\s*=\s*(?:"[^"]*"|'[^']*'|[^\s>]+)/gi, "")
    .replace(/\s(?:xlink:)?href\s*=\s*(?:"\s*javascript:[^"]*"|'\s*javascript:[^']*')/gi, "");
}

/** Wrap sanitized SVG in an offline, no-script document for `<iframe sandbox="" srcDoc={...} />`. */
export function svgPageDocument(svg: string, dark = false): string {
  const clean = sanitizeSvgMarkup(svg);
  const policy = "default-src 'none'; style-src 'unsafe-inline'; img-src data:; font-src data:; form-action 'none'; base-uri 'none'";
  const bg = dark ? "#191411" : "#faf7f4";
  const grid = dark ? "rgba(255,255,255,0.04)" : "rgba(30,24,20,0.04)";
  return (
    `<!doctype html><html><head>` +
    `<meta http-equiv="Content-Security-Policy" content="${policy}">` +
    `<style>` +
    `:root{color-scheme:${dark ? "dark" : "light"};}` +
    `html,body{margin:0;height:100%;width:100%;display:flex;align-items:center;justify-content:center;padding:16px;box-sizing:border-box;` +
    `background:${bg};background-image:radial-gradient(${grid} 1px,transparent 1px);background-size:16px 16px;overflow:auto;}` +
    `svg{max-width:100%;max-height:100%;height:auto;width:auto;display:block;}` +
    `</style></head><body>${clean}</body></html>`
  );
}

/** A reply as it is read out: its words only (no cards, links' addresses, Markdown marks). */
export function spokenText(reply: string): string {
  const rules: [RegExp, string][] = [
    [/```(?:card|json|ui)[ \t]*\n[\s\S]*?(?:\n```|$)/g, ""],
    [/```[\s\S]*?```/g, "（代码略）"],
    [/!\[[^\]]*\]\([^)]*\)/g, ""],
    [/\[([^\]]+)\]\([^)]*\)/g, "$1"],
    [/https?:\/\/\S+/g, ""],
    [/^\s*\|?\s*:?-{3,}.*$/gm, ""],
    [/\|/g, "，"],
    [/^[ \t]*，[ \t]*|[ \t]*，[ \t]*$/gm, ""],
    [/^\s{0,3}(#{1,6}|>|[-*+]|\d+[.)])\s+/gm, ""],
    [/(\*\*|__|~~|`|\*)/g, ""],
    [/[ \t]+/g, " "],
    [/\n{2,}/g, "\n"],
  ];
  return rules.reduce((text, [pattern, replacement]) => text.replace(pattern, replacement), reply).trim();
}

/** What a memory call saved about the user (``target: user``, add or patch), for 已记住. */
export function rememberedNotes(event: ToolProgressEvent): string[] {
  if (event.name !== "memory" || event.phase !== "end" || event.error) return [];
  const args = (event.arguments ?? {}) as Record<string, unknown>;
  if (args.target !== "user") return [];
  if (typeof event.result === "string" && /"success":\s*false/.test(event.result)) return [];
  const operations = Array.isArray(args.operations) ? (args.operations as Record<string, unknown>[]) : [args];
  return operations.flatMap((operation) => {
    if (operation.action !== "add" && operation.action !== "patch") return [];
    const raw = typeof operation.content === "string" ? operation.content : typeof operation.new_text === "string" ? operation.new_text : "";
    const content = raw.replace(/^【[^】]*】\s*/, "").trim();
    if (!content) return [];
    return [content.length > 40 ? `${content.slice(0, 40)}…` : content];
  });
}

/** A document being written in the turn at work: shown as it will look, before it is sent. */
export interface FileDraft {
  id: string;
  name: string;
  content: string;
  /** Written (the call has finished); it comes as a file next. */
  done: boolean;
}

/**
 * The newest document written in the turn at work, until the file itself arrives. Only what is
 * written for the user (documents, notes, tables, pages), not the scripts it writes for itself.
 */
export function fileDraft(messages: UIMessage[]): FileDraft | null {
  const turn = [...messages].reverse().find((message) => message.turnId)?.turnId;
  if (!turn) return null;
  const items = messages.filter((message) => message.turnId === turn);
  let newest: FileDraft | null = null;
  for (const message of items) {
    for (const event of message.toolEvents ?? []) {
      if (event.name !== "write_file" || event.error) continue;
      const args = (event.arguments ?? {}) as Record<string, unknown>;
      const path = typeof args.path === "string" ? args.path.replace(/\\/g, "/") : "";
      const content = typeof args.content === "string" ? args.content : "";
      const name = path.split("/").pop() ?? "";
      const kind = fileKind(name);
      if (!content || !["markdown", "text", "table", "web"].includes(kind)) continue;
      if (path.split("/").some((part) => part.startsWith(".")) || name === "SKILL.md") continue;
      newest = { id: event.call_id ?? `${message.id}-${name}`, name, content, done: event.phase === "end" };
    }
  }
  if (newest && items.some((message) => message.media?.some((item) => item.name === newest?.name))) return null;
  return newest;
}
