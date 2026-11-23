/*
 * A file in the chat, as Muse shows one: the top of its first page, then its icon, its name and what
 * it is (a tap opens it whole in the viewer). Text, Markdown, code and tables are drawn from the
 * file's own words; a PDF in the browser's own viewer; a web page in a sandboxed frame that can draw
 * but reach nothing. Others (an Excel file, a zip) are the row alone; audio plays where it is.
 */
import { useEffect, useState, type MouseEvent } from "react";

import { MarkdownText } from "@/components/MarkdownText";
import type { UIMediaAttachment } from "@/lib/types";

import { androidShell } from "./android-shell";
import {
  csvRows,
  fileBadge,
  fileEnding,
  fileKind,
  filePageDocument,
  fileSize,
  fileTitle,
  fileWords,
  isTextKind,
  markdownPreview,
  type FileDraft,
  type FileKind,
} from "./fileKinds";

const COLORS: Record<FileKind, string> = {
  pdf: "#e03d38", word: "#2b6bd6", sheet: "#21904f", table: "#21904f", slides: "#eb7329", web: "#8557d6",
  markdown: "#5c6e87", text: "#5c6e87", code: "#3b475e", audio: "#db4a85", archive: "#8c6b4d", other: "#8a8a8a",
};

/** Enough of a text file for its first page (a page file is read whole: it is drawn). */
export const TEXT_PREVIEW = 64 * 1024;
const PAGE_LIMIT = 2 * 1024 * 1024;

function parseTotalBytes(res: Response, fallbackBytes: number, truncated: boolean): number | null {
  const range = res.headers.get("Content-Range");
  if (range) {
    const match = /\/(\d+)\s*$/.exec(range);
    if (match) {
      const total = Number(match[1]);
      if (Number.isFinite(total) && total >= 0) return total;
    }
  }
  if (res.status === 200) {
    const length = res.headers.get("Content-Length");
    if (length !== null && length.trim() !== "") {
      const total = Number(length);
      if (Number.isFinite(total) && total >= 0) return total;
    }
    if (!truncated) return fallbackBytes;
  }
  return truncated ? null : fallbackBytes;
}

/**
 * Fetches at most ``maxBytes`` from ``url`` using HTTP Range + ReadableStream reader cancellation
 * so multi-megabyte CSV/Markdown/log attachments never buffer their full body in memory.
 */
export async function fetchBoundedTextPreview(
  url: string,
  maxBytes: number,
  signal?: AbortSignal,
): Promise<{ text: string; bytes: number | null }> {
  const res = await fetch(url, {
    credentials: "same-origin",
    headers: { Range: `bytes=0-${maxBytes - 1}` },
    signal,
  });
  if (res.status === 416 && /\/0\s*$/.test(res.headers.get("Content-Range") ?? "")) {
    return { text: "", bytes: 0 };
  }
  if (!res.ok) throw new Error(`HTTP ${res.status}`);

  const chunks: Uint8Array[] = [];
  let collected = 0;
  let truncated = false;

  if (res.body?.getReader) {
    const reader = res.body.getReader();
    try {
      while (collected < maxBytes) {
        const { done, value } = await reader.read();
        if (done) break;
        if (!value || value.byteLength === 0) continue;
        const room = maxBytes - collected;
        if (value.byteLength > room) {
          chunks.push(value.subarray(0, room));
          collected += room;
          truncated = true;
          await reader.cancel().catch(() => undefined);
          break;
        }
        chunks.push(value);
        collected += value.byteLength;
      }
      if (!truncated && collected >= maxBytes) {
        const probe = await reader.read();
        if (!probe.done) {
          truncated = true;
          await reader.cancel().catch(() => undefined);
        }
      }
    } finally {
      reader.releaseLock();
    }
  } else {
    const buf = new Uint8Array(await res.arrayBuffer());
    truncated = buf.byteLength > maxBytes;
    const slice = truncated ? buf.subarray(0, maxBytes) : buf;
    chunks.push(slice);
    collected = slice.byteLength;
  }

  const merged = new Uint8Array(collected);
  let offset = 0;
  for (const chunk of chunks) {
    merged.set(chunk, offset);
    offset += chunk.byteLength;
  }
  let decoded = new TextDecoder("utf-8").decode(merged).replace(/^\uFEFF/, "");
  if (truncated && decoded.endsWith("\uFFFD")) {
    decoded = decoded.slice(0, -1);
  }
  return {
    text: decoded,
    bytes: parseTotalBytes(res, collected, truncated),
  };
}

