import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { CardPlaceholder } from "@/companion/Cards";
import { ChatThread } from "@/companion/ChatThread";
import { CompanionScreen, LiveScreen } from "@/companion/CompanionScreen";
import { CompanionShell } from "@/companion/CompanionShell";
import { Composer } from "@/companion/Composer";
import { FileCard } from "@/companion/FileCard";
import type { CompanionState, ModelsPayload } from "@/companion/companion-api";
import { StatusSheet } from "@/companion/muse/Shell";
import { chatItems, dayLabel } from "@/companion/presentation";
import type { ChatSummary, UIMessage, UIToolEvent } from "@/lib/types";

const {
  requestMutationMock,
  streamState,
  sessionsState,
  historyState,
  fetchApprovalsMock,
  fetchAllApprovalsMock,
  fetchModelsMock,
  fetchComputerMock,
  fetchFeedMock,
  fetchCompanionStateMock,
  fetchHelpersMock,
  searchChatsMock,
  fetchSoulMock,
  fetchMemoryDocMock,
  fetchMemoryMock,
  fetchSkillsMock,
  fetchAutomationsMock,
} = vi.hoisted(() => ({
  requestMutationMock: vi.fn(),
  streamState: {
    messages: [] as UIMessage[],
    isStreaming: false,
    goalState: null,
    send: vi.fn(() => true),
    stop: vi.fn(),
    transcribeAudio: vi.fn(async () => ""),
    streamError: null,
    dismissStreamError: vi.fn(),
  },
  sessionsState: {
    sessions: [] as ChatSummary[],
    refresh: vi.fn(async () => undefined),
    createChat: vi.fn(async () => "c-new"),
  },
  historyState: {
    messages: [] as UIMessage[],
    loading: false,
    version: 1,
    hasPendingToolCalls: false,
    refresh: vi.fn(),
  },
  fetchApprovalsMock: vi.fn(),
  fetchAllApprovalsMock: vi.fn(),
  fetchModelsMock: vi.fn(),
  fetchComputerMock: vi.fn(),
  fetchFeedMock: vi.fn(),
  fetchCompanionStateMock: vi.fn(),
  fetchHelpersMock: vi.fn(),
  searchChatsMock: vi.fn(),
  fetchSoulMock: vi.fn(),
  fetchMemoryDocMock: vi.fn(),
  fetchMemoryMock: vi.fn(),
  fetchSkillsMock: vi.fn(),
  fetchAutomationsMock: vi.fn(),
}));

vi.mock("@/providers/ClientProvider", () => {
  const client = {
    status: "open" as const,
    onStatus: () => () => undefined,
    onApprovalUpdate: () => () => undefined,
    onFeedUpdate: () => () => undefined,
    requestMutation: requestMutationMock,
  };
  return { useClient: () => ({ client, token: "tok" }) };
});

vi.mock("@/companion/Pet", async (original) => ({
  ...(await original<typeof import("@/companion/Pet")>()),
  PetLibraryProvider: ({ children }: { children: ReactNode }) => <>{children}</>,
  PetView: () => <div data-testid="pet-view" />,
  PetStage: () => <div data-testid="pet-stage" />,
}));

vi.mock("@/hooks/useNanobotStream", () => ({
  useNanobotStream: () => streamState,
}));

vi.mock("@/hooks/useSessions", () => ({
  useSessions: () => sessionsState,
  useSessionHistory: () => historyState,
}));

vi.mock("@/lib/api", async (original) => ({
  ...(await original<typeof import("@/lib/api")>()),
  fetchAutomations: (...args: unknown[]) => fetchAutomationsMock(...args),
}));

vi.mock("@/companion/companion-api", async (original) => {
  const actual = await original<typeof import("@/companion/companion-api")>();
  return {
    ...actual,
    fetchApprovals: (...args: unknown[]) => fetchApprovalsMock(...args),
    fetchAllApprovals: (...args: unknown[]) => fetchAllApprovalsMock(...args),
    fetchModels: (...args: unknown[]) => fetchModelsMock(...args),
    fetchComputer: (...args: unknown[]) => fetchComputerMock(...args),
    fetchFeed: (...args: unknown[]) => fetchFeedMock(...args),
    fetchCompanionState: (...args: unknown[]) => fetchCompanionStateMock(...args),
    fetchHelpers: (...args: unknown[]) => fetchHelpersMock(...args),
    searchChats: (...args: unknown[]) => searchChatsMock(...args),
    fetchSoul: (...args: unknown[]) => fetchSoulMock(...args),
    fetchMemoryDoc: (...args: unknown[]) => fetchMemoryDocMock(...args),
    fetchMemory: (...args: unknown[]) => fetchMemoryMock(...args),
    fetchSkills: (...args: unknown[]) => fetchSkillsMock(...args),
  };
});

