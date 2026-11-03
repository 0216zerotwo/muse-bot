import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { Creation, CreationsPayload } from "@/companion/companion-api";
import { creationDay, CreationsView } from "@/companion/Creations";
import { FullScreen } from "@/companion/Screen";
import { Viewer } from "@/companion/Viewer";

const requestMutation = vi.fn();
vi.mock("@/providers/ClientProvider", () => ({ useClient: () => ({ client: { requestMutation }, token: "t" }) }));

const now = new Date();
const today = now.toISOString();
const earlier = new Date(now.getFullYear() - 1, 2, 5, 12).toISOString();

function creation(id: string, kind: Creation["kind"], title: string, created_at = today): Creation {
  return { id, kind, name: id.split("/")[1], title, origin: "generated", prompt: null, size: 2048, created_at, url: `/f/${id}` };
}

const payload: CreationsPayload = {
  items: [
    creation("图片/猫.png", "image", "窗边的猫"),
    creation("文档/行程.md", "document", "东京行程"),
    creation("视频/海.mp4", "video", "海浪", earlier),
  ],
  counts: { image: 1, video: 1, document: 1, audio: 0 },
};

vi.mock("@/companion/companion-api", async (original) => ({
  ...(await original<typeof import("@/companion/companion-api")>()),
  fetchCreations: vi.fn(async () => payload),
}));

afterEach(cleanup);

describe("creationDay", () => {
  it("names recent days and adds the year for older ones", () => {
    const at = new Date(2026, 9, 18, 15);
    expect(creationDay(new Date(2026, 9, 18, 9).toISOString(), at)).toBe("今天");
    expect(creationDay(new Date(2026, 9, 17, 23).toISOString(), at)).toBe("昨天");
    expect(creationDay(new Date(2026, 2, 5).toISOString(), at)).toBe("3月5日");
    expect(creationDay(new Date(2025, 2, 5).toISOString(), at)).toBe("2025年3月5日");
  });
});

describe("CreationsView", () => {
  it("files things by kind and day, with counts on the filters", async () => {
    render(<CreationsView active />);
    expect(await screen.findByRole("button", { name: "图片：窗边的猫" })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "全部 3" })).toHaveAttribute("aria-selected", "true");
    expect(screen.queryByRole("tab", { name: /音频/ })).toBeNull();
    expect(within(screen.getByRole("region", { name: "今天" })).getByText("东京行程")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("tab", { name: "文档 1" }));
    expect(screen.queryByRole("button", { name: "图片：窗边的猫" })).toBeNull();
    expect(screen.getByText("MD · 2 KB")).toBeInTheDocument();
  });

  it("removes an item from the list after confirming, keeping the file", async () => {
    requestMutation.mockResolvedValueOnce({ items: payload.items.slice(1), counts: { ...payload.counts, image: 0 } });
    render(<CreationsView active />);
    fireEvent.click(await screen.findByRole("button", { name: "图片：窗边的猫" }));

    const viewer = screen.getByRole("dialog", { name: "窗边的猫" });
    expect(within(viewer).getByRole("link", { name: "保存" })).toHaveAttribute("href", "/f/图片/猫.png?download=1");
    fireEvent.click(within(viewer).getByRole("button", { name: "移除" }));
    expect(within(viewer).getByText(/文件本身不会删除/)).toBeInTheDocument();
    fireEvent.click(within(viewer).getByRole("button", { name: "移除" }));

    await waitFor(() => expect(requestMutation).toHaveBeenCalledWith("companion.creations.hide", { id: "图片/猫.png" }));
    await waitFor(() => expect(screen.queryByRole("button", { name: "图片：窗边的猫" })).toBeNull());
  });
});

describe("back key", () => {
  it("closes only the topmost full-screen page", () => {
    const closeTasks = vi.fn();
    const closeViewer = vi.fn();
    render(
      <FullScreen title="任务" backLabel="返回聊天" onBack={closeTasks}>
        <Viewer
          items={[{ key: "a", kind: "image", url: "/a.png", name: "a.png", title: "图" }]}
          start={0}
          onClose={closeViewer}
        />
      </FullScreen>,
    );
    fireEvent.keyDown(document, { key: "Escape" });
    expect(closeViewer).toHaveBeenCalledTimes(1);
    expect(closeTasks).not.toHaveBeenCalled();
  });
});

describe("chat media", () => {
  it("opens a sent picture in the full-screen viewer", async () => {
    const { ChatThread } = await import("@/companion/ChatThread");
    const { chatItems } = await import("@/companion/presentation");
    const { ViewerProvider } = await import("@/companion/Viewer");
    const items = chatItems(
      [{ id: "m1", role: "assistant", content: "画好了", createdAt: 1, media: [{ kind: "image", url: "/api/media/s/p", name: "猫.png" }] }],
      1,
      [],
    );
    render(
      <ViewerProvider>
        <ChatThread items={items} typing={false} routes={new Map()} onDecide={async () => {}} />
      </ViewerProvider>,
    );
    fireEvent.click(screen.getByRole("img", { name: "猫.png" }));
    const viewer = screen.getByRole("dialog", { name: "猫.png" });
    expect(within(viewer).getByRole("link", { name: "保存" })).toHaveAttribute("href", "/api/media/s/p");
    expect(within(viewer).queryByRole("button", { name: "移除" })).toBeNull();
  });
});
