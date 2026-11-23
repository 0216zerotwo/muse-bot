import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { Helper, HelpersPayload } from "@/companion/companion-api";
import { Composer } from "@/companion/Composer";
import { chatTime, GroupSheet, HelperProfileSheet, HelpersScreen } from "@/companion/muse/Helpers";
import { MessengerEmpty } from "@/companion/muse/Messenger";

const requestMutation = vi.fn();
const session = { client: { requestMutation }, token: "t" };
vi.mock("@/providers/ClientProvider", () => ({ useClient: () => session }));

const fetchHelpers = vi.fn();
vi.mock("@/companion/companion-api", async (original) => ({
  ...(await original<typeof import("@/companion/companion-api")>()),
  fetchHelpers: (...args: unknown[]) => fetchHelpers(...args),
  fetchModels: () => Promise.resolve({ presets: [{ name: "glm" }, { name: "deepseek-flash" }] }),
  fetchTriggers: () => Promise.resolve([]),
}));

const fetchAutomations = vi.fn();
vi.mock("@/lib/api", async (original) => ({
  ...(await original<typeof import("@/lib/api")>()),
  fetchAutomations: (...args: unknown[]) => fetchAutomations(...args),
}));

const template = { name: "比价", emoji: "🛒", title: "买东西前比较几个选项", instructions: "只比较，不下单" };

function helper(partial: Partial<Helper> = {}): Helper {
  return {
    id: "h1", name: "记账", emoji: "💰", avatar: "", title: "记录和统计花销", instructions: "记下每笔收支", chat_id: "c1", notes: [],
    model: "", preset: "", is_default: false, setup: false, working: false, work: [], habits: [], last: null, created_at: 1, ...partial,
  };
}

function payload(helpers: Helper[], extra: Partial<HelpersPayload> = {}): HelpersPayload {
  return { helpers, templates: [template], max: 12, groups: [], max_group: 6, ...extra };
}

beforeEach(() => {
  requestMutation.mockReset();
  fetchHelpers.mockReset();
  fetchAutomations.mockReset();
  fetchAutomations.mockResolvedValue({ jobs: [] });
});
afterEach(cleanup);

describe("HelpersScreen", () => {
  it("opens a helper's chat when it is tapped", async () => {
    fetchHelpers.mockResolvedValue(payload([helper({ working: true })]));
    const onOpenChat = vi.fn();
    render(<HelpersScreen active onOpenChat={onOpenChat} />);
    fireEvent.click(await screen.findByRole("button", { name: /记账.*在做/ }));
    expect(onOpenChat).toHaveBeenCalledWith("c1");
  });

  it("makes a new helper to set up by talking, and opens its chat", async () => {
    fetchHelpers.mockResolvedValue(payload([]));
    requestMutation.mockResolvedValue(payload([helper({ name: "新帮手", setup: true, chat_id: "c9" })], { opened: "c9" }));
    const onOpenChat = vi.fn();
    render(<HelpersScreen active onOpenChat={onOpenChat} />);
    fireEvent.click(await screen.findByRole("button", { name: "新建" }));
    fireEvent.click(screen.getByRole("menuitem", { name: "新建帮手" }));
    fireEvent.click(screen.getByRole("button", { name: /聊着设定一个新帮手/ }));
    await waitFor(() => expect(onOpenChat).toHaveBeenCalledWith("c9"));
    expect(requestMutation).toHaveBeenCalledWith("companion.helpers.change", { action: "blank", idea: "" });
  });

  it("from a name alone, has it work out the rest", async () => {
    fetchHelpers.mockResolvedValue(payload([]));
    requestMutation.mockResolvedValue(payload([helper({ name: "邮件分拣", setup: true, chat_id: "c8" })], { opened: "c8" }));
    const onOpenChat = vi.fn();
    render(<HelpersScreen active onOpenChat={onOpenChat} />);
    fireEvent.click(await screen.findByRole("button", { name: "新建" }));
    fireEvent.click(screen.getByRole("menuitem", { name: "新建帮手" }));
    fireEvent.change(screen.getByRole("textbox", { name: "新帮手负责什么" }), { target: { value: " 邮件分拣 " } });
    fireEvent.click(screen.getByRole("button", { name: /建好，让它出个方案/ }));
    await waitFor(() => expect(onOpenChat).toHaveBeenCalledWith("c8"));
    expect(requestMutation).toHaveBeenCalledWith("companion.helpers.change", { action: "blank", idea: "邮件分拣" });
  });

  it("starts from a template, straight into its chat", async () => {
    fetchHelpers.mockResolvedValue(payload([]));
    requestMutation.mockResolvedValue(payload([helper({ name: "比价", chat_id: "c7" })], { opened: "c7" }));
    const onOpenChat = vi.fn();
    render(<HelpersScreen active onOpenChat={onOpenChat} />);
    fireEvent.click(await screen.findByRole("button", { name: "新建" }));
    fireEvent.click(screen.getByRole("menuitem", { name: "新建帮手" }));
    fireEvent.click(screen.getByRole("button", { name: /比价/ }));
    await waitFor(() => expect(onOpenChat).toHaveBeenCalledWith("c7"));
    expect(requestMutation).toHaveBeenCalledWith("companion.helpers.change", expect.objectContaining({ action: "create", name: "比价" }));
  });
});

