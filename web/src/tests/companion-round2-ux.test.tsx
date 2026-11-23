import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { AddModelSheet } from "@/companion/AddModelSheet";
import { AppearanceDock } from "@/companion/AppearanceDock";
import { ApprovalCard } from "@/companion/ApprovalCard";
import { EmptyThread } from "@/companion/ChatThread";
import { Composer } from "@/companion/Composer";
import { DevicesPanel } from "@/companion/DevicesPanel";
import { Pressable } from "@/companion/MessageActions";
import { ModelPanel } from "@/companion/ModelPanel";
import { NotificationsRow, notificationsNote, useNotifications } from "@/companion/NotificationsAction";
import { Onboarding } from "@/companion/Onboarding";
import { PermissionLevels } from "@/companion/PermissionLevels";
import { PageSheet } from "@/companion/Screen";
import { IdeasView } from "@/companion/TabViews";
import { Viewer } from "@/companion/Viewer";
import type { ApprovalRecord, CompanionState, Goal, ModelsPayload } from "@/companion/companion-api";
import { BrowserFullscreen } from "@/companion/muse/BrowserPane";
import { SwipeRow } from "@/companion/muse/Drawer";
import { GoalsScreen } from "@/companion/muse/GoalsScreen";
import { SoulMemoryPane } from "@/companion/muse/Identity";
import { MuseSheet } from "@/companion/muse/Sheet";
import { StatusSheet } from "@/companion/muse/Shell";

const {
  requestMutation,
  fetchGoalsMock,
  fetchRoutinesMock,
  fetchTriggersMock,
  fetchSoulMock,
  fetchMemoryDocMock,
  fetchMemoryMock,
  fetchModelCatalogMock,
  fetchVendorModelsMock,
  fetchPermissionsMock,
  fetchModelsMock,
  fetchSkillsMock,
  fetchHelpersMock,
  fetchIdeasMock,
  fetchDevicesMock,
  fetchAutomationsMock,
} = vi.hoisted(() => ({
  requestMutation: vi.fn(),
  fetchGoalsMock: vi.fn(),
  fetchRoutinesMock: vi.fn(),
  fetchTriggersMock: vi.fn(),
  fetchSoulMock: vi.fn(),
  fetchMemoryDocMock: vi.fn(),
  fetchMemoryMock: vi.fn(),
  fetchModelCatalogMock: vi.fn(),
  fetchVendorModelsMock: vi.fn(),
  fetchPermissionsMock: vi.fn(),
  fetchModelsMock: vi.fn(),
  fetchSkillsMock: vi.fn(),
  fetchHelpersMock: vi.fn(),
  fetchIdeasMock: vi.fn(),
  fetchDevicesMock: vi.fn(),
  fetchAutomationsMock: vi.fn(),
}));

vi.mock("@/providers/ClientProvider", () => {
  const client = { requestMutation };
  return { useClient: () => ({ client, token: "t" }) };
});

vi.mock("@/lib/api", async (original) => ({
  ...(await original<typeof import("@/lib/api")>()),
  fetchAutomations: (...args: unknown[]) => fetchAutomationsMock(...args),
  fetchMcpPresets: vi.fn(async () => ({ presets: [], installed_count: 0 })),
}));

vi.mock("@/companion/device", async (original) => ({
  ...(await original<typeof import("@/companion/device")>()),
  fetchDevices: (...args: unknown[]) => fetchDevicesMock(...args),
}));

