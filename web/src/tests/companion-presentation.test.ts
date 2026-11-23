import type { UIMessage } from "@/lib/types";

import type { ApprovalRecord } from "@/companion/companion-api";
import {
  activityLog,
  approvalAction,
  approvalCallIds,
  approvalKind,
  awaitingReply,
  chatItems,
  chunkReply,
  currentActivity,
  dayLabel,
  describeToolCall,
  isProactive,
  linkedMedia,
  rawToolName,
  toolActivity,
} from "@/companion/presentation";

function msg(partial: Partial<UIMessage> & Pick<UIMessage, "id" | "role">): UIMessage {
  return { content: "", createdAt: 0, ...partial };
}

describe("chunkReply", () => {
  it("splits paragraphs into separate bubbles", () => {
    expect(chunkReply("你好！\n\n今天想做点什么？")).toEqual(["你好！", "今天想做点什么？"]);
  });

  it("keeps code fences, lists, and headings with their body intact", () => {
    const reply = "步骤如下：\n\n1. 打开设置\n\n2. 选择模型\n\n```bash\necho a\n\necho b\n```";
    expect(chunkReply(reply)).toEqual([
      "步骤如下：",
      "1. 打开设置\n\n2. 选择模型",
      "```bash\necho a\n\necho b\n```",
    ]);
    expect(chunkReply("## 标题\n\n正文")).toEqual(["## 标题\n\n正文"]);
  });

  it("drops a stray fence left after a card (no empty code bubble)", () => {
    expect(chunkReply("```")).toEqual([]);
    expect(chunkReply("看结果：\n\n```\n```")).toEqual(["看结果："]);
  });

  it("folds long replies into intro, body and closing", () => {
    const reply = ["这是草稿：", "你好，", "我想报名。", "谢谢，", "Jordan", "可以发送吗？"].join("\n\n");
    expect(chunkReply(reply)).toEqual([
      "这是草稿：",
      "你好，\n\n我想报名。\n\n谢谢，\n\nJordan",
      "可以发送吗？",
    ]);
  });
});

describe("generated media links", () => {
  const video = "https://cdn.example.com/out/abc.mp4?Expires=1700000000&Signature=x%2By";
  const image = "https://cdn.example.com/img/cat.jpeg";

  it("shows linked videos and images as media instead of bare-link bubbles", () => {
    const reply = `视频做好了：\n\n${video}\n\n[看图](${image})。\n\n还要再改吗？`;
    expect(chunkReply(reply)).toEqual(["视频做好了：", "还要再改吗？"]);
    expect(linkedMedia(reply)).toEqual([
      { kind: "video", url: video },
      { kind: "image", url: image },
    ]);
  });

  it("keeps links inside sentences, ignores pages and Markdown images, and stops at Chinese text", () => {
    const reply = `我放在这里了${video}，有效期一天。参考 https://example.com/page 和 ![预览](${image})`;
    expect(chunkReply(reply)).toEqual([reply]);
    expect(linkedMedia(reply)).toEqual([{ kind: "video", url: video }]);
  });

  it("waits for a streaming reply to finish before loading a player", () => {
    const streaming = msg({ id: "a", role: "assistant", content: video, isStreaming: true });
    const [item] = chatItems([streaming], 0, []);
    expect(item.type === "reply" && item.media).toEqual([]);
    const [done] = chatItems([{ ...streaming, isStreaming: false }], 0, []);
    expect(done.type === "reply" && done.media).toEqual([{ kind: "video", url: video }]);
  });
});

