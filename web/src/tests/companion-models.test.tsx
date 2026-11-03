import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { ModelsPayload, RoutingDecision } from "@/companion/companion-api";
import { AddModelSheet } from "@/companion/AddModelSheet";
import { ModelDetail, ModelPanel } from "@/companion/ModelPanel";
import { replyRoute } from "@/companion/presentation";
import { priceSummary } from "@/companion/pricing";
import { formatCost, formatTokens, UsagePanel } from "@/companion/UsagePanel";
import type { UIMessage } from "@/lib/types";

const { requestMutation, fetchModels, fetchModelCatalog, fetchUsage } = vi.hoisted(() => ({
  requestMutation: vi.fn(),
  fetchModels: vi.fn(),
  fetchModelCatalog: vi.fn(),
  fetchUsage: vi.fn(),
}));

vi.mock("@/providers/ClientProvider", () => {
  const client = { requestMutation };
  return { useClient: () => ({ client, token: "t" }) };
});

vi.mock("@/companion/companion-api", async (original) => ({
  ...(await original<typeof import("@/companion/companion-api")>()),
  fetchModels,
  fetchModelCatalog,
  fetchUsage,
}));

const payload: ModelsPayload = {
  presets: [
    { name: "flash", model: "deepseek-flash", provider: "deepseek", vendor: "deepseek", reasoning_effort: null, max_tokens: 8192, temperature: 0.1, context_window_tokens: 1_000_000 },
    { name: "pro", model: "deepseek-pro", provider: "deepseek", vendor: "deepseek", reasoning_effort: "high", max_tokens: 8192, temperature: 0.1, context_window_tokens: 1_000_000 },
  ],
  max_tool_iterations: 200,
  default: "flash",
  fallbacks: [],
  settings: { enabled: false, router: null, fixed: null, models: {} },
  health: {},
  decisions: [],
  media: {
    image: { active: "seedream", models: [{ name: "seedream", model: "doubao-seedream-4-0", vendor: "volcengine", backend: "openai" }, { name: "cogview", model: "cogview-4", vendor: "zhipu", backend: "openai" }] },
    video: { active: null, models: [] },
  },
};

const vendors = [
  { id: "chatgpt", name: "ChatGPT 订阅", kinds: ["text", "image"], console: "", logo_url: null, color: "#000", note: "用 ChatGPT Plus / Pro 账号登录", has_key: false, group: "sub", login: "chatgpt" },
  { id: "deepseek", name: "DeepSeek", kinds: ["text"], console: "https://platform.deepseek.com", logo_url: null, color: "#4D6BFE", note: "", has_key: true, group: "cn", login: null },
  { id: "zhipu", name: "智谱 GLM", kinds: ["text", "image"], console: "https://bigmodel.cn", logo_url: null, color: "#3859FF", note: "", has_key: false, group: "cn", login: null },
];

beforeEach(() => fetchModelCatalog.mockResolvedValue(vendors));

afterEach(() => {
  cleanup();
  requestMutation.mockReset();
  fetchModels.mockReset();
  fetchModelCatalog.mockReset();
});

describe("who answered", () => {
  const reply = (extra: Partial<UIMessage>): UIMessage => ({ id: "a", role: "assistant", content: "好", createdAt: 0, ...extra });
  const decision: RoutingDecision = { turn_id: "t1", at_ms: 0, preset: "pro", rule: "路由模型挑选", reason: "要推理" };

  it("names the model the router picked and why", () => {
    const route = replyRoute(
      reply({ turnId: "t1", responseSources: [{ provider: "deepseek", model: "deepseek-pro", preset: "pro" }] }),
      new Map([["t1", decision]]),
    );
    expect(route).toEqual({ answeredBy: "pro", why: "路由模型挑选：要推理", fallback: false });
  });

  it("says so when a fallback answered, and stays quiet otherwise", () => {
    const fallback = reply({ responseSources: [{ provider: "volcengine", model: "doubao", preset: "backup", fallback: true }] });
    expect(replyRoute(fallback, new Map())).toEqual({ answeredBy: "backup", why: null, fallback: true });
    expect(replyRoute(reply({ turnId: "t9" }), new Map([["t1", decision]]))).toBeNull();
  });
});

