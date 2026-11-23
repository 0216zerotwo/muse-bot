import type { UIMediaAttachment, UIMessage } from "@/lib/types";

import { splitReply, type Card } from "./replyCards";
import { rememberedNotes } from "./fileKinds";
import type { ApprovalRecord, RoutingDecision } from "./companion-api";

/* ---------- Reply bubbles ---------- */

const FENCE = /^\s*(```|~~~)/;
const LIST_ITEM = /^\s*(?:[-*+]|\d+[.)])\s/;
const HEADING = /^\s*#{1,6}\s/;
/** Beyond this, a reply keeps its opening and closing lines and one body bubble. */
const MAX_BUBBLES = 4;

function paragraphs(content: string): string[] {
  const result: string[] = [];
  let current: string[] = [];
  let inFence = false;
  for (const line of content.split("\n")) {
    if (FENCE.test(line)) inFence = !inFence;
    if (!inFence && line.trim() === "") {
      if (current.length) result.push(current.join("\n"));
      current = [];
      continue;
    }
    current.push(line);
  }
  if (current.length) result.push(current.join("\n"));
  return result;
}

/* ---------- Generated media sent as links ---------- */

const MEDIA_URL = /https?:\/\/[A-Za-z0-9\-._~:/?#@!$&*+,;=%]+/g;
const VIDEO_PATH = /\.(?:mp4|m4v|mov|webm)$/i;
const IMAGE_PATH = /\.(?:png|jpe?g|gif|webp|avif)$/i;
const MARKDOWN_IMAGE = /!\[[^\]]*\]\([^)]*\)/g;
const MARKDOWN_LINK = /\[[^\]]*\]\(([^)\s]+)\)/g;

function mediaKind(url: string): "image" | "video" | null {
  let path: string;
  try {
    path = new URL(url).pathname;
  } catch {
    return null;
  }
  if (VIDEO_PATH.test(path)) return "video";
  if (IMAGE_PATH.test(path)) return "image";
  return null;
}

function mediaUrls(text: string): string[] {
  return [...text.replace(MARKDOWN_IMAGE, "").matchAll(MEDIA_URL)]
    .map((match) => match[0].replace(/[.,;:!?]+$/, ""))
    .filter((url) => mediaKind(url) !== null);
}

/**
 * Images and videos the companion links to. Generated media usually comes back
 * as a URL, so the chat shows it inline the way a person sends a photo.
 * Markdown images are left to the Markdown renderer.
 */
export function linkedMedia(content: string): UIMediaAttachment[] {
  const urls = [...new Set(mediaUrls(content))];
  return urls.map((url) => ({ kind: mediaKind(url) ?? "file", url }));
}

/** A paragraph that is nothing but a media link; the media itself replaces it. */
function isMediaOnly(paragraph: string): boolean {
  const urls = mediaUrls(paragraph);
  if (!urls.length) return false;
  let rest = paragraph.replace(MARKDOWN_LINK, (link, target: string) => (mediaKind(target) ? "" : link));
  for (const url of urls) rest = rest.split(url).join("");
  return rest.replace(/[\s\-*+>:：.,。、<>]/g, "") === "";
}

/** Fence marks with nothing inside (a model sometimes closes a card twice): no empty code bubble. */
const BARE_FENCE = /^(?:\s*(?:```|~~~)[\w-]*\s*(?:\n|$))+$/;

/**
 * Split an assistant reply into chat bubbles the way a person texts: each
 * paragraph is a bubble, while lists, code blocks and a heading with its body
 * stay together. Long replies become intro / body / closing, so a draft reads
 * "Here's a draft:" → the draft → "Good to send?".
 */
export function chunkReply(content: string): string[] {
  const merged: string[] = [];
  for (const paragraph of paragraphs(content)) {
    if (isMediaOnly(paragraph) || BARE_FENCE.test(paragraph)) continue;
    const previous = merged.at(-1);
    const continuesList =
      previous !== undefined &&
      LIST_ITEM.test(paragraph) &&
      LIST_ITEM.test(previous.split("\n").at(-1) ?? "");
    const followsHeading = previous !== undefined && HEADING.test(previous) && !previous.includes("\n");
    if (previous !== undefined && (continuesList || followsHeading)) {
      merged[merged.length - 1] = `${previous}\n\n${paragraph}`;
    } else {
      merged.push(paragraph);
    }
  }
  if (merged.length <= MAX_BUBBLES) return merged;
  return [merged[0], merged.slice(1, -1).join("\n\n"), merged[merged.length - 1]];
}

/* ---------- What the companion is doing right now ---------- */

export type ActivityKind =
  | "thinking"
  | "typing"
  | "search"
  | "read"
  | "files"
  | "run"
  | "image"
  | "schedule"
  | "send"
  | "recall"
  | "delegate"
  | "app"
  | "working";

export interface Activity {
  kind: ActivityKind;
  label: string;
}

