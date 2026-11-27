import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ChatModelBar, contextPercent } from "@/companion/ChatModelBar";
import { ChatThread, extractMarkdownHeadings } from "@/companion/ChatThread";
import { CommandPalette } from "@/companion/CommandPalette";
import { Composer } from "@/companion/Composer";
import { CreationsView } from "@/companion/Creations";
import { KnowledgeVault } from "@/companion/KnowledgeVault";
import { DailyUsageChart, UsagePanel } from "@/companion/UsagePanel";
import {
  archiveChat,
  fetchChatContext,
  importCreationToKnowledge,
  pinChat,
  pruneEmptyChats,
  readKnowledgeDocument,
  saveCreationArtifact,
  setChatModel,
  type ChatContextUsage,
  type CreationsPayload,
  type KnowledgeDocument,
  type KnowledgePayload,
  type ModelsPayload,
  type UsageDailyBucket,
} from "@/companion/companion-api";
import {
  DRAFT_STORE_KEY,
  MAX_DRAFT_CHATS,
  clearDraft,
  hasDraft,
  listDraftChatIds,
  loadDraft,
  saveDraft,
  subscribeDrafts,
} from "@/companion/draftStore";
import { ArtifactProvider, useArtifact } from "@/companion/muse/ArtifactContext";
import { ArtifactPane } from "@/companion/muse/ArtifactPane";
import { MuseDrawer } from "@/companion/muse/Drawer";
import { SideChatSheet } from "@/companion/muse/SideChatSheet";
import MarkdownTextRenderer, {
  extractTableMatrix,
  tableMatrixToCsv,
  tableMatrixToMarkdown,
  tableMatrixToTsv,
} from "@/components/MarkdownTextRenderer";
import { listSessions } from "@/lib/api";
import type { NanobotClient } from "@/lib/nanobot-client";
import type { ChatSummary, UIMessage } from "@/lib/types";
import { ClientProvider } from "@/providers/ClientProvider";

const requestMutation = vi.fn();

const mockClient = {
  status: "open",
  requestMutation,
  subscribe: vi.fn(() => () => undefined),
  onStatus: vi.fn(() => () => undefined),
  onThreadEvent: vi.fn(() => () => undefined),
  onFeedUpdate: vi.fn(() => () => undefined),
  onApprovalUpdate: vi.fn(() => () => undefined),
} as unknown as NanobotClient;

function renderWithClient(ui: React.ReactNode, token = "tok-w3") {
  return render(
    <ClientProvider client={mockClient} token={token}>
      {ui}
    </ClientProvider>,
  );
}

function stubMedia(options: { desktop?: boolean; wide?: boolean; coarse?: boolean } = {}) {
  Object.defineProperty(window, "matchMedia", {
    configurable: true,
    value: vi.fn((query: string) => ({
      matches: query.includes("1280px")
        ? Boolean(options.wide)
        : query.includes("1024px")
          ? Boolean(options.desktop ?? options.wide)
          : query.includes("coarse")
            ? Boolean(options.coarse)
            : false,
      media: query,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
    })),
  });
}

function stubDownloadMocks() {
  const createObjectURL = vi.fn(() => "blob:w3-mock");
  const revokeObjectURL = vi.fn();
  const origCreate = URL.createObjectURL;
  const origRevoke = URL.revokeObjectURL;
  URL.createObjectURL = createObjectURL;
  URL.revokeObjectURL = revokeObjectURL;
  const downloadedNames: string[] = [];
  const clickSpy = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (this: HTMLAnchorElement) {
    downloadedNames.push(this.download);
  });
  return {
    createObjectURL,
    revokeObjectURL,
    downloadedNames,
    clickSpy,
    restore() {
      URL.createObjectURL = origCreate;
      URL.revokeObjectURL = origRevoke;
    },
  };
}

function makeChat(id: string, extra: Partial<ChatSummary> = {}): ChatSummary {
  return {
    key: `websocket:${id}`,
    channel: "websocket",
    chatId: id,
    createdAt: null,
    updatedAt: null,
    title: extra.title ?? "",
    preview: extra.preview ?? "",
    ...extra,
  };
}

beforeEach(() => {
  vi.clearAllMocks();
  localStorage.clear();
  Object.defineProperty(navigator, "clipboard", {
    configurable: true,
    value: { writeText: vi.fn(async () => undefined) },
  });
});

afterEach(() => {
  cleanup();
  localStorage.clear();
  vi.restoreAllMocks();
  vi.useRealTimers();
});

