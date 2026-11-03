import { act, cleanup, render, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { ApprovalRecord, CustomAvatar } from "@/companion/companion-api";
import { PetSourceView, resolvePet } from "@/companion/Pet";
import { CELL_HEIGHT, CELL_WIDTH, moodAnimation, ROWS, rowDuration } from "@/companion/petAnimations";
import { usePetMood } from "@/companion/usePetMood";

vi.mock("@/providers/ClientProvider", () => ({ useClient: () => ({ client: {}, token: "t" }) }));

/* Sheets "load" unless their URL says missing. */
class FakeImage {
  onload: (() => void) | null = null;
  onerror: (() => void) | null = null;
  set src(url: string) {
    queueMicrotask(() => (url.includes("missing") ? this.onerror?.() : this.onload?.()));
  }
}

beforeEach(() => {
  vi.stubGlobal("Image", FakeImage);
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

const noon = new Date(2026, 9, 19, 12);
/** CSS offset of a frame in the atlas, as the browser normalises it. */
const at = (column: number, row: number) => `${column ? -column * CELL_WIDTH : 0}px ${row ? -row * CELL_HEIGHT : 0}px`;
const midnight = new Date(2026, 9, 19, 23, 30);

describe("moodAnimation", () => {
  const base = { connected: true, activity: null, pendingApprovals: 0, idleForMs: 0, now: noon };

  it("acts out the errand in progress", () => {
    expect(moodAnimation({ ...base, activity: "run" })).toBe("typing");
    expect(moodAnimation({ ...base, activity: "files" })).toBe("typing");
    expect(moodAnimation({ ...base, activity: "read" })).toBe("reading");
    expect(moodAnimation({ ...base, activity: "search" })).toBe("searching");
    expect(moodAnimation({ ...base, activity: "image" })).toBe("drawing");
    expect(moodAnimation({ ...base, activity: "send" })).toBe("writing");
    expect(moodAnimation({ ...base, activity: "thinking" })).toBe("review");
  });

  it("waves for approvals, sleeps offline or late at night once left alone", () => {
    expect(moodAnimation({ ...base, pendingApprovals: 1 })).toBe("waving");
    expect(moodAnimation({ ...base, connected: false })).toBe("sleeping");
    expect(moodAnimation({ ...base, now: midnight, idleForMs: 10_000 })).toBe("idle");
    expect(moodAnimation({ ...base, now: midnight, idleForMs: 3 * 60_000 })).toBe("sleeping");
    expect(moodAnimation({ ...base, now: noon, idleForMs: 3 * 60_000 })).toBe("idle");
  });

  it("times one pass of a row from its frame durations", () => {
    expect(rowDuration("waving")).toBe(140 * 3 + 280);
  });
});

describe("resolvePet", () => {
  const library: CustomAvatar[] = [
    { id: "aaaaaaaaaaaa", kind: "pet", name: "Zero", created_at: "", image_url: null, sheet_url: "/s.webp", actions_url: null },
    { id: "bbbbbbbbbbbb", kind: "image", name: "我的猫", created_at: "", image_url: "/i.webp", sheet_url: null, actions_url: null },
  ];

  it("maps every kind of avatar value", () => {
    expect(resolvePet(null, null)).toMatchObject({ type: "sprite", name: "橘猫" });
    expect(resolvePet("🐱", null)).toMatchObject({ type: "sprite", name: "橘猫" });
    expect(resolvePet("mochi:peach", null)).toEqual({ type: "mochi", variant: "peach" });
    expect(resolvePet("custom:aaaaaaaaaaaa", library)).toEqual({ type: "sprite", name: "Zero", sheet: "/s.webp", actions: null });
    expect(resolvePet("image:bbbbbbbbbbbb", library)).toEqual({ type: "image", name: "我的猫", url: "/i.webp" });
    expect(resolvePet("🐶", null)).toEqual({ type: "emoji", value: "🐶" });
  });

  it("shows the default pet for looks that are gone, and nothing for one not loaded yet", () => {
    expect(resolvePet("custom:cccccccccccc", library)).toMatchObject({ type: "sprite", name: "橘猫" });
    expect(resolvePet("image:bbbbbbbbbbbb", null)).toEqual({ type: "blank" });
    expect(resolvePet("pet:unknown", null)).toMatchObject({ type: "sprite", name: "橘猫" });
  });
});

describe("PetSourceView", () => {
  const sprite = (actions: string | null) => ({ type: "sprite" as const, name: "猫", sheet: "/sheet.webp", actions });
  const frame = (container: HTMLElement) => container.querySelector<HTMLElement>("[style*='background-image']");

  it("plays errands from the errands sheet", async () => {
    const { container } = render(<PetSourceView source={sprite("/actions.webp")} animation="typing" size={CELL_WIDTH} />);
    await act(async () => {});
    expect(frame(container)?.style.backgroundImage).toContain("/actions.webp");
    expect(frame(container)?.style.backgroundPosition).toBe(at(0, ROWS.typing.row));
    expect(container.querySelector("svg")).toBeNull();
  });

  it("falls back to the nearest base row with a badge when a pet has no errands", async () => {
    const { container } = render(<PetSourceView source={sprite("/missing.webp")} animation="reading" size={CELL_WIDTH} />);
    await act(async () => {});
    expect(frame(container)?.style.backgroundImage).toContain("/sheet.webp");
    expect(frame(container)?.style.backgroundPosition).toBe(at(0, ROWS.review.row));
    expect(container.querySelector("svg")).not.toBeNull();
  });

  it("steps through frames and reports the end of a one-off reaction", async () => {
    vi.useFakeTimers();
    const onDone = vi.fn();
    const { container } = render(
      <PetSourceView source={sprite(null)} animation="waving" size={CELL_WIDTH} loop={false} onDone={onDone} />,
    );
    await act(async () => {}); // the sheet loads
    act(() => void vi.advanceTimersByTime(140));
    expect(frame(container)?.style.backgroundPosition).toBe(at(1, ROWS.waving.row));
    act(() => void vi.advanceTimersByTime(rowDuration("waving")));
    expect(onDone).toHaveBeenCalledTimes(1);
  });
});

describe("usePetMood", () => {
  const approval = (status: ApprovalRecord["status"]) => ({ id: "a1", status }) as ApprovalRecord;

  it("waves hello, then idles; slumps on failure and jumps when an approval goes through", () => {
    const { result, rerender } = renderHook((props: { failed: boolean; approvals: ApprovalRecord[] }) =>
      usePetMood({ connected: true, activity: null, approvals: props.approvals, failed: props.failed }),
    { initialProps: { failed: false, approvals: [approval("pending")] } });

    expect(result.current).toMatchObject({ animation: "waving", loop: false });
    act(() => result.current.onDone());
    expect(result.current).toMatchObject({ animation: "waving", loop: true }); // still waiting for the approval

    rerender({ failed: false, approvals: [approval("executed")] });
    expect(result.current).toMatchObject({ animation: "jumping", loop: false });
    act(() => result.current.onDone());

    rerender({ failed: true, approvals: [approval("executed")] });
    expect(result.current).toMatchObject({ animation: "failed", loop: false });
  });

  it("lets work in progress win over an idle glance", () => {
    const { result, rerender } = renderHook((props: { activity: "run" | null }) =>
      usePetMood({ connected: true, activity: props.activity, approvals: [], failed: false }),
    { initialProps: { activity: null as "run" | null } });
    act(() => result.current.react("waiting"));
    rerender({ activity: "run" });
    expect(result.current).toMatchObject({ animation: "typing", loop: true });
  });
});