const TOOL_ACTIVITY: Record<string, Activity> = {
  web_search: { kind: "search", label: "正在搜索网页" },
  web_fetch: { kind: "read", label: "正在阅读网页" },
  read_file: { kind: "files", label: "正在翻看文件" },
  list_dir: { kind: "files", label: "正在翻看文件" },
  find_files: { kind: "files", label: "正在翻看文件" },
  grep: { kind: "files", label: "正在翻看文件" },
  write_file: { kind: "files", label: "正在整理文件" },
  edit_file: { kind: "files", label: "正在整理文件" },
  apply_patch: { kind: "files", label: "正在整理文件" },
  exec: { kind: "run", label: "正在运行命令" },
  exec_session: { kind: "run", label: "正在运行命令" },
  generate_image: { kind: "image", label: "正在画图" },
  cron: { kind: "schedule", label: "正在安排提醒" },
  message: { kind: "send", label: "正在发送消息" },
  search_sessions: { kind: "recall", label: "正在回忆之前的对话" },
  read_session: { kind: "recall", label: "正在回忆之前的对话" },
  list_sessions: { kind: "recall", label: "正在回忆之前的对话" },
  spawn: { kind: "delegate", label: "正在安排人手" },
  subagent: { kind: "delegate", label: "正在安排人手" },
  create_goal: { kind: "delegate", label: "正在制定计划" },
  update_goal: { kind: "delegate", label: "正在更新计划" },
  rg: { kind: "files", label: "正在翻看文件" },
  list_exec_sessions: { kind: "run", label: "正在查看运行中的命令" },
  run_cli_app: { kind: "run", label: "正在使用命令行应用" },
  send_session_message: { kind: "send", label: "正在给另一个对话发消息" },
  my: { kind: "working", label: "正在查看自己的设置" },
  // Hermes Agent's tools (the agent behind the companion plugin).
  web_extract: { kind: "read", label: "正在阅读网页" },
  terminal: { kind: "run", label: "正在运行命令" },
  execute_code: { kind: "run", label: "正在运行代码" },
  process_manage: { kind: "run", label: "正在查看运行中的命令" },
  search_files: { kind: "files", label: "正在翻看文件" },
  patch: { kind: "files", label: "正在整理文件" },
  image_generate: { kind: "image", label: "正在画图" },
  vision_analyze: { kind: "read", label: "正在看图" },
  cronjob_manage: { kind: "schedule", label: "正在安排提醒" },
  delegate_task: { kind: "delegate", label: "正在安排人手" },
  session_search: { kind: "recall", label: "正在回忆之前的对话" },
  memory: { kind: "recall", label: "正在记下来" },
  todo_list: { kind: "working", label: "正在列计划" },
  skill_view: { kind: "working", label: "正在翻自己的笔记" },
  skills_list: { kind: "working", label: "正在翻自己的笔记" },
  skill_manage: { kind: "working", label: "正在整理经验" },
  text_to_speech: { kind: "app", label: "正在录语音" },
  clarify: { kind: "thinking", label: "正在想要问你什么" },
  tool_search: { kind: "working", label: "正在找合适的办法" },
  tool_describe: { kind: "working", label: "正在找合适的办法" },
  // The companion plugin's own tools.
  trigger_add: { kind: "schedule", label: "正在设置自动触发" },
  trigger_list: { kind: "schedule", label: "正在查看自动触发" },
  trigger_remove: { kind: "schedule", label: "正在删除自动触发" },
  goal_list: { kind: "working", label: "正在查看目标" },
  goal_add: { kind: "delegate", label: "正在记下目标" },
  goal_update: { kind: "delegate", label: "正在更新目标进度" },
  goal_propose_plan: { kind: "delegate", label: "正在调整计划" },
  browser_hand_over: { kind: "app", label: "正在请你接手浏览器" },
  helper_ask: { kind: "delegate", label: "正在交给帮手" },
  browser_request: { kind: "delegate", label: "正在请主助理用浏览器" },
  credential_request: { kind: "app", label: "正在请你填写凭证" },
  avatar_create: { kind: "image", label: "正在画新形象" },
  avatar_pick: { kind: "image", label: "正在换上选好的形象" },
  avatar_reset: { kind: "image", label: "正在换回默认形象" },
  avatar_animate: { kind: "image", label: "正在让形象动起来" },
  connect_request: { kind: "app", label: "正在请你连接服务" },
  helper_create: { kind: "delegate", label: "正在新建帮手" },
  helper_note: { kind: "working", label: "正在记工作笔记" },
  feed_post: { kind: "working", label: "正在写动态" },
};

/**
 * Parse Hermes MCP tool names in both current ``mcp__<server>__<tool>`` and
 * legacy ``mcp_<server>_<tool>`` forms.
 */
export function parseMcpToolName(name: string): { server: string; tool: string } | null {
  if (name.startsWith("mcp__")) {
    const rest = name.slice(5);
    const sep = rest.indexOf("__");
    if (sep > 0 && sep + 2 < rest.length) {
      return { server: rest.slice(0, sep), tool: rest.slice(sep + 2) };
    }
    return null;
  }
  if (name.startsWith("mcp_")) {
    const rest = name.slice(4);
    const sep = rest.indexOf("_");
    if (sep > 0 && sep + 1 < rest.length) {
      return { server: rest.slice(0, sep), tool: rest.slice(sep + 1) };
    }
    return null;
  }
  return null;
}

