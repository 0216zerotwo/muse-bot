import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { AppearanceDock } from "@/companion/AppearanceDock";
import { MuseSheet } from "@/companion/muse/Sheet";
import { PageSheet, useEdgeSwipe } from "@/companion/Screen";

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

const at = (y: number) => ({ touches: [{ clientX: 100, clientY: y }] });

function pull(el: Element, from: number, to: number) {
  fireEvent.touchStart(el, at(from));
  for (let y = from + 10; y <= to; y += 20) fireEvent.touchMove(el, at(y));
  fireEvent.touchEnd(el);
}

describe("pulling a sheet down", () => {
  it("closes it past a short distance, and springs back short of it", () => {
    vi.useFakeTimers();
    const onClose = vi.fn();
    render(
      <PageSheet title="设置" onBack={onClose} onClose={onClose}>
        <p>内容</p>
      </PageSheet>,
    );
    const sheet = screen.getByRole("dialog", { name: "设置" });
    pull(screen.getByText("内容"), 300, 340);
    act(() => void vi.advanceTimersByTime(400));
    expect(onClose).not.toHaveBeenCalled();
    expect(sheet.style.transform).toBe("");

    pull(screen.getByText("内容"), 300, 520);
    act(() => void vi.advanceTimersByTime(400));
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("scrolls content that is scrolled down instead of closing", () => {
    vi.useFakeTimers();
    const onClose = vi.fn();
    render(
      <MuseSheet open onClose={onClose} title="伙伴">
        <div data-testid="list">长列表</div>
      </MuseSheet>,
    );
    const scroller = screen.getByTestId("list").parentElement!;
    Object.defineProperty(scroller, "scrollHeight", { value: 2000 });
    Object.defineProperty(scroller, "clientHeight", { value: 500 });
    scroller.scrollTop = 200;
    pull(screen.getByTestId("list"), 300, 600);
    act(() => void vi.advanceTimersByTime(400));
    expect(onClose).not.toHaveBeenCalled();
  });

  it("closes 设置 with a tap above it", () => {
    const onClose = vi.fn();
    const { container } = render(
      <PageSheet title="设置" onBack={() => {}} onClose={onClose}>
        <p>内容</p>
      </PageSheet>,
    );
    fireEvent.click(container.firstElementChild!.firstElementChild!);
    expect(onClose).toHaveBeenCalled();
  });

  it("closes 外观 with a tap anywhere else, without shading the chat", () => {
    const onDone = vi.fn();
    render(<AppearanceDock onDone={onDone} />);
    const outside = screen.getByTestId("appearance-outside");
    expect(outside.className).not.toMatch(/bg-/);
    fireEvent.click(screen.getByRole("radio", { name: "深色" }));
    expect(onDone).not.toHaveBeenCalled();
    fireEvent.click(outside);
    expect(onDone).toHaveBeenCalled();
  });
});

describe("a swipe in from the left edge", () => {
  const edgeSwipe = (fromX = 8) => {
    fireEvent.touchStart(document, { touches: [{ clientX: fromX, clientY: 400 }] });
    fireEvent.touchEnd(document, { changedTouches: [{ clientX: fromX + 160, clientY: 410 }] });
  };

  it("goes back from the page on top, and only from the very edge", () => {
    const onBack = vi.fn();
    render(
      <PageSheet title="设置" onBack={onBack} onClose={() => {}}>
        <p>内容</p>
      </PageSheet>,
    );
    edgeSwipe(120); // mid-screen: someone else's swipe
    expect(onBack).not.toHaveBeenCalled();
    edgeSwipe();
    expect(onBack).toHaveBeenCalledTimes(1);
  });

  it("opens the side menu on the main screen, but not under a page or a sheet", () => {
    const openMenu = vi.fn();
    function Main({ page, sheet }: { page?: boolean; sheet?: boolean }) {
      useEdgeSwipe(openMenu, true);
      return (
        <>
          {page && (
            <PageSheet title="设置" onBack={() => {}} onClose={() => {}}>
              <p>内容</p>
            </PageSheet>
          )}
          <MuseSheet open={!!sheet} onClose={() => {}}>
            <p>卡片</p>
          </MuseSheet>
        </>
      );
    }
    const { rerender } = render(<Main />);
    edgeSwipe();
    expect(openMenu).toHaveBeenCalledTimes(1);
    rerender(<Main page />);
    edgeSwipe();
    rerender(<Main sheet />);
    edgeSwipe();
    expect(openMenu).toHaveBeenCalledTimes(1);
  });
});