describe("Wave 3.1: Draft persistence, [草稿] badge, history recall, and char count", () => {
  it("persists per-chat drafts and quotes with 30-chat LRU eviction and storage sync", () => {
    const listener = vi.fn();
    const unsub = subscribeDrafts(listener);

    saveDraft("chat-a", "草稿正文", "引用的上文", 1000);
    expect(loadDraft("chat-a")).toEqual({ text: "草稿正文", quote: "引用的上文", updatedAt: 1000 });
    expect(hasDraft("chat-a")).toBe(true);
    expect(listDraftChatIds().has("chat-a")).toBe(true);
    expect(listener).toHaveBeenCalledTimes(1);

    // Saving identical content is a no-op
    saveDraft("chat-a", "草稿正文", "引用的上文", 1001);
    expect(listener).toHaveBeenCalledTimes(1);

    // Clearing removes draft
    clearDraft("chat-a");
    expect(loadDraft("chat-a")).toBeNull();
    expect(hasDraft("chat-a")).toBe(false);

    // Saving whitespace-only clears existing draft
    saveDraft("chat-b", "hello", null, 2000);
    saveDraft("chat-b", "   ", "  ", 2001);
    expect(loadDraft("chat-b")).toBeNull();

    // LRU cap at 30 sessions keeps newest 30
    for (let i = 0; i < MAX_DRAFT_CHATS + 5; i += 1) {
      saveDraft(`chat-${i}`, `text-${i}`, null, 10_000 + i);
    }
    expect(listDraftChatIds().size).toBe(MAX_DRAFT_CHATS);
    expect(loadDraft("chat-0")).toBeNull();
    expect(loadDraft("chat-4")).toBeNull();
    expect(loadDraft("chat-5")?.text).toBe("text-5");
    expect(loadDraft(`chat-${MAX_DRAFT_CHATS + 4}`)?.text).toBe(`text-${MAX_DRAFT_CHATS + 4}`);

    // Cross-tab storage event triggers listener
    window.dispatchEvent(new StorageEvent("storage", { key: DRAFT_STORE_KEY }));
    expect(listener).toHaveBeenCalled();

    // Edge cases: empty chatId, non-existent clear, corrupt JSON, non-object JSON
    expect(loadDraft("")).toBeNull();
    saveDraft("", "ignored", null);
    clearDraft("");
    clearDraft("never-existed");
    localStorage.setItem(DRAFT_STORE_KEY, "{not-valid-json");
    expect(loadDraft("chat-5")).toBeNull();
    localStorage.setItem(DRAFT_STORE_KEY, JSON.stringify(["not-an-object"]));
    expect(loadDraft("chat-5")).toBeNull();
    localStorage.setItem(
      DRAFT_STORE_KEY,
      JSON.stringify({ bad: null, empty: { text: "  ", quote: "", updatedAt: 1 }, ok: { text: "hi", quote: " q ", updatedAt: 2 } }),
    );
    expect(loadDraft("ok")).toEqual({ text: "hi", quote: " q ", updatedAt: 2 });
    expect(loadDraft("empty")).toBeNull();

    unsub();
  });

  it("shows [草稿] badge in MuseDrawer for side chats with unsent drafts", () => {
    saveDraft("s1", "还没发出的长草稿", null, Date.now());
    render(
      <MuseDrawer
        open
        onClose={vi.fn()}
        name="Muse"
        current="chat"
        activeChatId={null}
        order={["chat", "feed", "ideas", "goals", "creations"]}
        onReorder={vi.fn()}
        onTab={vi.fn()}
        sideChats={[makeChat("s1", { title: "产品评审" }), makeChat("s2", { title: "周末计划" })]}
        onOpenChat={vi.fn()}
        onNewChat={vi.fn()}
        onOpenSettings={vi.fn()}
        onEditChat={vi.fn()}
        onDeleteChat={vi.fn()}
      />,
    );

    expect(screen.getAllByTestId("drawer-draft-badge")).toHaveLength(1);
    expect(screen.getByText("[草稿]")).toBeInTheDocument();

    act(() => {
      clearDraft("s1");
    });
    expect(screen.queryByTestId("drawer-draft-badge")).toBeNull();
  });

  it("recalls sent history on desktop ArrowUp/ArrowDown, restores draft, and displays char count over 600 chars", () => {
    stubMedia({ coarse: false });
    let draft = "正在写的草稿";
    const onChange = vi.fn((next: string) => {
      draft = next;
    });
    const onSend = vi.fn();

    const { rerender } = renderWithClient(
      <Composer
        value={draft}
        onChange={onChange}
        onSend={onSend}
        onStop={vi.fn()}
        streaming={false}
        connected
        transcribeAudio={vi.fn()}
        sentHistory={["第一条历史问题", "第二条历史问题"]}
      />,
    );

    const box = screen.getByRole("textbox", { name: "发消息" }) as HTMLTextAreaElement;
    // Caret in the middle does NOT hijack ArrowUp
    box.setSelectionRange(3, 3);
    fireEvent.keyDown(box, { key: "ArrowUp" });
    expect(onChange).not.toHaveBeenCalled();

    // Caret at start (0, 0) recalls latest sent message
    box.setSelectionRange(0, 0);
    fireEvent.keyDown(box, { key: "ArrowUp" });
    expect(onChange).toHaveBeenLastCalledWith("第二条历史问题");

    rerender(
      <ClientProvider client={mockClient} token="tok-w3">
        <Composer
          value={draft}
          onChange={onChange}
          onSend={onSend}
          onStop={vi.fn()}
          streaming={false}
          connected
          transcribeAudio={vi.fn()}
          sentHistory={["第一条历史问题", "第二条历史问题"]}
        />
      </ClientProvider>,
    );

    box.setSelectionRange(0, 0);
    fireEvent.keyDown(box, { key: "ArrowUp" });
    expect(onChange).toHaveBeenLastCalledWith("第一条历史问题");

    rerender(
      <ClientProvider client={mockClient} token="tok-w3">
        <Composer
          value={draft}
          onChange={onChange}
          onSend={onSend}
          onStop={vi.fn()}
          streaming={false}
          connected
          transcribeAudio={vi.fn()}
          sentHistory={["第一条历史问题", "第二条历史问题"]}
        />
      </ClientProvider>,
    );

    // ArrowDown walks forward and finally restores the stashed draft
    box.setSelectionRange(0, 0);
    fireEvent.keyDown(box, { key: "ArrowDown" });
    expect(onChange).toHaveBeenLastCalledWith("第二条历史问题");

    rerender(
      <ClientProvider client={mockClient} token="tok-w3">
        <Composer
          value={draft}
          onChange={onChange}
          onSend={onSend}
          onStop={vi.fn()}
          streaming={false}
          connected
          transcribeAudio={vi.fn()}
          sentHistory={["第一条历史问题", "第二条历史问题"]}
        />
      </ClientProvider>,
    );

    box.setSelectionRange(0, 0);
    fireEvent.keyDown(box, { key: "ArrowDown" });
    expect(onChange).toHaveBeenLastCalledWith("正在写的草稿");

    // Char count hidden under 600 chars, shown above 600 chars
    expect(screen.queryByTestId("composer-char-count")).toBeNull();
    const longText = "字".repeat(612);
    rerender(
      <ClientProvider client={mockClient} token="tok-w3">
        <Composer
          value={longText}
          onChange={onChange}
          onSend={onSend}
          onStop={vi.fn()}
          streaming={false}
          connected
          transcribeAudio={vi.fn()}
        />
      </ClientProvider>,
    );
    expect(screen.getByTestId("composer-char-count")).toHaveTextContent("612 字");
  });
});