/** ``mcp__<server>__browser_click`` / ``mcp_<server>_browser_click`` or Hermes's own ``browser_click``. */
const BROWSER_TOOL = /^(?:mcp__?.+?__?)?(browser_[a-z_]+)$/;

const BROWSER_ACTIVITY: Record<string, string> = {
  browser_navigate: "正在打开网页",
  browser_navigate_back: "正在返回上一页",
  browser_snapshot: "正在看网页",
  browser_take_screenshot: "正在截图",
  browser_click: "正在点网页",
  browser_type: "正在填写网页",
  browser_fill_form: "正在填写网页",
  browser_select_option: "正在填写网页",
  browser_press_key: "正在操作网页",
  browser_wait_for: "正在等网页加载",
  browser_tabs: "正在切换网页",
  browser_back: "正在返回上一页",
  browser_press: "正在操作网页",
  browser_scroll: "正在翻网页",
  browser_vision: "正在看网页",
  browser_get_images: "正在看网页上的图",
};

export const THINKING: Activity = { kind: "thinking", label: "正在思考" };
const TYPING: Activity = { kind: "typing", label: "正在输入" };

export function toolActivity(name: string): Activity {
  const known = TOOL_ACTIVITY[name];
  if (known) return known;
  const mcp = parseMcpToolName(name);
  const browserTool = (mcp ? BROWSER_TOOL.exec(mcp.tool) : BROWSER_TOOL.exec(name))?.[1];
  if (browserTool) return { kind: "read", label: BROWSER_ACTIVITY[browserTool] ?? "正在操作浏览器" };
  const gated = approvalAction(name);
  if (gated) return { kind: "send", label: gated === "delete_event" ? "正在准备删除日程" : "正在准备邮件" };
  if (mcp) {
    if (mcp.server === "email") return { kind: "read", label: "正在查看邮箱" };
    return { kind: "app", label: mcp.server ? `正在使用 ${mcp.server}` : "正在使用应用" };
  }
  if (name.startsWith("mcp_")) {
    return { kind: "app", label: "正在使用应用" };
  }
  return { kind: "working", label: "正在处理" };
}

/** Status for the header while a turn runs; ``null`` when idle. */
export function currentActivity(messages: UIMessage[], streaming: boolean): Activity | null {
  if (!streaming) return null;
  for (let i = messages.length - 1; i >= 0; i -= 1) {
    const message = messages[i];
    if (message.role === "user") break;
    const running = message.toolEvents?.filter((event) => event.phase === "start");
    if (running?.length) return toolActivity(String(running.at(-1)?.name ?? ""));
    if (message.role !== "assistant" || message.kind === "trace") continue;
    if (message.isStreaming && message.content.trim()) return TYPING;
    if (message.reasoningStreaming) return THINKING;
  }
  return THINKING;
}

/* ---------- Approvals ---------- */

/** Must match the gated actions in the server's ``approvals.py``. */
const GATED_MCP_ACTIONS: Record<string, ApprovalAction> = {
  send_email: "send_email",
  send_mail: "send_email",
  reply_email: "send_email",
  send_message: "send_email",
  forward_email: "forward_email",
  delete_emails: "delete_emails",
  delete_email: "delete_emails",
  trash_email: "delete_emails",
  email_command: "email_command",
  delete_event: "delete_event",
  remove_event: "delete_event",
};
const APPROVAL_TOOL = /^mcp__?.+?__?(send_email|send_mail|reply_email|send_message|forward_email|delete_emails|delete_email|trash_email|email_command|delete_event|remove_event)$/;
/** Local trigger that reports approval outcomes back into the chat. */
export const APPROVAL_TRIGGER_NAME = "审批回执";

export type ApprovalAction =
  | "send_email"
  | "forward_email"
  | "delete_emails"
  | "email_command"
  | "delete_event"
  | "browser_submit"
  | "browser_upload"
  | "run_command"
  | "goal_plan"
  | "send_private"
  // Kinds the user set to 询问我 in 设置 → 权限.
  | "browse"
  | "payment"
  | "connector_read"
  | "connector_write"
  | "schedule";

/** Tools that always wait for approval; browser clicks depend on what they click. */
export function approvalAction(toolName: string): ApprovalAction | null {
  if (toolName === "goal_propose_plan") return "goal_plan";
  const mcp = parseMcpToolName(toolName);
  if (mcp && mcp.tool in GATED_MCP_ACTIONS) {
    return GATED_MCP_ACTIONS[mcp.tool];
  }
  const matched = APPROVAL_TOOL.exec(toolName)?.[1];
  return matched ? GATED_MCP_ACTIONS[matched] ?? null : null;
}

/** What an approval card is about, as decided by the server when the call was held. */
export function approvalKind(approval: ApprovalRecord): ApprovalAction {
  return (approval.action as ApprovalAction | undefined) || approvalAction(approval.tool_name) || "email_command";
}

/** The tool result the gateway returns for a call it held for approval. */
const DEFERRED_RESULT = "[等待用户确认]";

