import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { describeCapabilities, SettingsScreen } from "@/companion/Settings";
import { IdeasView } from "@/companion/TabViews";
import type { ChatSearchHit, CompanionState, FeedPayload, Goal, HelpersPayload, MemoryChange } from "@/companion/companion-api";
import { buildSearchSnippet, MuseDrawer, RECENT_SEARCHES_KEY, tokenizeSearchQuery } from "@/companion/muse/Drawer";
import { FeedScreen } from "@/companion/muse/Feed";
import { GoalsScreen } from "@/companion/muse/GoalsScreen";
import { HelpersScreen } from "@/companion/muse/Helpers";
import { classifyMemoryItem, SoulMemoryPane } from "@/companion/muse/Identity";
import type { ChatSummary } from "@/lib/types";

const {
  requestMutation,
  fetchCompanionStateMock,
  fetchModelsMock,
  fetchUsageMock,
  fetchSkillsMock,
  fetchMcpPresetsMock,
  fetchIdeasMock,
  fetchGoalsMock,
  fetchRoutinesMock,
  fetchTriggersMock,
  fetchAutomationsMock,
  fetchHelpersMock,
  fetchMemoryMock,
  fetchMemoryDocMock,
} = vi.hoisted(() => ({
  requestMutation: vi.fn(),
  fetchCompanionStateMock: vi.fn(),
  fetchModelsMock: vi.fn(),
  fetchUsageMock: vi.fn(),
  fetchSkillsMock: vi.fn(),
  fetchMcpPresetsMock: vi.fn(),
  fetchIdeasMock: vi.fn(),
  fetchGoalsMock: vi.fn(),
  fetchRoutinesMock: vi.fn(),
  fetchTriggersMock: vi.fn(),
  fetchAutomationsMock: vi.fn(),
  fetchHelpersMock: vi.fn(),
  fetchMemoryMock: vi.fn(),
  fetchMemoryDocMock: vi.fn(),
}));

vi.mock("@/providers/ClientProvider", () => {
  const client = { requestMutation };
  return { useClient: () => ({ client, token: "test-token" }) };
});

vi.mock("@/lib/api", async (original) => ({
  ...(await original<typeof import("@/lib/api")>()),
  fetchMcpPresets: (...args: unknown[]) => fetchMcpPresetsMock(...args),
  fetchAutomations: (...args: unknown[]) => fetchAutomationsMock(...args),
}));

vi.mock("@/companion/companion-api", async (original) => {
  const actual = await original<typeof import("@/companion/companion-api")>();
  return {
    ...actual,
    fetchCompanionState: (...args: unknown[]) => fetchCompanionStateMock(...args),
    fetchModels: (...args: unknown[]) => fetchModelsMock(...args),
    fetchUsage: (...args: unknown[]) => fetchUsageMock(...args),
    fetchSkills: (...args: unknown[]) => fetchSkillsMock(...args),
    fetchIdeas: (...args: unknown[]) => fetchIdeasMock(...args),
    fetchGoals: (...args: unknown[]) => fetchGoalsMock(...args),
    fetchRoutines: (...args: unknown[]) => fetchRoutinesMock(...args),
    fetchTriggers: (...args: unknown[]) => fetchTriggersMock(...args),
    fetchHelpers: (...args: unknown[]) => fetchHelpersMock(...args),
    fetchMemory: (...args: unknown[]) => fetchMemoryMock(...args),
    fetchMemoryDoc: (...args: unknown[]) => fetchMemoryDocMock(...args),
  };
});

function baseCompanion(capabilities?: CompanionState["capabilities"]): CompanionState {
  return {
    name: "小糯",
    avatar: "mochi:cream",
    chat_id: "main",
    tone: "warm",
    user_name: null,
    style_note: null,
    onboarded: true,
    timezone: "Asia/Shanghai",
    capabilities,
    updated_at: null,
  };
}

function makeChat(chatId: string, title: string, preview = ""): ChatSummary {
  return { key: `websocket:${chatId}`, channel: "websocket", chatId, createdAt: null, updatedAt: null, title, preview };
}

beforeEach(() => {
  localStorage.clear();
  vi.clearAllMocks();
  fetchModelsMock.mockResolvedValue({
    presets: [],
    max_tool_iterations: 50,
    default: "flash",
    fallbacks: [],
    settings: { enabled: true, router: null, fixed: null, models: {} },
    health: {},
    decisions: [],
    media: { image: { active: null, models: [] }, video: { active: null, models: [] } },
  });
  fetchUsageMock.mockResolvedValue({
    today: { cost: 0, by_source: {}, by_model: [], media: [] },
    days: [],
  });
  fetchSkillsMock.mockResolvedValue({ learned: [], bundled: [] });
  fetchMcpPresetsMock.mockResolvedValue({ presets: [], installed_count: 0 });
  fetchRoutinesMock.mockResolvedValue({ brief_time: "08:00", checkins: true });
  fetchTriggersMock.mockResolvedValue([]);
  fetchAutomationsMock.mockResolvedValue({ jobs: [] });
});

