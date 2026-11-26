import { copyTextToClipboard } from "@/lib/clipboard";
import { fetchWithTimeout } from "@/lib/http";

import { androidShell } from "./android-shell";

export interface ExportThreadResult {
  markdown: string;
  filename: string;
  method: "android" | "download" | "clipboard";
}

/** Parse RFC 5987/legacy filename from Content-Disposition or synthesize a clean `.md` name. */
export function parseExportFilename(
  disposition: string | null,
  title: string | undefined,
  chatId: string,
): string {
  if (disposition) {
    const star = /filename\*\s*=\s*(?:UTF-8|utf-8)''([^;\s]+)/i.exec(disposition);
    if (star?.[1]) {
      try {
        const decoded = decodeURIComponent(star[1].replace(/^["']|["']$/g, "")).trim();
        if (decoded) return decoded;
      } catch {
        // Fall through to plain filename / fallback.
      }
    }
    const plain = /filename\s*=\s*"([^"]+)"|filename\s*=\s*([^;\s]+)/i.exec(disposition);
    const raw = (plain?.[1] ?? plain?.[2] ?? "").trim();
    if (raw) return raw;
  }
  const cleanTitle = (title ?? "").replace(/[\\/:*?"<>|\r\n\t]+/g, " ").trim().slice(0, 40);
  return `${cleanTitle || `chat-${chatId.slice(0, 8)}`}.md`;
}

function downloadMarkdown(markdown: string, filename: string): boolean {
  if (typeof document === "undefined" || typeof URL === "undefined" || typeof URL.createObjectURL !== "function") {
    return false;
  }
  const blob = new Blob([markdown], { type: "text/markdown;charset=utf-8" });
  const objectUrl = URL.createObjectURL(blob);
  try {
    const link = document.createElement("a");
    link.href = objectUrl;
    link.download = filename;
    link.style.display = "none";
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
    return true;
  } catch {
    return false;
  } finally {
    window.setTimeout(() => URL.revokeObjectURL(objectUrl), 1_000);
  }
}

/** Export a conversation as Markdown and share natively on Android, download as `.md`, or copy to clipboard. */
export async function exportThread(
  token: string,
  chatId: string,
  options: { title?: string; mode?: "auto" | "download" | "copy" } = {},
): Promise<ExportThreadResult> {
  const key = chatId.startsWith("websocket:") ? chatId : `websocket:${chatId}`;
  const res = await fetchWithTimeout(
    `/api/sessions/${encodeURIComponent(key)}/export?format=md`,
    { headers: { Authorization: `Bearer ${token}` }, credentials: "same-origin" },
    20_000,
  );
  if (!res.ok) {
    let message = `导出失败（HTTP ${res.status}）`;
    try {
      const payload = (await res.json()) as { error?: string };
      if (payload?.error) message = payload.error;
    } catch {
      // Keep status message for non-JSON error responses.
    }
    throw new Error(message);
  }

  const markdown = await res.text();
  const filename = parseExportFilename(res.headers.get("Content-Disposition"), options.title, chatId);
  const shareTitle = options.title?.trim() || filename.replace(/\.md$/i, "");
  const mode = options.mode ?? "auto";
  const android = androidShell();

  android?.haptic?.("light");

  if (mode === "copy") {
    await copyTextToClipboard(markdown);
    return { markdown, filename, method: "clipboard" };
  }

  if (mode === "auto" && android?.shareText) {
    android.shareText(markdown, shareTitle);
    return { markdown, filename, method: "android" };
  }

  if (downloadMarkdown(markdown, filename)) {
    return { markdown, filename, method: "download" };
  }

  await copyTextToClipboard(markdown);
  return { markdown, filename, method: "clipboard" };
}
