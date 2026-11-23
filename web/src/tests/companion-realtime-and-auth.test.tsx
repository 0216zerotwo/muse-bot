import { act, cleanup, fireEvent, render, renderHook, screen, waitFor } from "@testing-library/react";
import { useState } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { CompanionApp } from "@/companion/CompanionApp";
import { consumePairCode, pairDevice, PairError } from "@/companion/device";
import { useNanobotStream } from "@/hooks/useNanobotStream";
import { fetchAutomationRunResult } from "@/lib/api";
import { deriveWsUrl } from "@/lib/bootstrap";
import { LOCAL_PREFS_STORAGE_KEY } from "@/lib/local-preferences";
import { NanobotClient } from "@/lib/nanobot-client";
import type { UIMessage } from "@/lib/types";
import { ClientProvider } from "@/providers/ClientProvider";

const { shellBehavior } = vi.hoisted(() => ({
  shellBehavior: { shouldThrow: false },
}));

vi.mock("@/companion/CompanionShell", () => ({
  CompanionShell: () => {
    if (shellBehavior.shouldThrow) {
      throw new Error("render boom");
    }
    return <div data-testid="companion-shell">shell-ok</div>;
  },
}));

class FakeSocket {
  readyState = 0; // CONNECTING
  sent: string[] = [];
  onopen: (() => void) | null = null;
  onmessage: ((ev: MessageEvent) => void) | null = null;
  onerror: (() => void) | null = null;
  onclose: ((ev: { code?: number }) => void) | null = null;

  constructor(public readonly url: string) {}

  send(data: string) {
    if (this.readyState !== 1) throw new Error("socket not open");
    this.sent.push(data);
  }

  open() {
    this.readyState = 1;
    this.onopen?.();
  }

  receive(payload: unknown) {
    this.onmessage?.(new MessageEvent("message", { data: JSON.stringify(payload) }));
  }

  close(code = 1000) {
    this.readyState = 3;
    this.onclose?.({ code });
  }
}

afterEach(() => {
  cleanup();
  shellBehavior.shouldThrow = false;
  vi.useRealTimers();
  vi.unstubAllGlobals();
  localStorage.clear();
  window.history.replaceState(null, "", "/companion.html");
});