describe("ModelPanel", () => {
  const echo = () =>
    requestMutation.mockImplementation(async (_action: string, { changes }: { changes: Record<string, unknown> }) => ({
      ...payload,
      settings: { ...payload.settings, ...changes },
    }));

  it("switches between picking automatically and one fixed model, and opens a model", async () => {
    fetchModels.mockResolvedValue(payload);
    fetchModelCatalog.mockResolvedValue(vendors);
    echo();
    const onOpenModel = vi.fn();
    render(<ModelPanel active onOpenModel={onOpenModel} />);

    fireEvent.click(await screen.findByRole("radio", { name: "自动选择" }));
    await waitFor(() => expect(requestMutation).toHaveBeenCalledWith("companion.models.update", { changes: { enabled: true } }));
    fireEvent.change(await screen.findByRole("combobox", { name: "日常用" }), { target: { value: "pro" } });
    await waitFor(() => expect(requestMutation).toHaveBeenCalledWith("companion.models.update", { changes: { default: "pro" } }));

    expect(await screen.findAllByText("DeepSeek · 思考高")).toHaveLength(1);
    fireEvent.click(screen.getByRole("button", { name: /^pro/ }));
    expect(onOpenModel).toHaveBeenCalledWith("pro");
  });

  it("caps the steps of one reply", async () => {
    fetchModels.mockResolvedValue(payload);
    fetchModelCatalog.mockResolvedValue(vendors);
    echo();
    render(<ModelPanel active onOpenModel={() => {}} />);
    fireEvent.change(await screen.findByRole("combobox", { name: "每次最多做几步" }), { target: { value: "50" } });
    await waitFor(() =>
      expect(requestMutation).toHaveBeenCalledWith("companion.models.update", { changes: { max_tool_iterations: 50 } }),
    );
  });

  it("needs at least two models to route", async () => {
    fetchModels.mockResolvedValue({ ...payload, presets: payload.presets.slice(0, 1) });
    fetchModelCatalog.mockResolvedValue(vendors);
    render(<ModelPanel active onOpenModel={() => {}} />);
    expect(await screen.findByRole("radio", { name: "自动选择" })).toBeDisabled();
  });
});

describe("model kinds", () => {
  it("lists drawing models apart, puts another one in use, and shows an empty video tab", async () => {
    fetchModels.mockResolvedValue(payload);
    fetchModelCatalog.mockResolvedValue(vendors);
    requestMutation.mockResolvedValue(payload);
    render(<ModelPanel active onOpenModel={() => {}} />);

    fireEvent.click(await screen.findByRole("radio", { name: "图像 2" }));
    expect(screen.getByLabelText("正在用")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /^cogview/ }));
    await waitFor(() =>
      expect(requestMutation).toHaveBeenCalledWith("companion.models.update", { changes: { media_active: { image: "cogview" } } }),
    );

    fireEvent.click(screen.getByRole("radio", { name: "视频" }));
    expect(screen.getByText("还没有视频模型")).toBeInTheDocument();
  });

  it("adds a drawing model from a company: only companies that draw, a recommended model, the key", async () => {
    fetchModels.mockResolvedValue(payload);
    fetchModelCatalog.mockResolvedValue(vendors);
    requestMutation.mockImplementation(async (action: string) =>
      action === "companion.models.vendor"
        ? { models: [{ id: "cogview-4", name: "CogView-4", note: "能写中文字", vision: false, reasoning: false, context: null, price_in: null, price_out: null, recommended: true }] }
        : { ...payload, added: "CogView-4" },
    );
    render(<ModelPanel active onOpenModel={() => {}} />);
    fireEvent.click(await screen.findByRole("radio", { name: "图像 2" }));
    fireEvent.click(screen.getByRole("button", { name: "添加图像模型" }));

    const sheet = within(await screen.findByRole("dialog"));
    expect(await sheet.findByRole("button", { name: /智谱 GLM/ })).toBeInTheDocument();
    expect(sheet.queryByRole("button", { name: /DeepSeek/ })).toBeNull();
    fireEvent.click(sheet.getByRole("button", { name: /智谱 GLM/ }));
    expect(await screen.findByRole("radio", { name: /CogView-4/ })).toHaveAttribute("aria-checked", "true");

    const add = screen.getByRole("button", { name: "添加" });
    expect(add).toBeDisabled();
    fireEvent.change(screen.getByPlaceholderText("粘贴 API key"), { target: { value: "k-123" } });
    fireEvent.click(add);
    await waitFor(() =>
      expect(requestMutation).toHaveBeenCalledWith(
        "companion.models.add",
        { kind: "image", vendor: "zhipu", model: "cogview-4", api_key: "k-123" },
        60_000,
      ),
    );
  });
});

