import fs from "node:fs";
import path from "node:path";

import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { AppearanceDock } from "@/companion/AppearanceDock";
import { ApprovalCard } from "@/companion/ApprovalCard";
import { ConnectCard } from "@/companion/ConnectCard";
import { CredentialCard } from "@/companion/CredentialCard";
import { Viewer } from "@/companion/Viewer";
import type { ApprovalRecord } from "@/companion/companion-api";
import type { McpPresetInfo } from "@/lib/types";

const { requestMutation, fetchMcpPresetsMock, runMcpPresetActionMock } = vi.hoisted(() => ({
  requestMutation: vi.fn(),
  fetchMcpPresetsMock: vi.fn(),
  runMcpPresetActionMock: vi.fn(),
}));

vi.mock("@/providers/ClientProvider", () => {
  const client = { requestMutation };
  return { useClient: () => ({ client, token: "t" }) };
});

vi.mock("@/lib/api", async (original) => ({
  ...(await original<typeof import("@/lib/api")>()),
  fetchMcpPresets: (...args: unknown[]) => fetchMcpPresetsMock(...args),
  runMcpPresetAction: (...args: unknown[]) => runMcpPresetActionMock(...args),
}));

function makePreset(partial: Partial<McpPresetInfo> = {}): McpPresetInfo {
  return {
    name: "email",
    display_name: "通用邮箱",
    description: "读写你的邮箱",
    category: "productivity",
    installed: false,
    configured: false,
    enabled: false,
    install_supported: true,
    status: "not_installed",
    runtime_status: "stopped",
    tool_count: 0,
    required_fields: [
      { name: "IMAP_USER", label: "邮箱账号", required: true, secret: false, configured: false, placeholder: "me@example.com" },
      { name: "IMAP_PASS", label: "授权码", required: true, secret: true, configured: false, placeholder: "输入授权码" },
    ],
    ...partial,
  };
}