describe("currentActivity", () => {
  const user = msg({ id: "u", role: "user", content: "帮我查天气" });

  it("is idle when no turn is running", () => {
    expect(currentActivity([user], false)).toBeNull();
  });

  it("names the running tool, including both single- and double-underscore MCP servers", () => {
    const trace = msg({
      id: "t",
      role: "assistant",
      kind: "trace",
      toolEvents: [{ call_id: "1", name: "web_search", phase: "end" }, { call_id: "2", name: "mcp_mail_send", phase: "start" }],
    });
    expect(currentActivity([user, trace], true)?.label).toBe("正在使用 mail");
    expect(toolActivity("mcp__github__create_issue").label).toBe("正在使用 github");
    expect(toolActivity("mcp__email__get_thread").label).toBe("正在查看邮箱");
    expect(toolActivity("mcp__browser__browser_navigate").label).toBe("正在打开网页");
    expect(approvalAction("mcp__icloud-calendar__delete_event")).toBe("delete_event");
    expect(approvalAction("mcp__icloud-calendar__update_event")).toBeNull();
    expect(rawToolName("mcp__icloud-calendar__list_events", "icloud-calendar")).toBe("list_events");
    expect(rawToolName("mcp__google_workspace__list_files", "google-workspace")).toBe("list_files");
    expect(rawToolName("mcp_brave_search_web", "brave-search")).toBe("web");
    expect(describeToolCall("mcp__icloud-calendar__create_event", { title: "周会" })).toBe("添加了日程「周会」");
  });

  it("reports typing once reply text streams, thinking otherwise", () => {
    expect(currentActivity([user], true)?.kind).toBe("thinking");
    const streaming = msg({ id: "a", role: "assistant", content: "今天", isStreaming: true });
    expect(currentActivity([user, streaming], true)?.kind).toBe("typing");
  });
});

describe("chatItems and feed", () => {
  const now = new Date(2026, 9, 16, 12, 0).getTime();
  const messages = [
    msg({ id: "u1", role: "user", content: "早", createdAt: now - 3 * 3600_000 }),
    msg({ id: "t1", role: "assistant", kind: "trace", createdAt: now - 3 * 3600_000 }),
    msg({ id: "a1", role: "assistant", content: "早上好\n\n要喝水哦", createdAt: now - 3 * 3600_000 + 5_000 }),
    msg({ id: "c1", role: "assistant", content: "提醒：该喝水了", source: { kind: "cron", label: "喝水提醒" }, createdAt: now }),
  ];

  it("hides activity rows, chunks replies and inserts time separators after gaps", () => {
    const items = chatItems(messages, now);
    expect(items.map((item) => item.type)).toEqual(["day", "user", "reply", "day", "reply"]);
    expect(items[2]).toMatchObject({ bubbles: ["早上好", "要喝水哦"] });
    expect(items[3]).toMatchObject({ label: "今天 12:00" });
  });

  it("keeps between-tool narration out of the thread", () => {
    const narration = msg({ id: "n1", role: "assistant", content: "Let me look at the stories.", activityKind: "model" });
    const thread = [msg({ id: "u2", role: "user", content: "查新闻" }), narration];
    expect(chatItems(thread, now).map((item) => item.type)).toEqual(["user"]);
    expect(awaitingReply(thread, true)).toBe(true);
  });

  it("shows the typing bubble until reply text starts streaming", () => {
    expect(awaitingReply(messages.slice(0, 2), true)).toBe(true);
    const streaming = msg({ id: "a2", role: "assistant", content: "嗯", isStreaming: true });
    expect(awaitingReply([...messages.slice(0, 2), streaming], true)).toBe(false);
    expect(awaitingReply(messages, false)).toBe(false);
  });
});