describe("the conversation list", () => {
  it("lists helpers and groups together, newest first, with the last message, and a dot for unread ones", async () => {
    localStorage.clear();
    localStorage.setItem("companion.helper-seen", JSON.stringify({ "*": 0 }));
    const h1 = helper({ last: { text: "记好了：午饭 32 元", at: Date.now() - 60_000, who: "" } });
    const h2 = helper({ id: "h2", name: "比价", emoji: "🛒", chat_id: "c2", last: { text: "帮我看看耳机", at: Date.now() - 3_600_000, who: "我" } });
    fetchHelpers.mockResolvedValue(
      payload([h1, h2], {
        groups: [{ id: "g1", name: "买房小组", members: ["h1", "h2"], chat_id: "g", created_at: 1, last: { text: "首付算好了", at: Date.now() - 1_000, who: "记账" } }],
      }),
    );
    const onOpenChat = vi.fn();
    render(<HelpersScreen active onOpenChat={onOpenChat} />);
    const rows = await screen.findAllByRole("button", { name: /买房小组|记账|比价/ });
    expect(rows.map((r) => r.textContent?.match(/买房小组|记账|比价/)?.[0])).toEqual(["买房小组", "记账", "比价"]);
    expect(rows[0]).toHaveTextContent("记账：首付算好了");
    // The helper's own reply is new; the user's own last message is not.
    expect(screen.getAllByLabelText("有新消息")).toHaveLength(2);
    fireEvent.click(rows[0]);
    expect(onOpenChat).toHaveBeenCalledWith("g");
  });

  it("formats times as a chat list does", () => {
    const now = new Date(2026, 9, 26, 21, 30);
    expect(chatTime(new Date(2026, 9, 26, 9, 5).getTime(), now)).toBe("09:05");
    expect(chatTime(new Date(2026, 9, 25, 9, 5).getTime(), now)).toBe("昨天");
    expect(chatTime(new Date(2026, 9, 1).getTime(), now)).toBe("10/1");
  });
});

describe("habits", () => {
  it("shows what it learned and where from, forgets one, and is told a new one", async () => {
    const learned = helper({ habits: [{ id: "x1", text: "行程别排太紧", by: "learned", at: Date.now() }] });
    fetchHelpers.mockResolvedValue(payload([learned]));
    requestMutation.mockResolvedValue(payload([helper()]));
    render(<HelperProfileSheet chatId="c1" onClose={vi.fn()} onChanged={vi.fn()} onDeleted={vi.fn()} />);
    expect(await screen.findByText("行程别排太紧")).toBeInTheDocument();
    expect(screen.getByText(/慢慢摸清的/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "忘掉：行程别排太紧" }));
    await waitFor(() => expect(requestMutation).toHaveBeenCalledWith("companion.helpers.change", { action: "habit_remove", id: "h1", habit: "x1" }));
    fireEvent.change(screen.getByRole("textbox", { name: "告诉它一个习惯" }), { target: { value: "金额保留两位小数" } });
    fireEvent.click(screen.getByRole("button", { name: "记住" }));
    await waitFor(() =>
      expect(requestMutation).toHaveBeenCalledWith("companion.helpers.change", { action: "habit_add", id: "h1", text: "金额保留两位小数" }),
    );
  });
});

