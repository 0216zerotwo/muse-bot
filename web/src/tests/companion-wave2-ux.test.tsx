import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { renderToStaticMarkup } from "react-dom/server";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ChatThread } from "@/companion/ChatThread";
import { CommandPalette, renderMatchedSnippet } from "@/companion/CommandPalette";
import { Composer } from "@/companion/Composer";
import { FollowUpChips, clearFollowupsCache } from "@/companion/FollowUpChips";
import { readsAfterVoice, setReadsAfterVoice, speakAloud } from "@/companion/MessageActions";
import { SourcesBar, isKnowledgeSource, isSafeHttpUrl, sourceDomainLabel } from "@/companion/SourcesBar";
import { exportThread, parseExportFilename } from "@/companion/exportThread";
import { MuseDrawer } from "@/companion/muse/Drawer";
import { SoulMemoryPane } from "@/companion/muse/Identity";
import { SideChatSheet } from "@/companion/muse/SideChatSheet";
import { useSpeechPlayer } from "@/companion/useSpeechPlayer";
import { parseWebuiThreadPayload } from "@/lib/api";
import type { NanobotClient } from "@/lib/nanobot-client";
import { createThreadProjectionState, projectThreadEvent, projectThreadEvents } from "@/lib/thread-event-projection";
import type { ChatSummary, UIMessage } from "@/lib/types";
import { ClientProvider } from "@/providers/ClientProvider";

const mockClient = {
  status: "open",
  requestMutation: vi.fn().mockResolvedValue({}),
  subscribe: vi.fn(() => () => undefined),
  onStatus: vi.fn(() => () => undefined),
  onThreadEvent: vi.fn(() => () => undefined),
  onFeedUpdate: vi.fn(() => () => undefined),
  onApprovalUpdate: vi.fn(() => () => undefined),
} as unknown as NanobotClient;

function jsonResponse(body: unknown, status = 200, headers: Record<string, string> = {}): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json", ...headers },
  });
}

function renderWithClient(ui: React.ReactNode, token = "tok-123") {
  return render(
    <ClientProvider client={mockClient} token={token}>
      {ui}
    </ClientProvider>,
  );
}