describe("approvals", () => {
  const approval = (id: string, callId: string, status: ApprovalRecord["status"]): ApprovalRecord => ({
    id,
    session_key: "websocket:c",
    call_id: callId,
    tool_name: "mcp_email_send_email",
    arguments: { recipients: ["a@example.com"], subject: "hi", body: "x" },
    status,
    created_at_ms: 0,
    decided_at_ms: null,
    result: null,
  });
  const ask = msg({
    id: "t",
    role: "assistant",
    kind: "trace",
    toolEvents: [{ call_id: "call-1", name: "mcp_email_send_email", phase: "end" }],
  });
  const reply = msg({ id: "a", role: "assistant", content: "草稿好了，确认一下？" });

  it("places a card where the companion asked, and keeps unplaced pending ones visible", () => {
    const items = chatItems(
      [msg({ id: "u", role: "user", content: "给 a 发邮件" }), ask, reply],
      0,
      [approval("apv_1", "call-1", "pending"), approval("apv_2", "gone", "pending"), approval("apv_3", "old", "executed")],
    );
    expect(items.map((item) => (item.type === "approval" ? item.id : item.type))).toEqual([
      "user",
      "apv_1",
      "reply",
      "apv_2",
    ]);
  });

  it("lists no tool steps in the thread: the header says them live, 做过的事 keeps them", () => {
    const work = msg({
      id: "w",
      role: "assistant",
      kind: "trace",
      toolEvents: [
        { call_id: "q1", name: "web_search", phase: "start", arguments: { query: "东京天气" } },
        { call_id: "q1", name: "web_search", phase: "end", arguments: { query: "东京天气" } },
      ],
    });
    const items = chatItems([msg({ id: "u", role: "user", content: "东京天气" }), work, msg({ id: "a", role: "assistant", content: "晴" })]);
    expect(items.map((item) => item.type)).toEqual(["user", "reply"]);
  });
  it("shows what a helper learned as a grey line, not a reply", () => {
    const items = chatItems([
      msg({ id: "u", role: "user", content: "以后别排太紧" }),
      msg({ id: "a", role: "assistant", content: "好的。" }),
      msg({ id: "n", role: "assistant", content: "记住了：行程别排太紧", source: { kind: "notice", label: "" } }),
    ]);
    expect(items.map((item) => item.type)).toEqual(["user", "reply", "notice"]);
    expect(items.at(-1)).toMatchObject({ text: "记住了：行程别排太紧" });
  });

  it("keeps a just-decided unplaced card visible so its outcome is seen", () => {
    const decided = { ...approval("apv_4", "gone", "failed"), decided_at_ms: 1_000 };
    expect(chatItems([], 2_000, [decided]).map((item) => item.id)).toEqual(["apv_4"]);
    expect(chatItems([], 1_000 + 16 * 60_000, [decided])).toEqual([]);
  });

  it("gates calendar deletion from any server, but not other calendar edits", () => {
    expect(approvalAction("mcp_icloud-calendar_delete_event")).toBe("delete_event");
    expect(approvalAction("mcp_icloud-calendar_update_event")).toBeNull();
  });

  it("only treats finished gated calls as recorded", () => {
    const started = msg({
      id: "s",
      role: "assistant",
      kind: "trace",
      toolEvents: [{ call_id: "call-2", name: "mcp_email_send_email", phase: "start" }],
    });
    expect(approvalCallIds([started, ask])).toEqual(["call-1"]);
  });

  it("recognises a browser click the server held for approval, by its result", () => {
    const clicks = msg({
      id: "b",
      role: "assistant",
      kind: "trace",
      createdAt: 5,
      toolEvents: [
        { call_id: "c-held", name: "mcp_browser_browser_click", phase: "end", arguments: { element: "提交订单" }, result: "[等待用户确认] 这个操作没有执行" },
        { call_id: "c-done", name: "mcp_browser_browser_click", phase: "end", arguments: { element: "下一页" }, result: "### Page" },
      ],
    });
    expect(approvalCallIds([clicks])).toEqual(["c-held"]);
    expect(activityLog([], [], [clicks]).map((entry) => entry.title)).toEqual(["在网页上点了「下一页」"]);

    const held = { ...approval("apv_9", "c-held", "pending"), tool_name: "mcp_browser_browser_click", arguments: { element: "提交订单" }, action: "browser_submit" };
    expect(approvalKind(held)).toBe("browser_submit");
    expect(activityLog([held], []).map((entry) => entry.title)).toEqual(["在网页上点按钮 「提交订单」"]);
    const said = { ...held, context: { browser: { host: "a.cn", page: "", target: "提交订单", paying: true, summary: "要在 a.cn 上点「提交订单」。点了可能就下单或付款了。" } } };
    expect(activityLog([said], []).map((entry) => entry.title)).toEqual(["要在 a.cn 上点「提交订单」"]);
  });

  it("shows approval receipts as ordinary replies, not as something it brought up on its own", () => {
    const receipt = msg({ id: "r", role: "assistant", content: "发好了", source: { kind: "local_trigger", label: "审批回执" } });
    expect(isProactive(receipt)).toBe(false);
  });
});