describe("subscriptions", () => {
  it("signs in with a code on the company's page, then lists the plan's models and adds one without a key", async () => {
    fetchModels.mockResolvedValue(payload);
    const waiting = { id: "l1", kind: "chatgpt", status: "waiting", error: null, user_code: "ABCD-1234", url: "https://auth.openai.com/codex/device", expires_in: 900 };
    requestMutation.mockImplementation(async (action: string) => {
      if (action === "companion.oauth.start") return waiting;
      if (action === "companion.oauth.status") return { ...waiting, status: "done" };
      if (action === "companion.models.vendor")
        return { models: [{ id: "gpt-6-sol", name: "GPT-6 Sol", note: "", vision: true, reasoning: true, context: 400000, price_in: null, price_out: null, recommended: true }] };
      return { ...payload, added: "GPT-6 Sol" };
    });
    render(<ModelPanel active onOpenModel={() => {}} />);
    fireEvent.click(await screen.findByRole("button", { name: "添加对话模型" }));
    const sheet = within(await screen.findByRole("dialog"));
    expect(sheet.getByText("用会员账号登录")).toBeInTheDocument();
    fireEvent.click(await sheet.findByRole("button", { name: /ChatGPT 订阅/ }));

    expect(sheet.queryByPlaceholderText("粘贴 API key")).toBeNull();
    fireEvent.click(sheet.getByRole("button", { name: "用 ChatGPT 账号登录" }));
    expect(await sheet.findByText("ABCD-1234")).toBeInTheDocument();
    expect(sheet.getByRole("link", { name: /打开登录页/ })).toHaveAttribute("href", "https://auth.openai.com/codex/device");

    expect(await sheet.findByText("已登录 ChatGPT 账号", {}, { timeout: 4000 })).toBeInTheDocument();
    expect(await sheet.findByRole("radio", { name: /GPT-6 Sol/ })).toHaveAttribute("aria-checked", "true");
    expect(sheet.getByText("会员额度")).toBeInTheDocument();
    fireEvent.click(sheet.getByRole("button", { name: "添加" }));
    await waitFor(() =>
      expect(requestMutation).toHaveBeenCalledWith("companion.models.add", { kind: "text", vendor: "chatgpt", model: "gpt-6-sol", api_key: "" }, 60_000),
    );
  });
});

describe("subscriptions signed in on this machine", () => {
  it("uses the Codex sign-in already on the computer instead of signing in again", async () => {
    fetchModels.mockResolvedValue(payload);
    fetchModelCatalog.mockResolvedValue(vendors.map((v) => (v.id === "chatgpt" ? { ...v, local_login: true } : v)));
    requestMutation.mockImplementation(async (action: string) =>
      action === "companion.oauth.import"
        ? { ok: true }
        : { models: [{ id: "gpt-6-sol", name: "GPT-6 Sol", note: "", vision: true, reasoning: true, context: 400000, price_in: null, price_out: null, recommended: true }] },
    );
    render(<ModelPanel active onOpenModel={() => {}} />);
    fireEvent.click(await screen.findByRole("button", { name: "添加对话模型" }));
    const sheet = within(await screen.findByRole("dialog"));
    fireEvent.click(await sheet.findByRole("button", { name: /ChatGPT 订阅/ }));
    fireEvent.click(sheet.getByRole("button", { name: /用这台电脑上已登录的 ChatGPT/ }));
    await waitFor(() => expect(requestMutation).toHaveBeenCalledWith("companion.oauth.import", { login: "chatgpt" }, 30_000));
    expect(await sheet.findByText("已登录 ChatGPT 账号")).toBeInTheDocument();
    expect(await sheet.findByRole("radio", { name: /GPT-6 Sol/ })).toBeInTheDocument();
  });
});