describe("Wave 2 UX upgrades", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    clearFollowupsCache();
  });

  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
    vi.useRealTimers();
    delete (window as unknown as { PersonalBotAndroid?: unknown }).PersonalBotAndroid;
  });

  describe("Citation source projection", () => {
    it("parses and projects sources on message and stream_end (even without text)", () => {
      const payload = parseWebuiThreadPayload({
        schemaVersion: 1,
        projection: "events",
        events: [
          {
            event: "delta",
            chat_id: "main-1",
            turn_id: "turn-1",
            text: "Answer body",
          },
          {
            event: "stream_end",
            chat_id: "main-1",
            turn_id: "turn-1",
            sources: [
              {
                id: 1,
                title: "Vitest Docs",
                url: "https://vitest.dev/guide/",
                domain: "vitest.dev",
                snippet: "Fast unit test framework.",
              },
            ],
          },
          {
            event: "turn_end",
            chat_id: "main-1",
            turn_id: "turn-1",
          },
          {
            event: "message",
            chat_id: "main-1",
            turn_id: "turn-2",
            text: "From vault",
            sources: [
              {
                id: 2,
                title: "Notes",
                url: "#knowledge:notes.md",
                domain: "知识库 · notes.md",
                snippet: "Local note",
              },
            ],
          },
        ],
      });
      expect(payload.events).toHaveLength(4);

      let state = createThreadProjectionState();
      state = projectThreadEvent(state, payload.events[0], { createId: () => "id-1", now: 1000 });
      state = projectThreadEvent(state, payload.events[1], { createId: () => "id-2", now: 1010 });
      state = projectThreadEvent(state, payload.events[2], { createId: () => "id-3", now: 1020 });
      const streamed = state.messages.find((m) => m.turnId === "turn-1");
      expect(streamed?.isStreaming).toBe(false);
      expect(streamed?.content).toBe("Answer body");
      expect(streamed?.sources?.[0]?.domain).toBe("vitest.dev");

      const replayed = projectThreadEvents(payload.events);
      expect(replayed.find((m) => m.turnId === "turn-1")?.sources?.[0]?.domain).toBe("vitest.dev");
      expect(replayed.find((m) => m.turnId === "turn-2")?.sources?.[0]?.url).toBe("#knowledge:notes.md");
    });
  });

  describe("CommandPalette", () => {
    const sideChats: ChatSummary[] = [
      { key: "websocket:side-1", channel: "websocket", chatId: "side-1", title: "周末旅行计划", createdAt: null, updatedAt: null, preview: "" },
    ];
    const helpers = [
      { id: "h-1", name: "代码助手", emoji: "💻", avatar: "", title: "修 bug", instructions: "", chat_id: "hc-1" },
    ];

    it("highlights precomputed backend matches and handles fallback query terms", () => {
      const html = renderToStaticMarkup(
        <span>{renderMatchedSnippet("杭州西湖周末去散步", [[0, 2], [4, 6]])}</span>,
      );
      expect(html).toContain("<b");
      expect(html).toContain(">杭州</b>");
      expect(html).toContain(">周末</b>");

      const fallbackHtml = renderToStaticMarkup(
        <span>{renderMatchedSnippet("Hello TypeScript World", undefined, "typescript")}</span>,
      );
      expect(fallbackHtml).toContain(">TypeScript</b>");
    });

    it("renders quick sections, runs scoped debounced search, highlights matches, and jumps on Enter", async () => {
      const fetchSpy = vi.spyOn(globalThis, "fetch").mockResolvedValue(
        jsonResponse({
          hits: [
            {
              chat_id: "side-1",
              chat_title: "周末旅行计划",
              role: "assistant",
              text: "建议去杭州西湖走走",
              snippet: "建议去杭州西湖走走",
              turn_id: "turn-42",
              at_ms: Date.now(),
              matches: [[3, 5]],
            },
          ],
        }),
      );

      const onClose = vi.fn();
      const onJumpToHit = vi.fn();
      const onOpenMemory = vi.fn();
      const onToggleTheme = vi.fn();
      const onExportChat = vi.fn();

      render(
        <CommandPalette
          token="tok"
          activeChatId="main-1"
          activeChatTitle="主要聊天"
          sideChats={sideChats}
          helpers={helpers}
          onClose={onClose}
          onSelectTab={vi.fn()}
          onSelectSideChat={vi.fn()}
          onNewSideChat={vi.fn()}
          onSelectHelper={vi.fn()}
          onOpenSettings={vi.fn()}
          onOpenMemory={onOpenMemory}
          onExportChat={onExportChat}
          onToggleTheme={onToggleTheme}
          onJumpToHit={onJumpToHit}
        />,
      );

      expect(screen.getByText("主要聊天")).toBeTruthy();
      expect(screen.getByText("周末旅行计划")).toBeTruthy();
      expect(screen.getByText("代码助手")).toBeTruthy();
      expect(screen.getByText("记忆管理")).toBeTruthy();

      fireEvent.click(screen.getByRole("option", { name: /记忆管理/ }));
      expect(onOpenMemory).toHaveBeenCalledTimes(1);

      fireEvent.click(screen.getByRole("option", { name: /导出当前对话/ }));
      expect(onExportChat).toHaveBeenCalledTimes(1);

      fireEvent.click(screen.getByRole("option", { name: /切换到深色主题/ }));
      expect(onToggleTheme).toHaveBeenCalledTimes(1);

      fireEvent.click(screen.getByRole("button", { name: "旁聊" }));
      const searchBox = screen.getByLabelText("搜索对话或输入指令");
      fireEvent.change(searchBox, { target: { value: "杭州" } });

      await waitFor(() => {
        expect(fetchSpy).toHaveBeenCalledTimes(1);
      });
      const calledUrl = String(fetchSpy.mock.calls[0]?.[0] ?? "");
      expect(calledUrl).toContain("q=%E6%9D%AD%E5%B7%9E");
      expect(calledUrl).toContain("scope=sides");

      const highlighted = await screen.findByText("杭州");
      expect(highlighted.tagName).toBe("B");

      fireEvent.keyDown(searchBox, { key: "Enter" });
      expect(onJumpToHit).toHaveBeenCalledWith(expect.objectContaining({ chat_id: "side-1", turn_id: "turn-42" }));
    });

    it("navigates items with ArrowUp/ArrowDown and closes on Escape", () => {
      const onClose = vi.fn();
      const onSelectTab = vi.fn();
      render(
        <CommandPalette
          token="tok"
          activeChatId="main-1"
          activeChatTitle="主对话"
          sideChats={[]}
          helpers={[]}
          onClose={onClose}
          onSelectTab={onSelectTab}
          onSelectSideChat={vi.fn()}
          onNewSideChat={vi.fn()}
          onSelectHelper={vi.fn()}
          onOpenSettings={vi.fn()}
          onOpenMemory={vi.fn()}
          onExportChat={vi.fn()}
          onJumpToHit={vi.fn()}
        />,
      );

      const dialog = screen.getByRole("dialog", { name: "全局指令面板" });
      fireEvent.keyDown(dialog, { key: "ArrowDown" });
      fireEvent.keyDown(dialog, { key: "Enter" });
      expect(onSelectTab).toHaveBeenCalledWith("feed");
      expect(onClose).toHaveBeenCalledTimes(1);

      fireEvent.keyDown(dialog, { key: "ArrowUp" });
      fireEvent.keyDown(document, { key: "Escape" });
      expect(onClose).toHaveBeenCalledTimes(2);
    });
  });

  describe("SourcesBar & FollowUpChips", () => {
    it("renders web + knowledge chips cleanly and opens details MuseSheet", () => {
      expect(isKnowledgeSource({ id: 1, title: "Note", url: "#knowledge:a.md" })).toBe(true);
      expect(isSafeHttpUrl("https://example.com")).toBe(true);
      expect(isSafeHttpUrl("javascript:alert(1)")).toBe(false);
      expect(sourceDomainLabel({ id: 1, title: "Docs", url: "https://www.react.dev/learn" })).toBe("react.dev");

      render(
        <SourcesBar
          sources={[
            { id: 1, title: "React Hooks", url: "https://react.dev/reference", domain: "react.dev", snippet: "Use hooks." },
            { id: 2, title: "项目规划", url: "#knowledge:plan.md", domain: "知识库", snippet: "Wave 2 milestone" },
          ]}
        />,
      );

      const extLink = screen.getByRole("link", { name: /react\.dev/ });
      expect(extLink.getAttribute("href")).toBe("https://react.dev/reference");
      expect(extLink.getAttribute("target")).toBe("_blank");

      fireEvent.click(screen.getByRole("button", { name: /\[2\] 知识库：项目规划/ }));
      expect(screen.getByText("参考来源（2）")).toBeTruthy();
      expect(screen.getByText("知识库条目")).toBeTruthy();
      expect(screen.getByText("网页来源")).toBeTruthy();
      expect(screen.getByText("Wave 2 milestone")).toBeTruthy();
    });

    it("fetches followups once per (chatId, turnId), caches them, and submits on click", async () => {
      const fetchSpy = vi.spyOn(globalThis, "fetch").mockResolvedValue(
        jsonResponse({
          chat_id: "main-1",
          turn_id: "turn-9",
          suggestions: ["帮我列个清单", "给我举个例子", "再精简一点"],
        }),
      );

      const onSelect = vi.fn();
      const { unmount } = renderWithClient(
        <FollowUpChips chatId="main-1" turnId="turn-9" enabled onSelect={onSelect} />,
      );

      const firstChip = await screen.findByRole("button", { name: "帮我列个清单" });
      expect(fetchSpy).toHaveBeenCalledTimes(1);
      fireEvent.click(firstChip);
      expect(onSelect).toHaveBeenCalledWith("帮我列个清单");

      unmount();
      renderWithClient(<FollowUpChips chatId="main-1" turnId="turn-9" enabled onSelect={onSelect} />);
      expect(await screen.findByRole("button", { name: "给我举个例子" })).toBeTruthy();
      expect(fetchSpy).toHaveBeenCalledTimes(1);
    });
  });

  describe("exportThread, MuseDrawer, SideChatSheet & useSpeechPlayer", () => {
    it("prefers Android native shareText on Android and downloads in browsers", async () => {
      expect(
        parseExportFilename("attachment; filename*=UTF-8''%E5%AF%B9%E8%AF%9D%201.md", "fallback", "c1"),
      ).toBe("对话 1.md");

      const shareText = vi.fn();
      const haptic = vi.fn();
      (window as unknown as { PersonalBotAndroid?: unknown }).PersonalBotAndroid = { shareText, haptic };
      vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(
        new Response("# Hello\nWorld", {
          status: 200,
          headers: { "Content-Disposition": 'attachment; filename="chat.md"' },
        }),
      );

      const androidRes = await exportThread("tok", "main-1", { title: "主对话" });
      expect(androidRes.method).toBe("android");
      expect(haptic).toHaveBeenCalledWith("light");
      expect(shareText).toHaveBeenCalledWith("# Hello\nWorld", "主对话");

      delete (window as unknown as { PersonalBotAndroid?: unknown }).PersonalBotAndroid;
      vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(new Response("# Browser MD", { status: 200 }));
      const origCreate = URL.createObjectURL;
      const origRevoke = URL.revokeObjectURL;
      URL.createObjectURL = vi.fn(() => "blob:mock-md");
      URL.revokeObjectURL = vi.fn();
      const clickSpy = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => undefined);
      try {
        const browserRes = await exportThread("tok", "main-1", { title: "导出测试" });
        expect(browserRes.method).toBe("download");
        expect(clickSpy).toHaveBeenCalledTimes(1);
      } finally {
        URL.createObjectURL = origCreate;
        URL.revokeObjectURL = origRevoke;
      }
    });

    it("exposes export entry points in MuseDrawer and SideChatSheet", async () => {
      const onExportCurrentChat = vi.fn();
      const onCloseDrawer = vi.fn();
      render(
        <MuseDrawer
          open
          onClose={onCloseDrawer}
          name="小缪"
          current="chat"
          activeChatId={null}
          order={["chat", "feed", "ideas", "goals", "helpers", "creations"]}
          onReorder={vi.fn()}
          onTab={vi.fn()}
          sideChats={[]}
          onOpenChat={vi.fn()}
          onNewChat={vi.fn()}
          onOpenSettings={vi.fn()}
          onEditChat={vi.fn()}
          onDeleteChat={vi.fn()}
          onExportCurrentChat={onExportCurrentChat}
        />,
      );
      fireEvent.click(screen.getByRole("button", { name: "导出当前对话" }));
      expect(onExportCurrentChat).toHaveBeenCalledTimes(1);
      expect(onCloseDrawer).toHaveBeenCalledTimes(1);

      const chat: ChatSummary = {
        key: "websocket:side-9",
        channel: "websocket",
        chatId: "side-9",
        title: "设计评审",
        createdAt: null,
        updatedAt: null,
        preview: "",
      };
      const onExportSide = vi.fn().mockResolvedValue(undefined);
      render(
        <SideChatSheet
          chat={chat}
          onClose={vi.fn()}
          onRename={vi.fn()}
          onDelete={vi.fn()}
          onExport={onExportSide}
        />,
      );
      fireEvent.click(screen.getByRole("button", { name: "导出对话 (.md)" }));
      await waitFor(() => {
        expect(onExportSide).toHaveBeenCalledWith(chat);
      });
    });

    it("uses neural TTS when ttsEnabled=true and smoothly falls back on error", async () => {
      const fetchSpy = vi
        .spyOn(globalThis, "fetch")
        .mockResolvedValueOnce(jsonResponse({ ok: true, url: "/api/tts/abc.mp3" }))
        .mockResolvedValueOnce(jsonResponse({ ok: false, error: "disabled" }, 503));

      const playMock = vi.fn().mockResolvedValue(undefined);
      const pauseMock = vi.fn();
      const OrigAudio = globalThis.Audio;
      globalThis.Audio = vi.fn(function FakeAudio() {
        return { play: playMock, pause: pauseMock, src: "", onended: null, onerror: null };
      }) as unknown as typeof Audio;

      const androidSpeak = vi.fn();
      const androidStop = vi.fn();
      (window as unknown as { PersonalBotAndroid?: unknown }).PersonalBotAndroid = {
        speak: androidSpeak,
        stopSpeaking: androidStop,
      };

      function Harness() {
        const player = useSpeechPlayer({ token: "tok", ttsEnabled: true });
        return (
          <div>
            <span data-testid="speech-status">{player.status}</span>
            <button type="button" onClick={() => void player.toggle("你好世界", "msg-1")}>
              play1
            </button>
            <button type="button" onClick={() => void player.play("回退语音", "msg-2")}>
              play2
            </button>
            <button type="button" onClick={player.stop}>
              stop
            </button>
          </div>
        );
      }

      try {
        render(<Harness />);
        fireEvent.click(screen.getByRole("button", { name: "play1" }));
        await waitFor(() => {
          expect(screen.getByTestId("speech-status").textContent).toBe("playing");
        });
        expect(fetchSpy).toHaveBeenCalledTimes(1);
        expect(playMock).toHaveBeenCalledTimes(1);

        fireEvent.click(screen.getByRole("button", { name: "play1" }));
        expect(screen.getByTestId("speech-status").textContent).toBe("idle");
        expect(pauseMock).toHaveBeenCalled();

        fireEvent.click(screen.getByRole("button", { name: "play2" }));
        await waitFor(() => {
          expect(androidSpeak).toHaveBeenCalledWith("回退语音", "zh-CN");
        });
      } finally {
        globalThis.Audio = OrigAudio;
      }
    });
  });

  describe("ChatThread interactions (edit, fork, selection quote, sources)", () => {
    it("supports inline edit & resubmit, fork side chat, and selection quote pill", async () => {
      const userMsg: UIMessage = {
        id: "u1",
        role: "user",
        content: "> [!QUOTE]\n> 先前讨论\n\n第一版问题",
        turnId: "turn-1",
        createdAt: 1000,
      };
      const replyMsg: UIMessage = {
        id: "a1",
        role: "assistant",
        content: "这是一段详细的分析结果。",
        turnId: "turn-1",
        createdAt: 2000,
        sources: [{ id: 1, title: "Docs", url: "https://docs.example.com", domain: "docs.example.com" }],
      };

      const onEditMessage = vi.fn().mockResolvedValue(undefined);
      const onForkMessage = vi.fn().mockResolvedValue(undefined);
      const onQuoteSelection = vi.fn();
      const haptic = vi.fn();
      (window as unknown as { PersonalBotAndroid?: unknown }).PersonalBotAndroid = { haptic };

      render(
        <ChatThread
          items={[
            { type: "user", id: "u1", message: userMsg },
            { type: "reply", id: "a1", message: replyMsg, blocks: [{ type: "bubble", text: replyMsg.content }], media: [] },
          ]}
          typing={false}
          routes={new Map()}
          onDecide={vi.fn()}
          onMessageAction={vi.fn()}
          onEditMessage={onEditMessage}
          onForkMessage={onForkMessage}
          onQuoteSelection={onQuoteSelection}
        />,
      );

      expect(screen.getByRole("link", { name: /docs\.example\.com/ })).toBeTruthy();

      const editButtons = screen.getAllByRole("button", { name: "编辑并重发" });
      fireEvent.click(editButtons[0]);
      const editor = screen.getByLabelText("编辑消息内容");
      fireEvent.change(editor, { target: { value: "修改后的问题" } });
      fireEvent.click(screen.getByRole("button", { name: "确认并重发" }));

      await waitFor(() => {
        expect(onEditMessage).toHaveBeenCalledWith(userMsg, "> [!QUOTE]\n> 先前讨论\n\n修改后的问题");
      });

      const forkButtons = screen.getAllByRole("button", { name: "从这里分叉旁聊" });
      fireEvent.click(forkButtons[forkButtons.length - 1]);
      expect(onForkMessage).toHaveBeenCalledWith(replyMsg);
      expect(haptic).toHaveBeenCalledWith("light");

      const replyBody = document.querySelector('[data-reply-body="true"]');
      expect(replyBody).toBeTruthy();
      const range = document.createRange();
      range.selectNodeContents(replyBody!);
      vi.spyOn(window, "getSelection").mockReturnValue({
        isCollapsed: false,
        rangeCount: 1,
        toString: () => "详细的分析结果",
        getRangeAt: () => range,
        removeAllRanges: vi.fn(),
      } as unknown as Selection);
      await act(async () => {
        document.dispatchEvent(new Event("selectionchange"));
      });

      const quoteBtn = await screen.findByRole("button", { name: "引用追问" });
      fireEvent.click(quoteBtn);
      expect(onQuoteSelection).toHaveBeenCalledWith("详细的分析结果");
    });
  });

  describe("Composer paste, drop & menu keyboard navigation", () => {
    it("supports slash & helper menu ArrowDown/Enter/Escape, paste files, drop overlay, and send haptic", async () => {
      vi.spyOn(globalThis, "fetch").mockImplementation((input) => {
        const url = String(input);
        if (url.includes("/helpers")) {
          return Promise.resolve(
            jsonResponse({
              helpers: [
                { id: "h1", name: "代码手", emoji: "💻", avatar: "", title: "写代码", instructions: "", chat_id: "hc1" },
                { id: "h2", name: "研究手", emoji: "🔎", avatar: "", title: "查资料", instructions: "", chat_id: "hc2" },
              ],
              groups: [],
              templates: [],
            }),
          );
        }
        return Promise.resolve(jsonResponse({ built_in: [], learned: [] }));
      });
      const onChange = vi.fn();
      const onSend = vi.fn();
      const haptic = vi.fn();
      (window as unknown as { PersonalBotAndroid?: unknown }).PersonalBotAndroid = { haptic };

      const { rerender } = renderWithClient(
        <Composer
          value="/"
          onChange={onChange}
          onSend={onSend}
          onStop={vi.fn()}
          streaming={false}
          connected
          transcribeAudio={vi.fn()}
        />,
      );

      const input = screen.getByLabelText("发消息");
      fireEvent.keyDown(input, { key: "ArrowDown" });
      fireEvent.keyDown(input, { key: "Enter" });
      expect(onChange).toHaveBeenCalledWith("/compact");

      fireEvent.keyDown(input, { key: "Escape" });
      expect(screen.queryByRole("list", { name: "快捷指令" })).toBeNull();

      rerender(
        <ClientProvider client={mockClient} token="tok-123">
          <Composer
            value="@"
            onChange={onChange}
            onSend={onSend}
            onStop={vi.fn()}
            streaming={false}
            connected
            transcribeAudio={vi.fn()}
          />
        </ClientProvider>,
      );
      expect(await screen.findByText("研究手")).toBeTruthy();
      fireEvent.keyDown(input, { key: "ArrowDown" });
      fireEvent.keyDown(input, { key: "Enter" });
      expect(onChange).toHaveBeenCalledWith("@研究手 ");

      const file = new File(["hello"], "note.txt", { type: "text/plain" });
      fireEvent.paste(input, {
        clipboardData: { files: [file], items: [] },
      });
      expect(await screen.findByText("note.txt")).toBeTruthy();

      const wrapper = input.closest("div.flex.flex-col.gap-1\\.5")!;
      fireEvent.dragEnter(wrapper, { dataTransfer: { types: ["Files"], files: [file] } });
      expect(screen.getByTestId("composer-drop-overlay")).toBeTruthy();
      const file2 = new File(["world"], "second.txt", { type: "text/plain" });
      fireEvent.drop(wrapper, { dataTransfer: { types: ["Files"], files: [file2] } });
      expect(await screen.findByText("second.txt")).toBeTruthy();

      rerender(
        <ClientProvider client={mockClient} token="tok-123">
          <Composer
            value="发送内容"
            onChange={onChange}
            onSend={onSend}
            onStop={vi.fn()}
            streaming={false}
            connected
            transcribeAudio={vi.fn()}
          />
        </ClientProvider>,
      );
      fireEvent.click(screen.getByRole("button", { name: "发送" }));
      expect(onSend).toHaveBeenCalled();
      expect(haptic).toHaveBeenCalledWith("light");
    });
  });

  describe("Identity memory pinning", () => {
    it("displays pinned items first with badge and toggles pin state via companion.memory.pin", async () => {
      vi.spyOn(globalThis, "fetch").mockImplementation((input) => {
        const url = String(input);
        if (url.includes("/memory-doc")) {
          return Promise.resolve(jsonResponse({ text: "# Memory", mtime: 1 }));
        }
        return Promise.resolve(
          jsonResponse({
            sections: [
              {
                file: "MEMORY.md",
                title: "我记下的",
                items: [
                  { id: "m1", file: "MEMORY.md", line: "- 普通记忆", text: "普通记忆", pinned: false },
                  { id: "m2", file: "MEMORY.md", line: "- 重要偏好", text: "重要偏好", pinned: true },
                ],
              },
            ],
            changes: [],
            learned_at: null,
          }),
        );
      });

      const reqMutation = vi.fn().mockResolvedValue({
        sections: [
          {
            file: "MEMORY.md",
            title: "我记下的",
            items: [
              { id: "m1", file: "MEMORY.md", line: "- 普通记忆", text: "普通记忆", pinned: true },
              { id: "m2", file: "MEMORY.md", line: "- 重要偏好", text: "重要偏好", pinned: true },
            ],
          },
        ],
        changes: [],
        learned_at: null,
        pinned: true,
      });
      const pinClient = { ...mockClient, requestMutation: reqMutation } as unknown as NanobotClient;
      const haptic = vi.fn();
      (window as unknown as { PersonalBotAndroid?: unknown }).PersonalBotAndroid = { haptic };

      render(
        <ClientProvider client={pinClient} token="tok">
          <SoulMemoryPane />
        </ClientProvider>,
      );

      fireEvent.click(await screen.findByText("MEMORY.md"));

      const items = await screen.findAllByRole("listitem");
      expect(items[0].textContent).toContain("重要偏好");
      expect(items[0].textContent).toContain("📌");

      const pinBtns = screen.getAllByRole("button", { name: "置顶" });
      fireEvent.click(pinBtns[0]);

      await waitFor(() => {
        expect(reqMutation).toHaveBeenCalledWith("companion.memory.pin", {
          file: "MEMORY.md",
          line: "- 普通记忆",
          pinned: true,
        });
      });
      expect(haptic).toHaveBeenCalledWith("light");

      // Test unpinning and error handling branch
      const unpinBtns = screen.getAllByRole("button", { name: "取消置顶" });
      reqMutation.mockRejectedValueOnce(new Error("pin failed"));
      fireEvent.click(unpinBtns[0]);
      expect(await screen.findByText("pin failed")).toBeTruthy();
    });
  });

  describe("Additional Wave 2 branch & fallback coverage", () => {
    it("covers CommandPalette empty/error search, clear button, and remaining action callbacks", async () => {
      const fetchSpy = vi.spyOn(globalThis, "fetch").mockRejectedValueOnce(new Error("network down"));
      const onSelectTab = vi.fn();
      const onSelectSideChat = vi.fn();
      const onNewSideChat = vi.fn();
      const onSelectHelper = vi.fn();
      const onOpenSettings = vi.fn();
      const onClose = vi.fn();

      const sideChats: ChatSummary[] = [
        { key: "websocket:s1", channel: "websocket", chatId: "s1", title: "旁聊甲", createdAt: null, updatedAt: null, preview: "最近内容" },
      ];
      const helpers = [
        { id: "h1", name: "译者", emoji: "", avatar: "", title: "翻译", instructions: "", chat_id: "hc1" },
      ];

      render(
        <CommandPalette
          token="tok"
          activeChatId="main-1"
          activeChatTitle="主聊"
          sideChats={sideChats}
          helpers={helpers}
          onClose={onClose}
          onSelectTab={onSelectTab}
          onSelectSideChat={onSelectSideChat}
          onNewSideChat={onNewSideChat}
          onSelectHelper={onSelectHelper}
          onOpenSettings={onOpenSettings}
          onOpenMemory={vi.fn()}
          onExportChat={vi.fn()}
          onJumpToHit={vi.fn()}
        />,
      );

      fireEvent.click(screen.getByRole("option", { name: /旁聊甲/ }));
      expect(onSelectSideChat).toHaveBeenCalledWith("s1");

      fireEvent.click(screen.getByRole("option", { name: /译者/ }));
      expect(onSelectHelper).toHaveBeenCalledWith("h1");

      fireEvent.click(screen.getByRole("option", { name: /模型设置/ }));
      expect(onOpenSettings).toHaveBeenCalledWith("models");

      fireEvent.click(screen.getByRole("option", { name: /技能管理/ }));
      fireEvent.click(screen.getByRole("option", { name: /连接设置/ }));
      fireEvent.click(screen.getByRole("option", { name: /权限设置/ }));
      expect(onOpenSettings).toHaveBeenCalledWith("permissions");

      fireEvent.click(screen.getByRole("option", { name: /新建旁聊/ }));
      expect(onNewSideChat).toHaveBeenCalledTimes(1);

      const box = screen.getByLabelText("搜索对话或输入指令");
      fireEvent.change(box, { target: { value: "不存在词" } });
      await waitFor(() => expect(fetchSpy).toHaveBeenCalled());
      expect(await screen.findByText("搜索暂时不可用")).toBeTruthy();

      fireEvent.click(screen.getByRole("button", { name: "清空搜索" }));
      expect((box as HTMLInputElement).value).toBe("");
    });

    it("covers useSpeechPlayer audio ended, error fallback to Web Speech API, and explicit stop", async () => {
      type AudioStub = {
        play: ReturnType<typeof vi.fn>;
        pause: ReturnType<typeof vi.fn>;
        src: string;
        onended: (() => void) | null;
        onerror: (() => void) | null;
      };
      let lastAudio: AudioStub | null = null;
      const OrigAudio = globalThis.Audio;
      const OrigUtterance = globalThis.SpeechSynthesisUtterance;
      const origSynth = window.speechSynthesis;

      globalThis.Audio = vi.fn(function FakeAudio() {
        lastAudio = {
          play: vi.fn().mockResolvedValue(undefined),
          pause: vi.fn(),
          src: "",
          onended: null,
          onerror: null,
        };
        return lastAudio;
      }) as unknown as typeof Audio;

      const speakMock = vi.fn();
      const cancelMock = vi.fn();
      Object.defineProperty(window, "speechSynthesis", {
        configurable: true,
        value: { speak: speakMock, cancel: cancelMock },
      });
      globalThis.SpeechSynthesisUtterance = vi.fn(function FakeUtterance(this: { text: string; lang: string }, text: string) {
        this.text = text;
        this.lang = "";
      }) as unknown as typeof SpeechSynthesisUtterance;

      vi.spyOn(globalThis, "fetch")
        .mockResolvedValueOnce(jsonResponse({ ok: true, url: "/a1.mp3" }))
        .mockResolvedValueOnce(jsonResponse({ ok: true, url: "/a2.mp3" }));

      function Harness({ ttsEnabled }: { ttsEnabled: boolean }) {
        const player = useSpeechPlayer({ token: "tok", ttsEnabled });
        return (
          <div>
            <span data-testid="status">{player.status}</span>
            <span data-testid="fb">{String(player.isFallback)}</span>
            <button type="button" onClick={() => void player.play("第一句", "m1")}>
              p1
            </button>
            <button type="button" onClick={() => void player.play("Second English sentence", "m2")}>
              p2
            </button>
            <button type="button" onClick={player.stop}>
              stop
            </button>
          </div>
        );
      }

      try {
        render(<Harness ttsEnabled />);
        fireEvent.click(screen.getByRole("button", { name: "p1" }));
        await waitFor(() => expect(screen.getByTestId("status").textContent).toBe("playing"));
        await act(async () => {
          lastAudio?.onended?.();
        });
        expect(screen.getByTestId("status").textContent).toBe("idle");

        fireEvent.click(screen.getByRole("button", { name: "p2" }));
        await waitFor(() => expect(lastAudio).toBeTruthy());
        lastAudio!.play.mockRejectedValueOnce(new Error("decode error"));
        await waitFor(() => expect(screen.getByTestId("status").textContent).toBe("playing"));
        expect(screen.getByTestId("fb").textContent).toBe("false");

        fireEvent.click(screen.getByRole("button", { name: "stop" }));
        expect(screen.getByTestId("status").textContent).toBe("idle");

        vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(jsonResponse({ ok: true, url: "/a3.mp3" }));
        let nextReject!: (err: Error) => void;
        (globalThis.Audio as unknown as ReturnType<typeof vi.fn>).mockImplementationOnce(function FailAudio() {
          return {
            play: () =>
              new Promise<void>((_res, rej) => {
                nextReject = rej;
              }),
            pause: vi.fn(),
            src: "",
            onended: null,
            onerror: null,
          };
        });
        fireEvent.click(screen.getByRole("button", { name: "p2" }));
        await waitFor(() => expect(nextReject).toBeTruthy());
        await act(async () => {
          nextReject(new Error("decode fail"));
        });
        await waitFor(() => expect(speakMock).toHaveBeenCalled());
        expect(screen.getByTestId("fb").textContent).toBe("true");

        fireEvent.click(screen.getByRole("button", { name: "stop" }));
        expect(cancelMock).toHaveBeenCalled();
        expect(screen.getByTestId("status").textContent).toBe("idle");
      } finally {
        globalThis.Audio = OrigAudio;
        globalThis.SpeechSynthesisUtterance = OrigUtterance;
        Object.defineProperty(window, "speechSynthesis", { configurable: true, value: origSynth });
      }
    });

    it("covers exportThread clipboard mode, fallback copy, and error response handling", async () => {
      const writeText = vi.fn().mockResolvedValue(undefined);
      Object.defineProperty(navigator, "clipboard", {
        configurable: true,
        value: { writeText },
      });

      vi.spyOn(globalThis, "fetch")
        .mockResolvedValueOnce(new Response("# Clipboard MD", { status: 200 }))
        .mockResolvedValueOnce(new Response(JSON.stringify({ error: "找不到会话" }), { status: 404 }));

      const copied = await exportThread("tok", "websocket:main-1", { mode: "copy", title: "导出" });
      expect(copied.method).toBe("clipboard");
      expect(writeText).toHaveBeenCalledWith("# Clipboard MD");

      await expect(exportThread("tok", "missing")).rejects.toThrow("找不到会话");
    });

    it("covers ChatThread inline edit cancel, error alert, Meta+Enter, and MessageActions toolbar buttons", async () => {
      const userMsg: UIMessage = {
        id: "u2",
        role: "user",
        content: "原始内容",
        turnId: "turn-2",
        createdAt: 1000,
      };
      const replyMsg: UIMessage = {
        id: "a2",
        role: "assistant",
        content: "回复内容",
        turnId: "turn-2",
        createdAt: 2000,
      };
      const onEditMessage = vi.fn().mockRejectedValueOnce(new Error("会话正忙")).mockResolvedValueOnce(undefined);
      const onMessageAction = vi.fn();

      render(
        <ChatThread
          items={[
            { type: "user", id: "u2", message: userMsg },
            { type: "reply", id: "a2", message: replyMsg, blocks: [{ type: "bubble", text: "回复内容" }], media: [] },
          ]}
          typing={false}
          routes={new Map()}
          onDecide={vi.fn()}
          onMessageAction={onMessageAction}
          onEditMessage={onEditMessage}
          onForkMessage={vi.fn()}
          speakingMessageId="a2"
          speechStatus="playing"
        />,
      );

      // Toolbar speech active state button + quote/copy/regenerate/delete
      fireEvent.click(screen.getByRole("button", { name: "停止朗读" }));
      expect(onMessageAction).toHaveBeenCalledWith("speak", replyMsg, "reply");
      fireEvent.click(screen.getAllByRole("button", { name: "复制" })[0]);
      expect(onMessageAction).toHaveBeenCalledWith("copy", userMsg, "user");
      fireEvent.click(screen.getAllByRole("button", { name: "引用" })[0]);
      fireEvent.click(screen.getByRole("button", { name: "重新生成" }));
      fireEvent.click(screen.getAllByRole("button", { name: "删除" })[0]);

      // Inline edit error + cancel + Meta+Enter
      fireEvent.click(screen.getByRole("button", { name: "编辑并重发" }));
      const editor = screen.getByLabelText("编辑消息内容");
      fireEvent.keyDown(editor, { key: "Enter", metaKey: true });
      expect(await screen.findByText("会话正忙")).toBeTruthy();

      fireEvent.keyDown(editor, { key: "Escape" });
      expect(screen.queryByLabelText("编辑消息内容")).toBeNull();
    });

    it("covers Composer items-paste fallback, @ menu ArrowUp/Escape, dragOver/dragLeave, and SideChatSheet rename/delete", async () => {
      const onChange = vi.fn();
      vi.spyOn(globalThis, "fetch").mockResolvedValue(
        jsonResponse({
          helpers: [
            { id: "h1", name: "甲", emoji: "🤖", avatar: "", title: "", instructions: "", chat_id: "hc1" },
            { id: "h2", name: "乙", emoji: "🦊", avatar: "", title: "", instructions: "", chat_id: "hc2" },
          ],
          groups: [],
          templates: [],
        }),
      );

      renderWithClient(
        <Composer
          value="@"
          onChange={onChange}
          onSend={vi.fn()}
          onStop={vi.fn()}
          streaming={false}
          connected
          transcribeAudio={vi.fn()}
        />,
      );

      const input = screen.getByLabelText("发消息");
      expect(await screen.findByText("乙")).toBeTruthy();
      fireEvent.keyDown(input, { key: "ArrowUp" });
      fireEvent.keyDown(input, { key: "Escape" });
      expect(screen.queryByRole("list", { name: "叫哪个帮手" })).toBeNull();

      const pasted = new File(["img"], "shot.png", { type: "text/plain" });
      fireEvent.paste(input, {
        clipboardData: {
          files: [],
          items: [{ kind: "file", getAsFile: () => pasted }],
        },
      });
      expect(await screen.findByText("shot.png")).toBeTruthy();

      const wrapper = input.closest("div.flex.flex-col.gap-1\\.5")!;
      fireEvent.dragEnter(wrapper, { dataTransfer: { types: ["Files"], files: [pasted] } });
      fireEvent.dragOver(wrapper, { dataTransfer: { types: ["Files"], files: [pasted] } });
      fireEvent.dragLeave(wrapper);
      expect(screen.queryByTestId("composer-drop-overlay")).toBeNull();

      // SideChatSheet rename and 2-step delete
      const chat: ChatSummary = {
        key: "websocket:s1",
        channel: "websocket",
        chatId: "s1",
        title: "旧标题",
        createdAt: null,
        updatedAt: null,
        preview: "",
      };
      const onRename = vi.fn();
      const onDelete = vi.fn();
      const onClose = vi.fn();
      render(
        <SideChatSheet chat={chat} onClose={onClose} onRename={onRename} onDelete={onDelete} />,
      );
      fireEvent.change(screen.getByLabelText("旁聊的名字"), { target: { value: "新标题" } });
      fireEvent.click(screen.getByRole("button", { name: "改名" }));
      expect(onRename).toHaveBeenCalledWith("s1", "新标题");

      fireEvent.click(screen.getByRole("button", { name: "删除这个旁聊" }));
      fireEvent.click(screen.getByRole("button", { name: "删除" }));
      expect(onDelete).toHaveBeenCalledWith("s1");
    });

    it("covers speech/export/theme/MessageActions remaining fallback branches", async () => {
      // 1. MessageActions speakAloud + readsAfterVoice persistence
      setReadsAfterVoice(false);
      expect(readsAfterVoice()).toBe(false);
      setReadsAfterVoice(true);
      expect(readsAfterVoice()).toBe(true);

      const androidSpeak = vi.fn();
      const androidStop = vi.fn();
      (window as unknown as { PersonalBotAndroid?: unknown }).PersonalBotAndroid = {
        speak: androidSpeak,
        stopSpeaking: androidStop,
        speaking: () => true,
      };
      speakAloud("停下来");
      expect(androidStop).toHaveBeenCalledTimes(1);
      (window as unknown as { PersonalBotAndroid?: unknown }).PersonalBotAndroid = { speak: androidSpeak };
      speakAloud("你好安卓");
      expect(androidSpeak).toHaveBeenCalledWith("你好安卓", "zh-CN");
      delete (window as unknown as { PersonalBotAndroid?: unknown }).PersonalBotAndroid;

      // 1b. MuseDrawer with precomputed backend matches
      vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(jsonResponse({ hits: [] }));
      const drawerSearch = vi.fn().mockResolvedValue([
        {
          chat_id: "c-match",
          chat_title: "项目讨论",
          role: "user",
          text: "测试多区间命中",
          snippet: "测试多区间命中",
          turn_id: "t-9",
          at_ms: Date.now(),
          matches: [[0, 2], [1, 4], [5, 7]],
        },
      ]);
      const onOpenHit = vi.fn();
      render(
        <MuseDrawer
          open
          onClose={vi.fn()}
          name="小缪"
          current="chat"
          activeChatId={null}
          order={["chat", "feed", "ideas", "goals", "helpers", "creations"]}
          onReorder={vi.fn()}
          onTab={vi.fn()}
          sideChats={[]}
          onOpenChat={vi.fn()}
          onNewChat={vi.fn()}
          onOpenSettings={vi.fn()}
          onEditChat={vi.fn()}
          onDeleteChat={vi.fn()}
          search={drawerSearch}
          onOpenHit={onOpenHit}
        />,
      );
      const drawerSearchInput = screen.getByLabelText("搜索旁聊和聊天记录");
      fireEvent.change(drawerSearchInput, { target: { value: "测试 命中" } });
      await waitFor(() => expect(drawerSearch).toHaveBeenCalled());
      const boldHits = await screen.findAllByText((_, el) => el?.tagName === "B");
      expect(boldHits.length).toBeGreaterThanOrEqual(2);
      fireEvent.click(screen.getByText("项目讨论"));
      expect(onOpenHit).toHaveBeenCalled();

      // Hit empty-token & no-match snippet branches in MuseDrawer
      drawerSearch.mockResolvedValueOnce([
        {
          chat_id: "c-long",
          chat_title: "长文本",
          role: "assistant",
          text: "这是一段非常非常长而且不包含查询关键字的连续中文文本内容用于测试省略截断逻辑是否正常触发。".repeat(3),
          turn_id: null,
          at_ms: Date.now(),
        },
      ]);
      fireEvent.change(drawerSearchInput, { target: { value: "不存在关键字" } });
      expect(await screen.findByText("长文本")).toBeTruthy();
      cleanup();

      // 2. exportThread fallback to clipboard when URL.createObjectURL is unavailable + plain filename + non-JSON error
      expect(parseExportFilename("attachment; filename*=UTF-8''%E0%A4%A; filename=\"alt.md\"", undefined, "c2")).toBe("alt.md");
      expect(parseExportFilename('attachment; filename="plain.md"', undefined, "c2")).toBe("plain.md");
      expect(parseExportFilename(null, "bad:/title*?", "chat-12345678")).toBe("bad title.md");

      const writeText = vi.fn().mockResolvedValue(undefined);
      Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText } });
      const origCreate = URL.createObjectURL;
      const origRevoke = URL.revokeObjectURL;
      URL.createObjectURL = vi.fn(() => "blob:throw-click");
      URL.revokeObjectURL = vi.fn();
      const throwingClick = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {
        throw new Error("blocked click");
      });
      vi.spyOn(globalThis, "fetch")
        .mockResolvedValueOnce(new Response("# Fallback copy", { status: 200 }))
        .mockResolvedValueOnce(new Response("Plain server error", { status: 500 }));
      try {
        const res = await exportThread("tok", "c1", { title: "主题" });
        expect(res.method).toBe("clipboard");
        expect(writeText).toHaveBeenCalledWith("# Fallback copy");
        await expect(exportThread("tok", "c1")).rejects.toThrow("导出失败（HTTP 500）");
      } finally {
        throwingClick.mockRestore();
        URL.createObjectURL = origCreate;
        URL.revokeObjectURL = origRevoke;
      }

      // 3. CommandPalette backdrop close, overlapping matches, empty search result, user/helper roles, and canExport=false
      const onClose = vi.fn();
      const paletteFetch = vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(jsonResponse({ hits: [] }));
      render(
        <CommandPalette
          token="tok"
          activeChatId="main-1"
          activeChatTitle="主聊"
          sideChats={[]}
          helpers={[]}
          canExport={false}
          onClose={onClose}
          onSelectTab={vi.fn()}
          onSelectSideChat={vi.fn()}
          onNewSideChat={vi.fn()}
          onSelectHelper={vi.fn()}
          onOpenSettings={vi.fn()}
          onOpenMemory={vi.fn()}
          onExportChat={vi.fn()}
          onJumpToHit={vi.fn()}
        />,
      );
      expect(screen.queryByRole("option", { name: /导出当前对话/ })).toBeNull();
      const overlayHtml = renderToStaticMarkup(
        <span>{renderMatchedSnippet("重叠区间测试", [[0, 2], [1, 3]])}</span>,
      );
      expect(overlayHtml).toContain(">重叠区</b>");

      fireEvent.click(screen.getByRole("option", { name: /切换到深色主题/ }));
      expect(document.documentElement.classList.contains("dark")).toBe(true);
      expect(onClose).toHaveBeenCalled();

      fireEvent.change(screen.getByLabelText("搜索对话或输入指令"), { target: { value: "完全不存在的内容" } });
      await waitFor(() => expect(paletteFetch).toHaveBeenCalled());
      expect(await screen.findByText("没有匹配的对话或指令")).toBeTruthy();

      const backdrop = screen.getByRole("dialog", { name: "全局指令面板" }).parentElement!;
      fireEvent.click(backdrop);
      expect(onClose).toHaveBeenCalledTimes(2);
      cleanup();

      // 3b. ChatThread speechLoading badge + collapsed selection + standalone onEditMessage without onMessageAction
      const standaloneMsg: UIMessage = {
        id: "u-alone",
        role: "user",
        content: "独立消息",
        turnId: "t-alone",
        createdAt: 1000,
      };
      const standaloneReply: UIMessage = {
        id: "a-alone",
        role: "assistant",
        content: "加载语音中",
        turnId: "t-alone",
        createdAt: 2000,
      };
      render(
        <ChatThread
          items={[
            { type: "user", id: "u-alone", message: standaloneMsg },
            { type: "reply", id: "a-alone", message: standaloneReply, blocks: [{ type: "bubble", text: "加载语音中" }], media: [] },
          ]}
          typing={false}
          routes={new Map()}
          onDecide={vi.fn()}
          onEditMessage={vi.fn()}
          onForkMessage={vi.fn()}
          onQuoteSelection={vi.fn()}
          speakingMessageId="a-alone"
          speechStatus="loading"
        />,
      );
      expect(screen.getByRole("button", { name: "合成中…" })).toBeTruthy();
      vi.spyOn(window, "getSelection").mockReturnValue({ isCollapsed: true, rangeCount: 0 } as unknown as Selection);
      await act(async () => {
        document.dispatchEvent(new Event("selectionchange"));
      });
      expect(screen.queryByRole("button", { name: "引用追问" })).toBeNull();
      cleanup();

      // 3c. useSpeechPlayer audio.onerror before play settles
      const OrigAudio2 = globalThis.Audio;
      const origUtt2 = globalThis.SpeechSynthesisUtterance;
      const origSynth2 = window.speechSynthesis;
      const errAudio: { onerror: (() => void) | null } = { onerror: null };
      globalThis.Audio = vi.fn(function PendingAudio() {
        return {
          play: () => new Promise<void>(() => undefined),
          pause: vi.fn(),
          src: "",
          onended: null,
          set onerror(fn: (() => void) | null) {
            errAudio.onerror = fn;
          },
          get onerror() {
            return errAudio.onerror;
          },
        };
      }) as unknown as typeof Audio;
      const fbSpeak = vi.fn();
      globalThis.SpeechSynthesisUtterance = vi.fn(function Utt() {
        return { onend: null, onerror: null };
      }) as unknown as typeof SpeechSynthesisUtterance;
      Object.defineProperty(window, "speechSynthesis", {
        configurable: true,
        value: { speak: fbSpeak, cancel: vi.fn(), speaking: false },
      });
      vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(jsonResponse({ ok: true, url: "/bad-stream.mp3" }));
      function PendingAudioHarness() {
        const player = useSpeechPlayer({ token: "tok", ttsEnabled: true });
        return (
          <button type="button" onClick={() => void player.play("触发onerror", "m-err")}>
            trigger-err
          </button>
        );
      }
      try {
        render(<PendingAudioHarness />);
        fireEvent.click(screen.getByRole("button", { name: "trigger-err" }));
        await waitFor(() => expect(errAudio.onerror).toBeTruthy());
        act(() => {
          errAudio.onerror?.();
        });
        expect(fbSpeak).toHaveBeenCalled();

        // No speechSynthesis and no Android TTS engine stops cleanly
        Object.defineProperty(window, "speechSynthesis", { configurable: true, value: undefined });
        vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(jsonResponse({ ok: false }));
        fireEvent.click(screen.getByRole("button", { name: "trigger-err" }));
        await waitFor(() => expect(screen.getByRole("button", { name: "trigger-err" })).toBeTruthy());
      } finally {
        globalThis.Audio = OrigAudio2;
        globalThis.SpeechSynthesisUtterance = origUtt2;
        Object.defineProperty(window, "speechSynthesis", { configurable: true, value: origSynth2 });
        cleanup();
      }

      // 3d. exportThread fallback when URL.createObjectURL is absent + Composer file > 20MB & + button
      const savedCreateObj = URL.createObjectURL;
      Object.defineProperty(URL, "createObjectURL", { configurable: true, value: undefined });
      vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(new Response("# No URL helper", { status: 200 }));
      try {
        const noUrlRes = await exportThread("tok", "c1", { title: "无URL" });
        expect(noUrlRes.method).toBe("clipboard");
      } finally {
        Object.defineProperty(URL, "createObjectURL", { configurable: true, value: savedCreateObj });
      }

      const origFetchOversize = vi.spyOn(globalThis, "fetch").mockResolvedValue(jsonResponse({ built_in: [], learned: [] }));
      renderWithClient(
        <Composer
          value="附件测试"
          onChange={vi.fn()}
          onSend={vi.fn()}
          onStop={vi.fn()}
          streaming={false}
          connected
          transcribeAudio={vi.fn()}
        />,
      );
      const oversize = new File(["x"], "huge.bin", { type: "application/octet-stream" });
      Object.defineProperty(oversize, "size", { value: 25 * 1024 * 1024 });
      const fileInput = document.querySelector('input[type="file"]') as HTMLInputElement;
      fireEvent.change(fileInput, { target: { files: [oversize] } });
      expect(await screen.findByText("「huge.bin」超过 20 MB，发不了")).toBeTruthy();
      fireEvent.click(screen.getByRole("button", { name: "添加文件" }));
      origFetchOversize.mockRestore();
      cleanup();

      // 3e. CommandPalette helper hit prefix & ChatThread cancel button click + fork without onMessageAction
      vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(
        jsonResponse({
          hits: [
            {
              chat_id: "hc-1",
              chat_title: "帮手群聊",
              helper: true,
              role: "assistant",
              text: "来自帮手的答案",
              snippet: "来自帮手的答案",
              turn_id: "t-helper",
              at_ms: Date.now(),
              matches: [[2, 4]],
            },
          ],
        }),
      );
      render(
        <CommandPalette
          token="tok"
          activeChatId="main-1"
          activeChatTitle="主聊"
          sideChats={[]}
          helpers={[]}
          onClose={vi.fn()}
          onSelectTab={vi.fn()}
          onSelectSideChat={vi.fn()}
          onNewSideChat={vi.fn()}
          onSelectHelper={vi.fn()}
          onOpenSettings={vi.fn()}
          onOpenMemory={vi.fn()}
          onExportChat={vi.fn()}
          onJumpToHit={vi.fn()}
        />,
      );
      fireEvent.change(screen.getByLabelText("搜索对话或输入指令"), { target: { value: "帮手" } });
      expect(await screen.findByText(/帮手：/)).toBeTruthy();
      cleanup();

      const forkSpy = vi.fn();
      render(
        <ChatThread
          items={[
            {
              type: "user",
              id: "u-cancel",
              message: { id: "u-cancel", role: "user", content: "取消编辑", turnId: "t-c", createdAt: 1000 },
            },
          ]}
          typing={false}
          routes={new Map()}
          onDecide={vi.fn()}
          onEditMessage={vi.fn()}
          onForkMessage={forkSpy}
        />,
      );
      fireEvent.click(screen.getByRole("button", { name: "从这里分叉旁聊" }));
      expect(forkSpy).toHaveBeenCalledTimes(1);
      fireEvent.click(screen.getByRole("button", { name: "编辑并重发" }));
      fireEvent.click(screen.getByRole("button", { name: "取消" }));
      expect(screen.queryByLabelText("编辑消息内容")).toBeNull();
      cleanup();

      // 3f. Composer quote clear, attachment remove, Enter send & MessageActions Shift+F10 / Escape
      const onClearQuote = vi.fn();
      const onSendEnter = vi.fn();
      renderWithClient(
        <Composer
          value="直接回车发送"
          onChange={vi.fn()}
          onSend={onSendEnter}
          onStop={vi.fn()}
          streaming={false}
          connected
          quote="引用一段话"
          onClearQuote={onClearQuote}
          transcribeAudio={vi.fn()}
        />,
      );
      fireEvent.click(screen.getByRole("button", { name: "不引用了" }));
      expect(onClearQuote).toHaveBeenCalledTimes(1);
      const box = screen.getByLabelText("发消息");
      const attach = new File(["abc"], "a.txt", { type: "text/plain" });
      fireEvent.paste(box, { clipboardData: { files: [attach], items: [] } });
      expect(await screen.findByText("a.txt")).toBeTruthy();
      fireEvent.click(screen.getByRole("button", { name: "移除" }));
      await waitFor(() => expect(screen.queryByText("a.txt")).toBeNull());
      fireEvent.keyDown(box, { key: "Enter" });
      expect(onSendEnter).toHaveBeenCalledTimes(1);
      cleanup();

      render(
        <ChatThread
          items={[
            {
              type: "user",
              id: "u-menu",
              message: { id: "u-menu", role: "user", content: "键盘菜单", turnId: "t-m", createdAt: 1000 },
            },
          ]}
          typing={false}
          routes={new Map()}
          onDecide={vi.fn()}
          onMessageAction={vi.fn()}
        />,
      );
      const msgBubble = screen.getByText("键盘菜单").closest('[tabindex="0"]')!;
      fireEvent.keyDown(msgBubble, { key: "F10", shiftKey: true });
      expect(screen.getByRole("menu", { name: "这条消息" })).toBeTruthy();
      fireEvent.keyDown(window, { key: "Escape" });
      expect(screen.queryByRole("menu", { name: "这条消息" })).toBeNull();
      cleanup();

      // 3g. useSpeechPlayer stale play resolution after stop
      const OrigAudio3 = globalThis.Audio;
      let resolvePlay!: () => void;
      const pauseStale = vi.fn();
      globalThis.Audio = vi.fn(function StaleAudio() {
        return {
          play: () =>
            new Promise<void>((res) => {
              resolvePlay = res;
            }),
          pause: pauseStale,
          src: "",
          onended: null,
          onerror: null,
        };
      }) as unknown as typeof Audio;
      vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(jsonResponse({ ok: true, url: "/stale.mp3" }));
      function StaleHarness() {
        const player = useSpeechPlayer({ token: "tok", ttsEnabled: true });
        return (
          <>
            <button type="button" onClick={() => void player.play("延时播放", "m-stale")}>
              play-stale
            </button>
            <button type="button" onClick={player.stop}>
              stop-stale
            </button>
          </>
        );
      }
      try {
        render(<StaleHarness />);
        fireEvent.click(screen.getByRole("button", { name: "play-stale" }));
        await waitFor(() => expect(resolvePlay).toBeTruthy());
        fireEvent.click(screen.getByRole("button", { name: "stop-stale" }));
        await act(async () => {
          resolvePlay();
        });
        expect(pauseStale).toHaveBeenCalled();

        // Hit read-only audio.src catch + AbortError swallow during loading
        let rejectAbort!: (err: DOMException) => void;
        vi.spyOn(globalThis, "fetch").mockImplementationOnce(
          () =>
            new Promise((_res, rej) => {
              rejectAbort = rej;
            }),
        );
        fireEvent.click(screen.getByRole("button", { name: "play-stale" }));
        await waitFor(() => expect(rejectAbort).toBeTruthy());
        await act(async () => {
          rejectAbort(new DOMException("Aborted", "AbortError"));
        });
      } finally {
        globalThis.Audio = OrigAudio3;
        cleanup();
      }

      // 3h. ChatThread highlightedTurnId + Composer > MAX_FILES cap & Android attach error
      render(
        <ChatThread
          items={[
            {
              type: "user",
              id: "u-hl",
              message: { id: "u-hl", role: "user", content: "高亮定位", turnId: "t-hl", createdAt: 1000 },
            },
          ]}
          highlightedTurnId="t-hl"
          typing={false}
          routes={new Map()}
          onDecide={vi.fn()}
        />,
      );
      expect(document.querySelector('[data-highlighted="true"]')).toBeTruthy();
      cleanup();

      // Test selection outside reply body clears quote pill + non-highlighted user turn + ReplyTurn without onAction
      render(
        <ChatThread
          items={[
            {
              type: "user",
              id: "u-plain",
              message: { id: "u-plain", role: "user", content: "普通提问", turnId: "", createdAt: 1000 },
            },
            {
              type: "reply",
              id: "a-plain",
              message: { id: "a-plain", role: "assistant", content: "回答内容", turnId: "", createdAt: 2000 },
              blocks: [{ type: "bubble", text: "回答内容" }],
              media: [],
            },
          ]}
          typing={false}
          routes={new Map()}
          onDecide={vi.fn()}
          onQuoteSelection={vi.fn()}
        />,
      );
      const plainRange = document.createRange();
      plainRange.selectNodeContents(screen.getByText("普通提问"));
      vi.spyOn(window, "getSelection").mockReturnValue({
        isCollapsed: false,
        rangeCount: 1,
        toString: () => "普通提问",
        getRangeAt: () => plainRange,
      } as unknown as Selection);
      await act(async () => {
        document.dispatchEvent(new Event("selectionchange"));
      });
      expect(screen.queryByRole("button", { name: "引用追问" })).toBeNull();
      cleanup();

      vi.spyOn(globalThis, "fetch").mockResolvedValue(jsonResponse({ built_in: [], learned: [] }));
      renderWithClient(
        <Composer
          value="多文件"
          onChange={vi.fn()}
          onSend={vi.fn()}
          onStop={vi.fn()}
          streaming={false}
          connected
          transcribeAudio={vi.fn()}
        />,
      );
      const manyFiles = Array.from({ length: 7 }, (_, i) => new File(["x"], `f${i}.txt`, { type: "text/plain" }));
      fireEvent.paste(screen.getByLabelText("发消息"), { clipboardData: { files: manyFiles, items: [] } });
      expect(await screen.findByText("一次最多 6 个文件")).toBeTruthy();

      // Composer focus/blur, streaming stop, voiceEnabled=false, / ArrowUp, and mouseEnter
      const composerTextarea = screen.getByLabelText("发消息");
      fireEvent.focus(composerTextarea);
      fireEvent.blur(composerTextarea);
      cleanup();

      const onStop = vi.fn();
      const { rerender: rerenderComp } = renderWithClient(
        <Composer
          value="/"
          onChange={vi.fn()}
          onSend={vi.fn()}
          onStop={onStop}
          streaming
          connected
          onFocusChange={vi.fn()}
          voiceEnabled={false}
          transcribeAudio={vi.fn()}
        />,
      );
      const slashTextarea = screen.getByLabelText("发消息");
      fireEvent.keyDown(slashTextarea, { key: "ArrowUp" });
      const firstSlashBtn = screen.getByRole("button", { name: /整理上下文/ });
      fireEvent.mouseEnter(firstSlashBtn);
      fireEvent.click(screen.getByRole("button", { name: "停止" }));
      expect(onStop).toHaveBeenCalledTimes(1);

      rerenderComp(
        <ClientProvider client={mockClient} token="tok-123">
          <Composer
            value=""
            onChange={vi.fn()}
            onSend={vi.fn()}
            onStop={onStop}
            streaming={false}
            connected
            voiceEnabled={false}
            transcribeAudio={vi.fn()}
          />
        </ClientProvider>,
      );
      expect(screen.queryByRole("button", { name: "语音输入" })).toBeNull();

      // 4. Web Speech API utterance onend / onerror + Android polling stop
      const utterances: Array<{ onend: (() => void) | null; onerror: (() => void) | null }> = [];
      const OrigUtterance = globalThis.SpeechSynthesisUtterance;
      const origSynth = window.speechSynthesis;
      globalThis.SpeechSynthesisUtterance = vi.fn(function FakeUtt(this: { onend: null; onerror: null }) {
        this.onend = null;
        this.onerror = null;
        utterances.push(this);
      }) as unknown as typeof SpeechSynthesisUtterance;
      Object.defineProperty(window, "speechSynthesis", {
        configurable: true,
        value: { speak: vi.fn(), cancel: vi.fn(), speaking: false },
      });

      function DirectHarness() {
        const player = useSpeechPlayer({ token: "tok", ttsEnabled: false });
        return (
          <div>
            <span data-testid="dir-status">{player.status}</span>
            <button type="button" onClick={() => void player.play("短句一", "d1")}>
              d1
            </button>
            <button type="button" onClick={() => void player.play("短句二", "d2")}>
              d2
            </button>
          </div>
        );
      }

      try {
        render(<DirectHarness />);
        fireEvent.click(screen.getByRole("button", { name: "d1" }));
        expect(screen.getByTestId("dir-status").textContent).toBe("playing");
        act(() => {
          utterances[0]?.onend?.();
        });
        expect(screen.getByTestId("dir-status").textContent).toBe("idle");

        fireEvent.click(screen.getByRole("button", { name: "d2" }));
        expect(screen.getByTestId("dir-status").textContent).toBe("playing");
        act(() => {
          utterances[1]?.onerror?.();
        });
        expect(screen.getByTestId("dir-status").textContent).toBe("idle");

        vi.useFakeTimers();
        let speakingFlag = true;
        (window as unknown as { PersonalBotAndroid?: unknown }).PersonalBotAndroid = {
          speak: vi.fn(),
          stopSpeaking: vi.fn(),
          speaking: () => speakingFlag,
        };
        fireEvent.click(screen.getByRole("button", { name: "d1" }));
        expect(screen.getByTestId("dir-status").textContent).toBe("playing");
        speakingFlag = false;
        act(() => {
          vi.advanceTimersByTime(1_600);
        });
        expect(screen.getByTestId("dir-status").textContent).toBe("idle");
      } finally {
        vi.useRealTimers();
        globalThis.SpeechSynthesisUtterance = OrigUtterance;
        Object.defineProperty(window, "speechSynthesis", { configurable: true, value: origSynth });
      }
    });
  });
});