describe("deleting a helper", () => {
  it("swipes a helper left, asks, then deletes it", async () => {
    fetchHelpers.mockResolvedValue(payload([helper()]));
    requestMutation.mockResolvedValue(payload([]));
    const onOpenChat = vi.fn();
    render(<HelpersScreen active onOpenChat={onOpenChat} />);
    const row = await screen.findByRole("button", { name: /记账/ });
    fireEvent.pointerDown(row, { clientX: 300, clientY: 10 });
    fireEvent.pointerMove(row, { clientX: 180, clientY: 12 });
    fireEvent.pointerUp(row, { clientX: 180, clientY: 12 });
    fireEvent.click(row);
    expect(onOpenChat).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "删除" }));
    expect(screen.getByText("删除「记账」？")).toBeInTheDocument();
    fireEvent.click(screen.getAllByRole("button", { name: "删除" }).at(-1)!);
    await waitFor(() => expect(requestMutation).toHaveBeenCalledWith("companion.helpers.change", { action: "delete", id: "h1" }));
  });
});

describe("group chats", () => {
  it("puts two helpers in a group and opens it", async () => {
    const two = payload([helper(), helper({ id: "h2", name: "比价", emoji: "🛒", chat_id: "c2" })]);
    fetchHelpers.mockResolvedValue(two);
    requestMutation.mockResolvedValue({ ...two, opened: "g-chat" });
    const onSaved = vi.fn();
    render(<GroupSheet open group={null} onClose={vi.fn()} onSaved={onSaved} />);
    fireEvent.change(screen.getByRole("textbox", { name: "群名" }), { target: { value: "买电脑" } });
    fireEvent.click(await screen.findByRole("checkbox", { name: /记账/ }));
    expect(screen.getByRole("button", { name: "建好，开始聊" })).toBeDisabled();
    fireEvent.click(screen.getByRole("checkbox", { name: /比价/ }));
    fireEvent.click(screen.getByRole("button", { name: "建好，开始聊" }));
    await waitFor(() => expect(onSaved).toHaveBeenCalledWith("g-chat"));
    expect(requestMutation).toHaveBeenCalledWith("companion.groups.change", { action: "create", name: "买电脑", members: ["h1", "h2"] });
  });
});

describe("HelperProfileSheet", () => {
  it("shows its profile, edits it with labelled fields and a model, and puts a changed default back", async () => {
    const changed = payload([helper({ preset: "记账", is_default: false, title: "改过的职责" })]);
    fetchHelpers.mockResolvedValue(changed);
    requestMutation.mockResolvedValue(changed);
    render(<HelperProfileSheet chatId="c1" onClose={vi.fn()} onChanged={vi.fn()} onDeleted={vi.fn()} />);
    fireEvent.click(await screen.findByRole("button", { name: "编辑资料" }));
    expect(screen.getByText("它负责什么")).toBeInTheDocument();
    expect(screen.getByText(/它每次做事都会照着这里来/)).toBeInTheDocument();
    await waitFor(() => expect(screen.getByRole("option", { name: "glm" })).toBeInTheDocument());
    fireEvent.change(screen.getByRole("combobox", { name: "用哪个模型" }), { target: { value: "glm" } });
    fireEvent.click(screen.getByRole("button", { name: "保存" }));
    await waitFor(() =>
      expect(requestMutation).toHaveBeenCalledWith("companion.helpers.change", expect.objectContaining({ action: "update", id: "h1", model: "glm" })),
    );
    fireEvent.click(await screen.findByRole("button", { name: "编辑资料" }));
    fireEvent.click(await screen.findByRole("button", { name: "恢复默认设置" }));
    await waitFor(() => expect(requestMutation).toHaveBeenCalledWith("companion.helpers.change", { action: "reset", id: "h1" }));
  });
});