describe("Wave 3.2: Canvas Live Edit Mode, Split View, In-Place Save, and Refine Quote", () => {
  it("supports wide-screen split view, Tab indentation, 250ms debounced live preview, revert, in-place save, and refine with quote", async () => {
    stubMedia({ desktop: true, wide: true });
    const dl = stubDownloadMocks();
    const onRefine = vi.fn();
    requestMutation
      .mockResolvedValueOnce({
        items: [],
        counts: { image: 0, video: 0, document: 1, audio: 0 },
        saved_id: "文档/live-demo.html",
        updated: false,
      })
      .mockResolvedValueOnce({
        items: [],
        counts: { image: 0, video: 0, document: 1, audio: 0 },
        saved_id: "文档/live-demo.html",
        updated: true,
      });

    function Launcher() {
      const ctx = useArtifact();
      return (
        <button
          type="button"
          onClick={() =>
            ctx?.openArtifact({
              language: "html",
              title: "交互原型",
              code: "<div>\n<h1>原始版本</h1>\n</div>",
            })
          }
        >
          open-live
        </button>
      );
    }

    try {
      renderWithClient(
        <ArtifactProvider onRefine={onRefine}>
          <Launcher />
          <ArtifactPane />
        </ArtifactProvider>,
      );

      fireEvent.click(screen.getByRole("button", { name: "open-live" }));
      const pane = await screen.findByTestId("artifact-pane-desktop");

      // Wide desktop shows 预览 | 编辑 | 分屏
      expect(within(pane).getByRole("tab", { name: "预览" })).toBeInTheDocument();
      expect(within(pane).getByRole("tab", { name: "编辑" })).toBeInTheDocument();
      expect(within(pane).getByRole("tab", { name: "分屏" })).toBeInTheDocument();

      // Switch to split view
      fireEvent.click(within(pane).getByRole("tab", { name: "分屏" }));
      expect(within(pane).getByTestId("artifact-split-view")).toBeInTheDocument();

      const editor = within(pane).getByTestId("artifact-code-editor") as HTMLTextAreaElement;
      expect(within(pane).getByTestId("artifact-line-numbers")).toHaveTextContent("123");

      // Tab inserts 2 spaces
      editor.setSelectionRange(6, 6);
      fireEvent.keyDown(editor, { key: "Tab" });
      expect(editor.value).toBe("<div>\n  <h1>原始版本</h1>\n</div>");

      // Shift+Tab removes leading 2 spaces
      editor.setSelectionRange(8, 8);
      fireEvent.keyDown(editor, { key: "Tab", shiftKey: true });
      expect(editor.value).toBe("<div>\n<h1>原始版本</h1>\n</div>");

      // Edit source -> dirty badge + debounced live iframe update
      vi.useFakeTimers();
      fireEvent.change(editor, { target: { value: "<div>\n<h1>实时修改版</h1>\n</div>" } });
      expect(within(pane).getByTestId("artifact-dirty-badge")).toHaveTextContent("已修改");

      act(() => {
        vi.advanceTimersByTime(260);
      });
      const iframe = within(pane).getByTitle("HTML 预览：交互原型") as HTMLIFrameElement;
      expect(iframe.getAttribute("srcdoc")).toContain("实时修改版");
      vi.useRealTimers();

      // Copy & Download use edited code
      fireEvent.click(within(pane).getByRole("button", { name: "复制源码" }));
      await waitFor(() =>
        expect(navigator.clipboard.writeText).toHaveBeenCalledWith("<div>\n<h1>实时修改版</h1>\n</div>"),
      );
      fireEvent.click(within(pane).getByRole("button", { name: "下载" }));
      expect(dl.downloadedNames).toEqual(["交互原型.html"]);

      // First save creates new creation without id
      fireEvent.click(within(pane).getByRole("button", { name: "存入资源库" }));
      await waitFor(() =>
        expect(requestMutation).toHaveBeenNthCalledWith(1, "companion.creations.save", {
          title: "交互原型",
          content: "<div>\n<h1>实时修改版</h1>\n</div>",
          format: "html",
        }),
      );
      expect(await within(pane).findByRole("button", { name: "已存入资源库" })).toBeInTheDocument();

      // Subsequent edit + save updates in place with saved_id -> 已更新资源库
      fireEvent.change(editor, { target: { value: "<div>\n<h1>第三版</h1>\n</div>" } });
      fireEvent.click(within(pane).getByRole("button", { name: "更新资源库" }));
      await waitFor(() =>
        expect(requestMutation).toHaveBeenNthCalledWith(2, "companion.creations.save", {
          id: "文档/live-demo.html",
          title: "交互原型",
          content: "<div>\n<h1>第三版</h1>\n</div>",
          format: "html",
        }),
      );
      expect(await within(pane).findByRole("button", { name: "已更新资源库" })).toBeInTheDocument();

      // Refine carries edited source as quote
      fireEvent.click(within(pane).getByRole("button", { name: "让伙伴继续完善" }));
      expect(onRefine).toHaveBeenCalledWith(
        "请帮我继续完善「交互原型」：",
        expect.stringContaining("<h1>第三版</h1>"),
      );

      // Revert restores original code and clears dirty badge
      fireEvent.click(within(pane).getByRole("button", { name: "还原" }));
      expect(editor.value).toBe("<div>\n<h1>原始版本</h1>\n</div>");
      expect(within(pane).queryByTestId("artifact-dirty-badge")).toBeNull();
    } finally {
      dl.restore();
    }
  });
});