export function FileBadge({ name, kind }: { name: string; kind: FileKind }) {
  return (
    <span
      className="relative grid h-11 w-9 shrink-0 place-items-end justify-center rounded-[7px] pb-1.5 text-[9px] font-extrabold tracking-wide text-white"
      style={{ background: COLORS[kind], clipPath: "polygon(0 0, 68% 0, 100% 24%, 100% 100%, 0 100%)" }}
      aria-hidden="true"
    >
      {fileBadge(name)}
    </span>
  );
}

/** A document's first page from its own words (a page file, drawn in a frame that reaches nothing). */
function TextPage({ name, title, text }: { name: string; title: string; text: string }) {
  const kind = fileKind(name);
  if (kind === "web") {
    return (
      <iframe
        title={`预览：${title}`}
        sandbox="allow-scripts"
        referrerPolicy="no-referrer"
        srcDoc={filePageDocument(text)}
        loading="lazy"
        className="pointer-events-none h-full w-full border-0 bg-white"
      />
    );
  }
  if (kind === "table") {
    const rows = csvRows(text, fileEnding(name) === "tsv" ? "\t" : ",");
    return (
      <table className="m-3 text-left text-[11.5px] leading-snug">
        <tbody>
          {rows.map((row, r) => (
            <tr key={r} className={r === 0 ? "font-semibold" : ""} style={r === 0 ? { borderBottom: "1px solid var(--c-border)" } : undefined}>
              {row.map((cell, c) => (
                <td key={c} className="max-w-[9rem] truncate py-1 pr-4">{cell}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    );
  }
  const start = text.slice(0, 2400);
  if (kind === "markdown") {
    return (
      // The page in small, as a document's first page looks (the renderer sets its own sizes).
      <div className="px-5 py-4" style={{ zoom: 0.72 }}>
        <MarkdownText>{markdownPreview(start)}</MarkdownText>
      </div>
    );
  }
  return (
    <pre className={`m-0 whitespace-pre-wrap break-words px-4 py-3 leading-snug ${kind === "code" ? "font-mono text-[10.5px]" : "text-[12px]"}`}>
      {start}
    </pre>
  );
}

/** What it is writing, as the file will look, while it works; the file's own card takes over once sent. */
export function DraftCard({ draft }: { draft: FileDraft }) {
  const kind = fileKind(draft.name);
  const title = fileTitle(draft.name);
  return (
    <div
      className="c-bubble-agent c-rise flex w-[min(400px,85vw)] flex-col overflow-hidden rounded-[22px]"
      style={{ border: "1px solid var(--c-hairline)" }}
      role="status"
      aria-label={`${draft.done ? "已写好" : "正在写"}「${title}」`}
    >
      <div className="relative mx-2 mt-2 h-[200px] overflow-hidden rounded-[15px]" style={{ background: "var(--c-surface)", border: "1px solid var(--c-border)" }}>
        <TextPage name={draft.name} title={title} text={draft.content} />
        {isTextKind(kind) && (
          <span className="pointer-events-none absolute inset-x-0 bottom-0 h-10" style={{ background: "linear-gradient(to bottom, transparent, var(--c-surface))" }} />
        )}
      </div>
      <div className="flex min-h-14 items-center gap-3 px-3.5 py-2.5">
        <FileBadge name={draft.name} kind={kind} />
        <span className="min-w-0 flex-1">
          <span className="block truncate text-[15px] font-semibold">{title}</span>
          <span className="c-muted block text-[12.5px]">{draft.done ? "已写好" : "正在写…"}</span>
        </span>
      </div>
    </div>
  );
}

/** First pages already drawn this session, by file link. */
const pdfPages = new Map<string, string>();

function useAndroidPdfPage(url: string | undefined): string | null {
  const [picture, setPicture] = useState<string | null>(() => (url ? pdfPages.get(url) ?? null : null));
  useEffect(() => {
    const android = androidShell();
    if (!url || !android?.pdfPreview) return;
    const known = pdfPages.get(url);
    if (known !== undefined) return setPicture(known || null);
    const id = Math.random().toString(36).slice(2);
    const take = (event: Event) => {
      const answer = (event as CustomEvent<{ id: string; image: string }>).detail;
      if (answer?.id !== id) return;
      pdfPages.set(url, answer.image);
      setPicture(answer.image || null);
    };
    window.addEventListener("companion:pdf-preview", take);
    android.pdfPreview(new URL(url, window.location.href).href, id);
    return () => window.removeEventListener("companion:pdf-preview", take);
  }, [url]);
  return picture;
}

export function FileCard({ item, onOpen }: { item: UIMediaAttachment; onOpen: (event: MouseEvent) => void }) {
  const name = item.name ?? "文件";
  const kind = fileKind(name);
  const title = fileTitle(name);
  const read = !!item.url && (isTextKind(kind) || kind === "web");
  // Only where the browser draws a PDF itself (Android's shows a download button in a frame instead).
  const pdfPage = kind === "pdf" && !!item.url && navigator.pdfViewerEnabled === true;
  const [text, setText] = useState<string | null>(null);
  const [bytes, setBytes] = useState<number | null>(null);

  useEffect(() => {
    if (!read || !item.url) return;
    let alive = true;
    const controller = new AbortController();
    const limit = kind === "web" ? PAGE_LIMIT : TEXT_PREVIEW;
    fetchBoundedTextPreview(item.url, limit, controller.signal)
      .then(({ text: preview, bytes: total }) => {
        if (!alive) return;
        setBytes(total);
        setText(preview);
      })
      .catch(() => {
        if (alive && !controller.signal.aborted) setText("");
      });
    return () => {
      alive = false;
      controller.abort();
    };
  }, [read, item.url, kind]);

  // In the Android app the phone draws a PDF's first page (PdfRenderer) for its paper.
  const pdfPicture = useAndroidPdfPage(kind === "pdf" && !pdfPage ? item.url : undefined);

  const page = (() => {
    if (pdfPicture) {
      return <img src={pdfPicture} alt="" className="pointer-events-none h-full w-full bg-white object-cover object-top" />;
    }
    if (pdfPage) {
      return <iframe title={`预览：${title}`} src={`${item.url}#toolbar=0&navpanes=0&scrollbar=0&view=FitH`} loading="lazy" className="pointer-events-none h-full w-full border-0 bg-white" />;
    }
    if (!read || text === "") return null;
    if (text === null) return <div className="h-full w-full animate-pulse motion-reduce:animate-none" style={{ background: "var(--c-fill)" }} />;
    return <TextPage name={name} title={title} text={text} />;
  })();

  const words = [fileWords(kind), bytes !== null ? fileSize(bytes) : null].filter(Boolean).join(" · ");
  return (
    <div className="c-bubble-agent flex w-[min(400px,85vw)] flex-col overflow-hidden rounded-[22px]" style={{ border: "1px solid var(--c-hairline)" }}>
      {page && (
        <a
          href={item.url}
          target="_blank"
          rel="noreferrer"
          onClick={onOpen}
          aria-label={`打开${fileWords(kind)}「${title}」`}
          className="relative mx-2 mt-2 block h-[200px] overflow-hidden rounded-[15px]"
          style={{ background: "var(--c-surface)", border: "1px solid var(--c-border)" }}
        >
          {page}
          {(isTextKind(kind)) && (
            <span
              className="pointer-events-none absolute inset-x-0 bottom-0 h-10"
              style={{ background: "linear-gradient(to bottom, transparent, var(--c-surface))" }}
            />
          )}
        </a>
      )}
      <a href={item.url} target="_blank" rel="noreferrer" onClick={onOpen} className="c-pressable flex min-h-14 items-center gap-3 px-3.5 py-2.5">
        <FileBadge name={name} kind={kind} />
        <span className="min-w-0 flex-1">
          <span className="block truncate text-[15px] font-semibold">{title}</span>
          <span className="c-muted block text-[12.5px]">{words}</span>
        </span>
      </a>
      {kind === "audio" && item.url && <audio controls preload="none" src={item.url} className="mx-3 mb-3 w-[calc(100%-1.5rem)]" />}
    </div>
  );
}