describe("NanobotClient realtime resilience & cross-stack events", () => {
  it("queues requestMutation while CONNECTING and during reconnect, and rejects when idle or closed", async () => {
    vi.useFakeTimers();
    const sockets: FakeSocket[] = [];
    const idleClient = new NanobotClient({
      url: "ws://127.0.0.1:8766/ws",
      reconnect: false,
      socketFactory: (url) => {
        const s = new FakeSocket(url);
        sockets.push(s);
        return s as unknown as WebSocket;
      },
    });

    await expect(idleClient.requestMutation("companion.test", {})).rejects.toMatchObject({ status: 503 });

    const client = new NanobotClient({
      url: "ws://127.0.0.1:8766/ws",
      socketFactory: (url) => {
        const s = new FakeSocket(url);
        sockets.push(s);
        return s as unknown as WebSocket;
      },
    });
    client.connect();
    expect(sockets).toHaveLength(1);
    expect(sockets[0].readyState).toBe(0);

    // 1. Mutation issued while socket is still CONNECTING queues until open.
    const connectingMutation = client.requestMutation<{ ok: boolean }>("companion.approvals.decide", { id: "apv_1" });
    expect(sockets[0].sent).toHaveLength(0);

    sockets[0].open();
    expect(sockets[0].sent).toHaveLength(1);
    const req1 = JSON.parse(sockets[0].sent[0]) as { request_id: string; action: string };
    expect(req1.action).toBe("companion.approvals.decide");
    sockets[0].receive({ event: "webui_response", request_id: req1.request_id, ok: true, result: { ok: true } });
    await expect(connectingMutation).resolves.toEqual({ ok: true });

    // 2. Mutation issued during foreground reconnectNow(true) queues and flushes on the new socket.
    client.reconnectNow(true);
    const reconnectMutation = client.requestMutation<{ saved: number }>("companion.models.update", { a: 1 });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(550);
    });
    expect(sockets).toHaveLength(2);
    sockets[1].open();
    const req2 = JSON.parse(sockets[1].sent[0]) as { request_id: string; action: string };
    expect(req2.action).toBe("companion.models.update");
    sockets[1].receive({ event: "webui_response", request_id: req2.request_id, ok: true, result: { saved: 2 } });
    await expect(reconnectMutation).resolves.toEqual({ saved: 2 });

    // 3. Explicit close() rejects in-flight mutations immediately.
    const aborted = client.requestMutation("companion.never", {});
    client.close();
    await expect(aborted).rejects.toMatchObject({ status: 503 });
  });

  it("isolates throwing chat listeners and dispatches feed_updated, approval_updated, and updated upload tokens", async () => {
    const sockets: FakeSocket[] = [];
    const fetchCalls: { url: string; auth: string | null }[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const headers = new Headers(init?.headers);
        fetchCalls.push({ url: String(input), auth: headers.get("Authorization") });
        return new Response(JSON.stringify({ reference: "att-ref-1" }), { status: 200 });
      }),
    );

    const client = new NanobotClient({
      url: "ws://127.0.0.1:8766/ws?token=ws-old",
      socketFactory: (url) => {
        const s = new FakeSocket(url);
        sockets.push(s);
        return s as unknown as WebSocket;
      },
    });
    client.connect();
    sockets[0].open();
    sockets[0].receive({
      event: "ready",
      chat_id: "main",
      upload: { path: "/api/attachments", token: "ws-old" },
    });

    const onFeed = vi.fn();
    const onApproval = vi.fn();
    client.onFeedUpdate(onFeed);
    client.onApprovalUpdate(onApproval);

    const secondListener = vi.fn();
    client.onChat("main", () => {
      throw new TypeError("Illegal constructor");
    });
    client.onChat("main", secondListener);

    expect(() => sockets[0].receive({ event: "turn_end", chat_id: "main", turn_id: "t1" })).not.toThrow();
    expect(secondListener).toHaveBeenCalledTimes(1);

    sockets[0].receive({ event: "feed_updated" });
    sockets[0].receive({ event: "approval_updated" });
    expect(onFeed).toHaveBeenCalledTimes(1);
    expect(onApproval).toHaveBeenCalledTimes(1);

    // Renewing token updates attachment upload Bearer header without dropping the live socket.
    client.updateUploadToken("ws-fresh-24h");
    const uploadPromise = client.sendAttachments("main", "看图", [
      { data_url: "data:image/png;base64,iVBORw0KGgo=", name: "pic.png" },
    ]);
    await waitFor(() => expect(fetchCalls).toHaveLength(1));
    expect(fetchCalls[0].auth).toBe("Bearer ws-fresh-24h");

    // Confirm delivery receipt so sendAttachments resolves cleanly.
    const msgFrame = sockets[0].sent
      .map((raw) => JSON.parse(raw) as { type: string; turn_id?: string })
      .find((f) => f.type === "message");
    sockets[0].receive({ event: "message_accepted", chat_id: "main", turn_id: msgFrame?.turn_id });
    await uploadPromise;
    client.close();
  });
});