describe("Wave 3.3: Per-chat Model Picker, Context Watermark, Pin/Archive/Prune, and API contracts", () => {
  it("parses extended /api/sessions fields and calls new companion API mutations/endpoints accurately", async () => {
    vi.spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            sessions: [
              {
                key: "websocket:s-pin",
                created_at: "2026-11-27T01:00:00Z",
                updated_at: "2026-11-27T02:00:00Z",
                title: "核心架构",
                preview: "讨论方案",
                pinned: true,
                archived: false,
                empty: false,
                model_preset: "pro",
                reasoning_effort: "high",
              },
              {
                key: "websocket:s-arch",
                created_at: "2026-11-26T01:00:00Z",
                updated_at: "2026-11-26T02:00:00Z",
                title: "旧记录",
                preview: "",
                archived: true,
                empty: true,
              },
            ],
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        ),
      )
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            chat_id: "s-pin",
            estimated_tokens: 24_000,
            context_window: 200_000,
            usage_ratio: 0.12,
            tier1_threshold: 120_000,
            tier2_threshold: 160_000,
            has_summary_checkpoint: true,
            model: "claude-sonnet",
            model_preset: "pro",
            reasoning_effort: "high",
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        ),
      );

    const sessions = await listSessions("tok");
    expect(sessions[0]).toMatchObject({
      chatId: "s-pin",
      pinned: true,
      empty: false,
      modelPreset: "pro",
      reasoningEffort: "high",
    });
    expect(sessions[1]).toMatchObject({
      chatId: "s-arch",
      archived: true,
      empty: true,
    });

    const ctx = await fetchChatContext("tok", "s-pin");
    expect(ctx.usage_ratio).toBe(0.12);
    expect(contextPercent(ctx)).toBe(12);

    requestMutation.mockResolvedValue({ ok: true, removed_count: 2 });
    await setChatModel(mockClient, "s-pin", { model_preset: "pro", reasoning_effort: "high" });
    expect(requestMutation).toHaveBeenCalledWith("companion.chats.change", {
      id: "s-pin",
      action: "set_model",
      model_preset: "pro",
      reasoning_effort: "high",
    });

    await pinChat(mockClient, "s-pin", true);
    expect(requestMutation).toHaveBeenCalledWith("companion.chats.change", {
      id: "s-pin",
      action: "pin",
      pinned: true,
    });

    await archiveChat(mockClient, "s-arch", true);
    expect(requestMutation).toHaveBeenCalledWith("companion.chats.change", {
      id: "s-arch",
      action: "archive",
      archived: true,
    });

    await pruneEmptyChats(mockClient);
    expect(requestMutation).toHaveBeenCalledWith("companion.chats.change", {
      action: "prune_empty",
    });

    await readKnowledgeDocument(mockClient, "doc-1");
    expect(requestMutation).toHaveBeenCalledWith("companion.knowledge.change", {
      action: "read",
      id: "doc-1",
    });

    await saveCreationArtifact(mockClient, { id: "文档/a.md", title: "A", content: "v2", format: "md" });
    expect(requestMutation).toHaveBeenCalledWith("companion.creations.save", {
      id: "文档/a.md",
      title: "A",
      content: "v2",
      format: "md",
    });

    await importCreationToKnowledge(mockClient, "文档/a.md", { replaceExisting: true });
    expect(requestMutation).toHaveBeenCalledWith("companion.knowledge.change", {
      action: "import_creation",
      creation_id: "文档/a.md",
      replace_existing: true,
    });
  });

  it("renders ChatModelBar model/reasoning picker and context watermark popover with /compact action", async () => {
    const models: ModelsPayload = {
      presets: [
        {
          name: "flash",
          model: "deepseek-chat",
          provider: "deepseek",
          vendor: "deepseek",
          reasoning_effort: null,
          max_tokens: 8192,
          temperature: 0.2,
          context_window_tokens: 128_000,
        },
        {
          name: "pro",
          model: "claude-sonnet",
          provider: "anthropic",
          vendor: "anthropic",
          reasoning_effort: "high",
          max_tokens: 8192,
          temperature: 0.2,
          context_window_tokens: 200_000,
        },
      ],
      max_tool_iterations: 50,
      default: "flash",
      fallbacks: [],
      settings: { enabled: false, router: null, fixed: null, models: {} },
      health: {
        flash: { ok: true, error: null, checked_at: 1 },
        pro: { ok: false, error: "timeout", checked_at: 1 },
      },
      decisions: [],
      media: { image: { active: null, models: [] }, video: { active: null, models: [] } },
    };
    const usage: ChatContextUsage = {
      estimated_tokens: 176_000,
      context_window: 200_000,
      usage_ratio: 0.88,
      tier1_threshold: 120_000,
      tier2_threshold: 160_000,
      has_summary_checkpoint: true,
      model: "claude-sonnet",
      model_preset: null,
      reasoning_effort: null,
    };

    requestMutation
      .mockResolvedValueOnce({ chat_id: "c1", model_preset: "pro", reasoning_effort: null })
      .mockResolvedValueOnce({ chat_id: "c1", model_preset: "pro", reasoning_effort: "high" })
      .mockResolvedValueOnce({ chat_id: "c1", model_preset: null, reasoning_effort: null });

    const onModelChanged = vi.fn();
    const onCompact = vi.fn();

    renderWithClient(
      <ChatModelBar
        chatId="c1"
        session={makeChat("c1")}
        models={models}
        contextUsage={usage}
        hasMessages
        streaming={false}
        connected
        onModelChanged={onModelChanged}
        onCompact={onCompact}
      />,
    );

    const modelBtn = screen.getByTestId("chat-model-capsule");
    expect(modelBtn).toHaveTextContent("flash");
    fireEvent.click(modelBtn);

    const popover = screen.getByTestId("chat-model-popover");
    fireEvent.click(within(popover).getByRole("button", { name: /pro/ }));
    await waitFor(() =>
      expect(requestMutation).toHaveBeenCalledWith("companion.chats.change", {
        id: "c1",
        action: "set_model",
        model_preset: "pro",
        reasoning_effort: null,
      }),
    );

    fireEvent.click(within(popover).getByRole("radio", { name: "高" }));
    await waitFor(() =>
      expect(requestMutation).toHaveBeenCalledWith("companion.chats.change", {
        id: "c1",
        action: "set_model",
        model_preset: "pro",
        reasoning_effort: "high",
      }),
    );

    fireEvent.click(within(popover).getByRole("button", { name: "恢复默认" }));
    await waitFor(() =>
      expect(requestMutation).toHaveBeenCalledWith("companion.chats.change", {
        id: "c1",
        action: "set_model",
        model_preset: null,
        reasoning_effort: null,
      }),
    );
    expect(onModelChanged).toHaveBeenCalledTimes(3);

    // Open context watermark popover and click /compact
    const ctxBtn = screen.getByTestId("chat-context-capsule");
    expect(ctxBtn).toHaveTextContent("88%");
    fireEvent.click(ctxBtn);

    const ctxPopover = screen.getByTestId("chat-context-popover");
    expect(ctxPopover).toHaveTextContent("已有摘要检查点");
    fireEvent.click(within(ctxPopover).getByRole("button", { name: /立即压缩上下文/ }));
    expect(onCompact).toHaveBeenCalledTimes(1);
    expect(screen.queryByTestId("chat-context-popover")).toBeNull();

    // Escape & outside mousedown close popovers; error alerts render cleanly
    fireEvent.click(modelBtn);
    expect(screen.getByTestId("chat-model-popover")).toBeInTheDocument();
    fireEvent.keyDown(document, { key: "Escape" });
    expect(screen.queryByTestId("chat-model-popover")).toBeNull();

    fireEvent.click(modelBtn);
    requestMutation.mockRejectedValueOnce(new Error("模型预设不存在"));
    fireEvent.click(within(screen.getByTestId("chat-model-popover")).getByRole("button", { name: /pro/ }));
    expect(await screen.findByRole("alert")).toHaveTextContent("模型预设不存在");
    fireEvent.mouseDown(document.body);
    expect(screen.queryByTestId("chat-model-popover")).toBeNull();

    // Warning tone (70%) + empty state renders null + contextPercent clamp
    expect(contextPercent({ ...usage, usage_ratio: -0.2 })).toBe(0);
    expect(contextPercent({ ...usage, usage_ratio: 1.5 })).toBe(100);
    const { container: emptyContainer } = renderWithClient(
      <ChatModelBar
        chatId="c-empty"
        session={makeChat("c-empty")}
        models={null}
        contextUsage={null}
        hasMessages={false}
        streaming={false}
        connected
      />,
    );
    expect(emptyContainer.firstChild).toBeNull();
  });

  it("supports pinned ordering, collapsible archived group, prune empty chats in MuseDrawer, and pin/archive in SideChatSheet", () => {
    const onPinChat = vi.fn();
    const onArchiveChat = vi.fn();
    const onPruneEmptyChats = vi.fn();

    render(
      <MuseDrawer
        open
        onClose={vi.fn()}
        name="Muse"
        current="chat"
        activeChatId={null}
        order={["chat", "feed", "ideas", "goals", "creations"]}
        onReorder={vi.fn()}
        onTab={vi.fn()}
        sideChats={[
          makeChat("c-normal", { title: "普通讨论", preview: "内容", empty: false }),
          makeChat("c-pinned", { title: "置顶项目", preview: "重要", pinned: true, empty: false }),
          makeChat("c-empty", { title: "", preview: "", empty: true }),
          makeChat("c-archived", { title: "历史归档", preview: "旧消息", archived: true, empty: false }),
        ]}
        onOpenChat={vi.fn()}
        onNewChat={vi.fn()}
        onOpenSettings={vi.fn()}
        onEditChat={vi.fn()}
        onDeleteChat={vi.fn()}
        onPinChat={onPinChat}
        onArchiveChat={onArchiveChat}
        onPruneEmptyChats={onPruneEmptyChats}
      />,
    );

    // Pinned chat appears first in the active list
    const list = screen.getByRole("list", { name: "旁聊列表" });
    const items = within(list).getAllByRole("listitem");
    expect(items[0]).toHaveTextContent("置顶项目");
    expect(within(items[0]).getByTestId("drawer-pin-icon")).toBeInTheDocument();

    // Quick pin toggle
    fireEvent.click(screen.getByRole("button", { name: "置顶「普通讨论」" }));
    expect(onPinChat).toHaveBeenCalledWith("c-normal", true);

    // Prune empty button
    fireEvent.click(screen.getByRole("button", { name: "清理空旁聊" }));
    expect(onPruneEmptyChats).toHaveBeenCalledTimes(1);

    // Archived group starts collapsed and expands on click
    expect(screen.queryByText("历史归档")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: /已归档 \(1\)/ }));
    expect(screen.getByText("历史归档")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "取消归档「历史归档」" }));
    expect(onArchiveChat).toHaveBeenCalledWith("c-archived", false);

    // SideChatSheet pin/archive actions
    const onClose = vi.fn();
    render(
      <SideChatSheet
        chat={makeChat("c-normal", { title: "普通讨论" })}
        onClose={onClose}
        onRename={vi.fn()}
        onPin={onPinChat}
        onArchive={onArchiveChat}
        onDelete={vi.fn()}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "置顶旁聊" }));
    expect(onPinChat).toHaveBeenCalledWith("c-normal", true);
    fireEvent.click(screen.getByRole("button", { name: "归档旁聊" }));
    expect(onArchiveChat).toHaveBeenCalledWith("c-normal", true);
  });
});