const baseCompanion: CompanionState = {
  name: "小糯",
  avatar: "mochi:cream",
  chat_id: "main",
  tone: "warm",
  user_name: null,
  style_note: null,
  onboarded: true,
  timezone: "Asia/Shanghai",
  updated_at: null,
};

function memoryEvent(note: string): UIToolEvent {
  return {
    call_id: "mem-1",
    name: "memory",
    phase: "end",
    arguments: { target: "user", action: "add", content: note },
    result: '{"success": true}',
  };
}

beforeEach(() => {
  fetchApprovalsMock.mockResolvedValue([]);
  fetchAllApprovalsMock.mockResolvedValue([]);
  fetchModelsMock.mockResolvedValue({ models: [], decisions: [] } as unknown as ModelsPayload);
  fetchComputerMock.mockResolvedValue({ running: false, since: null, page: null, hold: null, handoff: null, last_action: null });
  fetchFeedMock.mockResolvedValue({ instructions: "", daily: false, time: "08:00", generated_at: null, posts: [] });
  fetchCompanionStateMock.mockResolvedValue(baseCompanion);
  fetchHelpersMock.mockResolvedValue({ helpers: [], templates: [], max: 12, groups: [], max_group: 6 });
  searchChatsMock.mockResolvedValue([]);
  fetchSoulMock.mockResolvedValue({ text: "温柔可靠" });
  fetchMemoryDocMock.mockResolvedValue({ text: "## 关于你\n\n不吃香菜" });
  fetchMemoryMock.mockResolvedValue({ user: [], memory: [] });
  fetchSkillsMock.mockResolvedValue({ learned: [], bundled: [] });
  fetchAutomationsMock.mockResolvedValue({ jobs: [] });
  streamState.messages = [];
  streamState.isStreaming = false;
  streamState.streamError = null;
  sessionsState.sessions = [
    { key: "websocket:main", channel: "websocket", chatId: "main", createdAt: 1, updatedAt: 2, title: "主要聊天", preview: "" },
    { key: "websocket:side-1", channel: "websocket", chatId: "side-1", createdAt: 1, updatedAt: 2, title: "周末旅行", preview: "去西湖" },
  ];
  historyState.messages = [];
  historyState.loading = false;
  historyState.version = 1;
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.clearAllMocks();
});

describe("ChatThread streaming cursor & screen-reader completion announcement", () => {
  it("avoids whole-thread aria-live spam, renders a streaming cursor, and announces once on completion", () => {
    const initialHistory: UIMessage[] = [
      { id: "u1", role: "user", content: "早安", turnId: "t1", createdAt: 1_700_000_000_000 },
      { id: "a1", role: "assistant", content: "早安，今天想做点什么？", turnId: "t1", createdAt: 1_700_000_001_000 },
    ];
    const { container, rerender } = render(
      <ChatThread items={chatItems(initialHistory, 1_700_000_010_000)} typing={false} routes={new Map()} onDecide={vi.fn()} companionName="小糯" />,
    );

    // Outer container should not be aria-live="polite"
    expect(container.firstElementChild).not.toHaveAttribute("aria-live");
    // Initial history load does not falsely announce old messages
    const srStatus = screen.getByRole("status");
    expect(srStatus).toHaveClass("sr-only");
    expect(srStatus).toHaveTextContent("");
    expect(container.querySelector("[data-streaming-cursor]")).toBeNull();

    // Streaming delta arrives: cursor appears with motion-reduce support, sr-only stays quiet
    const streamingMessages: UIMessage[] = [
      ...initialHistory,
      { id: "u2", role: "user", content: "推荐一杯热饮", turnId: "t2", createdAt: 1_700_000_002_000 },
      { id: "a2", role: "assistant", content: "试试桂花拿铁", turnId: "t2", createdAt: 1_700_000_003_000, isStreaming: true },
    ];
    rerender(
      <ChatThread items={chatItems(streamingMessages, 1_700_000_010_000)} typing={false} routes={new Map()} onDecide={vi.fn()} companionName="小糯" />,
    );
    const cursor = container.querySelector("[data-streaming-cursor]");
    expect(cursor).not.toBeNull();
    expect(cursor?.className).toContain("motion-reduce:animate-none");
    expect(screen.getByRole("status")).toHaveTextContent("");

    // Streaming completes: cursor disappears and screen reader announces the completed reply once
    const completedMessages: UIMessage[] = streamingMessages.map((m) => (m.id === "a2" ? { ...m, content: "试试桂花拿铁，暖胃又清香。", isStreaming: false } : m));
    rerender(
      <ChatThread items={chatItems(completedMessages, 1_700_000_010_000)} typing={false} routes={new Map()} onDecide={vi.fn()} companionName="小糯" />,
    );
    expect(container.querySelector("[data-streaming-cursor]")).toBeNull();
    expect(screen.getByRole("status")).toHaveTextContent("小糯回复：试试桂花拿铁，暖胃又清香。");
  });
});