vi.mock("@/companion/companion-api", async (original) => {
  const actual = await original<typeof import("@/companion/companion-api")>();
  return {
    ...actual,
    fetchGoals: (...args: unknown[]) => fetchGoalsMock(...args),
    fetchRoutines: (...args: unknown[]) => fetchRoutinesMock(...args),
    fetchTriggers: (...args: unknown[]) => fetchTriggersMock(...args),
    fetchSoul: (...args: unknown[]) => fetchSoulMock(...args),
    fetchMemoryDoc: (...args: unknown[]) => fetchMemoryDocMock(...args),
    fetchMemory: (...args: unknown[]) => fetchMemoryMock(...args),
    fetchModelCatalog: (...args: unknown[]) => fetchModelCatalogMock(...args),
    fetchVendorModels: (...args: unknown[]) => fetchVendorModelsMock(...args),
    fetchPermissions: (...args: unknown[]) => fetchPermissionsMock(...args),
    fetchModels: (...args: unknown[]) => fetchModelsMock(...args),
    fetchSkills: (...args: unknown[]) => fetchSkillsMock(...args),
    fetchHelpers: (...args: unknown[]) => fetchHelpersMock(...args),
    fetchIdeas: (...args: unknown[]) => fetchIdeasMock(...args),
  };
});

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

const baseCompanion: CompanionState = {
  name: "小糯",
  avatar: "mochi:cream",
  chat_id: "c1",
  tone: "warm",
  user_name: null,
  style_note: null,
  onboarded: false,
  timezone: null,
  updated_at: null,
};

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.clearAllMocks();
  delete (window as Window & { PersonalBotAndroid?: unknown }).PersonalBotAndroid;
});

describe("Unified overlay Escape / Android Back stack", () => {
  it("closes a nested MuseSheet first before closing the underlying PageSheet", () => {
    const closePage = vi.fn();
    const closeSheet = vi.fn();
    const { rerender } = render(
      <>
        <PageSheet title="设置" onBack={closePage} onClose={closePage}>
          <p>设置内容</p>
        </PageSheet>
        <MuseSheet open onClose={closeSheet} title="添加模型">
          <p>弹窗内容</p>
        </MuseSheet>
      </>,
    );

    fireEvent.keyDown(document, { key: "Escape" });
    expect(closeSheet).toHaveBeenCalledTimes(1);
    expect(closePage).not.toHaveBeenCalled();

    rerender(
      <>
        <PageSheet title="设置" onBack={closePage} onClose={closePage}>
          <p>设置内容</p>
        </PageSheet>
        <MuseSheet open={false} onClose={closeSheet} title="添加模型">
          <p>弹窗内容</p>
        </MuseSheet>
      </>,
    );

    fireEvent.keyDown(document, { key: "Escape" });
    expect(closePage).toHaveBeenCalledTimes(1);
  });

  it("closes AppearanceDock on Escape", () => {
    const onDone = vi.fn();
    render(<AppearanceDock onDone={onDone} />);
    fireEvent.keyDown(document, { key: "Escape" });
    expect(onDone).toHaveBeenCalledTimes(1);
  });
});