describe("Wave 3.4: GFM Table Toolbar and Long-Reply Outline / Collapse", () => {
  it("converts table matrices to TSV, Markdown, and UTF-8 BOM CSV cleanly and exposes toolbar actions", async () => {
    const dl = stubDownloadMocks();
    try {
      expect(tableMatrixToTsv(["指标", "数值"], [["延迟\tms", "12\n3"]])).toBe("指标\t数值\n延迟 ms\t12 3");
      expect(tableMatrixToMarkdown(["A|B", "C"], [["1", "2"]])).toBe("| A\\|B | C |\n| --- | --- |\n| 1 | 2 |");
      expect(tableMatrixToCsv(["名称", "说明"], [["甲", '含"引号",逗号']])).toBe(
        '\uFEFF名称,说明\r\n甲,"含""引号"",逗号"',
      );
      expect(extractTableMatrix(null)).toEqual({ headers: [], rows: [] });

      render(
        <MarkdownTextRenderer>
          {"| 框架 | 语言 | 体积 |\n| --- | --- | --- |\n| React | TS | 42KB |\n| Vite | TS | 12KB |"}
        </MarkdownTextRenderer>,
      );

      const tableRegion = screen.getByTestId("markdown-data-table");
      expect(within(tableRegion).getByTestId("markdown-table-toolbar")).toHaveTextContent("2×3");

      fireEvent.click(within(tableRegion).getByRole("button", { name: "复制表格" }));
      await waitFor(() =>
        expect(navigator.clipboard.writeText).toHaveBeenCalledWith("框架\t语言\t体积\nReact\tTS\t42KB\nVite\tTS\t12KB"),
      );

      fireEvent.click(within(tableRegion).getByRole("button", { name: "复制 MD" }));
      await waitFor(() =>
        expect(navigator.clipboard.writeText).toHaveBeenCalledWith(
          "| 框架 | 语言 | 体积 |\n| --- | --- | --- |\n| React | TS | 42KB |\n| Vite | TS | 12KB |",
        ),
      );

      fireEvent.click(within(tableRegion).getByRole("button", { name: "下载 CSV" }));
      expect(dl.downloadedNames).toEqual(["table.csv"]);
    } finally {
      dl.restore();
    }
  });

  it("extracts headings outside fenced code and renders outline/collapse bar on long completed replies", async () => {
    const md = [
      "## 第一章 背景",
      "正文介绍。",
      "```md",
      "## 代码块里的伪标题",
      "```",
      "### 1.1 目标拆解",
      "详细说明。",
      "## 第二章 实施路线",
      "最后总结。",
    ].join("\n");

    const headings = extractMarkdownHeadings(md);
    expect(headings).toEqual([
      { level: 2, text: "第一章 背景", index: 0 },
      { level: 3, text: "1.1 目标拆解", index: 1 },
      { level: 2, text: "第二章 实施路线", index: 2 },
    ]);

    const replyMessage: UIMessage = {
      id: "m-long",
      role: "assistant",
      content: md,
      createdAt: Date.now(),
      isStreaming: false,
      turnId: "t-long",
    };

    const { rerender } = render(
      <ChatThread
        items={[{ type: "reply", id: "r1", message: replyMessage, blocks: [{ type: "bubble", text: md }], media: [] }]}
        typing={false}
        routes={new Map()}
        onDecide={vi.fn()}
      />,
    );
    await screen.findByText("正文介绍。");

    const outlineBtn = screen.getByRole("button", { name: /目录 \(3 节\)/ });
    fireEvent.click(outlineBtn);
    const nav = screen.getByTestId("reply-outline-nav");
    expect(within(nav).getByRole("button", { name: "第一章 背景" })).toBeInTheDocument();
    expect(within(nav).getByRole("button", { name: "1.1 目标拆解" })).toBeInTheDocument();
    expect(within(nav).getByRole("button", { name: "第二章 实施路线" })).toBeInTheDocument();

    // Collapse & expand
    fireEvent.click(screen.getByRole("button", { name: "收起长文" }));
    expect(screen.getByTestId("reply-collapsed-fade")).toBeInTheDocument();

    // Clicking a heading while collapsed auto-expands
    fireEvent.click(within(nav).getByRole("button", { name: "第二章 实施路线" }));
    expect(screen.queryByTestId("reply-collapsed-fade")).toBeNull();

    // Streaming reply does NOT display outline/collapse bar
    rerender(
      <ChatThread
        items={[
          {
            type: "reply",
            id: "r1",
            message: { ...replyMessage, isStreaming: true },
            blocks: [{ type: "bubble", text: md }],
            media: [],
          },
        ]}
        typing={false}
        routes={new Map()}
        onDecide={vi.fn()}
      />,
    );
    expect(screen.queryByTestId("reply-outline-bar")).toBeNull();
  });
});