describe("useNanobotStream non-destructive history reconciliation & Android notifications", () => {
  it("preserves live regeneration stream deltas and local state across historyVersion updates", () => {
    const sockets: FakeSocket[] = [];
    const client = new NanobotClient({
      url: "ws://127.0.0.1:8766/ws",
      socketFactory: (url) => {
        const s = new FakeSocket(url);
        sockets.push(s);
        return s as unknown as WebSocket;
      },
    });
    client.connect();
    sockets[0].open();

    const userMsg: UIMessage = { id: "u1", role: "user", content: "讲个笑话", turnId: "t1", createdAt: 1 };
    const oldReply: UIMessage = { id: "a-old", role: "assistant", content: "旧的笑话", turnId: "t1", createdAt: 2 };

    let mountCount = 0;
    function StatefulHarness({
      initial,
      version,
    }: {
      initial: UIMessage[];
      version: number;
    }) {
      const [sheetOpen, setSheetOpen] = useState(false);
      useState(() => {
        mountCount += 1;
        return mountCount;
      });
      const stream = useNanobotStream("c1", initial, false, undefined, undefined, version);
      return (
        <ClientProvider client={client} token="tok">
          <button type="button" onClick={() => setSheetOpen(true)}>open-2fa</button>
          <span data-testid="sheet-state">{sheetOpen ? "open" : "closed"}</span>
          <span data-testid="bubbles">
            {stream.messages.map((m) => `${m.role}:${m.content}`).join("|")}
          </span>
        </ClientProvider>
      );
    }

    const wrapper = ({ children }: { children: React.ReactNode }) => (
      <ClientProvider client={client} token="tok">{children}</ClientProvider>
    );

    const { rerender } = render(<StatefulHarness initial={[userMsg, oldReply]} version={1} />, { wrapper });
    fireEvent.click(screen.getByText("open-2fa"));
    expect(screen.getByTestId("sheet-state")).toHaveTextContent("open");
    expect(screen.getByTestId("bubbles")).toHaveTextContent("user:讲个笑话|assistant:旧的笑话");

    // Regenerate starts streaming new delta frames over WS BEFORE HTTP thread reload finishes.
    act(() => {
      sockets[0].receive({ event: "goal_status", chat_id: "c1", turn_id: "t2", status: "running", started_at: 10 });
      sockets[0].receive({ event: "delta", chat_id: "c1", turn_id: "t2", turn_phase: "answer", text: "新的前半段" });
    });

    // HTTP reload finishes with oldReply deleted on disk (new turn not yet persisted).
    rerender(<StatefulHarness initial={[userMsg]} version={2} />);

    // Component did NOT remount, 2FA sheet stayed open, oldReply was removed, and live delta survived!
    expect(mountCount).toBe(1);
    expect(screen.getByTestId("sheet-state")).toHaveTextContent("open");
    expect(screen.getByTestId("bubbles")).toHaveTextContent("user:讲个笑话|assistant:新的前半段");

    // Subsequent WS deltas append seamlessly to the same live assistant bubble.
    act(() => {
      sockets[0].receive({ event: "delta", chat_id: "c1", turn_id: "t2", turn_phase: "answer", text: "＋后半段" });
      sockets[0].receive({ event: "stream_end", chat_id: "c1", turn_id: "t2", turn_phase: "answer" });
      sockets[0].receive({ event: "turn_end", chat_id: "c1", turn_id: "t2" });
    });
    expect(screen.getByTestId("bubbles")).toHaveTextContent("user:讲个笑话|assistant:新的前半段＋后半段");
    client.close();
  });

  it("falls back to ServiceWorkerRegistration.showNotification when Notification constructor throws on Android", async () => {
    const sockets: FakeSocket[] = [];
    const client = new NanobotClient({
      url: "ws://127.0.0.1:8766/ws",
      socketFactory: (url) => {
        const s = new FakeSocket(url);
        sockets.push(s);
        return s as unknown as WebSocket;
      },
    });
    client.connect();
    sockets[0].open();

    localStorage.setItem(LOCAL_PREFS_STORAGE_KEY, JSON.stringify({ schemaVersion: 1, browserNotifications: true }));
    Object.defineProperty(document, "visibilityState", { value: "hidden", configurable: true });
    const showNotification = vi.fn(async () => undefined);
    Object.defineProperty(navigator, "serviceWorker", {
      value: { ready: Promise.resolve({ showNotification }) },
      configurable: true,
    });
    const IllegalNotification = Object.assign(
      function () {
        throw new TypeError("Illegal constructor");
      },
      { permission: "granted" },
    );
    vi.stubGlobal("Notification", IllegalNotification);

    renderHook(() => useNanobotStream("c1", []), {
      wrapper: ({ children }) => <ClientProvider client={client} token="tok">{children}</ClientProvider>,
    });

    act(() => {
      sockets[0].receive({ event: "turn_end", chat_id: "c1", turn_id: "t1", outcome: "completed" });
    });

    await waitFor(() => expect(showNotification).toHaveBeenCalledTimes(1));
    expect(showNotification.mock.calls[0][0]).toBe("伙伴");
    Object.defineProperty(document, "visibilityState", { value: "visible", configurable: true });
    client.close();
  });
});

