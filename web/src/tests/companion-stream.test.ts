import { describe, expect, it } from "vitest";

import { projectThreadEvents } from "@/lib/thread-event-projection";
import type { ThreadProjectionEvent } from "@/lib/types";

const turn = { chat_id: "c", turn_id: "t1", turn_phase: "answer" } as const;
const answers = (events: ThreadProjectionEvent[]) =>
  projectThreadEvents(events).filter((m) => m.role === "assistant").map((m) => m.content);

describe("a reply rewritten while it streams (as the companion server sends it)", () => {
  it("stays one bubble: the new text replaces the old, and more text follows it", () => {
    expect(
      answers([
        { event: "user_message", chat_id: "c", turn_id: "t1", turn_phase: "user", text: "找 modelId" } as unknown as ThreadProjectionEvent,
        { ...turn, event: "delta", stream_id: "s1", text: "我找到了：modelId 是 fe50" },
        { ...turn, event: "stream_end", stream_id: "s1", text: "我找到了：它的 modelId 是 fe50cd", resuming: true, merge_next: true },
        { ...turn, event: "delta", stream_id: "s1", text: "f3040449" },
        { ...turn, event: "stream_end", stream_id: "s1" },
      ] as ThreadProjectionEvent[]),
    ).toEqual(["我找到了：它的 modelId 是 fe50cdf3040449"]);
  });

  it("takes a final text unlike the stream as the bubble's text", () => {
    expect(
      answers([
        { event: "user_message", chat_id: "c", turn_id: "t1", turn_phase: "user", text: "找 modelId" } as unknown as ThreadProjectionEvent,
        { ...turn, event: "delta", stream_id: "s1", text: "我先看看 Fish Audio 上" },
        { ...turn, event: "stream_end", stream_id: "s1", text: "找到了：fe50cdf3040449" },
      ] as ThreadProjectionEvent[]),
    ).toEqual(["找到了：fe50cdf3040449"]);
  });
});