describe("Wave 3.5: Knowledge Vault Chunk Reader, Quote-to-Ask, Overwrite Re-import, and Cmd+K Shortcuts", () => {
  const doc: KnowledgeDocument = {
    id: "doc-w3",
    title: "设计规范",
    filename: "design.md",
    collection: "产品",
    mime: "text/markdown",
    size_bytes: 2048,
    char_count: 900,
    chunk_count: 2,
    content_sha256: "sha-w3",
    origin: "creation:文档/design.md",
    created_at: Math.floor(Date.now() / 1000) - 100,
    updated_at: Math.floor(Date.now() / 1000) - 50,
  };

  const kbPayload: KnowledgePayload = {
    documents: [doc],
    collections: ["产品"],
    collection_counts: { 产品: 1 },
    stats: { doc_count: 1, chunk_count: 2, total_bytes: 2048 },
    hits: [
      {
        doc_id: "doc-w3",
        title: "设计规范",
        filename: "design.md",
        collection: "产品",
        chunk_index: 0,
        heading: "色彩系统",
        snippet: "主题色采用暖瓷色调",
        content: "主题色采用暖瓷色调，深色模式自动降低对比度。",
        score: 8.8,
        matches: [[0, 3]],
      },
    ],
  };

  it("opens full chunk reader, copies chunks, and quotes chunks/hits into chat", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify(kbPayload), { status: 200, headers: { "Content-Type": "application/json" } }),
    );
    requestMutation.mockResolvedValueOnce({
      document: doc,
      chunks: [
        { chunk_index: 0, heading: "色彩系统", content: "主题色采用暖瓷色调。" },
        { chunk_index: 1, heading: "动效时长", content: "过渡时长不超过 220ms。" },
      ],
    });
    const onQuoteToAsk = vi.fn();

    renderWithClient(<KnowledgeVault active onQuoteToAsk={onQuoteToAsk} />);
    const openBtn = await screen.findByRole("button", { name: "阅读「设计规范」全部章节分块" });
    expect(screen.getByText(/作品导入/)).toBeInTheDocument();

    fireEvent.click(openBtn);
    await waitFor(() =>
      expect(requestMutation).toHaveBeenCalledWith("companion.knowledge.change", {
        action: "read",
        id: "doc-w3",
      }),
    );

    const sheet = await screen.findByTestId("knowledge-reader-sheet");
    const chunks = within(sheet).getAllByTestId("knowledge-reader-chunk");
    expect(chunks).toHaveLength(2);
    expect(chunks[1]).toHaveTextContent("动效时长");

    // Copy chunk
    fireEvent.click(within(chunks[0]).getByRole("button", { name: "复制片段" }));
    await waitFor(() => expect(navigator.clipboard.writeText).toHaveBeenCalledWith("主题色采用暖瓷色调。"));

    // Quote to ask from reader
    fireEvent.click(within(chunks[1]).getByRole("button", { name: "引用去提问" }));
    expect(onQuoteToAsk).toHaveBeenCalledWith(
      expect.stringContaining("[知识库 · 设计规范 / 动效时长]"),
      "结合「设计规范」的这段内容，",
    );

    // Search hit card also offers 复制片段 & 引用去提问
    const searchBox = screen.getByRole("searchbox", { name: "检索知识库" });
    fireEvent.change(searchBox, { target: { value: "暖瓷" } });
    fireEvent.submit(searchBox.closest("form")!);
    const quoteHitBtn = await screen.findByRole("button", { name: "引用去提问" });
    fireEvent.click(quoteHitBtn);
    expect(onQuoteToAsk).toHaveBeenCalledTimes(2);
  });

  it("supports sectionRequest jumping to 知识库 and overwrite re-import in CreationsView", async () => {
    const creations: CreationsPayload = {
      items: [
        {
          id: "文档/plan.md",
          kind: "document",
          name: "plan.md",
          title: "迭代计划",
          origin: "canvas",
          prompt: null,
          size: 512,
          created_at: new Date().toISOString(),
          url: "/f/文档/plan.md",
        },
      ],
      counts: { image: 0, video: 0, document: 1, audio: 0 },
    };
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = String(input);
      if (url.includes("/creations")) return new Response(JSON.stringify(creations), { status: 200 });
      return new Response(JSON.stringify(kbPayload), { status: 200 });
    });
    requestMutation
      .mockResolvedValueOnce({ ...kbPayload, added: { ...doc, title: "迭代计划", chunk_count: 2 } })
      .mockResolvedValueOnce({ ...kbPayload, added: { ...doc, title: "迭代计划", chunk_count: 3 } });

    const { rerender } = renderWithClient(<CreationsView active />);
    await screen.findByText("迭代计划");

    // First import
    fireEvent.click(screen.getByRole("button", { name: "将「迭代计划」加入知识库" }));
    expect(await screen.findByText(/已将「迭代计划」加入知识库/)).toBeInTheDocument();

    // Second click re-imports with replace_existing: true
    fireEvent.click(screen.getByRole("button", { name: "重新导入并覆盖「迭代计划」到知识库" }));
    await waitFor(() =>
      expect(requestMutation).toHaveBeenLastCalledWith("companion.knowledge.change", {
        action: "import_creation",
        creation_id: "文档/plan.md",
        replace_existing: true,
      }),
    );
    expect(await screen.findByText(/已覆盖更新知识库中的「迭代计划」/)).toBeInTheDocument();

    // sectionRequest switches straight to 知识库
    rerender(
      <ClientProvider client={mockClient} token="tok-w3">
        <CreationsView active sectionRequest={{ section: "knowledge", at: Date.now() }} />
      </ClientProvider>,
    );
    expect(await screen.findByTestId("knowledge-vault")).toBeInTheDocument();
  });

  it("exposes 用量统计, 外观设置, 知识库, and 清理空旁聊 in CommandPalette", () => {
    const onOpenSettings = vi.fn();
    const onOpenAppearance = vi.fn();
    const onOpenKnowledge = vi.fn();
    const onPruneEmptyChats = vi.fn();

    render(
      <CommandPalette
        token="tok"
        activeChatId="main"
        activeChatTitle="主要聊天"
        sideChats={[
          makeChat("s1", { title: "普通旁聊" }),
          makeChat("s2", { title: "置顶旁聊", pinned: true }),
          makeChat("s3", { title: "已归档旁聊", archived: true }),
        ]}
        helpers={[]}
        emptySideChatCount={2}
        onClose={vi.fn()}
        onSelectTab={vi.fn()}
        onSelectSideChat={vi.fn()}
        onNewSideChat={vi.fn()}
        onSelectHelper={vi.fn()}
        onOpenSettings={onOpenSettings}
        onOpenAppearance={onOpenAppearance}
        onOpenKnowledge={onOpenKnowledge}
        onPruneEmptyChats={onPruneEmptyChats}
        onOpenMemory={vi.fn()}
        onExportChat={vi.fn()}
        onJumpToHit={vi.fn()}
      />,
    );

    expect(screen.getByRole("option", { name: /📌 置顶旁聊/ })).toBeInTheDocument();
    expect(screen.queryByRole("option", { name: /已归档旁聊/ })).toBeNull();

    fireEvent.click(screen.getByRole("option", { name: /用量统计/ }));
    expect(onOpenSettings).toHaveBeenCalledWith("usage");

    fireEvent.click(screen.getByRole("option", { name: /外观设置/ }));
    expect(onOpenAppearance).toHaveBeenCalledTimes(1);

    fireEvent.click(screen.getByRole("option", { name: /知识库/ }));
    expect(onOpenKnowledge).toHaveBeenCalledTimes(1);

    fireEvent.click(screen.getByRole("option", { name: /清理空旁聊/ }));
    expect(onPruneEmptyChats).toHaveBeenCalledTimes(1);
  });
});