describe("GoalsScreen inline confirmation & proposal error feedback", () => {
  const sampleGoal: Goal = {
    id: "g1",
    title: "每周跑步三次",
    category: "health",
    due: null,
    checkin: null,
    steps: [{ text: "买跑鞋", status: "pending" }],
    log: [],
    done: false,
    paused: false,
    created_at: 1_700_000_000,
    updated_at: 1_700_000_000,
  } as unknown as Goal;

  it("deletes a goal via inline two-step confirmation without window.confirm", async () => {
    const confirmSpy = vi.spyOn(window, "confirm");
    fetchGoalsMock.mockResolvedValue([sampleGoal]);
    fetchRoutinesMock.mockResolvedValue({ brief_time: "08:00", checkins: true });
    fetchTriggersMock.mockResolvedValue([]);
    fetchAutomationsMock.mockResolvedValue({ jobs: [] });
    requestMutation.mockResolvedValue({ goals: [] });

    render(
      <GoalsScreen
        active
        name="小糯"
        approvals={[]}
        onDecide={vi.fn()}
        onSend={vi.fn()}
        working={null}
        computer={null}
        onOpenCreations={vi.fn()}
      />,
    );

    fireEvent.click(await screen.findByText("每周跑步三次"));
    fireEvent.click(screen.getByRole("button", { name: "删除目标" }));
    expect(confirmSpy).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "取消" })).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "确认删除" }));
    await waitFor(() =>
      expect(requestMutation).toHaveBeenCalledWith("companion.goals.change", { id: "g1", action: "remove" }),
    );
    confirmSpy.mockRestore();
  });

  it("surfaces errors when approving a goal proposal fails", async () => {
    fetchGoalsMock.mockResolvedValue([sampleGoal]);
    fetchRoutinesMock.mockResolvedValue({ brief_time: null, checkins: false });
    fetchTriggersMock.mockResolvedValue([]);
    fetchAutomationsMock.mockResolvedValue({ jobs: [] });

    const proposal: ApprovalRecord = {
      id: "ap1",
      call_id: "c1",
      session_key: "websocket:c1",
      tool: "goals",
      action: "goal_plan",
      arguments: { reason: "雨天改室内", steps: ["室内跳绳 20 分钟"] },
      context: { goal: { title: "每周跑步三次", steps: [{ text: "买跑鞋", status: "done" }] } },
      status: "pending",
      created_at_ms: 1_000,
    };

    render(
      <GoalsScreen
        active
        name="小糯"
        approvals={[proposal]}
        onDecide={vi.fn().mockRejectedValue(new Error("网络断开，没能更新计划"))}
        onSend={vi.fn()}
        working={null}
        computer={null}
        onOpenCreations={vi.fn()}
      />,
    );

    fireEvent.click(await screen.findByRole("button", { name: "用小糯的计划" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("网络断开，没能更新计划");
  });
});

describe("SoulMemoryPane race guard, dirty-edit prompt, and tidyMemory", () => {
  it("ignores stale SOUL.md responses after switching to MEMORY.md, guards dirty edits, and tidies memory", async () => {
    const slowSoul = deferred<{ text: string }>();
    fetchSoulMock.mockReturnValueOnce(slowSoul.promise);
    fetchMemoryDocMock.mockResolvedValue({ text: "【喜好】喜欢乌龙茶" });
    fetchMemoryMock.mockResolvedValue({ changes: [], learned_at: null });
    requestMutation.mockResolvedValue({ changes: [], tidied: { changed: 2 } });

    render(<SoulMemoryPane />);

    fireEvent.click(screen.getByRole("button", { name: /SOUL\.md/ }));
    fireEvent.click(screen.getByRole("button", { name: /MEMORY\.md/ }));

    const editor = await screen.findByRole("textbox", { name: "MEMORY.md" });
    await waitFor(() => expect(editor).toHaveValue("【喜好】喜欢乌龙茶"));

    // Late SOUL.md resolution must NOT clobber MEMORY.md.
    await act(async () => {
      slowSoul.resolve({ text: "我是旧的 SOUL 内容" });
    });
    expect(screen.getByRole("textbox", { name: "MEMORY.md" })).toHaveValue("【喜好】喜欢乌龙茶");

    // Tidy memory action
    fireEvent.click(screen.getByRole("button", { name: "整理一下" }));
    await waitFor(() => expect(requestMutation).toHaveBeenCalledWith("companion.memory.tidy", {}, 180_000));
    expect(await screen.findByText("整理了 2 处")).toBeInTheDocument();

    // Dirty edit guard when switching tabs
    fireEvent.change(screen.getByRole("textbox", { name: "MEMORY.md" }), {
      target: { value: "【喜好】喜欢乌龙茶\n\n【习惯】早起" },
    });
    fireEvent.click(screen.getByRole("button", { name: /SOUL\.md/ }));
    expect(screen.getByRole("alert")).toHaveTextContent("还有没保存的修改，要放弃吗？");
    fireEvent.click(screen.getByRole("button", { name: "继续编辑" }));
    expect(screen.queryByRole("alert")).toBeNull();
    expect(screen.getByRole("textbox", { name: "MEMORY.md" })).toHaveValue("【喜好】喜欢乌龙茶\n\n【习惯】早起");
  });
});

describe("AddModelSheet, PermissionLevels, DevicesPanel, and NotificationsAction resilience", () => {
  it("ignores stale vendor catalog responses when switching vendors rapidly", async () => {
    const slowVendorA = deferred<{ models: { id: string; name: string; recommended?: boolean }[]; checked: null }>();
    fetchModelCatalogMock.mockResolvedValue([
      { id: "openai", name: "OpenAI", group: "global", kinds: ["text"], has_key: true, key_hint: null, base_url: "https://api.openai.com/v1" },
      { id: "deepseek", name: "DeepSeek", group: "cn", kinds: ["text"], has_key: true, key_hint: null, base_url: "https://api.deepseek.com/v1" },
    ]);
    fetchVendorModelsMock.mockImplementation((_client: unknown, vendorId: string) => {
      if (vendorId === "openai") return slowVendorA.promise;
      return Promise.resolve({
        models: [{ id: "deepseek-chat", name: "DeepSeek V3", recommended: true }],
        checked: null,
      });
    });

    render(<AddModelSheet open kind="text" onClose={vi.fn()} onAdded={vi.fn()} />);

    fireEvent.click(await screen.findByRole("button", { name: /OpenAI/ }));
    fireEvent.click(screen.getByRole("button", { name: "换一个服务商" }));
    fireEvent.click(await screen.findByRole("button", { name: /DeepSeek/ }));

    expect(await screen.findByText("DeepSeek V3")).toBeInTheDocument();

    await act(async () => {
      slowVendorA.resolve({ models: [{ id: "gpt-4o", name: "GPT-4o", recommended: true }], checked: null });
    });
    expect(screen.queryByText("GPT-4o")).toBeNull();
    expect(screen.getByText("DeepSeek V3")).toBeInTheDocument();
  });

  it("closes LevelPicker on Escape without closing PageSheet, and rolls back failed permission updates", async () => {
    fetchPermissionsMock.mockResolvedValue({
      kinds: [
        {
          key: "run_command",
          label: "运行命令",
          detail: "在服务器上执行终端命令",
          level: "ask",
          levels: ["deny", "ask", "allow"],
        },
      ],
    });
    requestMutation.mockRejectedValueOnce(new Error("保存权限失败"));
    const closePage = vi.fn();

    render(
      <PageSheet title="执行权限" onBack={closePage} onClose={closePage}>
        <PermissionLevels />
      </PageSheet>,
    );

    const trigger = await screen.findByRole("button", { name: "运行命令：询问我" });
    expect(trigger).toHaveTextContent("询问我");

    fireEvent.click(trigger);
    expect(screen.getByRole("listbox", { name: "运行命令" })).toBeInTheDocument();

    // Escape dismisses dropdown only, not PageSheet.
    fireEvent.keyDown(window, { key: "Escape" });
    expect(screen.queryByRole("listbox", { name: "运行命令" })).toBeNull();
    expect(closePage).not.toHaveBeenCalled();

    // Optimistic update rolls back when mutation rejects.
    fireEvent.click(trigger);
    fireEvent.click(screen.getByRole("option", { name: "允许" }));
    expect(await screen.findByText("保存权限失败")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "运行命令：询问我" })).toHaveTextContent("询问我");
  });

  it("shows explicit error & retry in DevicesPanel when fetchDevices fails instead of claiming localhost", async () => {
    fetchDevicesMock
      .mockRejectedValueOnce(new Error("offline"))
      .mockResolvedValueOnce([{ id: "d1", name: "我的手机", paired_at: 1_700_000_000_000, last_seen: 1_700_000_000_000 }]);

    render(<DevicesPanel onDisconnect={vi.fn()} />);

    expect(await screen.findByText("设备列表没读出来，检查一下网络")).toBeInTheDocument();
    expect(screen.queryByText("这台是服务器本机，不需要配对。")).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "重试" }));
    expect(await screen.findByText("我的手机")).toBeInTheDocument();
  });

  it("recovers from a hanging navigator.serviceWorker.ready with a bounded timeout", async () => {
    vi.useFakeTimers();
    const originalSecure = window.isSecureContext;
    const originalSw = navigator.serviceWorker;
    const originalPush = (window as Window & { PushManager?: unknown }).PushManager;
    const originalNotif = (window as Window & { Notification?: unknown }).Notification;

    Object.defineProperty(window, "isSecureContext", { configurable: true, value: true });
    Object.defineProperty(navigator, "serviceWorker", {
      configurable: true,
      value: { ready: new Promise<ServiceWorkerRegistration>(() => {}) },
    });
    Object.defineProperty(window, "PushManager", { configurable: true, value: function PushManager() {} });
    Object.defineProperty(window, "Notification", {
      configurable: true,
      value: { permission: "default", requestPermission: vi.fn() },
    });

    function Probe() {
      const { status, toggle } = useNotifications();
      return (
        <div>
          <NotificationsRow status={status} onToggle={toggle} />
          <p>{notificationsNote(status)}</p>
        </div>
      );
    }

    try {
      render(<Probe />);
      expect(screen.getByText("检查中")).toBeInTheDocument();
      await act(async () => {
        vi.advanceTimersByTime(4_100);
      });
      expect(screen.getByText(/后台通知服务未就绪/)).toBeInTheDocument();
    } finally {
      Object.defineProperty(window, "isSecureContext", { configurable: true, value: originalSecure });
      Object.defineProperty(navigator, "serviceWorker", { configurable: true, value: originalSw });
      Object.defineProperty(window, "PushManager", { configurable: true, value: originalPush });
      Object.defineProperty(window, "Notification", { configurable: true, value: originalNotif });
    }
  });
});

