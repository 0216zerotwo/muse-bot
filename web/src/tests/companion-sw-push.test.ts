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
});