describe("Interactive Brain memory notice pill & StatusSheet initialPane", () => {
  it("tags remembered notes with kind='memory' and opens StatusSheet straight to 身份与记忆", async () => {
    const messages: UIMessage[] = [
      { id: "u1", role: "user", content: "我不吃香菜", turnId: "t1", createdAt: 1_700_000_000_000 },
      { id: "t-mem", role: "assistant", content: "", kind: "trace", turnId: "t1", createdAt: 1_700_000_001_000, toolEvents: [memoryEvent("【饮食】不吃香菜")] },
      { id: "a1", role: "assistant", content: "记下了。", turnId: "t1", createdAt: 1_700_000_002_000 },
      { id: "n1", role: "assistant", content: "系统提示", source: { kind: "notice", label: "system" }, createdAt: 1_700_000_003_000 } as UIMessage,
    ];
    const items = chatItems(messages, 1_700_000_010_000);
    const memNotice = items.find((item) => item.type === "notice" && item.kind === "memory");
    expect(memNotice).toMatchObject({ type: "notice", kind: "memory", text: "✓ 已记住：不吃香菜", note: "不吃香菜" });

    const onOpenMemory = vi.fn();
    render(<ChatThread items={items} typing={false} routes={new Map()} onDecide={vi.fn()} onOpenMemory={onOpenMemory} />);

    const pillButton = screen.getByRole("button", { name: "✓ 已记住：不吃香菜，查看记忆" });
    expect(pillButton).toBeInTheDocument();
    fireEvent.click(pillButton);
    expect(onOpenMemory).toHaveBeenCalledTimes(1);
    cleanup();

    // StatusSheet honors initialPane={3} even when there are pending approvals
    render(
      <StatusSheet
        open
        initialPane={3}
        onClose={vi.fn()}
        face={<div />}
        name="小糯"
        statusLine="在线"
        working={false}
        onStop={vi.fn()}
        approvals={[
          {
            id: "ap-1",
            session_key: "websocket:main",
            call_id: "c-1",
            tool_name: "exec",
            arguments: { command: "ls" },
            status: "pending",
            created_at_ms: 1000,
            decided_at_ms: null,
            result: null,
          },
        ]}
        history={[]}
        computer={null}
        onDecide={vi.fn()}
        onTaught={vi.fn()}
      />,
    );

    await waitFor(() => expect(fetchAutomationsMock).toHaveBeenCalled());
    expect(screen.getByRole("tab", { name: "身份与记忆" })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByRole("button", { name: /SOUL\.md/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /MEMORY\.md/ })).toBeInTheDocument();
  });
});

