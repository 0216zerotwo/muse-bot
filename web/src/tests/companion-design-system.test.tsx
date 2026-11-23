import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { act, cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { McpPresetInfo } from "@/lib/types";
import { RecommendedServices } from "@/companion/ConnectSheet";
import { CreationsView } from "@/companion/Creations";
import { SelectRow, Switch } from "@/companion/Menu";
import { ModelPanel } from "@/companion/ModelPanel";
import { MoreServicesSheet } from "@/companion/MoreServices";
import { ProactivePanel } from "@/companion/ProactivePanel";
import { Skeleton, SkeletonCard, SkeletonGrid, SkeletonLines, SkeletonRows } from "@/companion/Skeleton";
import { SkillsPanel } from "@/companion/SkillsPanel";
import { UsagePanel } from "@/companion/UsagePanel";
import { VaultPanel } from "@/companion/VaultPanel";
import * as companionApi from "@/companion/companion-api";

const requestMutation = vi.fn();
const session = { client: { requestMutation }, token: "t" };
vi.mock("@/providers/ClientProvider", () => ({ useClient: () => session }));

vi.mock("@/companion/companion-api", async (original) => {
  const actual = await original<typeof import("@/companion/companion-api")>();
  return {
    ...actual,
    fetchModels: vi.fn(),
    fetchModelCatalog: vi.fn(),
    fetchUsage: vi.fn(),
    fetchRoutines: vi.fn(),
    fetchSkills: vi.fn(),
    fetchVault: vi.fn(),
    fetchCreations: vi.fn(),
  };
});

function deferred<T>() {
  let resolvePromise!: (value: T) => void;
  const promise = new Promise<T>((res) => {
    resolvePromise = res;
  });
  return { promise, resolve: resolvePromise };
}

beforeEach(() => {
  vi.clearAllMocks();
});

afterEach(cleanup);

describe("Skeleton primitives", () => {
  it("exposes accessible role=status and aria-busy landmarks while hiding inner shimmer bars", () => {
    const { container } = render(
      <div>
        <SkeletonRows rows={3} label="加载列表" />
        <SkeletonCard lines={2} label="加载卡片" />
        <SkeletonCard lines={1} status={false} />
        <SkeletonGrid count={3} variant="vendor" label="加载服务商" />
        <SkeletonLines lines={2} label="加载详情" />
        <Skeleton className="h-4 w-10" />
      </div>,
    );

    const statuses = screen.getAllByRole("status");
    expect(statuses.map((el) => el.getAttribute("aria-label"))).toEqual([
      "加载列表",
      "加载卡片",
      "加载服务商",
      "加载详情",
    ]);
    for (const el of statuses) {
      expect(el).toHaveAttribute("aria-busy", "true");
    }
    const bars = container.querySelectorAll(".c-skeleton");
    expect(bars.length).toBeGreaterThan(8);
    for (const bar of bars) {
      expect(bar).toHaveAttribute("aria-hidden", "true");
      expect(bar.className).toContain("max-w-full");
    }
  });
});

describe("Async skeleton states and cohesive empty states across PR-A panels", () => {
  it("renders skeleton status in ModelPanel, UsagePanel, and ProactivePanel until data arrives", async () => {
    const modelsReq = deferred<companionApi.ModelsPayload>();
    const catalogReq = deferred<{ vendors: companionApi.ModelVendor[] }>();
    const usageReq = deferred<companionApi.UsagePayload>();
    const routinesReq = deferred<companionApi.Routines>();

    vi.mocked(companionApi.fetchModels).mockReturnValueOnce(modelsReq.promise);
    vi.mocked(companionApi.fetchModelCatalog).mockReturnValueOnce(catalogReq.promise);
    vi.mocked(companionApi.fetchUsage).mockReturnValueOnce(usageReq.promise);
    vi.mocked(companionApi.fetchRoutines).mockReturnValueOnce(routinesReq.promise);

    render(
      <div>
        <ModelPanel active onOpenModel={vi.fn()} />
        <UsagePanel active />
        <ProactivePanel />
      </div>,
    );

    expect(screen.getByRole("status", { name: "正在读取模型" })).toHaveAttribute("aria-busy", "true");
    expect(screen.getByRole("status", { name: "正在读取用量" })).toHaveAttribute("aria-busy", "true");
    expect(screen.getByRole("status", { name: "正在读取主动提醒" })).toHaveAttribute("aria-busy", "true");
    expect(screen.queryByText("正在读取…")).not.toBeInTheDocument();

    const emptyPeriod: companionApi.UsagePeriod = {
      calls: 0,
      input_tokens: 0,
      output_tokens: 0,
      cached_tokens: 0,
      cost: null,
      by_source: {},
      by_model: [],
      media: [],
    };
    await act(async () => {
      modelsReq.resolve({
        default: null,
        presets: [],
        fallbacks: [],
        max_tool_iterations: 20,
        settings: { enabled: false, fixed: null, models: {} },
        media: {
          image: { active: null, models: [] },
          video: { active: null, models: [] },
        },
      });
      catalogReq.resolve({ vendors: [] });
      usageReq.resolve({ today: emptyPeriod, week: emptyPeriod });
      routinesReq.resolve({ brief_time: "08:00", checkins: true });
    });

    expect(await screen.findByText("还没有对话模型")).toBeInTheDocument();
    expect(screen.getByText("这段时间还没有用量。")).toBeInTheDocument();
    expect(screen.getByText("每日早报")).toBeInTheDocument();
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });

  it("prevents empty-card flash in VaultPanel, SkillsPanel, and CreationsView before fetch resolves", async () => {
    const vaultReq = deferred<companionApi.VaultPayload>();
    const skillsReq = deferred<companionApi.SkillsPayload>();
    const creationsReq = deferred<companionApi.CreationsPayload>();

    vi.mocked(companionApi.fetchVault).mockReturnValueOnce(vaultReq.promise);
    vi.mocked(companionApi.fetchSkills).mockReturnValueOnce(skillsReq.promise);
    vi.mocked(companionApi.fetchCreations).mockReturnValueOnce(creationsReq.promise);

    render(
      <div>
        <VaultPanel />
        <SkillsPanel onDraft={vi.fn()} onTeach={vi.fn()} />
        <CreationsView active />
      </div>,
    );

    expect(screen.getByRole("status", { name: "正在读取网站登录" })).toBeInTheDocument();
    expect(screen.getByRole("status", { name: "正在读取密钥" })).toBeInTheDocument();
    expect(screen.getByRole("status", { name: "正在读取技能" })).toBeInTheDocument();
    expect(screen.getByRole("status", { name: "正在读取作品" })).toBeInTheDocument();

    await act(async () => {
      vaultReq.resolve({ logins: [], keys: [] });
      skillsReq.resolve({
        learned: [],
        bundled: [{ name: "web-search", description: "Search the web", updated_at: "2026-11-23T00:00:00Z" }],
      });
      creationsReq.resolve({
        items: [],
        counts: { image: 0, video: 0, document: 0, audio: 0 },
      });
    });

    expect(await screen.findByText("还没有。模型的密钥在「模型」里管理。")).toBeInTheDocument();
    expect(screen.getByText(/还没有。它把一件事做成后会自己记成技能/)).toBeInTheDocument();
    expect(screen.getByText("还没有作品")).toBeInTheDocument();
  });
});

describe("Mobile touch targets (>= 44px) and focus-visible rings", () => {
  const recommendedPreset: McpPresetInfo = {
    name: "notion",
    display_name: "Notion",
    description: "读写 Notion 页面与数据库",
    category: "productivity",
    transport: "stdio",
    configured: false,
    enabled: false,
    required_fields: [],
  };

  const catalogPreset: McpPresetInfo = {
    name: "linear",
    display_name: "Linear",
    description: "管理项目 Issue 与路线图",
    category: "productivity",
    transport: "stdio",
    configured: false,
    enabled: false,
    install_supported: true,
    required_fields: [],
  };

  it("uses min-h-11 touch targets on service connect actions, filter tabs, and vault/switch controls", () => {
    render(
      <div>
        <Switch checked label="测试开关" onChange={vi.fn()} />
        <SelectRow
          label="思考模式"
          value="auto"
          options={[
            { value: "auto", label: "自动" },
            { value: "high", label: "深度" },
          ]}
          onChange={vi.fn()}
        />
        <RecommendedServices presets={[recommendedPreset]} onConnect={vi.fn()} />
      </div>,
    );

    const switchBtn = screen.getByRole("switch", { name: "测试开关" });
    expect(switchBtn.className).toContain("after:-inset-y-1.5");

    const selectEl = screen.getByRole("combobox", { name: "思考模式" });
    expect(selectEl.closest("label")?.className).toContain("has-[:focus-visible]:outline-2");

    expect(screen.getByRole("button", { name: "连接 Notion" }).className).toContain("min-h-11");

    cleanup();

    render(
      <MoreServicesSheet
        open
        presets={[catalogPreset]}
        onClose={vi.fn()}
        onConnect={vi.fn()}
        onChanged={vi.fn()}
      />,
    );
    expect(screen.getByRole("button", { name: "连接 Linear" }).className).toContain("min-h-11");
  });
});

describe("CSS token harmony, reduced motion, and UI Kit invariants", () => {
  const companionCss = readFileSync(resolve(__dirname, "../companion/companion.css"), "utf8");
  const kitCss = readFileSync(resolve(__dirname, "../../public/ui-kit/kit.css"), "utf8");

  it("disables all infinite pet, pulse, and shimmer animations under prefers-reduced-motion", () => {
    const reducedBlocks = [...companionCss.matchAll(/@media\s*\(prefers-reduced-motion:\s*reduce\)\s*\{([\s\S]*?)\}/g)]
      .map((match) => match[1])
      .join("\n");

    for (const selector of [
      ".c-pet-breathe",
      ".c-pet-bob",
      ".c-pet-jitter",
      ".c-pet-sway",
      ".c-pet-wiggle",
      ".c-pet-hop",
      ".c-pet-droop",
      ".c-pet-sleep",
      ".c-pet-zz",
      ".c-skeleton",
      ".animate-pulse",
      ".sheet-in",
      ".om-rise",
    ]) {
      expect(reducedBlocks).toContain(selector);
    }
  });

  it("maps Markdown-in-bubble typography, code, blockquotes, and tables to --c-* tokens", () => {
    expect(companionCss).toContain(".companion .c-bubble-text .prose");
    expect(companionCss).toContain("--tw-prose-links: var(--c-primary)");
    expect(companionCss).toContain('.companion .c-bubble-text [data-testid="markdown-data-table"]');
    expect(companionCss).toContain(".companion .c-bubble-text :not(pre) > code");
    expect(companionCss).toContain(".companion .c-bubble-text .code-content");
    expect(companionCss).toContain(".companion .c-bubble-text blockquote");
    expect(companionCss).toContain(".companion .c-bubble-text th");
    // Dark-mode user bubbles must derive link/quote colors from --c-bubble-user-text (never dark --c-accent-soft).
    expect(companionCss).toMatch(/\.companion \.c-bubble-user\.c-bubble-text a\s*\{[^}]*--c-bubble-user-text/);
    expect(companionCss).not.toMatch(/\.companion \.c-bubble-user\.c-bubble-text (?:a|blockquote)\s*\{[^}]*--c-accent-soft/);
  });

  it("aligns kit.css with --c-* tokens, 16px/44px mobile inputs, and short class names", () => {
    for (const token of [".card", ".soft", ".accent-card", ".seg", ".chips", "textarea:focus-visible"]) {
      expect(kitCss).toContain(token);
    }
    expect(kitCss).toContain("var(--c-rim-soft, rgb(255 255 255 / 0.14))");
    expect(kitCss).toMatch(/input\[type="text"\][\s\S]*?font-size:\s*16px/);
    expect(kitCss).toMatch(/input\[type="text"\][\s\S]*?min-height:\s*44px/);
  });
});
