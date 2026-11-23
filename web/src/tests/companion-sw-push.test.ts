import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { describe, expect, it, vi } from "vitest";

const SW_SCRIPT = readFileSync(resolve(process.cwd(), "public/sw.js"), "utf8");
const ORIGIN = "https://bot.test";

function loadSw(windows: { url: string; focus: ReturnType<typeof vi.fn> }[] = []) {
  const listeners = new Map<string, (event: Record<string, unknown>) => void>();
  const showNotification = vi.fn(async () => undefined);
  const openWindow = vi.fn(async () => undefined);
  const self = {
    location: { origin: ORIGIN },
    skipWaiting: vi.fn(),
    registration: { showNotification },
    clients: { claim: vi.fn(), matchAll: vi.fn(async () => windows), openWindow },
    addEventListener: (type: string, cb: (event: Record<string, unknown>) => void) => listeners.set(type, cb),
  };
  new Function("self", "caches", "fetch", SW_SCRIPT)(self, {}, vi.fn());
  const fire = async (type: string, event: Record<string, unknown>) => {
    let wait: Promise<unknown> = Promise.resolve();
    listeners.get(type)?.({ ...event, waitUntil: (p: Promise<unknown>) => (wait = p) });
    await wait;
  };
  return { fire, showNotification, openWindow };
}

describe("companion push", () => {
  it("shows the pushed message as a notification that opens the companion", async () => {
    const sw = loadSw();
    const payload = { title: "伙伴", body: "该喝水了", tag: "cron", url: "/companion.html" };
    await sw.fire("push", { data: { json: () => payload, text: () => JSON.stringify(payload) } });
    expect(sw.showNotification).toHaveBeenCalledWith(
      "伙伴",
      expect.objectContaining({ body: "该喝水了", tag: "cron", data: { url: "/companion.html" } }),
    );
  });

  it("focuses an open companion window, otherwise opens one", async () => {
    const focus = vi.fn(async () => undefined);
    const notification = { close: vi.fn(), data: { url: "/companion.html" } };

    const withWindow = loadSw([{ url: `${ORIGIN}/companion.html`, focus }]);
    await withWindow.fire("notificationclick", { notification });
    expect(focus).toHaveBeenCalled();
    expect(withWindow.openWindow).not.toHaveBeenCalled();

    const without = loadSw([]);
    await without.fire("notificationclick", { notification });
    expect(without.openWindow).toHaveBeenCalledWith(`${ORIGIN}/companion.html`);
  });

  it("blocks cross-origin notification URLs and falls back to /companion.html", async () => {
    const sw = loadSw([]);
    await sw.fire("notificationclick", {
      notification: { close: vi.fn(), data: { url: "https://evil.example/phish" } },
    });
    expect(sw.openWindow).toHaveBeenCalledWith(`${ORIGIN}/companion.html`);
  });

  it("prunes stale hashed assets on activate when only /companion.html is cached while preserving /companion-manifest.json", async () => {
    const store = new Map<string, { text: () => Promise<string>; json: () => Promise<unknown> }>();
    store.set(`${ORIGIN}/companion.html`, {
      text: async () => '<!doctype html><script type="module" src="/assets/companion-NEW.js"></script>',
      json: async () => ({}),
    });
    store.set(`${ORIGIN}/companion-manifest.json`, {
      text: async () => "{}",
      json: async () => ({}),
    });
    store.set(`${ORIGIN}/asset-manifest.json`, {
      text: async () => "{}",
      json: async () => ({
        "companion.html": { file: "assets/companion-NEW.js", css: ["assets/companion-NEW.css"] },
      }),
    });
    store.set(`${ORIGIN}/assets/companion-NEW.js`, { text: async () => "", json: async () => ({}) });
    store.set(`${ORIGIN}/assets/companion-NEW.css`, { text: async () => "", json: async () => ({}) });
    store.set(`${ORIGIN}/assets/companion-OLD.js`, { text: async () => "", json: async () => ({}) });
    store.set(`${ORIGIN}/brand/companion_icon_192.png`, { text: async () => "", json: async () => ({}) });

    const cache = {
      match: vi.fn(async (key: string | { url: string }) => {
        const raw = typeof key === "string" ? key : key.url;
        const url = new URL(raw, ORIGIN).href;
        return store.get(url);
      }),
      keys: vi.fn(async () => Array.from(store.keys()).map((url) => ({ url }))),
      delete: vi.fn(async (req: { url: string }) => store.delete(req.url)),
    };
    const caches = {
      keys: vi.fn(async () => ["nanobot-static-v3", "nanobot-static-v2"]),
      delete: vi.fn(async () => true),
      open: vi.fn(async () => cache),
    };

    const listeners = new Map<string, (event: Record<string, unknown>) => void>();
    const self = {
      location: { origin: ORIGIN },
      skipWaiting: vi.fn(),
      registration: { showNotification: vi.fn() },
      clients: { claim: vi.fn(), matchAll: vi.fn(async () => []), openWindow: vi.fn() },
      addEventListener: (type: string, cb: (event: Record<string, unknown>) => void) => listeners.set(type, cb),
    };
    new Function("self", "caches", "fetch", SW_SCRIPT)(self, caches, vi.fn());

    let wait: Promise<unknown> = Promise.resolve();
    listeners.get("activate")?.({ waitUntil: (p: Promise<unknown>) => (wait = p) });
    await wait;

    expect(caches.delete).toHaveBeenCalledWith("nanobot-static-v2");
    expect(store.has(`${ORIGIN}/assets/companion-OLD.js`)).toBe(false);
    expect(store.has(`${ORIGIN}/assets/companion-NEW.js`)).toBe(true);
    expect(store.has(`${ORIGIN}/assets/companion-NEW.css`)).toBe(true);
    expect(store.has(`${ORIGIN}/companion.html`)).toBe(true);
    expect(store.has(`${ORIGIN}/companion-manifest.json`)).toBe(true);
    expect(store.has(`${ORIGIN}/brand/companion_icon_192.png`)).toBe(true);
  });
});