describe("ModelDetail", () => {
  it("sets how hard and how long the model may think and answer", async () => {
    fetchModels.mockResolvedValue(payload);
    requestMutation.mockResolvedValue(payload);
    render(<ModelDetail name="pro" />);

    expect(await screen.findByRole("radio", { name: "高" })).toHaveAttribute("aria-checked", "true");
    fireEvent.click(screen.getByRole("radio", { name: "关闭" }));
    await waitFor(() =>
      expect(requestMutation).toHaveBeenCalledWith("companion.models.update", { changes: { presets: { pro: { reasoning_effort: "none" } } } }),
    );
    fireEvent.click(screen.getByRole("radio", { name: "默认" }));
    await waitFor(() =>
      expect(requestMutation).toHaveBeenLastCalledWith("companion.models.update", { changes: { presets: { pro: { reasoning_effort: null } } } }),
    );
    fireEvent.change(screen.getByRole("combobox", { name: "单次最多" }), { target: { value: "16384" } });
    await waitFor(() =>
      expect(requestMutation).toHaveBeenLastCalledWith("companion.models.update", { changes: { presets: { pro: { max_tokens: 16384 } } } }),
    );
  });

  it("saves whether a model can see images", async () => {
    fetchModels.mockResolvedValue(payload);
    requestMutation.mockResolvedValue(payload);
    render(<ModelDetail name="pro" />);
    fireEvent.click(await screen.findByRole("switch", { name: "能看图" }));
    await waitFor(() => expect(requestMutation).toHaveBeenCalled());
    const [, { changes }] = requestMutation.mock.calls[0];
    expect(changes.models.pro).toMatchObject({ tier: "balanced", vision: true });
  });
});

describe("usage numbers", () => {
  it("reads like a person would say them", () => {
    expect([formatTokens(980), formatTokens(123_456), formatTokens(250_000_000)]).toEqual(["980", "12.3 万", "2.5 亿"]);
    expect([formatCost(null), formatCost(0.004), formatCost(1.234)]).toEqual([null, "不到 1 分钱", "约 ¥1.23"]);
  });
});

describe("用量 with some models unpriced", () => {
  it("says which models the estimate leaves out", async () => {
    const row = (preset: string, cost: number | null) => ({ provider: "", model: preset, preset, calls: 1, input_tokens: 1000, output_tokens: 10, cost });
    const period = { by_model: [row("deepseek-flash", null), row("glm", 0.09)], by_source: { user: 2020 }, cost: 0.09 };
    fetchUsage.mockResolvedValue({ today: period, week: period });
    render(<UsagePanel active />);
    expect(await screen.findByText("约 ¥0.09，不含 deepseek-flash（没填价格）")).toBeInTheDocument();
  });
});

describe("prices in their own style", () => {
  it("puts tokens, seconds, clips and pictures each in one short line", () => {
    expect(priceSummary({ unit: "mtok", rows: [{ spec: "", input: 2.1, output: 8.4, cached: 0.42 }] })).toEqual({
      main: "¥2.1 / ¥8.4",
      rows: ["缓存命中 ¥0.42"],
    });
    expect(priceSummary({ unit: "second", rows: [{ spec: "768P", price: 0.26 }, { spec: "480P", price: 0.16 }] })).toEqual({
      main: "¥0.16/秒起",
      rows: ["768P ¥0.26/秒", "480P ¥0.16/秒"],
    });
    expect(priceSummary({ unit: "video", rows: [{ spec: "768P·6秒", price: 1.35 }] })?.main).toBe("¥1.35/条");
    expect(priceSummary({ unit: "image", rows: [{ spec: "", price: 0.025 }] })?.main).toBe("¥0.025/张");
    expect(priceSummary(null)).toBeNull();
  });
});

describe("用量 of pictures and clips", () => {
  it("lists them under the chat models, priced or not", async () => {
    const period = {
      by_model: [],
      by_source: {},
      cost: 4.16,
      media: [
        { name: "海螺", kind: "video" as const, model: "MiniMax-H3-Lite", count: 4, seconds: 16, cost: 4.16 },
        { name: "画", kind: "image" as const, model: "img", count: 2, seconds: 0, cost: null },
      ],
    };
    fetchUsage.mockResolvedValue({ today: period, week: period });
    render(<UsagePanel active />);
    expect(await screen.findByText("做了 4 段视频，共 16 秒")).toBeInTheDocument();
    expect(screen.getByText("没有价格")).toBeInTheDocument();
    expect(screen.getByText("约 ¥4.16，不含 画（没填价格）")).toBeInTheDocument();
  });
});