describe("helper routines and sharing", () => {
  const job = (id: string, name: string, owner?: { id: string; name: string; emoji: string }) => ({
    id, name, enabled: true, schedule: { kind: "cron", expr: "0 21 * * *" }, payload: { message: "" }, state: {}, ...(owner ? { owner } : {}),
  });

  it("lists only the scheduled jobs this helper set up, and pauses one", async () => {
    fetchHelpers.mockResolvedValue(payload([helper()]));
    const mine = job("j1", "记账提醒", { id: "h1", name: "记账", emoji: "💰" });
    fetchAutomations.mockResolvedValue({ jobs: [mine, job("j2", "动态更新")] });
    requestMutation.mockResolvedValue({ jobs: [{ ...mine, enabled: false }] });
    render(<HelperProfileSheet chatId="c1" onClose={vi.fn()} onChanged={vi.fn()} onDeleted={vi.fn()} />);
    expect(await screen.findByText("记账提醒")).toBeInTheDocument();
    expect(screen.queryByText("动态更新")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("switch", { name: "记账提醒" }));
    await waitFor(() => expect(requestMutation.mock.calls.some(([action, body]) => action === "automation.disable" && body.id === "j1")).toBe(true));
  });

  it("imports shared settings: reviewed in the editor, then created", async () => {
    fetchHelpers.mockResolvedValue(payload([]));
    requestMutation.mockResolvedValue(payload([helper({ name: "读书" })]));
    render(<HelpersScreen active onOpenChat={vi.fn()} />);
    fireEvent.click(await screen.findByRole("button", { name: "新建" }));
    fireEvent.click(screen.getByRole("menuitem", { name: "导入设定" }));
    const box = screen.getByRole("textbox", { name: "帮手设定" });
    fireEvent.change(box, { target: { value: "随便写的" } });
    expect(screen.getByText("这不像是一份帮手设定。")).toBeInTheDocument();
    fireEvent.change(box, { target: { value: '看看这个：{"helper":1,"name":"读书","emoji":"📚","title":"每周读书笔记","instructions":"每周一本"}' } });
    fireEvent.click(screen.getByRole("button", { name: /下一步/ }));
    expect(screen.getByRole("textbox", { name: "名字" })).toHaveValue("读书");
    fireEvent.click(screen.getByRole("button", { name: "保存" }));
    await waitFor(() =>
      expect(requestMutation).toHaveBeenCalledWith("companion.helpers.change", expect.objectContaining({ action: "create", name: "读书", title: "每周读书笔记" })),
    );
  });
});

describe("Composer @ mentions", () => {
  it("lists helpers after @ and fills the picked one in", async () => {
    fetchHelpers.mockResolvedValue(payload([helper()]));
    const onChange = vi.fn();
    render(
      <Composer value="帮我 @" onChange={onChange} onSend={vi.fn()} onStop={vi.fn()} streaming={false} connected transcribeAudio={vi.fn()} onFocusChange={vi.fn()} />,
    );
    fireEvent.click(await screen.findByRole("button", { name: /记账/ }));
    expect(onChange).toHaveBeenCalledWith("帮我 @记账 ");
  });
});

describe("a helper's empty chat", () => {
  it("offers a few things to ask it, sent with a tap", () => {
    const onSend = vi.fn();
    render(<MessengerEmpty face={null} title="记账" hint="记下每笔收支。有事直接说。" starters={["午饭 32 元", "这个月花了多少？"]} onSend={onSend} />);
    fireEvent.click(screen.getByRole("button", { name: "这个月花了多少？" }));
    expect(onSend).toHaveBeenCalledWith("这个月花了多少？");
  });
});
