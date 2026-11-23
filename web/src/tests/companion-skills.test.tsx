import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { SkillsPayload } from "@/companion/companion-api";
import { Composer } from "@/companion/Composer";
import { SkillsPanel } from "@/companion/SkillsPanel";
import { stepText, TeachSheet } from "@/companion/muse/TeachSheet";

const requestMutation = vi.fn();
// One client for the whole test, as the app has.
const session = { client: { requestMutation }, token: "t" };
vi.mock("@/providers/ClientProvider", () => ({ useClient: () => session }));

const skills: SkillsPayload = {
  learned: [{ name: "market-data-comparison", description: "比较几天里的指数表现", category: "productivity", updated_at: Date.now() }],
  bundled: [{ name: "arxiv", description: "Search arXiv papers", category: "research", updated_at: 0 }],
};
vi.mock("@/companion/companion-api", async (original) => ({
  ...(await original<typeof import("@/companion/companion-api")>()),
  fetchSkills: () => Promise.resolve(skills),
  fetchHelpers: () => Promise.resolve({ helpers: [], templates: [], max: 12 }),
}));

beforeEach(() => requestMutation.mockReset());
afterEach(cleanup);

describe("SkillsPanel", () => {
  it("lists what it learned, folds the bundled ones, and starts a message with a skill", async () => {
    requestMutation.mockResolvedValue({ name: "market-data-comparison", content: "---\nname: market-data-comparison\n---\n## 步骤\n1. 查指数", bundled: false });
    const onDraft = vi.fn();
    render(<SkillsPanel onDraft={onDraft} onTeach={vi.fn()} />);
    expect(await screen.findByText("market data comparison")).toBeInTheDocument();
    expect(screen.queryByText("arxiv")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /自带的 1 个/ }));
    expect(screen.getByText("arxiv")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /market data comparison/ }));
    expect(requestMutation).toHaveBeenCalledWith("companion.skills.change", { action: "read", name: "market-data-comparison" });
    expect(await screen.findByRole("button", { name: "编辑步骤" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "用一次" }));
    expect(onDraft).toHaveBeenCalledWith("用「market-data-comparison」技能：");
  });

  it("opens teaching from the top", async () => {
    const onTeach = vi.fn();
    render(<SkillsPanel onDraft={vi.fn()} onTeach={onTeach} />);
    await screen.findByText("market data comparison");
    fireEvent.click(screen.getByRole("button", { name: /教它一个新技能/ }));
    expect(onTeach).toHaveBeenCalled();
  });
});

describe("TeachSheet", () => {
  const steps = [
    { do: "open" as const, url: "https://kyfw.12306.cn" },
    { do: "click" as const, target: "按钮「查询」" },
    { do: "type" as const, target: "输入框「密码」", text: "（密码，不记录）" },
  ];

  it("words the steps", () => {
    expect(steps.map(stepText)).toEqual(["打开 https://kyfw.12306.cn", "点 按钮「查询」", "在输入框「密码」里输入「（密码，不记录）」"]);
  });

  it("turns the steps into a skill to read before saving it", async () => {
    requestMutation
      .mockResolvedValueOnce({ name: "check-train", content: "---\nname: check-train\n---\n## 步骤" })
      .mockResolvedValueOnce(skills);
    render(<TeachSheet steps={steps} name="Adam" onClose={vi.fn()} />);
    fireEvent.change(screen.getByRole("textbox", { name: "这是在做什么" }), { target: { value: "查高铁" } });
    fireEvent.click(screen.getByRole("button", { name: "整理成技能" }));
    expect(requestMutation).toHaveBeenCalledWith("companion.skills.change", { action: "draft", goal: "查高铁", steps }, 180_000);
    expect(await screen.findByRole("textbox", { name: "技能名" })).toHaveValue("check-train");
    fireEvent.click(screen.getByRole("button", { name: "保存技能" }));
    expect(await screen.findByText(/学会了「check-train」/)).toBeInTheDocument();
    expect(requestMutation).toHaveBeenLastCalledWith("companion.skills.change", expect.objectContaining({ action: "save", name: "check-train", new: true }));
  });
});

describe("Composer / presets", () => {
  const composer = (value: string, extra: Record<string, unknown> = {}) => {
    const onChange = vi.fn();
    render(
      <Composer value={value} onChange={onChange} onSend={vi.fn()} onStop={vi.fn()} streaming={false} connected transcribeAudio={vi.fn()} onFocusChange={vi.fn()} {...extra} />,
    );
    return onChange;
  };

  it("lists ways of working after a leading /, and fills the picked one in", async () => {
    const onChange = composer("/");
    await screen.findByRole("button", { name: /market-data-comparison/ });
    expect(screen.getByRole("button", { name: /长任务/ })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /整理上下文/ }));
    expect(onChange).toHaveBeenCalledWith("/compact");
  });

  it("narrows by what was typed", async () => {
    const onChange = composer("/go");
    await act(async () => {});
    expect(screen.queryByRole("button", { name: /整理上下文/ })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /长任务/ }));
    expect(onChange).toHaveBeenCalledWith("/goal ");
  });

  it("shows what is being quoted, and drops it on request", () => {
    const onClearQuote = vi.fn();
    composer("", { quote: "周六去阳明山", onClearQuote });
    expect(screen.getByText("周六去阳明山")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "不引用了" }));
    expect(onClearQuote).toHaveBeenCalled();
  });
});