describe("检测 ID 和价格", () => {
  it("reads the platform and shows its current models with their prices", async () => {
    const vendor = { id: "minimax", name: "MiniMax", kinds: ["text", "video"], console: "https://x", group: "cn", login: null, local_login: false, logo_url: null, color: "#F23F5D", note: "", has_key: true };
    fetchModelCatalog.mockResolvedValue([vendor]);
    const old = { id: "MiniMax-H3", name: "海螺 H3", note: "", vision: false, reasoning: false, context: null, price_in: null, price_out: null, recommended: true };
    const lite = { ...old, id: "MiniMax-H3-Lite", name: "海螺 H3 Lite", checked: true, price: { unit: "second", rows: [{ spec: "768P", price: 0.26 }, { spec: "480P", price: 0.16 }] } };
    let finish: (value: unknown) => void = () => {};
    requestMutation.mockImplementation((action: string) =>
      action === "companion.models.check"
        ? new Promise((resolve) => (finish = resolve))
        : Promise.resolve({ models: [old], checked: null, can_check: true }),
    );
    render(<AddModelSheet open kind="video" onClose={() => {}} onAdded={() => {}} />);
    fireEvent.click(await screen.findByRole("button", { name: /MiniMax/ }));
    fireEvent.click(await screen.findByRole("button", { name: /检测 ID 和价格/ }));
    expect(requestMutation).toHaveBeenCalledWith("companion.models.check", { vendor: "minimax", kind: "video" }, 150_000);
    expect(screen.getByText(/正在读MiniMax官网的模型和价格/)).toBeInTheDocument();
    finish({ models: [lite, { ...old, missing: true }], checked: { at: Date.now() / 1000, sources: [] }, can_check: true });
    expect(await screen.findByText("¥0.16/秒起")).toBeInTheDocument();
    expect(screen.getByText("768P ¥0.26/秒 · 480P ¥0.16/秒")).toBeInTheDocument();
    expect(screen.getByText(/已按官网核对 · 刚刚/)).toBeInTheDocument();
    expect(screen.getByText(/官网上没找到了/)).toBeInTheDocument();
  });
});

describe("检测价格 on a drawing model", () => {
  it("is offered for a company's model, not for one on a subscription", async () => {
    fetchModels.mockResolvedValue({
      ...payload,
      media: {
        image: {
          active: "GPT Image 2",
          models: [
            { name: "GPT Image 2", model: "gpt-image-2-medium", vendor: "chatgpt", backend: "openai-codex" },
            { name: "CogView-4", model: "cogview-4", vendor: "zhipu", backend: "openai" },
          ],
        },
        video: { active: null, models: [] },
      },
    });
    render(<ModelPanel active onOpenModel={() => {}} />);
    fireEvent.click(await screen.findByRole("radio", { name: "图像 2" }));
    await screen.findByText("GPT Image 2");
    await waitFor(() => expect(screen.getAllByRole("button", { name: "检测价格" })).toHaveLength(1));
  });
});

describe("用量 on a subscription", () => {
  it("shows 会员额度 instead of a price, and doesn't count it as unpriced", async () => {
    const period = {
      by_model: [{ provider: "", model: "gpt-6.1-sol", preset: "Sol", calls: 3, input_tokens: 1000, output_tokens: 10, cost: 0, subscription: true }],
      by_source: { user: 1010 },
      cost: 0.5,
      media: [
        { name: "GPT Image 2", kind: "image" as const, model: "gpt-image-2", count: 2, seconds: 0, cost: 0, subscription: true },
        { name: "海螺", kind: "video" as const, model: "MiniMax-H3-Lite", count: 1, seconds: 4, cost: 0.5 },
      ],
    };
    fetchUsage.mockResolvedValue({ today: period, week: period });
    render(<UsagePanel active />);
    expect(await screen.findAllByText("会员额度")).toHaveLength(2);
    // The headline and the clip: nothing left out of the estimate.
    expect(screen.getAllByText("约 ¥0.50")).toHaveLength(2);
  });
});