describe("Wave 3.6: UsagePanel Cache Savings Card and 14-Day SVG Stacked Trend Chart", () => {
  it("renders cache hit/savings card and interactive 14-day stacked SVG trend chart", async () => {
    const daily: UsageDailyBucket[] = Array.from({ length: 14 }, (_, i) => ({
      date: `2026-11-${String(i + 14).padStart(2, "0")}`,
      input_tokens: (i + 1) * 10_000,
      cached_tokens: (i + 1) * 6_000,
      output_tokens: (i + 1) * 2_000,
      cost: Number(((i + 1) * 0.12).toFixed(2)),
      media_cost: i === 13 ? 0.5 : 0,
      requests: i + 2,
    }));

    vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(
      new Response(
        JSON.stringify({
          today: {
            by_model: [{ provider: "anthropic", model: "claude", preset: "pro", calls: 10, input_tokens: 140_000, cached_tokens: 84_000, output_tokens: 28_000, cost: 1.68 }],
            by_source: { user: 168_000 },
            input_tokens: 140_000,
            cached_tokens: 84_000,
            cost: 1.68,
          },
          week: {
            by_model: [],
            by_source: { user: 500_000 },
            input_tokens: 400_000,
            cached_tokens: 272_000,
            cost: 5.2,
          },
          daily,
          cache_hit_rate: 0.68,
          cache_saved_cost: 1.45,
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      ),
    );

    renderWithClient(<UsagePanel active />);

    const cacheCard = await screen.findByTestId("usage-cache-savings-card");
    expect(cacheCard).toHaveTextContent("68%");
    expect(cacheCard).toHaveTextContent("约 ¥1.45");

    const chart = screen.getByTestId("usage-daily-chart");
    expect(chart.querySelector("svg")).not.toBeNull();
    // Day 14 cost (1.68) already includes media_cost on backend; must NOT double-add 0.50 into 2.18.
    const latestDetail = screen.getByTestId("usage-day-detail");
    expect(latestDetail).toHaveTextContent("2026-11-27");
    expect(latestDetail).toHaveTextContent("约 ¥1.68");
    expect(latestDetail).not.toHaveTextContent("¥2.18");

    // Hover/click/focus/keyboard an earlier day bar to inspect exact breakdown
    const earlierBar = screen.getByTestId("usage-day-bar-2026-11-15");
    fireEvent.mouseEnter(earlierBar);
    fireEvent.focus(earlierBar);
    fireEvent.keyDown(earlierBar, { key: "Enter" });
    fireEvent.click(earlierBar);
    const detail = screen.getByTestId("usage-day-detail");
    expect(detail).toHaveTextContent("2026-11-15 · 3 次请求");
    expect(detail).toHaveTextContent("新输入 8000 · 缓存 1.2 万 · 输出 4000");

    // Zero-activity day bar (backend sends cost: 0.0), sub-cent savings, and empty today period with 14-day analytics
    vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(
      new Response(
        JSON.stringify({
          today: { by_model: [], by_source: {}, cost: null },
          week: { by_model: [], by_source: {}, cost: null },
          daily: [
            { date: "2026-11-26", input_tokens: 0, cached_tokens: 0, output_tokens: 0, cost: 0, media_cost: 0, requests: 0 },
            { date: "2026-11-27", input_tokens: 1000, cached_tokens: 500, output_tokens: 200, cost: 0.004, media_cost: 0, requests: 1 },
          ],
          cache_hit_rate: 0.5,
          cache_saved_cost: 0.004,
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      ),
    );
    cleanup();
    renderWithClient(<UsagePanel active />);
    expect(await screen.findByText("不到 1 分钱")).toBeInTheDocument();
    expect(screen.getByTestId("usage-day-detail")).toHaveTextContent("不到 1 分钱");
    expect(screen.getByText(/下方可查看近 14 天用量趋势/)).toBeInTheDocument();
    fireEvent.click(screen.getByTestId("usage-day-bar-2026-11-26"));
    expect(screen.getByTestId("usage-day-detail")).toHaveTextContent("2026-11-26 · 0 次请求");
    expect(screen.getByTestId("usage-day-detail")).not.toHaveTextContent("不到 1 分钱");

    // Standalone empty daily chart returns null cleanly
    const { container } = render(<DailyUsageChart daily={[]} />);
    expect(container.firstChild).toBeNull();
  });
});

describe("Wave 3.5b: Knowledge reader error/retry and CompanionScreen desktop Cmd+K trigger", () => {
  it("recovers from KnowledgeVault reader errors via retry and handles empty chunk list", async () => {
    const emptyDoc: KnowledgeDocument = {
      id: "doc-empty",
      title: "空白文档",
      filename: "empty.txt",
      collection: "默认",
      mime: "text/plain",
      size_bytes: 0,
      char_count: 0,
      chunk_count: 0,
      content_sha256: "sha-0",
      origin: "upload",
      created_at: Math.floor(Date.now() / 1000),
      updated_at: Math.floor(Date.now() / 1000),
    };
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({
          documents: [emptyDoc],
          collections: ["默认"],
          collection_counts: { 默认: 1 },
          stats: { doc_count: 1, chunk_count: 0, total_bytes: 0 },
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      ),
    );
    requestMutation
      .mockRejectedValueOnce(new Error("读取超时"))
      .mockResolvedValueOnce({ document: emptyDoc, chunks: [] });

    renderWithClient(<KnowledgeVault active />);
    fireEvent.click(await screen.findByRole("button", { name: "阅读「空白文档」全部章节分块" }));
    const sheet = await screen.findByTestId("knowledge-reader-sheet");
    expect(await within(sheet).findByRole("alert")).toHaveTextContent("读取分块失败：读取超时");

    fireEvent.click(within(sheet).getByRole("button", { name: "重试" }));
    expect(await within(sheet).findByText("该文档暂无可读分块")).toBeInTheDocument();
  });
});