describe("ModelPanel, ApprovalCard, Composer, Onboarding, Viewer, StatusSheet & keyboard actions", () => {
  const emptyModelsPayload: ModelsPayload = {
    default: null,
    presets: [],
    fallbacks: [],
    max_tool_iterations: 20,
    settings: {
      enabled: false,
      fixed: null,
      models: {},
    } as unknown as ModelsPayload["settings"],
    decisions: [],
    health: {},
    media: {
      image: { active: "flux-schnell", models: [{ name: "flux-schnell", model: "flux-schnell", vendor: "fal", price: null }] },
      video: { active: null, models: [] },
    },
  };

  it("renders empty chat-model CTA and allows deleting a sole active media model", async () => {
    fetchModelsMock.mockResolvedValue(emptyModelsPayload);
    fetchModelCatalogMock.mockResolvedValue([
      { id: "fal", name: "fal.ai", group: "global", kinds: ["image"], has_key: true, key_hint: null, base_url: "" },
    ]);
    requestMutation.mockResolvedValue({
      ...emptyModelsPayload,
      media: { ...emptyModelsPayload.media, image: { active: null, models: [] } },
    });

    render(<ModelPanel active onOpenModel={vi.fn()} />);

    expect(await screen.findByText("还没有对话模型")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "添加对话模型" })).toBeInTheDocument();

    fireEvent.click(screen.getByRole("radio", { name: /图像/ }));
    expect(await screen.findByText("flux-schnell")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "检测价格" })).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "删除" }));
    fireEvent.click(screen.getByRole("button", { name: "确认删除" }));
    await waitFor(() =>
      expect(requestMutation).toHaveBeenCalledWith("companion.models.remove", { name: "flux-schnell" }),
    );
  });

  it("shows payment warning, target host, and disables expired approvals on ApprovalCard", () => {
    const expiredPayment: ApprovalRecord = {
      id: "pay1",
      call_id: "call_pay",
      session_key: "websocket:c1",
      tool: "browser_click",
      action: "payment",
      arguments: { label: "立即支付 ¥99" },
      context: { browser: { paying: true, host: "checkout.example.com", title: "收银台" } },
      status: "pending",
      created_at_ms: Date.now() - 120_000,
      expires_at_ms: Date.now() - 1_000,
    };

    render(<ApprovalCard approval={expiredPayment} onDecide={vi.fn()} />);

    expect(screen.getByText("付款或下单")).toBeInTheDocument();
    expect(screen.getByText(/checkout\.example\.com/)).toBeInTheDocument();
    const expiredBtn = screen.getByRole("button", { name: "已超时失效" });
    expect(expiredBtn).toBeDisabled();
  });

  it("merges learned skills into Composer / menu and hides voice mic when voiceEnabled is false", async () => {
    fetchHelpersMock.mockResolvedValue({ helpers: [], groups: [], templates: [] });
    fetchSkillsMock.mockResolvedValue({
      learned: [{ name: "周报汇总", description: "按模板整理本周工作进展", category: "办公" }],
      builtin: [],
    });
    const onChange = vi.fn();

    render(
      <Composer
        value="/"
        onChange={onChange}
        onSend={vi.fn()}
        onStop={vi.fn()}
        streaming={false}
        connected
        voiceEnabled={false}
        transcribeAudio={vi.fn()}
      />,
    );

    expect(screen.queryByRole("button", { name: "按住说话" })).toBeNull();
    const skillBtn = await screen.findByRole("button", { name: /周报汇总/ });
    fireEvent.click(skillBtn);
    expect(onChange).toHaveBeenCalledWith("用「周报汇总」技能帮我：");
  });

  it("renders IdeasView empty state when ideas list is empty", async () => {
    fetchIdeasMock.mockResolvedValue({ ideas: [], updated_at: null });
    render(<IdeasView active onDo={vi.fn()} />);
    expect(await screen.findByText("暂时还没有新点子")).toBeInTheDocument();
  });

  it("inserts model step in Onboarding when no models are configured and offers notification permission prompt", async () => {
    fetchModelsMock.mockResolvedValue(emptyModelsPayload);
    const requestPermission = vi.fn(async () => "granted" as NotificationPermission);
    const originalNotif = (window as Window & { Notification?: unknown }).Notification;
    Object.defineProperty(window, "Notification", {
      configurable: true,
      value: { permission: "default", requestPermission },
    });

    try {
      render(<Onboarding companion={baseCompanion} onDone={vi.fn()} />);
      await waitFor(() => expect(screen.getByLabelText("第 1 步，共 6 步")).toBeInTheDocument());

      const next = () => fireEvent.click(screen.getByRole("button", { name: /开始|下一步/ }));
      next(); // look
      next(); // personality
      next(); // model
      expect(screen.getByText("先给小糯配一个对话模型")).toBeInTheDocument();
      expect(screen.getByRole("button", { name: "选择并添加模型" })).toBeInTheDocument();

      next(); // proactive
      fireEvent.click(screen.getByRole("button", { name: "开启通知" }));
      await waitFor(() => expect(requestPermission).toHaveBeenCalled());
      expect(await screen.findByText("已开启通知提醒")).toBeInTheDocument();
    } finally {
      Object.defineProperty(window, "Notification", { configurable: true, value: originalNotif });
    }
  });

  it("renders starter chips on EmptyThread, opens background approvals in StatusSheet, and calls PersonalBotAndroid.openFile in Viewer", async () => {
    const onSend = vi.fn();
    render(
      <EmptyThread
        avatar="mochi:cream"
        starters={[{ label: "理一下今天要做的事", prompt: "帮我排个优先级" }]}
        onSend={onSend}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "理一下今天要做的事" }));
    expect(onSend).toHaveBeenCalledWith("帮我排个优先级");
    cleanup();

    fetchAutomationsMock.mockResolvedValue({ jobs: [] });
    fetchTriggersMock.mockResolvedValue([]);
    const bgApproval: ApprovalRecord = {
      id: "bg1",
      call_id: "c_bg",
      session_key: "websocket:helper_1",
      tool: "bash",
      action: "run_command",
      arguments: { command: "ls -la", reason: "检查目录" },
      status: "pending",
      created_at_ms: 1_000,
    };
    render(
      <StatusSheet
        open
        onClose={vi.fn()}
        face={<span>🙂</span>}
        name="小糯"
        statusLine="1 件事等你确认"
        working={false}
        onStop={vi.fn()}
        approvals={[bgApproval]}
        sessionKey="websocket:c1"
        history={[]}
        computer={null}
        onDecide={vi.fn()}
        onTaught={vi.fn()}
      />,
    );
    expect(await screen.findByText("来自后台任务")).toBeInTheDocument();
    expect(screen.getByText("ls -la")).toBeInTheDocument();
    cleanup();

    const openFile = vi.fn();
    (window as Window & { PersonalBotAndroid?: unknown }).PersonalBotAndroid = { openFile };
    render(
      <Viewer
        items={[{ key: "doc1", kind: "document", url: "/api/media/s/report.pdf", name: "report.pdf", title: "报告" }]}
        start={0}
        onClose={vi.fn()}
      />,
    );
    fireEvent.click(screen.getByRole("link", { name: /用其他应用打开/ }));
    expect(openFile).toHaveBeenCalledWith(expect.stringContaining("/api/media/s/report.pdf"), "report.pdf");
  });

  it("supports keyboard triggers on SwipeRow and Pressable", () => {
    const onEdit = vi.fn();
    const onDelete = vi.fn();
    render(
      <ul>
        <SwipeRow swiped={false} onSwipe={vi.fn()} onOpen={vi.fn()} onEdit={onEdit} onDelete={onDelete}>
          <span>测试旁聊</span>
        </SwipeRow>
      </ul>,
    );

    const row = screen.getByRole("button", { name: "测试旁聊" });
    fireEvent.keyDown(row, { key: "F2" });
    expect(onEdit).toHaveBeenCalledTimes(1);
    fireEvent.keyDown(row, { key: "Delete" });
    expect(onDelete).toHaveBeenCalledTimes(1);
    cleanup();

    const onAction = vi.fn();
    render(
      <Pressable align="start" onAction={onAction}>
        <span>一条回复</span>
      </Pressable>,
    );
    const messageBox = screen.getByText("一条回复").parentElement!;
    fireEvent.keyDown(messageBox, { key: "ContextMenu" });
    expect(screen.getByRole("menu", { name: "这条消息" })).toBeInTheDocument();
    fireEvent.keyDown(window, { key: "Escape" });
    expect(screen.queryByRole("menu")).toBeNull();
  });

  it("keeps BrowserFullscreen on top of MuseSheet on Escape while typing and sends done when leaving", async () => {
    const sent: Record<string, unknown>[] = [];
    let latestWs: { onmessage: ((ev: { data: string }) => void) | null } | null = null;
    const OriginalWS = globalThis.WebSocket;
    class MockWS {
      static readonly OPEN = 1;
      readyState = 1;
      onmessage: ((ev: { data: string }) => void) | null = null;
      onclose: (() => void) | null = null;
      constructor() {
        // eslint-disable-next-line @typescript-eslint/no-this-alias
        latestWs = this;
      }
      send(raw: string) {
        sent.push(JSON.parse(raw) as Record<string, unknown>);
      }
      addEventListener() {}
      close() {}
    }
    globalThis.WebSocket = MockWS as unknown as typeof WebSocket;

    const closeSheet = vi.fn();
    const closeBrowser = vi.fn();
    try {
      render(
        <MuseSheet open onClose={closeSheet} title="状态">
          <BrowserFullscreen name="小糯" take onClose={closeBrowser} />
        </MuseSheet>,
      );

      act(() => {
        latestWs?.onmessage?.({ data: JSON.stringify({ type: "hold", hold: "user" }) });
        latestWs?.onmessage?.({ data: JSON.stringify({ type: "focus", editable: true }) });
      });
      expect(screen.getByText("直接打字，就会输进网页")).toBeInTheDocument();

      // First Escape exits typing mode and forwards Escape to browser without closing BrowserFullscreen or MuseSheet.
      fireEvent.keyDown(document, { key: "Escape" });
      expect(closeBrowser).not.toHaveBeenCalled();
      expect(closeSheet).not.toHaveBeenCalled();
      expect(sent).toContainEqual({ type: "key", key: "Escape" });

      // Second Escape leaves BrowserFullscreen and sends {"type":"done"} without closing underlying MuseSheet.
      fireEvent.keyDown(document, { key: "Escape" });
      expect(closeBrowser).toHaveBeenCalledTimes(1);
      expect(closeSheet).not.toHaveBeenCalled();
      expect(sent).toContainEqual({ type: "done" });
    } finally {
      globalThis.WebSocket = OriginalWS;
    }
  });

  it("enables Android native reminders via useNotifications in Onboarding", async () => {
    fetchModelsMock.mockResolvedValue({ ...emptyModelsPayload, presets: [{ name: "gpt-4o", model: "gpt-4o", vendor: "openai", temperature: 0.7 }] });
    let enabled = false;
    const setRemindersEnabled = vi.fn((on: boolean) => {
      enabled = on;
    });
    (window as Window & { PersonalBotAndroid?: unknown }).PersonalBotAndroid = {
      remindersEnabled: () => enabled,
      setRemindersEnabled,
    };

    render(<Onboarding companion={baseCompanion} onDone={vi.fn()} />);
    const next = () => fireEvent.click(screen.getByRole("button", { name: /开始|下一步/ }));
    next(); // look
    next(); // personality
    next(); // proactive

    fireEvent.click(await screen.findByRole("button", { name: "开启通知" }));
    await waitFor(() => expect(setRemindersEnabled).toHaveBeenCalledWith(true));
    expect(await screen.findByText("已开启通知提醒")).toBeInTheDocument();
  });

  it("refreshes Composer / learned skills when reopened without refetching on every keystroke", async () => {
    fetchSkillsMock
      .mockResolvedValueOnce({ learned: [{ name: "周报汇总", description: "整理本周工作", category: "办公" }], builtin: [] })
      .mockResolvedValueOnce({
        learned: [
          { name: "周报汇总", description: "整理本周工作", category: "办公" },
          { name: "报销贴票", description: "自动归类发票", category: "财务" },
        ],
        builtin: [],
      });

    const { rerender } = render(
      <Composer
        value="/"
        onChange={vi.fn()}
        onSend={vi.fn()}
        onStop={vi.fn()}
        streaming={false}
        connected
        transcribeAudio={vi.fn()}
      />,
    );

    expect(await screen.findByRole("button", { name: /周报汇总/ })).toBeInTheDocument();
    expect(fetchSkillsMock).toHaveBeenCalledTimes(1);

    // Typing further inside the same "/" session does NOT refetch.
    rerender(
      <Composer
        value="/周"
        onChange={vi.fn()}
        onSend={vi.fn()}
        onStop={vi.fn()}
        streaming={false}
        connected
        transcribeAudio={vi.fn()}
      />,
    );
    expect(fetchSkillsMock).toHaveBeenCalledTimes(1);

    // Closing "/" then reopening "/" fetches newly learned skills.
    rerender(
      <Composer
        value=""
        onChange={vi.fn()}
        onSend={vi.fn()}
        onStop={vi.fn()}
        streaming={false}
        connected
        transcribeAudio={vi.fn()}
      />,
    );
    rerender(
      <Composer
        value="/"
        onChange={vi.fn()}
        onSend={vi.fn()}
        onStop={vi.fn()}
        streaming={false}
        connected
        transcribeAudio={vi.fn()}
      />,
    );
    expect(await screen.findByRole("button", { name: /报销贴票/ })).toBeInTheDocument();
    expect(fetchSkillsMock).toHaveBeenCalledTimes(2);
  });
});