afterEach(() => {
  cleanup();
  localStorage.clear();
  vi.useRealTimers();
});

describe("MuseDrawer multi-keyword search, centered snippets, and recent searches", () => {
  it("tokenizes queries and centers long snippets around the earliest match with ellipses", () => {
    expect(tokenizeSearchQuery("  咖啡   手冲  咖啡 ")).toEqual(["咖啡", "手冲"]);

    const longText = `${"前文铺垫内容很长".repeat(8)}今天早上试了云南豆子的手冲咖啡，风味有明显柑橘香${"后续补充说明也很长".repeat(8)}`;
    const snippet = buildSearchSnippet(longText, "手冲 柑橘", 48);
    expect(snippet.prefixEllipsis).toBe(true);
    expect(snippet.suffixEllipsis).toBe(true);
    const matchedWords = snippet.segments.filter((seg) => seg.match).map((seg) => seg.text);
    expect(matchedWords).toContain("手冲");
    expect(matchedWords).toContain("柑橘");
  });

  it("highlights multiple keywords, shows role & session badges, and persists recent searches", async () => {
    vi.useFakeTimers();
    const hit: ChatSearchHit = {
      chat_id: "trip",
      chat_title: "周末出行",
      role: "user",
      text: "下周去杭州西湖徒步，记得带上雨伞和充电宝。",
      turn_id: "turn-9",
      at_ms: Date.now(),
    };
    const search = vi.fn(async () => [hit]);
    const onOpenHit = vi.fn();
    const onClose = vi.fn();

    const { rerender } = render(
      <MuseDrawer
        open
        onClose={onClose}
        name="小糯"
        current="chat"
        activeChatId={null}
        order={["chat", "feed", "ideas", "goals", "creations"]}
        onReorder={vi.fn()}
        onTab={vi.fn()}
        sideChats={[makeChat("c1", "周末计划", "准备露营帐篷"), makeChat("c2", "读书清单", "科幻小说")]}
        onOpenChat={vi.fn()}
        onNewChat={vi.fn()}
        onOpenSettings={vi.fn()}
        onEditChat={vi.fn()}
        onDeleteChat={vi.fn()}
        search={search}
        onOpenHit={onOpenHit}
      />,
    );

    const input = screen.getByRole("textbox", { name: "搜索旁聊和聊天记录" });
    // Multi-keyword filtering on side chats
    fireEvent.change(input, { target: { value: "周末 露营" } });
    expect(screen.getByRole("button", { name: "周末计划" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "读书清单" })).not.toBeInTheDocument();

    // Multi-keyword search on chat history
    fireEvent.change(input, { target: { value: "杭州 雨伞" } });
    await act(async () => {
      vi.advanceTimersByTime(400);
    });
    expect(search).toHaveBeenCalledWith("杭州 雨伞");
    expect(screen.getByText("杭州").tagName).toBe("B");
    expect(screen.getByText("雨伞").tagName).toBe("B");
    expect(screen.getByText("我")).toBeInTheDocument();
    expect(screen.getByText("周末出行")).toBeInTheDocument();

    // Tapping a hit saves recent search and invokes onOpenHit(hit) unchanged
    fireEvent.click(screen.getByRole("button", { name: /西湖徒步/ }));
    expect(onOpenHit).toHaveBeenCalledWith(hit);
    expect(JSON.parse(localStorage.getItem(RECENT_SEARCHES_KEY) ?? "[]")).toEqual(["杭州 雨伞"]);

    // Reopening drawer shows persisted recent search chip; tapping it populates query, clearing wipes storage
    rerender(
      <MuseDrawer
        open={false}
        onClose={onClose}
        name="小糯"
        current="chat"
        activeChatId={null}
        order={["chat", "feed", "ideas", "goals", "creations"]}
        onReorder={vi.fn()}
        onTab={vi.fn()}
        sideChats={[]}
        onOpenChat={vi.fn()}
        onNewChat={vi.fn()}
        onOpenSettings={vi.fn()}
        onEditChat={vi.fn()}
        onDeleteChat={vi.fn()}
        search={search}
        onOpenHit={onOpenHit}
      />,
    );
    rerender(
      <MuseDrawer
        open
        onClose={onClose}
        name="小糯"
        current="chat"
        activeChatId={null}
        order={["chat", "feed", "ideas", "goals", "creations"]}
        onReorder={vi.fn()}
        onTab={vi.fn()}
        sideChats={[]}
        onOpenChat={vi.fn()}
        onNewChat={vi.fn()}
        onOpenSettings={vi.fn()}
        onEditChat={vi.fn()}
        onDeleteChat={vi.fn()}
        search={search}
        onOpenHit={onOpenHit}
      />,
    );

    expect(screen.getByText("最近搜索")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "杭州 雨伞" }));
    expect(screen.getByRole("textbox", { name: "搜索旁聊和聊天记录" })).toHaveValue("杭州 雨伞");

    fireEvent.click(screen.getByRole("button", { name: "清空搜索" }));
    expect(screen.getByRole("textbox", { name: "搜索旁聊和聊天记录" })).toHaveValue("");

    fireEvent.click(screen.getByRole("button", { name: "清空最近搜索" }));
    expect(screen.queryByText("最近搜索")).not.toBeInTheDocument();
    expect(localStorage.getItem(RECENT_SEARCHES_KEY)).toBeNull();
  });
});