function wasDeferred(event: { name?: string; result?: unknown }): boolean {
  return (
    Boolean(event.name && approvalAction(event.name)) ||
    (typeof event.result === "string" && event.result.startsWith(DEFERRED_RESULT))
  );
}

/**
 * Call ids of gated tool calls that have finished in the thread. A finished
 * call has its approval recorded on the server, so these drive refetching.
 */
export function approvalCallIds(messages: UIMessage[]): string[] {
  const ids = new Set<string>();
  for (const message of messages) {
    for (const event of message.toolEvents ?? []) {
      if (event.call_id && event.name && event.phase !== "start" && wasDeferred(event)) {
        ids.add(event.call_id);
      }
    }
  }
  return [...ids];
}

/* ---------- Chat stream items ---------- */

export type ChatItem =
  | { type: "day"; id: string; label: string }
  /** A small grey line, not a message (a helper learned one of the user's habits, or a memory was saved). */
  | { type: "notice"; id: string; text: string; kind?: "memory"; note?: string }
  | { type: "user"; id: string; message: UIMessage }
  | {
      type: "reply";
      id: string;
      message: UIMessage;
      /** The text bubbles alone. */
      bubbles: string[];
      /** Bubbles and cards in reply order. */
      blocks: ReplyBlock[];
      media: UIMediaAttachment[];
      /** Under a turn's last reply when it used tools: its steps and how long it took (做了 6 步 ›). */
      task?: TurnTask;
    }
  | { type: "approval"; id: string; approval: ApprovalRecord };

export interface TurnTask {
  steps: { id: string; title: string; failed: boolean }[];
  /** From the question to the last reply. */
  ms: number;
}

