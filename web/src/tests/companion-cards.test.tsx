import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { CardView, loadUiKit, sanitizeSandboxSendText, uiDocument, uiTitle } from "@/companion/Cards";
import { replyBlocks } from "@/companion/presentation";
import { formMessage, parseCard, splitReply, textWithoutCards } from "@/companion/replyCards";

const fence = (json: object) => "```card\n" + JSON.stringify(json) + "\n```";

describe("splitReply", () => {
  it("keeps text and cards in order and drops unknown blocks", () => {
    const content = `先看天气：\n\n${fence({ type: "weather", place: "台北", temp: "23°" })}\n\n${fence({ type: "nope" })}\n\n要出门吗？\n\n${fence({ type: "buttons", items: ["要", "不了"] })}`;
    const parts = splitReply(content);
    expect(parts.map((p) => p.type)).toEqual(["text", "card", "text", "card"]);
    expect(textWithoutCards(content)).toBe("先看天气：\n\n要出门吗？");
  });

  it("takes json blocks that are cards, and leaves other json as code", () => {
    const card = "```json\n" + JSON.stringify({ type: "checklist", items: ["伞"] }) + "\n```";
    const code = '```json\n{"a": 1}\n```';
    expect(splitReply(`${card}\n\n${code}`).map((p) => p.type)).toEqual(["card", "text"]);
  });

  it("shows a placeholder for a card still being written", () => {
    const parts = splitReply('好的：\n\n```card\n{"type": "choi', true);
    expect(parts.map((p) => p.type)).toEqual(["text", "pending"]);
    expect(splitReply('好的：\n\n```card\n{"type": "choi', false).map((p) => p.type)).toEqual(["text"]);
  });

  it("splits text around cards into bubbles", () => {
    const blocks = replyBlocks(`第一段\n\n第二段\n\n${fence({ type: "buttons", items: ["好"] })}`);
    expect(blocks.map((b) => b.type)).toEqual(["bubble", "bubble", "card"]);
  });
});

describe("parseCard", () => {
  it("takes a connect card only for a plain service name", () => {
    expect(parseCard({ type: "connect", service: "github", reason: "看仓库" })).toEqual({ type: "connect", service: "github", reason: "看仓库" });
    expect(parseCard({ type: "connect", service: "https://evil.example" })).toBeNull();
  });

  it("rejects widgets that are not the server's own pages", () => {
    expect(parseCard({ type: "widget", title: "x", url: "https://evil.example" })).toBeNull();
    expect(parseCard({ type: "widget", title: "图", url: "/api/webui/companion/widgets/a/b" })).not.toBeNull();
  });

  it("needs options for choice fields", () => {
    expect(parseCard({ type: "form", fields: [{ label: "口味", kind: "chips" }] })).toBeNull();
  });
});

