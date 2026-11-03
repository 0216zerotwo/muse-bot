import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { Goal } from "@/companion/companion-api";
import { GoalsScreen } from "@/companion/muse/GoalsScreen";

const session = { client: { requestMutation: vi.fn() }, token: "t" };
vi.mock("@/providers/ClientProvider", () => ({ useClient: () => session }));
vi.mock("@/lib/api", async (original) => ({
  ...(await original<typeof import("@/lib/api")>()),
  fetchAutomations: () => Promise.resolve({ jobs: [] }),
}));

const now = Date.now();
const goal: Goal = {
  id: "g1",
  title: "整理一份 MacBook 选购建议",
  steps: [
    { text: "列出候选机型", status: "done" },
    { text: "比较参数和价格", status: "doing" },
  ],
  checkin: null,
  due: null,
  done: false,
  category: "other",
  created_at: now - 3_600_000,
  next_checkin_ms: null,
  log: [
    { at: now - 3_000_000, kind: "step", text: "做完了第 1 步：列出候选机型", status: "done" },
    { at: now - 2_000_000, kind: "helper", text: "交给「比价」：比较 Air 15 和 Pro 14", helper: "比价", emoji: "🛒" },
    { at: now - 1_000_000, kind: "file", text: "MacBook 选购建议.md" },
  ],
};
vi.mock("@/companion/companion-api", async (original) => ({
  ...(await original<typeof import("@/companion/companion-api")>()),
  fetchGoals: () => Promise.resolve([goal]),
}));

afterEach(cleanup);

describe("a goal's work", () => {
  it("shows its record newest first, and that it is being worked on", async () => {
    const onOpenCreations = vi.fn();
    render(
      <GoalsScreen
        active
        name="Adam"
        approvals={[]}
        onDecide={vi.fn()}
        onSend={vi.fn()}
        working={{ label: "正在搜索", asked: "继续推进我的目标「整理一份 MacBook 选购建议」" }}
        computer={null}
        onOpenCreations={onOpenCreations}
      />,
    );
    fireEvent.click(await screen.findByText("整理一份 MacBook 选购建议"));
    expect(screen.getByText(/Adam正在推进 · 正在搜索/)).toBeInTheDocument();
    const rows = screen.getAllByRole("listitem").map((li) => li.textContent ?? "");
    const record = rows.filter((t) => /做出了文件|交给「比价」|做完了第 1 步/.test(t));
    expect(record[0]).toMatch(/做出了文件/);
    expect(record[2]).toMatch(/做完了第 1 步/);
    fireEvent.click(screen.getByRole("button", { name: /做出了文件：MacBook 选购建议\.md/ }));
    expect(onOpenCreations).toHaveBeenCalled();
  });
});