/** "8 秒", "1 分 12 秒", "1 小时 3 分". */
export function elapsedWords(ms: number): string {
  const seconds = Math.max(1, Math.floor(ms / 1000));
  if (seconds < 60) return `${seconds} 秒`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)} 分${seconds % 60 ? ` ${seconds % 60} 秒` : ""}`;
  const minutes = Math.floor((seconds % 3600) / 60);
  return `${Math.floor(seconds / 3600)} 小时${minutes ? ` ${minutes} 分` : ""}`;
}

/** Bookkeeping (recall, notes, tool lookups) stays out of 做过的事; the header still says it. */
const QUIET_TOOLS = new Set(["recall", "thinking", "working"]);

/** The plumbing of a task (shell commands, code, reading files and its own notes): how it worked,
 * not what it did for the user, so 做过的事 leaves it out
 * (while it runs, the header still says what it is doing). */
const BEHIND_THE_SCENES = new Set([
  "exec", "exec_session", "terminal", "execute_code", "process",
  "read_file", "list_dir", "find_files", "grep", "rg", "search_files",
  "skill_view", "skills_list", "todo", ...QUIET_TOOLS,
]);

/** Also looking things up for itself (its lists, its tools) and its own housekeeping. */
function behindTheScenes(name: string): boolean {
  return BEHIND_THE_SCENES.has(name) || /(^list_|_list$|_recent$|_describe$|^tool_search$)/.test(name)
    || toolActivity(name).kind === "working";
}

function replyItem(message: UIMessage): Extract<ChatItem, { type: "reply" }> {
  const blocks = replyBlocks(message.content, message.isStreaming);
  return {
    type: "reply",
    id: message.id,
    message,
    blocks,
    bubbles: blocks.flatMap((block) => (block.type === "bubble" ? [block.text] : [])),
    // A URL still streaming in would reload the player on every token.
    media: message.isStreaming ? [] : linkedMedia(message.content),
  };
}

export type ReplyBlock = { type: "bubble"; text: string } | { type: "card"; card: Card } | { type: "pending"; kind?: "ui" };

/** A reply as texting bubbles with its interactive cards in place. */
export function replyBlocks(content: string, streaming = false): ReplyBlock[] {
  return splitReply(content, streaming).flatMap((part): ReplyBlock[] =>
    part.type === "text" ? chunkReply(part.text).map((text) => ({ type: "bubble", text })) : [part],
  );
}

function isApprovalReceipt(message: UIMessage): boolean {
  return message.source?.label === APPROVAL_TRIGGER_NAME;
}

/** Proactive message (scheduled task, trigger) rather than a reply to the user. */
export function isProactive(message: UIMessage): boolean {
  return message.role === "assistant" && !!message.source && !isApprovalReceipt(message);
}

function isVisibleReply(message: UIMessage): boolean {
  if (message.role !== "assistant" || message.kind === "trace") return false;
  // Narration emitted between tool calls ("Let me look at…") is progress, not
  // a reply; the header status already says what the companion is doing.
  if (message.activityKind === "model") return false;
  // The gateway's "still working" notices (⏳ …): the chips and the header already show progress.
  if (message.content.trimStart().startsWith("⏳")) return false;
  return message.content.trim().length > 0 || !!message.media?.length || !!message.images?.length;
}

const DAY_GAP_MS = 60 * 60 * 1000;

export function dayLabel(timestamp: number, now = Date.now()): string {
  const date = new Date(timestamp);
  const time = date.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit", hour12: false });
  const startOfToday = new Date(now);
  startOfToday.setHours(0, 0, 0, 0);
  const dayMs = 24 * 60 * 60 * 1000;
  // Next runs of scheduled tasks are in the future.
  const startOfTomorrow = new Date(startOfToday);
  startOfTomorrow.setDate(startOfTomorrow.getDate() + 1);
  const startOfDayAfter = new Date(startOfTomorrow);
  startOfDayAfter.setDate(startOfDayAfter.getDate() + 1);
  if (timestamp >= startOfDayAfter.getTime()) return `${date.getMonth() + 1}月${date.getDate()}日 ${time}`;
  if (timestamp >= startOfTomorrow.getTime()) return `明天 ${time}`;
  if (timestamp >= startOfToday.getTime()) return `今天 ${time}`;
  if (timestamp >= startOfToday.getTime() - dayMs) return `昨天 ${time}`;
  return `${date.getMonth() + 1}月${date.getDate()}日 ${time}`;
}

/** Messages as a texting thread: user turns, chunked replies, approval cards, time separators. */
export function chatItems(messages: UIMessage[], now = Date.now(), approvals: ApprovalRecord[] = []): ChatItem[] {
  const items: ChatItem[] = [];
  const byCallId = new Map(approvals.map((approval) => [approval.call_id, approval]));
  const placed = new Set<string>();
  let lastTime: number | null = null;
  // What it saved about the user in a turn (已记住：…), shown after that turn's last reply.
  const remembered = new Map<string, string[]>();
  const lastReplyOf = new Map<string, number>();
  // Each turn's steps (one per tool call, its end replacing its start) and its first moment.
  const steps = new Map<string, Map<string, TurnTask["steps"][number]>>();
  const startOf = new Map<string, number>();
  // What it does on the way to a reply isn't listed in the thread: the header says it live
  // (正在操作浏览器…), and 做过的事 keeps the record.
  for (const message of messages) {
    // An approval card sits where the companion asked for it.
    for (const event of message.toolEvents ?? []) {
      const approval = event.call_id ? byCallId.get(event.call_id) : undefined;
      if (approval && !placed.has(approval.id)) {
        placed.add(approval.id);
        items.push({ type: "approval", id: approval.id, approval });
      }
      if (message.turnId && event.call_id && event.name && (event.phase === "end" || event.phase === "error")) {
        const turnSteps = steps.get(message.turnId) ?? new Map();
        turnSteps.set(event.call_id, {
          id: event.call_id,
          title: describeToolCall(String(event.name), event.arguments),
          failed: event.phase === "error" || Boolean(event.error),
        });
        steps.set(message.turnId, turnSteps);
      }
      const notes = message.turnId ? rememberedNotes(event) : [];
      if (notes.length && message.turnId) {
        const kept = remembered.get(message.turnId) ?? [];
        remembered.set(message.turnId, [...kept, ...notes.filter((note) => !kept.includes(note))]);
      }
    }
    if (message.turnId && message.createdAt && !startOf.has(message.turnId)) startOf.set(message.turnId, message.createdAt);
    if (message.source?.kind === "notice") {
      if (message.content.trim()) items.push({ type: "notice", id: message.id, text: message.content.trim() });
      continue;
    }
    const isUser = message.role === "user";
    if (!isUser && !isVisibleReply(message)) continue;
    if (message.createdAt && (lastTime === null || message.createdAt - lastTime > DAY_GAP_MS)) {
      items.push({ type: "day", id: `day-${message.id}`, label: dayLabel(message.createdAt, now) });
    }
    if (message.createdAt) lastTime = message.createdAt;
    items.push(isUser ? { type: "user", id: message.id, message } : replyItem(message));
    if (!isUser && message.turnId) lastReplyOf.set(message.turnId, items.length - 1);
  }
  for (const [turn, index] of lastReplyOf) {
    const reply = items[index];
    const turnSteps = [...(steps.get(turn)?.values() ?? [])];
    if (reply.type === "reply" && turnSteps.length) {
      reply.task = { steps: turnSteps, ms: Math.max(0, (reply.message.createdAt ?? 0) - (startOf.get(turn) ?? reply.message.createdAt ?? 0)) };
    }
  }
  // Last first, so the places of the earlier ones don't move.
  for (const [turn, index] of [...lastReplyOf].sort((a, b) => b[1] - a[1])) {
    const notes = remembered.get(turn) ?? [];
    items.splice(
      index + 1,
      0,
      ...notes.map((note, n) => ({
        type: "notice" as const,
        kind: "memory" as const,
        id: `remembered-${turn}-${n}`,
        text: `✓ 已记住：${note}`,
        note,
      })),
    );
  }
  // A card whose request is not in the loaded history still shows at the end
  // while it needs an answer, and briefly after, so the outcome stays visible.
  for (const approval of approvals) {
    if (placed.has(approval.id)) continue;
    const recentlyDecided =
      approval.decided_at_ms !== null && now - approval.decided_at_ms < RECENT_DECISION_MS;
    if (approval.status === "pending" || recentlyDecided) {
      items.push({ type: "approval", id: approval.id, approval });
    }
  }
  return items;
}

const RECENT_DECISION_MS = 15 * 60 * 1000;

/** Whether to show the "•••" bubble: a turn is running and no reply text has started yet. */
export function awaitingReply(messages: UIMessage[], streaming: boolean): boolean {
  if (!streaming) return false;
  for (let i = messages.length - 1; i >= 0; i -= 1) {
    const message = messages[i];
    if (message.role === "user") return true;
    if (isVisibleReply(message)) return !message.isStreaming;
  }
  return true;
}

/* ---------- Activity log ---------- */

export interface ActivityEntry {
  id: string;
  /** An approved/declined action, a scheduled-task run, or any other tool call. */
  kind: "approval" | "task" | "action";
  /** For tool calls: what sort of thing it did (picks the icon). */
  activity?: ActivityKind;
  title: string;
  /** Empty when there is nothing to add (a tool call that simply worked). */
  status: string;
  tone: "ok" | "bad" | "pending" | "muted";
  at: number;
}

const APPROVAL_VERBS: Record<ApprovalAction, string> = {
  send_email: "发邮件",
  forward_email: "转发邮件",
  delete_emails: "删除邮件",
  email_command: "邮件操作",
  delete_event: "删除日程",
  browser_submit: "在网页上点按钮",
  browser_upload: "上传文件到网页",
  run_command: "运行命令",
  goal_plan: "修改目标计划",
  send_private: "把信息发到网站",
  browse: "打开网页",
  payment: "付款或下单",
  connector_read: "读取连接的服务",
  connector_write: "在连接的服务里修改",
  schedule: "设定时任务",
};

const APPROVAL_STATUS: Record<ApprovalRecord["status"], [string, ActivityEntry["tone"]]> = {
  pending: ["等你确认", "pending"],
  executed: ["已完成", "ok"],
  failed: ["没成功", "bad"],
  rejected: ["你取消了", "muted"],
};

/** Normalize string[], comma/semicolon-separated strings, or scalars into clean string items. */
export function normalizeAddressList(value: unknown): string[] {
  if (Array.isArray(value)) {
    return value
      .map((item) => (typeof item === "string" || typeof item === "number" ? String(item).trim() : ""))
      .filter(Boolean);
  }
  if (typeof value === "string") {
    return value
      .split(/[,;\n]+/)
      .map((item) => item.trim())
      .filter(Boolean);
  }
  return [];
}

/** Read email recipients from ``recipients`` or fallback ``to`` (matching backend ``_recipients``). */
export function approvalRecipients(args: Record<string, unknown>): string[] {
  const primary = normalizeAddressList(args.recipients);
  return primary.length > 0 ? primary : normalizeAddressList(args.to);
}

/** Normalize message/email/item IDs supplied as string[], number[], or comma-separated strings. */
export function normalizeIdList(value: unknown): string[] {
  if (Array.isArray(value)) {
    return value
      .filter(
        (item): item is string | number =>
          (typeof item === "string" && item.trim().length > 0) ||
          (typeof item === "number" && Number.isFinite(item)),
      )
      .map((item) => String(item).trim());
  }
  if (typeof value === "number" && Number.isFinite(value)) {
    return [String(value)];
  }
  if (typeof value === "string") {
    return value
      .split(/[,;\s]+/)
      .map((item) => item.trim())
      .filter(Boolean);
  }
  return [];
}

function approvalTitle(approval: ApprovalRecord): string {
  // "要在 apple.com.cn 上点「立即购买」": its first sentence says it all.
  const summary = approval.context?.browser?.summary;
  if (summary) return summary.split("。")[0];
  const verb = APPROVAL_VERBS[approvalKind(approval)];
  const to = approvalRecipients(approval.arguments).join("、");
  const subject = typeof approval.arguments.subject === "string" ? approval.arguments.subject : "";
  const element = typeof approval.arguments.element === "string" ? approval.arguments.element : "";
  const site = approval.context?.egress?.host ?? "";
  return [verb, to && `给 ${to}`, subject && `「${subject}」`, element && `「${element}」`, site && `（${site}）`]
    .filter(Boolean)
    .join(" ");
}

/* ---------- What a connection lets it do ---------- */

/** Tools whose name says they only look (``list_events``, ``get_email``…). */
const READ_ONLY_TOOL = /^(list|get|search|read|query|find|check|debug|fetch|view|describe|count|lookup)_/;

/** ``mcp__<server>__<tool>`` / ``mcp_<server>_<tool>`` → ``<tool>`` (including ``-`` → ``_`` sanitized server names). */
export function rawToolName(name: string, server: string): string {
  const sanitized = server.replace(/[^A-Za-z0-9_]/g, "_");
  for (const prefix of [`mcp__${sanitized}__`, `mcp__${server}__`, `mcp_${sanitized}_`, `mcp_${server}_`]) {
    if (name.startsWith(prefix)) return name.slice(prefix.length);
  }
  return name;
}

export function isReadOnlyTool(raw: string): boolean {
  return READ_ONLY_TOOL.test(raw);
}

const TOOL_LABELS: Record<string, string> = {
  list_calendars: "查看日历",
  list_events: "查看日程",
  create_event: "添加日程",
  update_event: "修改日程",
  delete_event: "删除日程",
  debug_calendar_connection: "检查日历连接",
  send_email: "发邮件",
  forward_email: "转发邮件",
  delete_emails: "删除邮件",
  email_command: "执行邮件协议命令",
  generate_video: "生成视频",
  query_video_generation: "查看视频进度",
  text_to_image: "生成图片",
  text_to_audio: "生成语音",
  voice_clone: "克隆声音",
  voice_design: "设计声音",
  list_voices: "查看可用声音",
  play_audio: "播放音频",
  browser_navigate: "打开网页",
  browser_navigate_back: "返回上一页",
  browser_snapshot: "看网页",
  browser_take_screenshot: "截图",
  browser_click: "点击",
  browser_type: "输入",
  browser_fill_form: "填表",
  browser_select_option: "选择选项",
  browser_press_key: "按键",
  browser_hover: "悬停",
  browser_drag: "拖动",
  browser_drop: "拖入文件",
  browser_file_upload: "上传文件",
  browser_find: "在网页里查找",
  browser_wait_for: "等网页加载",
  browser_tabs: "切换标签页",
  browser_handle_dialog: "处理弹窗",
  browser_evaluate: "运行网页脚本",
  browser_resize: "调整窗口大小",
  browser_emulate_media: "切换显示模式",
  browser_console_messages: "看网页控制台",
  browser_network_requests: "看网络请求",
  browser_network_request: "看网络请求详情",
  browser_close: "关闭浏览器",
};

/** A connector tool in plain words; unknown ones keep their own name, made readable. */
export function toolLabel(raw: string): string {
  return TOOL_LABELS[raw] ?? raw.replace(/_/g, " ");
}

/* ---------- What it did, in plain words ---------- */

function argText(args: unknown, ...keys: string[]): string {
  if (!args || typeof args !== "object") return "";
  for (const key of keys) {
    const value = (args as Record<string, unknown>)[key];
    if (typeof value === "string" && value.trim()) return value.trim();
  }
  return "";
}

function shorten(text: string, max = 36): string {
  const flat = text.replace(/\s+/g, " ");
  return flat.length > max ? `${flat.slice(0, max - 1)}…` : flat;
}

function siteOf(url: string): string {
  try {
    return new URL(url).hostname.replace(/^www\./, "");
  } catch {
    return shorten(url);
  }
}

function fileOf(path: string): string {
  return path.split(/[\\/]/).filter(Boolean).at(-1) ?? path;
}

/** Connector tools by their last word (``mcp_<server>_<tool>``). */
const APP_ACTIONS: Record<string, (args: unknown) => string> = {
  list_events: () => "查看了日程",
  create_event: (args) => {
    const title = argText((args as Record<string, unknown>)?.create_event_request ?? args, "title", "summary");
    return title ? `添加了日程「${shorten(title)}」` : "添加了日程";
  },
  update_event: () => "修改了日程",
  list_calendars: () => "查看了日历",
  generate_video: (args) => `开始生成视频：${shorten(argText(args, "prompt"))}`,
  query_video_generation: () => "查看了视频生成进度",
  text_to_image: (args) => `生成了图片：${shorten(argText(args, "prompt"))}`,
  text_to_audio: () => "生成了一段语音",
  browser_navigate: (args) => `打开了网页 ${siteOf(argText(args, "url"))}`,
  browser_navigate_back: () => "在网页上返回了上一页",
  browser_snapshot: () => "看了网页内容",
  browser_take_screenshot: () => "截了网页的图",
  browser_click: (args) => `在网页上点了「${shorten(argText(args, "element"), 24)}」`,
  browser_type: (args) => `在网页的「${shorten(argText(args, "element"), 20)}」里输入了内容`,
  browser_fill_form: () => "在网页上填写了表单",
  browser_select_option: (args) => `在网页上选了「${shorten(argText(args, "element"), 20)}」`,
  browser_file_upload: () => "往网页上传了文件",
  browser_press_key: (args) => `在网页上按了 ${argText(args, "key")}`.trim(),
  browser_tabs: () => "切换了网页标签",
  browser_close: () => "关闭了浏览器",
};

/** One finished tool call as a sentence, e.g. "搜索了「东京天气」". */
export function describeToolCall(name: string, args: unknown): string {
  switch (name) {
    case "helper_ask":
      return `交给「${shorten(argText(args, "helper"), 12)}」：${shorten(argText(args, "task"), 24)}`;
    case "connect_request":
      return `请你连接 ${shorten(argText(args, "service"), 16)}`;
    case "helper_create":
      return `新建了帮手「${shorten(argText(args, "name", "title"), 12)}」`;
    case "web_search":
      return `搜索了「${shorten(argText(args, "query", "q"))}」`;
    case "web_fetch":
      return `看了网页 ${siteOf(argText(args, "url"))}`;
    case "read_file":
    case "list_dir":
    case "find_files":
    case "grep":
    case "rg":
      return `翻看了 ${fileOf(argText(args, "path", "pattern")) || "文件"}`;
    case "list_sessions":
    case "read_session":
    case "search_sessions":
      return "翻看了之前的对话";
    case "write_file":
    case "edit_file":
    case "apply_patch":
      return `整理了文件 ${fileOf(argText(args, "path"))}`.trim();
    case "exec":
    case "exec_session":
      return `运行了命令 ${shorten(argText(args, "command", "cmd"), 28)}`.trim();
    case "generate_image":
      return `画了一张图：${shorten(argText(args, "prompt"))}`;
    case "cron": {
      const action = argText(args, "action");
      if (action === "add") return `安排了定时任务「${shorten(argText(args, "name", "message"))}」`;
      if (action === "remove") return "取消了一个定时任务";
      return "查看了定时任务";
    }
    case "message":
      return "给你发了消息";
    case "spawn":
    case "subagent":
      return "安排了一个助手去做事";
    case "create_goal":
      return "制定了计划";
    case "update_goal":
      return "更新了计划进度";
    case "my":
      return "查看了自己的设置";
  }
  const mcp = parseMcpToolName(name);
  if (mcp) {
    const known = APP_ACTIONS[mcp.tool];
    if (known) return known(args);
    if (mcp.tool.startsWith("browser_")) return "操作了浏览器";
    if (mcp.server === "email") return "查看了邮箱";
    return `使用了 ${mcp.server}（${mcp.tool}）`;
  }
  return toolActivity(name).label.replace(/^正在/, "");
}

/** Finished tool calls in the chat, newest first; actions shown as approval cards are left out. */

export function toolActions(messages: UIMessage[]): ActivityEntry[] {
  const entries = new Map<string, ActivityEntry>();
  for (const message of messages) {
    for (const event of message.toolEvents ?? []) {
      const name = String(event.name ?? "");
      if (!name || !event.call_id || wasDeferred(event) || behindTheScenes(name)) continue;
      if (event.phase !== "end" && event.phase !== "error") continue;
      const failed = event.phase === "error" || Boolean(event.error);
      entries.set(event.call_id, {
        id: event.call_id,
        kind: "action",
        activity: toolActivity(name).kind,
        title: describeToolCall(name, event.arguments),
        status: failed ? "出错了" : "",
        tone: failed ? "bad" : "muted",
        at: message.createdAt,
      });
    }
  }
  return [...entries.values()];
}

/** Everything done (or waiting to be done) on the user's behalf, newest first. */
export function activityLog(
  approvals: ApprovalRecord[],
  jobs: {
    id: string;
    name: string;
    runs?: { id?: string; at_ms: number; status: string }[];
    state?: { run_history?: { run_at_ms: number; status: string }[] };
  }[],
  messages: UIMessage[] = [],
  limit = 30,
): ActivityEntry[] {
  const entries: ActivityEntry[] = approvals.map((approval) => {
    const [status, tone] = APPROVAL_STATUS[approval.status];
    return {
      id: approval.id,
      kind: "approval",
      title: approvalTitle(approval),
      status,
      tone,
      at: approval.decided_at_ms ?? approval.created_at_ms,
    };
  });
  for (const job of jobs) {
    if (job.runs?.length) {
      for (const run of job.runs) {
        entries.push({
          id: `${job.id}-${run.id ?? run.at_ms}`,
          kind: "task",
          title: job.name,
          status: run.status === "ok" ? "已运行" : run.status === "skipped" ? "跳过了" : "出错了",
          tone: run.status === "ok" ? "ok" : run.status === "skipped" ? "muted" : "bad",
          at: run.at_ms,
        });
      }
    } else {
      for (const run of job.state?.run_history ?? []) {
        entries.push({
          id: `${job.id}-${run.run_at_ms}`,
          kind: "task",
          title: job.name,
          status: run.status === "ok" ? "已运行" : run.status === "skipped" ? "跳过了" : "出错了",
          tone: run.status === "ok" ? "ok" : run.status === "skipped" ? "muted" : "bad",
          at: run.run_at_ms,
        });
      }
    }
  }
  entries.push(...toolActions(messages));
  return entries.sort((a, b) => b.at - a.at).slice(0, limit);
}

/* ---------- Who answered ---------- */

export interface ReplyRoute {
  /** The preset (or model) that wrote this reply. */
  answeredBy: string;
  /** Why it was chosen, when the router made the choice. */
  why: string | null;
  /** The chosen model failed and a fallback answered instead. */
  fallback: boolean;
}

/** Shown under a reply when the router picked its model or a fallback stepped in. */
export function replyRoute(message: UIMessage, decisions: ReadonlyMap<string, RoutingDecision>): ReplyRoute | null {
  const decision = message.turnId ? decisions.get(message.turnId) : undefined;
  const source = message.responseSources?.at(-1);
  const fallback = Boolean(source?.fallback);
  if (!decision && !fallback) return null;
  const answeredBy = source?.preset || source?.model || decision?.preset;
  if (!answeredBy) return null;
  const why = decision ? (decision.reason ? `${decision.rule}：${decision.reason}` : decision.rule) : null;
  return { answeredBy, why, fallback };
}
