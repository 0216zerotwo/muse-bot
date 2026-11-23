import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { FeedPayload } from "@/companion/companion-api";
import { MuseDrawer } from "@/companion/muse/Drawer";
import { FeedScreen } from "@/companion/muse/Feed";
import type { ChatSummary } from "@/lib/types";

const requestMutation = vi.fn();
vi.mock("@/providers/ClientProvider", () => ({ useClient: () => ({ client: { requestMutation }, token: "t" }) }));

afterEach(() => {
  cleanup();
  localStorage.clear();
  vi.useRealTimers();
});

function chat(chatId: string, title: string, preview = ""): ChatSummary {
  return { key: `websocket:${chatId}`, channel: "websocket", chatId, createdAt: null, updatedAt: null, title, preview };
}

function drawer(overrides: Partial<Parameters<typeof MuseDrawer>[0]> = {}) {
  const props = {
    open: true,
    onClose: vi.fn(),
    name: "Adam",
    current: "chat" as const,
    activeChatId: null,
    order: ["chat", "feed", "ideas", "goals", "creations"] as Parameters<typeof MuseDrawer>[0]["order"],
    onReorder: vi.fn(),
    onTab: vi.fn(),
    sideChats: [chat("a", "饮食记录"), chat("b", "", "最近股票怎么样")],
    onOpenChat: vi.fn(),
    onNewChat: vi.fn(),
    onOpenSettings: vi.fn(),
    onEditChat: vi.fn(),
    onDeleteChat: vi.fn(),
    ...overrides,
  };
  render(<MuseDrawer {...props} />);
  return props;
}

describe("MuseDrawer", () => {
  it("finds messages in any chat once typing pauses, and opens the one tapped", async () => {
    vi.useFakeTimers();
    const hit = { chat_id: "main", chat_title: "主要聊天", role: "assistant" as const, text: "以后推荐菜都避开香菜。", turn_id: "t1", at_ms: 1 };
    const search = vi.fn(async () => [hit]);
    const props = drawer({ search, onOpenHit: vi.fn() });
    fireEvent.change(screen.getByRole("textbox", { name: "搜索旁聊和聊天记录" }), { target: { value: "香菜" } });
    expect(search).not.toHaveBeenCalled();
    await act(async () => {
      vi.advanceTimersByTime(400);
    });
    expect(search).toHaveBeenCalledWith("香菜");
    expect(screen.getByText("聊天记录")).toBeInTheDocument();
    expect(screen.getByText("香菜").tagName).toBe("B");
    fireEvent.click(screen.getByRole("button", { name: /以后推荐菜都避开/ }));
    expect(props.onOpenHit).toHaveBeenCalledWith(hit);
    expect(props.onClose).toHaveBeenCalled();
  });

  it("closes when swiped back to the left, but not from a side chat (its own swipe shows 删除)", () => {
    vi.useFakeTimers();
    const props = drawer();
    const swipe = (el: Element) => {
      const at = (x: number) => ({ touches: [{ clientX: x, clientY: 400 }] });
      fireEvent.touchStart(el, at(300));
      for (let x = 290; x >= 120; x -= 20) fireEvent.touchMove(el, at(x));
      fireEvent.touchEnd(el);
      act(() => void vi.advanceTimersByTime(400));
    };
    swipe(screen.getByRole("button", { name: "饮食记录" }));
    expect(props.onClose).not.toHaveBeenCalled();
    swipe(screen.getByRole("heading", { name: "Adam" }));
    expect(props.onClose).toHaveBeenCalledTimes(1);
  });

  it("lists the tabs and the side chats under the companion's name", () => {
    drawer();
    expect(screen.getByRole("heading", { name: "Adam" })).toBeInTheDocument();
    for (const label of ["主要聊天", "动态", "点子", "目标", "资源库"]) expect(screen.getByRole("button", { name: label })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "饮食记录" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "最近股票怎么样" })).toBeInTheDocument();
  });

  it("opens a tab or a side chat on a tap", () => {
    const props = drawer();
    fireEvent.click(screen.getByRole("button", { name: "动态" }));
    expect(props.onTab).toHaveBeenCalledWith("feed");
    fireEvent.click(screen.getByRole("button", { name: "饮食记录" }));
    expect(props.onOpenChat).toHaveBeenCalledWith("a");
  });

  it("searches the side chats, and has settings and a new side chat at the bottom", () => {
    const props = drawer();
    fireEvent.change(screen.getByRole("textbox", { name: "搜索旁聊和聊天记录" }), { target: { value: "股票" } });
    expect(screen.queryByRole("button", { name: "饮食记录" })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "新开旁聊" }));
    expect(props.onNewChat).toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "设置" }));
    expect(props.onOpenSettings).toHaveBeenCalled();
  });

  it("deletes a side chat after swiping it left", () => {
    const props = drawer();
    const row = screen.getByRole("button", { name: "饮食记录" });
    fireEvent.pointerDown(row, { clientX: 200, clientY: 10 });
    fireEvent.pointerMove(row, { clientX: 100, clientY: 12 });
    fireEvent.pointerUp(row, { clientX: 100, clientY: 12 });
    fireEvent.click(row); // the click that ends a swipe does not open the chat
    expect(props.onOpenChat).not.toHaveBeenCalled();
    fireEvent.click(screen.getAllByRole("button", { name: "删除" })[0]);
    expect(props.onDeleteChat).toHaveBeenCalledWith("a");
  });

  it("opens a side chat's settings after a long press", () => {
    vi.useFakeTimers();
    const props = drawer();
    fireEvent.pointerDown(screen.getByRole("button", { name: "饮食记录" }));
    act(() => vi.advanceTimersByTime(500));
    expect(props.onEditChat).toHaveBeenCalledWith(expect.objectContaining({ chatId: "a" }));
  });

});