describe("MessageActions timestamp, Composer tactile controls, and motion-reduce skeletons", () => {
  it("renders formatted message timestamp in Pressable menu without changing menuitem count", () => {
    vi.useFakeTimers();
    const ts = new Date(2026, 4, 10, 14, 35).getTime();
    const msgs: UIMessage[] = [
      { id: "u1", role: "user", content: "下午去散步吗", turnId: "t1", createdAt: ts },
      { id: "a1", role: "assistant", content: "好呀，天气正好。", turnId: "t1", createdAt: ts + 5_000 },
    ];
    render(<ChatThread items={chatItems(msgs, ts + 10_000)} typing={false} routes={new Map()} onDecide={vi.fn()} onMessageAction={vi.fn()} />);

    fireEvent.pointerDown(screen.getByText("好呀，天气正好。"), { button: 0, clientX: 12, clientY: 12 });
    act(() => {
      vi.advanceTimersByTime(500);
    });

    const timeEl = screen.getByTestId("message-timestamp");
    expect(timeEl.tagName).toBe("TIME");
    expect(timeEl).toHaveTextContent(dayLabel(ts + 5_000));
    expect(screen.getAllByRole("menuitem").map((el) => el.textContent)).toEqual(["引用", "复制", "朗读", "重新生成", "删除"]);
  });

  it("triggers subtle haptic feedback on Composer send and stop, and applies motion-reduce to skeletons", async () => {
    const vibrate = vi.fn(() => true);
    Object.defineProperty(navigator, "vibrate", { value: vibrate, configurable: true });
    const onSend = vi.fn();
    const onStop = vi.fn();

    const { rerender } = render(
      <Composer
        value="你好"
        onChange={vi.fn()}
        onSend={onSend}
        onStop={onStop}
        streaming={false}
        connected
        transcribeAudio={vi.fn()}
      />,
    );

    const sendBtn = screen.getByRole("button", { name: "发送" });
    expect(sendBtn.firstElementChild?.className).toContain("group-active:scale-90");
    fireEvent.click(sendBtn);
    expect(onSend).toHaveBeenCalledWith("你好", [], false);
    expect(vibrate).toHaveBeenCalledWith(8);

    rerender(
      <Composer
        value=""
        onChange={vi.fn()}
        onSend={onSend}
        onStop={onStop}
        streaming
        connected
        transcribeAudio={vi.fn()}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "停止" }));
    expect(onStop).toHaveBeenCalledTimes(1);
    expect(vibrate).toHaveBeenCalledWith(10);
    cleanup();

    // CardPlaceholder & FileCard loading preview include motion-reduce:animate-none
    const { container: cardContainer } = render(<CardPlaceholder />);
    expect(cardContainer.firstElementChild?.className).toContain("motion-reduce:animate-none");
    cleanup();

    // Never-resolving fetch keeps FileCard in loading skeleton state until unmount aborts it cleanly
    vi.stubGlobal(
      "fetch",
      vi.fn(
        (_url: RequestInfo | URL, init?: RequestInit) =>
          new Promise<Response>((_resolve, reject) => {
            init?.signal?.addEventListener("abort", () => reject(new DOMException("Aborted", "AbortError")));
          }),
      ),
    );
    const { container: fileContainer, unmount } = render(
      <FileCard item={{ kind: "document", name: "notes.md", url: "/files/notes.md" }} />,
    );
    const pulseEl = fileContainer.querySelector(".animate-pulse");
    expect(pulseEl).not.toBeNull();
    expect(pulseEl?.className).toContain("motion-reduce:animate-none");
    unmount();
  });
});