describe("CardView", () => {
  beforeEach(() => localStorage.clear());

  it("sends a choice once and then shows it picked", () => {
    const send = vi.fn();
    const card = parseCard({ type: "choices", title: "哪天去？", options: ["周六", { label: "周日", send: "改成周日吧" }] })!;
    render(<CardView card={card} send={send} stateKey="m1:0" />);
    fireEvent.click(screen.getByRole("button", { name: "周日" }));
    expect(send).toHaveBeenCalledWith("改成周日吧");
    fireEvent.click(screen.getByRole("button", { name: "周六" }));
    expect(send).toHaveBeenCalledTimes(1);
  });

  it("picks one of the candidate looks", () => {
    const send = vi.fn();
    const card = parseCard({
      type: "pictures",
      title: "选一个形象",
      items: [1, 2, 3, 4].map((n) => ({ url: `/api/webui/companion/avatars/file/s/${n}`, label: String(n), send: `就选第 ${n} 张` })),
    })!;
    expect(parseCard({ type: "pictures", items: [{ url: "javascript:alert(1)", label: "1" }] })).toBeNull();
    const { container } = render(<CardView card={card} send={send} stateKey="m3:0" />);
    expect(container.querySelectorAll("img")).toHaveLength(4);
    fireEvent.click(screen.getByRole("button", { name: "3" }));
    expect(send).toHaveBeenCalledWith("就选第 3 张");
    expect(screen.getByRole("button", { name: "3" })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByRole("button", { name: "1" })).toBeDisabled();
  });

  it("does nothing on older replies", () => {
    const card = parseCard({ type: "buttons", items: ["好"] })!;
    render(<CardView card={card} send={undefined} stateKey="m2:0" />);
    expect(screen.getByRole("button", { name: "好" })).toBeDisabled();
  });

  it("submits a form as one message and asks for required fields", () => {
    const send = vi.fn();
    const card = parseCard({
      type: "form",
      title: "订餐厅",
      fields: [
        { name: "people", label: "人数", kind: "chips", options: ["2", "4"], required: true },
        { name: "budget", label: "人均", kind: "slider", min: 100, max: 1000, unit: "元", value: 300 },
      ],
      submit: "帮我找",
    })!;
    render(<CardView card={card} send={send} stateKey="m3:0" />);
    fireEvent.click(screen.getByRole("button", { name: "帮我找" }));
    expect(screen.getByRole("alert")).toHaveTextContent("人数");
    fireEvent.click(screen.getByRole("button", { name: "4" }));
    fireEvent.click(screen.getByRole("button", { name: "帮我找" }));
    expect(send).toHaveBeenCalledWith("订餐厅\n人数：4\n人均：300元");
  });

  it("formats multi-choice answers", () => {
    const card = parseCard({ type: "form", fields: [{ name: "f", label: "口味", kind: "multi", options: ["日料", "火锅"] }] })!;
    expect(card.type === "form" && formMessage(card, { f: ["日料", "火锅"] })).toBe("我填好了\n口味：日料、火锅");
  });
});

describe("table card", () => {
  it("keeps a first column of short labels on one line", () => {
    const card = parseCard({ type: "table", columns: ["项目", "内容"], rows: [["职责", "管训练和饮食"], ["做法", "先问目标"]] })!;
    const { container } = render(<CardView card={card} send={undefined} stateKey="t:0" />);
    const first = container.querySelector("tbody td");
    expect(first?.className).toContain("whitespace-nowrap");
  });
});