beforeEach(() => {
  vi.clearAllMocks();
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("Viewer zoom controls, keyboard shortcuts, and error/loading recovery", () => {
  it("supports double-click 1x/2x toggle, +/-/reset toolbar buttons, and keyboard shortcuts", () => {
    render(
      <Viewer
        items={[{ key: "img-1", kind: "image", url: "/media/cat.png", name: "cat.png", title: "橘猫" }]}
        start={0}
        onClose={vi.fn()}
      />,
    );

    const img = screen.getByRole("img", { name: "橘猫" });
    expect(screen.getByRole("status", { name: "正在加载图片" })).toBeInTheDocument();
    fireEvent.load(img);
    expect(screen.queryByRole("status", { name: "正在加载图片" })).not.toBeInTheDocument();

    const zoomInBtn = screen.getByRole("button", { name: "放大" });
    const zoomOutBtn = screen.getByRole("button", { name: "缩小" });
    const resetBtn = screen.getByRole("button", { name: "重置缩放" });

    expect(zoomInBtn.className).toContain("min-h-11");
    expect(zoomInBtn.className).toContain("focus-visible:outline");
    expect(zoomOutBtn).toBeDisabled();
    expect(resetBtn).toHaveTextContent("100%");

    // Double-click toggles 1x -> 2x -> 1x
    fireEvent.doubleClick(img.parentElement!);
    expect(resetBtn).toHaveTextContent("200%");
    expect(img).toHaveAttribute("data-zoom", "2");
    fireEvent.doubleClick(img.parentElement!);
    expect(resetBtn).toHaveTextContent("100%");

    // Toolbar buttons step zoom
    fireEvent.click(zoomInBtn);
    expect(resetBtn).toHaveTextContent("150%");
    fireEvent.click(resetBtn);
    expect(resetBtn).toHaveTextContent("100%");

    // Keyboard +, -, 0 shortcuts
    fireEvent.keyDown(document, { key: "+" });
    expect(resetBtn).toHaveTextContent("150%");
    fireEvent.keyDown(document, { key: "=" });
    expect(resetBtn).toHaveTextContent("200%");
    fireEvent.keyDown(document, { key: "-" });
    expect(resetBtn).toHaveTextContent("150%");
    fireEvent.keyDown(document, { key: "0" });
    expect(resetBtn).toHaveTextContent("100%");
  });

  it("shows broken image error card and recovers on retry", () => {
    render(
      <Viewer
        items={[{ key: "img-err", kind: "image", url: "/media/broken.png", name: "broken.png", title: "损坏图片" }]}
        start={0}
        onClose={vi.fn()}
      />,
    );

    fireEvent.error(screen.getByRole("img", { name: "损坏图片" }));
    expect(screen.getByRole("alert")).toHaveTextContent("图片加载失败");

    fireEvent.click(screen.getByRole("button", { name: "重试" }));
    expect(screen.getByRole("img", { name: "损坏图片" })).toBeInTheDocument();
  });

  it("shows retryable error state for broken text documents and clear PDF guidance", async () => {
    const fetchSpy = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(new Response("err", { status: 500 }))
      .mockResolvedValueOnce(new Response("恢复后的文档内容", { status: 200 }));

    const { unmount } = render(
      <Viewer
        items={[{ key: "doc-txt", kind: "document", url: "/media/note.txt", name: "note.txt", title: "笔记" }]}
        start={0}
        onClose={vi.fn()}
      />,
    );

    expect(await screen.findByRole("alert")).toHaveTextContent("文件预览加载失败");
    fireEvent.click(screen.getByRole("button", { name: "重试" }));
    expect(await screen.findByText("恢复后的文档内容")).toBeInTheDocument();
    expect(fetchSpy).toHaveBeenCalledTimes(2);
    unmount();

    render(
      <Viewer
        items={[{ key: "doc-pdf", kind: "document", url: "/media/report.pdf", name: "report.pdf", title: "年报" }]}
        start={0}
        onClose={vi.fn()}
      />,
    );
    expect(screen.getByText(/PDF 文件暂不支持页内直接预览/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /用其他应用打开/ })).toBeInTheDocument();
  });
});

describe("ConnectCard, CredentialCard, and ApprovalCard interactive polish", () => {
  it("toggles secret visibility, surfaces inline errors, and connects in ConnectCard", async () => {
    const preset = makePreset();
    fetchMcpPresetsMock.mockRejectedValueOnce(new Error("网络断开")).mockResolvedValueOnce({ presets: [preset] });
    runMcpPresetActionMock
      .mockResolvedValueOnce({ presets: [preset], last_action: { ok: false, message: "认证失败", error: "授权码无效" } })
      .mockResolvedValueOnce({ presets: [{ ...preset, installed: true, configured: true, enabled: true }] });
    const send = vi.fn();

    render(<ConnectCard service="email" reason="需要查收邮件" send={send} />);
    expect(await screen.findByRole("alert")).toHaveTextContent("网络断开");

    fireEvent.click(screen.getByRole("button", { name: "重试" }));
    fireEvent.click(await screen.findByRole("button", { name: "连接" }));

    const secretInput = screen.getByLabelText("授权码") as HTMLInputElement;
    expect(secretInput.type).toBe("password");
    const toggleBtn = screen.getByRole("button", { name: "显示授权码" });
    expect(toggleBtn).toHaveAttribute("aria-pressed", "false");
    expect(toggleBtn.className).toContain("min-h-11");

    fireEvent.click(toggleBtn);
    expect(secretInput.type).toBe("text");
    expect(screen.getByRole("button", { name: "隐藏授权码" })).toHaveAttribute("aria-pressed", "true");

    fireEvent.change(screen.getByLabelText("邮箱账号"), { target: { value: "me@example.com" } });
    fireEvent.change(secretInput, { target: { value: "bad-pass" } });
    fireEvent.click(screen.getByRole("button", { name: "确认连接" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("授权码无效");

    fireEvent.click(screen.getByRole("button", { name: "确认连接" }));
    await waitFor(() => expect(send).toHaveBeenCalledWith("通用邮箱 连好了，接着做刚才的事"));
    expect(screen.getAllByText("已连接").length).toBeGreaterThanOrEqual(1);
  });

  it("toggles secret visibility and shows inline errors on CredentialCard", async () => {
    requestMutation.mockRejectedValueOnce(new Error("变量名不合法")).mockResolvedValueOnce({ logins: [], keys: [] });
    const send = vi.fn();

    render(<CredentialCard card={{ type: "credential", kind: "api_key", service: "OpenAI", env: "OPENAI_API_KEY" }} send={send} />);
    const secretInput = screen.getByLabelText("密钥") as HTMLInputElement;
    expect(secretInput.type).toBe("password");

    const toggle = screen.getByRole("button", { name: "显示密钥" });
    expect(toggle).toHaveAttribute("aria-pressed", "false");
    fireEvent.click(toggle);
    expect(secretInput.type).toBe("text");
    expect(screen.getByRole("button", { name: "隐藏密钥" })).toHaveAttribute("aria-pressed", "true");

    fireEvent.change(secretInput, { target: { value: "sk-test-123" } });
    fireEvent.click(screen.getByRole("button", { name: "加密保存" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("变量名不合法");

    fireEvent.click(screen.getByRole("button", { name: "加密保存" }));
    expect(await screen.findByText("已加密保存")).toBeInTheDocument();
    expect(send).toHaveBeenCalledWith("OpenAI 的密钥存好了，接着做");
  });

  it("shows submitting spinner, inline error alert, and 44px hit targets in ApprovalCard", async () => {
    let rejectDecision!: (reason: Error) => void;
    const onDecide = vi.fn(
      () =>
        new Promise<void>((_, reject) => {
          rejectDecision = reject;
        }),
    );
    const approval: ApprovalRecord = {
      id: "a1",
      session_key: "websocket:main",
      call_id: "c1",
      tool_name: "bash",
      action: "run_command",
      arguments: { command: "ls -la", reason: "查看目录" },
      status: "pending",
      created_at_ms: 1_000,
      decided_at_ms: null,
      result: null,
    };

    render(<ApprovalCard approval={approval} onDecide={onDecide} onLater={vi.fn()} />);

    const laterBtn = screen.getByRole("button", { name: "稍后再说" });
    expect(laterBtn.className).toContain("min-h-11");

    const approveBtn = screen.getByRole("button", { name: "运行" });
    expect(approveBtn.className).toContain("motion-reduce:transition-none");
    fireEvent.click(approveBtn);

    expect(screen.getByRole("button", { name: "运行中…" })).toBeDisabled();
    await act(async () => {
      rejectDecision(new Error("执行超时"));
    });
    expect(await screen.findByRole("alert")).toHaveTextContent("执行超时");
  });
});

describe("AppearanceDock radiogroup accessibility & hit targets", () => {
  it("supports keyboard arrow switching inside radiogroups and 44px touch targets", () => {
    render(<AppearanceDock onDone={vi.fn()} />);

    expect(screen.getByRole("radiogroup", { name: "模式" })).toBeInTheDocument();
    expect(screen.getByRole("radiogroup", { name: "形象大小" })).toBeInTheDocument();

    const lightRadio = screen.getByRole("radio", { name: "浅色" });
    expect(lightRadio.className).toContain("min-h-11");
    expect(lightRadio.className).toContain("focus-visible:outline");

    fireEvent.click(lightRadio);
    expect(lightRadio).toHaveAttribute("aria-checked", "true");
    expect(lightRadio).toHaveAttribute("tabindex", "0");

    fireEvent.keyDown(lightRadio, { key: "ArrowRight" });
    expect(screen.getByRole("radio", { name: "深色" })).toHaveAttribute("aria-checked", "true");

    const doneBtn = screen.getByRole("button", { name: "完成" });
    expect(doneBtn.className).toContain("min-h-11");
  });
});

describe("ui-kit kit.js theme-aware Chart.js defaults", () => {
  const kitSource = fs.readFileSync(path.resolve(__dirname, "../../public/ui-kit/kit.js"), "utf-8");

  afterEach(() => {
    document.documentElement.style.removeProperty("--c-ink");
    document.documentElement.style.removeProperty("--c-muted");
    document.documentElement.style.removeProperty("--c-border");
    document.documentElement.style.removeProperty("--c-surface");
    delete (window as Window & { ui?: unknown }).ui;
    delete (window as Window & { Chart?: unknown }).Chart;
  });

  it("applies dark prefers-color-scheme fallbacks and reacts to CSS variable updates", () => {
    let dark = true;
    const listeners: Array<() => void> = [];
    const originalMatchMedia = window.matchMedia;
    Object.defineProperty(window, "matchMedia", {
      configurable: true,
      value: vi.fn((query: string) => ({
        matches: query.includes("prefers-color-scheme: dark") ? dark : false,
        media: query,
        addEventListener: (_: string, fn: () => void) => listeners.push(fn),
        removeEventListener: vi.fn(),
      })),
    });

    const chartConfigs: Array<{ options: Record<string, unknown> }> = [];
    const updateSpy = vi.fn();
    class FakeChart {
      static defaults = {
        color: "",
        borderColor: "",
        plugins: { legend: { labels: { color: "" } }, title: { color: "" } },
      };
      data: unknown;
      options: Record<string, unknown>;
      constructor(_canvas: HTMLCanvasElement, config: { data: unknown; options: Record<string, unknown> }) {
        this.data = config.data;
        this.options = config.options;
        chartConfigs.push(config);
      }
      update = updateSpy;
    }

    (window as Window & { Chart?: unknown }).Chart = FakeChart;

    try {
      // Execute kit.js IIFE against test window/document
      new Function(kitSource)();
      const ui = (
        window as Window & {
          ui: {
            chartTheme: () => { ink: string; muted: string; border: string };
            chart: (el: HTMLElement, spec: Record<string, unknown>) => { chart: FakeChart };
          };
        }
      ).ui;

      // Dark fallback without CSS vars
      expect(ui.chartTheme()).toMatchObject({
        ink: "#f5efe9",
        muted: "#b9aba0",
        border: "rgba(255,255,255,0.14)",
      });
      expect(FakeChart.defaults.color).toBe("#b9aba0");
      expect(FakeChart.defaults.borderColor).toBe("rgba(255,255,255,0.14)");
      expect(FakeChart.defaults.plugins.legend.labels.color).toBe("#f5efe9");

      const host = document.createElement("div");
      document.body.appendChild(host);
      ui.chart(host, { type: "bar", labels: ["一月"], datasets: [{ label: "支出", data: [120] }] });
      expect(chartConfigs).toHaveLength(1);

      // Push explicit theme CSS variables via companion:theme message
      fireEvent(
        window,
        new MessageEvent("message", {
          data: {
            type: "companion:theme",
            tokens: {
              "--c-ink": "#221a16",
              "--c-muted": "#77665c",
              "--c-border": "rgba(34,26,22,0.12)",
            },
          },
        }),
      );
      expect(FakeChart.defaults.color).toBe("#77665c");
      expect(FakeChart.defaults.borderColor).toBe("rgba(34,26,22,0.12)");
      expect(FakeChart.defaults.plugins.legend.labels.color).toBe("#221a16");
      expect(updateSpy).toHaveBeenCalledWith("none");

      // Light fallback when CSS vars cleared & color-scheme switches to light
      document.documentElement.style.removeProperty("--c-ink");
      document.documentElement.style.removeProperty("--c-muted");
      document.documentElement.style.removeProperty("--c-border");
      dark = false;
      listeners.forEach((fn) => fn());
      expect(ui.chartTheme()).toMatchObject({
        ink: "#1e1814",
        muted: "#6e6058",
        border: "rgba(30,24,20,0.085)",
      });
      host.remove();
    } finally {
      Object.defineProperty(window, "matchMedia", { configurable: true, value: originalMatchMedia });
    }
  });
});