describe("CompanionApp token renewal retry, foreground check & ErrorBoundary", () => {
  it("retries failed token refresh with backoff, refreshes on foreground wake, and recovers from render errors", async () => {
    vi.useFakeTimers();
    let bootCount = 0;
    let failNextRefresh = true;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes("/webui/bootstrap")) {
          bootCount += 1;
          if (bootCount === 2 && failNextRefresh) {
            failNextRefresh = false;
            throw new Error("Network blip");
          }
          return new Response(
            JSON.stringify({
              ws_path: "/ws",
              token: `ws-tok-${bootCount}`,
              api_token: `api-tok-${bootCount}`,
              expires_in: 120,
            }),
            { status: 200, headers: { "Content-Type": "application/json" } },
          );
        }
        return new Response("{}", { status: 200 });
      }),
    );

    vi.stubGlobal(
      "WebSocket",
      class extends FakeSocket {
        static OPEN = 1;
        static CLOSING = 2;
      },
    );

    render(<CompanionApp />);
    await act(async () => {
      await Promise.resolve();
    });
    expect(screen.getByTestId("companion-shell")).toBeInTheDocument();
    expect(bootCount).toBe(1);

    // Scheduled renewal at 60s hits transient network blip (bootCount=2), then retries after 5s backoff (bootCount=3)!
    await act(async () => {
      await vi.advanceTimersByTimeAsync(60_100);
    });
    expect(bootCount).toBe(2);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(5_100);
    });
    expect(bootCount).toBe(3);

    // ErrorBoundary catches child render crashes and recovers when 重试 is clicked.
    cleanup();
    shellBehavior.shouldThrow = true;
    const errSpy = vi.spyOn(console, "error").mockImplementation(() => {});
    render(<CompanionApp />);
    await act(async () => {
      await Promise.resolve();
    });
    expect(screen.getByText(/页面出了一点小问题/)).toBeInTheDocument();
    shellBehavior.shouldThrow = false;
    fireEvent.click(screen.getByRole("button", { name: "重试" }));
    await act(async () => {
      await Promise.resolve();
    });
    expect(screen.getByTestId("companion-shell")).toBeInTheDocument();
    errSpy.mockRestore();
  });
});

describe("Pairing URL, HTTP status mapping, dev WS port & automation run endpoint", () => {
  it("extracts pair codes cleanly without crashing on malformed percent-encoding or extra hash params", () => {
    window.history.replaceState(null, "", "/companion.html#foo=1&pair=ABCD-2345&bar=2");
    expect(consumePairCode()).toBe("ABCD-2345");
    expect(window.location.hash).toBe("#foo=1&bar=2");

    window.history.replaceState(null, "", "/companion.html#pair=%E0%A4%A");
    expect(() => consumePairCode()).not.toThrow();
    expect(consumePairCode()).toBe("");
  });

  it("maps 5xx to offline, 429 to locked, and 401 to wrong_code in pairDevice", async () => {
    const mockStatus = (status: number, body: object = {}) =>
      vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify(body), { status })));

    mockStatus(502);
    await expect(pairDevice("ABCD-2345")).rejects.toEqual(new PairError("offline"));

    mockStatus(429);
    await expect(pairDevice("ABCD-2345")).rejects.toEqual(new PairError("locked"));

    mockStatus(401, { error: "invalid" });
    await expect(pairDevice("ABCD-2345")).rejects.toEqual(new PairError("wrong_code"));
  });

  it("targets gateway port 8766 on Vite dev port 5173 and encodes automation run detail paths", async () => {
    const origPort = window.location.port;
    Object.defineProperty(window, "location", {
      value: new URL("http://127.0.0.1:5173/companion.html"),
      configurable: true,
    });
    expect(deriveWsUrl("/ws", "abc")).toBe("ws://127.0.0.1:8766/ws?token=abc");
    expect(deriveWsUrl("/ws/computer", "abc")).toBe("ws://127.0.0.1:8766/ws/computer?token=abc");
    Object.defineProperty(window, "location", {
      value: new URL(`http://localhost:${origPort || "3000"}/companion.html`),
      configurable: true,
    });

    const requestedUrls: string[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        requestedUrls.push(String(input));
        return new Response(JSON.stringify({ id: "run-9", output: "今日早报内容" }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }),
    );
    const res = await fetchAutomationRunResult("tok", "job/1", "run-9");
    expect(requestedUrls).toEqual(["/api/webui/automations/job%2F1/runs/run-9"]);
    expect(res.response).toBe("今日早报内容");
  });
});