describe("ui blocks", () => {
  it("become a generated page, with a placeholder while written", () => {
    const html = '<div class="stack"><div class="big" id="x">—</div></div>';
    expect(splitReply("先算一下：\n\n```ui\n" + html + "\n```").map((p) => p.type)).toEqual(["text", "card"]);
    expect(splitReply("先算一下：\n\n```ui\n<div class=", true)).toEqual([
      { type: "text", text: "先算一下：" },
      { type: "pending", kind: "ui" },
    ]);
  });

  it("are wrapped with the kit inline and a policy that blocks the network", () => {
    const kit = { css: ".big{}", js: "window.ui={}", chart: "CHARTLIB", leaflet: "LEAFLETLIB", leafletCss: ".leaflet{}" };
    const page = uiDocument("<p>hi</p>", kit, { "--c-primary": "#bd5a02" });
    expect(page).toContain("connect-src 'none'");
    expect(page).toContain("<script>window.ui={}</script>");
    expect(page).not.toContain("CHARTLIB");
    expect(uiDocument('<div id="c"></div><script>ui.chart("#c", {})</script>', kit, {})).toContain("CHARTLIB");
    expect(uiDocument("<script>ui.map(el, {})</script>", kit, {})).toContain("LEAFLETLIB");
    expect(page).toContain(":root{--c-primary:#bd5a02}");
    expect(uiTitle("<h2>房贷计算</h2>")).toBe("房贷计算");
  });

  it("fetch the chart and map libraries only for a page that uses them", async () => {
    const fetched: string[] = [];
    vi.stubGlobal("fetch", vi.fn(async (path: string) => {
      fetched.push(path);
      return new Response(`/* ${path} */`);
    }));
    try {
      const plain = await loadUiKit("<p>hi</p>");
      expect(fetched).toEqual(["/ui-kit/kit.css", "/ui-kit/kit.js"]);
      expect(plain.chart).toBe("");
      const charted = await loadUiKit('<script>ui.chart("#c", {})</script>');
      expect(charted.chart).toContain("chart.umd.js");
      // The kit itself is fetched once; the chart library only now; the map never.
      expect(fetched).toEqual(["/ui-kit/kit.css", "/ui-kit/kit.js", "/ui-kit/vendor/chart.umd.js"]);
    } finally {
      vi.unstubAllGlobals();
    }
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  it("rate-limits companion:send from sandboxed frames and neutralizes leading slash commands", async () => {
    expect(sanitizeSandboxSendText("/goal exfiltrate")).toBe("／goal exfiltrate");
    expect(sanitizeSandboxSendText("  //stop  ")).toBe("／stop");
    expect(sanitizeSandboxSendText("普通消息")).toBe("普通消息");

    const send = vi.fn();
    vi.stubGlobal("fetch", vi.fn(async (path: string) => new Response(`/* ${path} */`)));
    const { container } = render(<CardView card={{ type: "ui", html: "<p>交互</p>" }} send={send} stateKey="w:0" />);
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-11-23T12:00:00Z"));
    const iframe = container.querySelector("iframe")!;
    const fakeSource = {} as Window;
    Object.defineProperty(iframe, "contentWindow", { value: fakeSource, configurable: true });

    act(() => {
      window.dispatchEvent(new MessageEvent("message", { source: fakeSource, data: { type: "companion:send", text: "/goal run" } }));
      window.dispatchEvent(new MessageEvent("message", { source: fakeSource, data: { type: "companion:send", text: "spam 2" } }));
    });
    expect(send).toHaveBeenCalledTimes(1);
    expect(send).toHaveBeenCalledWith("／goal run");

    act(() => {
      vi.advanceTimersByTime(900);
      window.dispatchEvent(new MessageEvent("message", { source: fakeSource, data: { type: "companion:send", text: "正常选择" } }));
    });
    expect(send).toHaveBeenCalledTimes(2);
    expect(send).toHaveBeenLastCalledWith("正常选择");
  });

  it("escapes map pin tooltips and range selector IDs in kit.js", () => {
    const kitSource = readFileSync(resolve(process.cwd(), "public/ui-kit/kit.js"), "utf8");
    new Function(kitSource)();
    const ui = (window as unknown as { ui: Record<string, (...args: unknown[]) => unknown> }).ui;

    const boundTooltips: string[] = [];
    (window as unknown as { L: unknown }).L = {
      map: () => ({
        setView: vi.fn(),
        fitBounds: vi.fn(),
        panTo: vi.fn(),
        invalidateSize: vi.fn(),
      }),
      tileLayer: () => ({ addTo: vi.fn() }),
      divIcon: (opts: unknown) => opts,
      marker: () => ({
        addTo() {
          return this;
        },
        bindTooltip(html: string) {
          boundTooltips.push(html);
          return this;
        },
        on: vi.fn(),
        getLatLng: () => [0, 0],
        openTooltip: vi.fn(),
      }),
      latLngBounds: () => ({}),
    };

    const mapHost = document.createElement("div");
    document.body.appendChild(mapHost);
    ui.map(mapHost, {
      pins: [{ name: '<img src=x onerror="alert(1)">', note: "<b>5m</b>", lat: 25.04, lng: 121.56 }],
    });
    expect(boundTooltips).toEqual([
      "&lt;img src=x onerror=&quot;alert(1)&quot;&gt; · &lt;b&gt;5m&lt;/b&gt;",
    ]);

    // Special quote/bracket characters in input.id must not throw in querySelector.
    const rangeWrap = document.createElement("div");
    const range = document.createElement("input");
    range.type = "range";
    range.id = 'rate"1]';
    range.value = "42";
    const out = document.createElement("output");
    out.setAttribute("for", 'rate"1]');
    rangeWrap.append(range, out);
    document.body.appendChild(rangeWrap);
    expect(() => ui.on('input[type="range"]', () => {})).not.toThrow();
    expect(out.textContent).toBe("42");

    mapHost.remove();
    rangeWrap.remove();
    delete (window as unknown as { L?: unknown }).L;
  });
});