describe("SettingsScreen runtime capabilities & diagnostics", () => {
  it("describes ready vs missing capabilities with actionable setup guidance", () => {
    const ready = describeCapabilities(baseCompanion({ stt: true, push: true, browser: true }), { kind: "on" });
    expect(ready.map((d) => [d.key, d.status])).toEqual([
      ["stt", "已就绪"],
      ["push", "已开启"],
      ["browser", "已就绪"],
    ]);

    const missing = describeCapabilities(baseCompanion({ stt: false, push: false, browser: false }), { kind: "off" });
    expect(missing.map((d) => [d.key, d.status])).toEqual([
      ["stt", "未就绪"],
      ["push", "服务端未启用"],
      ["browser", "未就绪"],
    ]);
    expect(missing[0].guidance).toMatch(/ffmpeg|SenseVoice/);
    expect(missing[1].guidance).toMatch(/Web Push|VAPID/);
    expect(missing[2].guidance).toMatch(/Playwright|Chromium/);
  });

  it("renders live capability diagnostics in SettingsRoot", async () => {
    fetchCompanionStateMock.mockResolvedValue(baseCompanion({ stt: false, push: false, browser: true }));
    render(
      <SettingsScreen
        start="root"
        onClose={vi.fn()}
        onLogout={vi.fn()}
        onDraft={vi.fn()}
        onTeach={vi.fn()}
        onAppearance={vi.fn()}
      />,
    );

    expect(await screen.findByText("能力与诊断")).toBeInTheDocument();
    expect(await screen.findByText(/SenseVoice/)).toBeInTheDocument();
    expect(screen.getByText("服务端未启用")).toBeInTheDocument();
    expect(screen.getByText(/遇到登录或验证码时可随时交给你接手/)).toBeInTheDocument();
  });
});

describe("Secondary tabs cohesive empty states, error-retry cards, and active progress", () => {
  it("shows an error-retry card on IdeasView failure and offers primary generate CTA when empty", async () => {
    fetchIdeasMock.mockRejectedValueOnce(new Error("模型网关超时")).mockResolvedValueOnce({ generated_at: 1, ideas: [] });
    requestMutation.mockResolvedValueOnce({
      generated_at: 2,
      ideas: [{ title: "整理本周收据", reason: "周五要报销", prompt: "帮我整理本周收据" }],
    });

    render(<IdeasView active onDo={vi.fn()} />);

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("点子没加载出来");
    expect(alert).toHaveTextContent("模型网关超时");
    expect(screen.queryByText("暂时还没有新点子")).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "重试" }));
    expect(await screen.findByText("暂时还没有新点子")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "现在想几个" }));
    expect(requestMutation).toHaveBeenCalledWith("companion.ideas.refresh", {}, 90_000);
    expect(await screen.findByText("整理本周收据")).toBeInTheDocument();
  });

  it("shows active writing card and retryable like error on FeedScreen", async () => {
    const feed: FeedPayload = {
      instructions: "关注前端工程与咖啡",
      daily: true,
      time: "08:00",
      generated_at: 1,
      posts: [{ id: "p1", ts: Date.now(), title: "Vite 新特性", body: "构建更快了。", area: "tech", image: null, prompt: "聊聊" }],
    };
    requestMutation.mockRejectedValueOnce(new Error("网络波动")).mockResolvedValueOnce({ ...feed, liked: ["p1"] });
    const onChange = vi.fn();

    render(<FeedScreen data={feed} writing onEditInstructions={vi.fn()} onWriteNow={vi.fn()} onDiscuss={vi.fn()} onChange={onChange} />);
    expect(screen.getByText("正在写新一期动态…")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "喜欢" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("网络波动");

    fireEvent.click(screen.getByRole("button", { name: "重试" }));
    await waitFor(() => expect(onChange).toHaveBeenCalledWith(expect.objectContaining({ liked: ["p1"] })));
  });

  it("shows retry card and live progress badge in GoalsScreen and HelpersScreen", async () => {
    const activeGoal: Goal = {
      id: "g1",
      title: "学会游泳",
      steps: [
        { text: "学会换气", status: "done" },
        { text: "游完 50 米", status: "doing" },
      ],
      checkin: "weekly mon 09:00",
      due: null,
      done: false,
      category: "health",
      created_at: Date.now(),
      next_checkin_ms: null,
    };
    fetchGoalsMock.mockRejectedValueOnce(new Error("服务不可达")).mockResolvedValueOnce([activeGoal]);

    render(
      <GoalsScreen
        active
        name="小糯"
        approvals={[]}
        onDecide={vi.fn()}
        onSend={vi.fn()}
        working={{ label: "整理训练计划", asked: "推进目标「学会游泳」" }}
      />,
    );

    expect(await screen.findByText("目标列表没加载出来")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "重试" }));
    expect(await screen.findByText("学会游泳")).toBeInTheDocument();
    expect(screen.getByText("推进中")).toBeInTheDocument();
    cleanup();

    const emptyHelpers: HelpersPayload = { helpers: [], templates: [], max: 12, groups: [], max_group: 6 };
    fetchHelpersMock.mockRejectedValueOnce(new Error("读取失败")).mockResolvedValueOnce(emptyHelpers);
    render(<HelpersScreen active onOpenChat={vi.fn()} />);

    expect(await screen.findByText("帮手列表没加载出来")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "重试" }));
    expect(await screen.findByText("还没有帮手")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "创建第一个帮手" }));
    expect(screen.getByRole("textbox", { name: "新帮手负责什么" })).toBeInTheDocument();
  });
});

