/*
 * Files in the chat, as Muse shows a document (its first page, then its name and kind), and the
 * small things around a reply the chat apps have: its words as read out (朗读), and what it saved
 * about the user on the way (已记住). Pure, so the thread and the tests share it.
 */
import type { ToolProgressEvent, UIMessage } from "@/lib/types";

export type FileKind =
  | "pdf" | "markdown" | "text" | "code" | "table" | "web" | "word" | "sheet" | "slides" | "audio" | "archive" | "other";

const KINDS: Record<string, FileKind> = {};
const add = (kind: FileKind, endings: string) => endings.split(" ").forEach((ending) => (KINDS[ending] = kind));
add("pdf", "pdf");
add("markdown", "md markdown mdown mkd");
add("text", "txt text log");
add("table", "csv tsv");
add("web", "html htm xhtml");
add("word", "doc docx rtf pages odt");
add("sheet", "xls xlsx numbers ods");
add("slides", "ppt pptx key odp");
add("audio", "mp3 m4a aac wav aif aiff caf flac ogg opus amr");
add("archive", "zip rar 7z tar gz tgz");
add("code", "json jsonl py js mjs ts tsx jsx swift sh bash zsh ps1 bat yaml yml toml ini cfg conf xml css scss sql c h cc cpp hpp java kt go rs rb php lua r cs vue");

export function fileEnding(name: string): string {
  const clean = name.split(/[?#]/)[0];
  const dot = clean.lastIndexOf(".");
  return dot > 0 && dot > clean.lastIndexOf("/") ? clean.slice(dot + 1).toLowerCase() : "";
}

export function fileKind(name: string): FileKind {
  return KINDS[fileEnding(name)] ?? "other";
}

const WORDS: Record<FileKind, string> = {
  pdf: "PDF", markdown: "文档", word: "文档", text: "文本", code: "代码", table: "表格", sheet: "表格",
  web: "网页", slides: "演示文稿", audio: "音频", archive: "压缩包", other: "文件",
};

/** The words under its name. */
export function fileWords(kind: FileKind): string {
  return WORDS[kind];
}

/** Drawn from its own text. */
export function isTextKind(kind: FileKind): boolean {
  return kind === "markdown" || kind === "text" || kind === "code" || kind === "table";
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