describe("Scroll-to-latest pill & jump-to-message from search hits", () => {
  it("shows floating scroll-to-latest pill with unread reply count when scrolled up and scrolls down on click", async () => {
    streamState.messages = [
      { id: "u1", role: "user", content: "第一问", turnId: "t1", createdAt: 1_700_000_000_000 },
      { id: "a1", role: "assistant", content: "第一答", turnId: "t1", createdAt: 1_700_000_001_000 },
    ];

    const { container, rerender } = render(
      <LiveScreen
        ready
        historyVersion={1}
        chatId="main"
        sessionKey="websocket:main"
        initial={streamState.messages}
        pending={false}
        companion={baseCompanion}
        tab="chat"
        onTab={vi.fn()}
        order={["chat", "feed", "ideas", "goals", "helpers", "creations"]}
        side={null}
        onEditSide={vi.fn()}
        onOpenChat={vi.fn()}
        teachRequest={0}
        draft=""
        onDraft={vi.fn()}
        onOpenMenu={vi.fn()}
        onUpdateCompanion={vi.fn(async () => undefined)}
        onChangeLook={vi.fn()}
        onTurnEnd={vi.fn()}
        onReload={vi.fn()}
      />,
    );

    await waitFor(() => expect(fetchApprovalsMock).toHaveBeenCalled());
    expect(screen.queryByRole("button", { name: /回到最新/ })).not.toBeInTheDocument();

    const scrollBox = container.querySelector(".overflow-y-auto") as HTMLDivElement;
    const scrollToSpy = vi.fn();
    scrollBox.scrollTo = scrollToSpy;
    Object.defineProperty(scrollBox, "scrollHeight", { value: 1200, configurable: true });
    Object.defineProperty(scrollBox, "clientHeight", { value: 400, configurable: true });
    Object.defineProperty(scrollBox, "scrollTop", { value: 120, writable: true, configurable: true });

    fireEvent.scroll(scrollBox);
    expect(screen.getByRole("button", { name: "回到最新" })).toHaveTextContent("回到最新");

    // While scrolled up, a new assistant reply lands -> pill updates to "1 条新回复"
    streamState.messages = [
      ...streamState.messages,
      { id: "a2", role: "assistant", content: "顺便提醒你带伞。", turnId: "t2", createdAt: 1_700_000_005_000 },
    ];
    rerender(
      <LiveScreen
        ready
        historyVersion={1}
        chatId="main"
        sessionKey="websocket:main"
        initial={streamState.messages}
        pending={false}
        companion={baseCompanion}
        tab="chat"
        onTab={vi.fn()}
        order={["chat", "feed", "ideas", "goals", "helpers", "creations"]}
        side={null}
        onEditSide={vi.fn()}
        onOpenChat={vi.fn()}
        teachRequest={0}
        draft=""
        onDraft={vi.fn()}
        onOpenMenu={vi.fn()}
        onUpdateCompanion={vi.fn(async () => undefined)}
        onChangeLook={vi.fn()}
        onTurnEnd={vi.fn()}
        onReload={vi.fn()}
      />,
    );

    const unreadPill = screen.getByRole("button", { name: "回到最新，1 条新回复" });
    expect(unreadPill).toHaveTextContent("1 条新回复");

    fireEvent.click(unreadPill);
    expect(scrollToSpy).toHaveBeenCalledWith({ top: 1200, behavior: "smooth" });
    expect(screen.queryByRole("button", { name: /回到最新/ })).not.toBeInTheDocument();
  });

  it("scrolls to target data-turn-id and pulses highlight for 1.2s on jumpRequest and CompanionShell search hit", async () => {
    vi.useFakeTimers();
    const scrollIntoViewSpy = vi.fn();
    HTMLElement.prototype.scrollIntoView = scrollIntoViewSpy;

    const msgs: UIMessage[] = [
      { id: "u1", role: "user", content: "去哪里骑行", turnId: "turn-westlake", createdAt: 1_700_000_000_000 },
      { id: "a1", role: "assistant", content: "推荐去西湖杨公堤骑行。", turnId: "turn-westlake", createdAt: 1_700_000_001_000 },
      { id: "u2", role: "user", content: "好的谢谢", turnId: "turn-2", createdAt: 1_700_000_002_000 },
    ];
    historyState.messages = msgs;
    streamState.messages = msgs;

    const { container } = render(
      <CompanionScreen
        session={{ key: "websocket:main", channel: "websocket", chatId: "main", createdAt: 1, updatedAt: 2, preview: "" }}
        companion={baseCompanion}
        tab="chat"
        onTab={vi.fn()}
        order={["chat", "feed", "ideas", "goals", "helpers", "creations"]}
        side={null}
        onEditSide={vi.fn()}
        onOpenChat={vi.fn()}
        draftRequest={null}
        jumpRequest={{ turnId: "turn-westlake", at: 42 }}
        teachRequest={0}
        onOpenMenu={vi.fn()}
        onUpdateCompanion={vi.fn(async () => undefined)}
        onChangeLook={vi.fn()}
        onTurnEnd={vi.fn()}
      />,
    );

    await act(async () => {
      await Promise.resolve();
    });

    expect(scrollIntoViewSpy).toHaveBeenCalledWith({ behavior: "smooth", block: "center" });
    const highlighted = container.querySelectorAll('[data-turn-id="turn-westlake"][data-highlighted="true"]');
    expect(highlighted.length).toBe(2);

    act(() => {
      vi.advanceTimersByTime(1250);
    });
    expect(container.querySelectorAll('[data-highlighted="true"]').length).toBe(0);
    cleanup();

    // End-to-end CompanionShell -> MuseDrawer onOpenHit -> CompanionScreen jumpRequest
    scrollIntoViewSpy.mockClear();
    searchChatsMock.mockResolvedValue([
      {
        chat_id: "side-1",
        chat_title: "周末旅行",
        role: "assistant",
        text: "推荐去西湖杨公堤骑行。",
        turn_id: "turn-westlake",
        at_ms: 1_700_000_001_000,
      },
    ]);

    const { container: shellContainer } = render(<CompanionShell onLogout={vi.fn()} />);
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });

    fireEvent.click(screen.getByRole("button", { name: "菜单" }));
    fireEvent.change(screen.getByRole("textbox", { name: "搜索旁聊和聊天记录" }), { target: { value: "杨公堤" } });
    await act(async () => {
      vi.advanceTimersByTime(400);
      await Promise.resolve();
    });

    fireEvent.click(screen.getByRole("button", { name: /推荐去/ }));
    await act(async () => {
      await Promise.resolve();
    });

    expect(scrollIntoViewSpy).toHaveBeenCalledWith({ behavior: "smooth", block: "center" });
    expect(shellContainer.querySelector('[data-turn-id="turn-westlake"][data-highlighted="true"]')).not.toBeNull();
    act(() => {
      vi.advanceTimersByTime(1250);
    });
  });
});