describe("SoulMemoryPane memory provenance and fade explanation", () => {
  it("distinguishes user_written, learned, and faded entries and explains memory fade", async () => {
    const changes: MemoryChange[] = [
      {
        id: "c1",
        at: 1_700_000_100,
        file: "USER.md",
        kind: "learn-add",
        before: [],
        after: "【喜好与习惯】最近迷上手冲咖啡",
      },
      {
        id: "c2",
        at: 1_700_000_200,
        file: "USER.md",
        kind: "fade",
        before: ["【近况】上周三去看了牙医补右后槽牙"],
        after: "【近况】之前看过牙医",
        why: "超过一个月没再提起，保留大概",
      },
    ];

    expect(
      classifyMemoryItem(
        { id: "m1", file: "USER.md", line: "【个人信息】对花生过敏", text: "【个人信息】对花生过敏" },
        changes,
      ),
    ).toBe("user_written");
    expect(
      classifyMemoryItem(
        { id: "m2", file: "USER.md", line: "【喜好与习惯】最近迷上手冲咖啡", text: "【喜好与习惯】最近迷上手冲咖啡" },
        changes,
      ),
    ).toBe("learned");
    expect(
      classifyMemoryItem(
        { id: "m3", file: "USER.md", line: "【近况】之前看过牙医", text: "【近况】之前看过牙医" },
        changes,
      ),
    ).toBe("faded");

    fetchMemoryDocMock.mockResolvedValue({
      text: "## 个人信息\n\n对花生过敏\n\n## 喜好与习惯\n\n最近迷上手冲咖啡\n\n## 近况\n\n之前看过牙医\n",
    });
    fetchMemoryMock.mockResolvedValue({
      sections: [
        {
          file: "USER.md",
          title: "关于你",
          items: [
            { id: "m1", file: "USER.md", line: "【个人信息】对花生过敏", text: "【个人信息】对花生过敏" },
            { id: "m2", file: "USER.md", line: "【喜好与习惯】最近迷上手冲咖啡", text: "【喜好与习惯】最近迷上手冲咖啡" },
            { id: "m3", file: "USER.md", line: "【近况】之前看过牙医", text: "【近况】之前看过牙医" },
          ],
        },
      ],
      changes,
      learned_at: 1_700_000_200,
    });

    render(<SoulMemoryPane />);
    fireEvent.click(screen.getByRole("button", { name: /MEMORY\.md/ }));

    expect(await screen.findByText(/记忆来源与自然淡化/)).toBeInTheDocument();
    expect(await screen.findByText(/1 条你写的 · 2 条自动提炼/)).toBeInTheDocument();
    expect(screen.getAllByText("你写的").length).toBeGreaterThanOrEqual(2);
    expect(screen.getByText("聊天中学到")).toBeInTheDocument();
    expect(screen.getByText("已淡化")).toBeInTheDocument();
    expect(screen.getByText("很久没提，记个大概")).toBeInTheDocument();
    expect(screen.getByText("超过一个月没再提起，保留大概")).toBeInTheDocument();
  });
});