describe("activityLog", () => {
  it("merges approvals and task runs, newest first, with readable titles", () => {
    const approval: ApprovalRecord = {
      id: "apv_1",
      session_key: "websocket:c",
      call_id: "c1",
      tool_name: "mcp_email_send_email",
      arguments: { recipients: ["sarah@example.com"], subject: "周六爬山", body: "去吗" },
      status: "executed",
      created_at_ms: 100,
      decided_at_ms: 300,
      result: "ok",
    };
    const job = { id: "j1", name: "早报", state: { run_history: [{ run_at_ms: 200, status: "ok" }, { run_at_ms: 400, status: "error" }] } };
    expect(activityLog([approval], [job]).map((entry) => [entry.title, entry.status])).toEqual([
      ["早报", "出错了"],
      ["发邮件 给 sarah@example.com 「周六爬山」", "已完成"],
      ["早报", "已运行"],
    ]);

    const currentSchemaJob = {
      id: "j2",
      name: "晚报",
      runs: [
        { id: "r1", at_ms: 500, status: "ok" },
        { id: "r2", at_ms: 600, status: "skipped" },
      ],
    };
    const toStringApproval: ApprovalRecord = {
      ...approval,
      id: "apv_2",
      tool_name: "mcp__email__send_email",
      arguments: { to: "a@example.com, b@example.com", subject: "提醒" },
      decided_at_ms: 550,
    };
    expect(activityLog([toStringApproval], [currentSchemaJob]).map((entry) => [entry.title, entry.status])).toEqual([
      ["晚报", "跳过了"],
      ["发邮件 给 a@example.com、b@example.com 「提醒」", "已完成"],
      ["晚报", "已运行"],
    ]);
  });

  it("adds the finished tool calls that did something for the user, in plain words, without repeating approvals", () => {
    const trace = (id: string, at: number, events: UIMessage["toolEvents"]) =>
      msg({ id, role: "assistant", kind: "trace", createdAt: at, toolEvents: events });
    const messages = [
      trace("t1", 10, [
        { call_id: "a", name: "web_search", phase: "start", arguments: { query: "东京 十月 天气" } },
        { call_id: "a", name: "web_search", phase: "end", arguments: { query: "东京 十月 天气" } },
      ]),
      trace("t2", 20, [
        { call_id: "b", name: "web_fetch", phase: "end", arguments: { url: "https://www.jma.go.jp/forecast" } },
        { call_id: "c", name: "mcp_icloud-calendar_create_event", phase: "end", arguments: { create_event_request: { title: "爬山" } } },
        { call_id: "d", name: "mcp_email_send_email", phase: "end", arguments: {} },
        { call_id: "e", name: "exec", phase: "error", arguments: { command: "ls" }, error: "denied" },
      ]),
    ];
    expect(activityLog([], [], messages).map((entry) => [entry.title, entry.status, entry.activity])).toEqual([
      ["看了网页 jma.go.jp", "", "read"],
      ["添加了日程「爬山」", "", "app"],
      ["搜索了「东京 十月 天气」", "", "search"],
    ]);
  });
});

describe("dayLabel", () => {
  it("names past and upcoming days", () => {
    const now = new Date(2026, 9, 20, 20, 58).getTime();
    expect(dayLabel(new Date(2026, 9, 20, 9, 5).getTime(), now)).toBe("今天 09:05");
    expect(dayLabel(new Date(2026, 9, 19, 20, 43).getTime(), now)).toBe("昨天 20:43");
    expect(dayLabel(new Date(2026, 9, 21, 8, 30).getTime(), now)).toBe("明天 08:30");
    expect(dayLabel(new Date(2026, 9, 23, 8, 30).getTime(), now)).toBe("10月23日 08:30");
  });
});