describe("FeedScreen", () => {
  const data: FeedPayload = {
    instructions: "为我打造一个关于我兴趣的动态版块。",
    daily: true,
    time: "08:00",
    generated_at: 2,
    posts: [{ id: "p1", ts: Date.now(), title: "新显卡发布", body: "详见[官网](https://example.com)。", area: "tech", image: null, prompt: "值得买吗？" }],
  };

  it("shows the steering sentence and the posts, and discusses one", async () => {
    const onDiscuss = vi.fn();
    const onEdit = vi.fn();
    render(<FeedScreen data={data} writing={false} onEditInstructions={onEdit} onWriteNow={vi.fn()} onDiscuss={onDiscuss} onChange={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: data.instructions }));
    expect(onEdit).toHaveBeenCalled();
    expect(screen.getByText("新显卡发布")).toBeInTheDocument();
    // The link appears once the markdown renderer's chunk has loaded: slow when every test runs at once.
    expect(await screen.findByRole("link", { name: "官网" }, { timeout: 15_000 })).toHaveAttribute("href", "https://example.com");
    fireEvent.click(screen.getByRole("button", { name: /讨论/ }));
    expect(onDiscuss).toHaveBeenCalledWith(data.posts[0]);
  }, 20_000);

  it("keeps a heart on the server", async () => {
    requestMutation.mockResolvedValueOnce({ ...data, liked: ["p1"] });
    const onChange = vi.fn();
    render(<FeedScreen data={data} writing={false} onEditInstructions={vi.fn()} onWriteNow={vi.fn()} onDiscuss={vi.fn()} onChange={onChange} />);
    await screen.findByRole("link", { name: "官网" }, { timeout: 15_000 });
    fireEvent.click(screen.getByRole("button", { name: "喜欢" }));
    expect(requestMutation).toHaveBeenCalledWith("companion.feed.change", { action: "like", id: "p1", on: true });
    await waitFor(() => expect(onChange).toHaveBeenCalledWith(expect.objectContaining({ liked: ["p1"] })));
  });

  it("offers to write one now when empty", () => {
    const onWriteNow = vi.fn();
    render(<FeedScreen data={{ ...data, posts: [] }} writing={false} onEditInstructions={vi.fn()} onWriteNow={onWriteNow} onDiscuss={vi.fn()} onChange={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "现在就写一期" }));
    expect(onWriteNow).toHaveBeenCalled();
  });
});
