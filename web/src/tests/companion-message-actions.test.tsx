import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ChatThread } from "@/companion/ChatThread";
import { chatItems } from "@/companion/presentation";
import type { UIMessage } from "@/lib/types";
import { formatQuotedUserMessage } from "@/lib/user-message-quote";

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

const messages: UIMessage[] = [
  { id: "u", role: "user", content: formatQuotedUserMessage("那就周六吧", "周末去阳明山"), turnId: "t1", createdAt: 1 },
  { id: "a", role: "assistant", content: "好，周六去。", turnId: "t1", createdAt: 2 },
];

function thread(onMessageAction = vi.fn()) {
  render(
    <ChatThread items={chatItems(messages, 3)} typing={false} routes={new Map()} onDecide={vi.fn()} onMessageAction={onMessageAction} />,
  );
  return onMessageAction;
}

describe("long-press on a message", () => {
  it("shows a quoted message's quote above it", () => {
    thread();
    expect(screen.getByText("周末去阳明山")).toBeInTheDocument();
    expect(screen.getByText("那就周六吧")).toBeInTheDocument();
  });

  it("offers 引用 / 复制 / 删除 after holding, and says which message", () => {
    vi.useFakeTimers();
    const onAction = thread();
    fireEvent.pointerDown(screen.getByText("好，周六去。"), { button: 0, clientX: 10, clientY: 10 });
    act(() => vi.advanceTimersByTime(500));
    fireEvent.click(screen.getByRole("menuitem", { name: /删除/ }));
    expect(onAction).toHaveBeenCalledWith("delete", expect.objectContaining({ id: "a" }), "reply");
    expect(screen.queryByRole("menu")).not.toBeInTheDocument();
  });

  it("a scroll is not a hold", () => {
    vi.useFakeTimers();
    thread();
    const bubble = screen.getByText("好，周六去。");
    fireEvent.pointerDown(bubble, { button: 0, clientX: 10, clientY: 10 });
    fireEvent.pointerMove(bubble, { clientX: 10, clientY: 60 });
    act(() => vi.advanceTimersByTime(500));
    expect(screen.queryByRole("menu")).not.toBeInTheDocument();
  });
});

describe("a long reply", () => {
  it("shows in full, nothing folded behind 展开全文, and no list of what it did above it", () => {
    const long = Array.from({ length: 8 }, (_, i) => `第 ${i + 1} 段：${"内容".repeat(40)}`).join("\n\n");
    const reply: UIMessage[] = [
      { id: "u", role: "user", content: "详细说说", turnId: "t2", createdAt: 1 },
      {
        id: "w", role: "assistant", content: "", kind: "trace", turnId: "t2", createdAt: 2,
        toolEvents: [{ call_id: "q1", name: "web_search", phase: "end", arguments: { query: "x" } }],
      } as UIMessage,
      { id: "a", role: "assistant", content: long, turnId: "t2", createdAt: 3 },
    ];
    render(<ChatThread items={chatItems(reply, 4)} typing={false} routes={new Map()} onDecide={vi.fn()} />);
    expect(screen.getByText(/第 8 段/)).toBeInTheDocument();
    expect(screen.queryByText(/展开全文/)).not.toBeInTheDocument();
    expect(screen.queryByRole("list", { name: "它做了这些" })).not.toBeInTheDocument();
  });
});

describe("the companion's line after a helper's answer", () => {
  it("is named, so it isn't read as the helper's", () => {
    const thread: UIMessage[] = [
      { id: "u", role: "user", content: "@翻译 good night", turnId: "t3", createdAt: 1 },
      { id: "h", role: "assistant", content: "晚安。", source: { kind: "helper", label: "🌐 翻译", id: "x" }, createdAt: 2 } as UIMessage,
      { id: "a", role: "assistant", content: "两种说法都行。", turnId: "t3", createdAt: 3 },
    ];
    render(<ChatThread items={chatItems(thread, 4)} typing={false} routes={new Map()} onDecide={vi.fn()} companionName="小鲸" />);
    expect(screen.getByText("小鲸")).toBeInTheDocument();
    expect(screen.getByText("翻译")).toBeInTheDocument();
  });
});

describe("a card's question", () => {
  it("stays open when a reminder lands after it", () => {
    const card = "明天吃什么？\n\n```card\n" + JSON.stringify({ type: "choices", options: ["番茄牛腩饭", "香菇滑鸡饭"] }) + "\n```";
    const thread: UIMessage[] = [
      { id: "u", role: "user", content: "明天中午吃什么", turnId: "t4", createdAt: 1 },
      { id: "a", role: "assistant", content: card, turnId: "t4", createdAt: 2 },
      { id: "r", role: "assistant", content: "该喝水啦", source: { kind: "cron", label: "提醒喝水" }, createdAt: 3 } as UIMessage,
    ];
    const onSend = vi.fn();
    render(<ChatThread items={chatItems(thread, 4)} typing={false} routes={new Map()} onDecide={vi.fn()} onSend={onSend} />);
    fireEvent.click(screen.getByRole("button", { name: "香菇滑鸡饭" }));
    expect(onSend).toHaveBeenCalledWith("香菇滑鸡饭");
  });
});
