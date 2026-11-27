import { memo, useEffect, useMemo, useRef, useState, type MouseEvent, type ReactNode } from "react";
import { BellRing, Brain, ChevronDown, ChevronRight, ChevronUp, ListChecks, ListTree, Quote, Target } from "lucide-react";

import { MarkdownText } from "@/components/MarkdownText";
import { formatQuotedUserMessage, parseQuotedUserMessage } from "@/lib/user-message-quote";
import type { UIMediaAttachment, UIMessage } from "@/lib/types";

import { androidShell } from "./android-shell";
import { ApprovalCard } from "./ApprovalCard";
import { EmojiIcon } from "./EmojiIcon";
import { DraftCard, FileCard } from "./FileCard";
import { spokenText, type FileDraft } from "./fileKinds";
import { Pressable, type MessageAction } from "./MessageActions";
import { SourcesBar } from "./SourcesBar";
import type { SpeechPlayerStatus } from "./useSpeechPlayer";
import { usePetScale } from "./appearance";
import { CardPlaceholder, CardView } from "./Cards";
import type { ApprovalRecord, ApprovalScope, RoutingDecision } from "./companion-api";
import { goalText, isGoalMessage } from "./GoalBanner";
import { PetStage, PetView } from "./Pet";
import type { PetAnimation } from "./petAnimations";
import { elapsedWords, isProactive, replyRoute, type ChatItem, type ReplyBlock, type ReplyRoute, type TurnTask } from "./presentation";
import { useOpenViewer, type ViewerItem } from "./Viewer";

function hello(now = new Date()): string {
  const hour = now.getHours();
  return hour < 5 ? "这么晚还没睡？" : hour < 11 ? "早上好" : hour < 14 ? "中午好" : hour < 18 ? "下午好" : "晚上好";
}

export function EmptyThread({
  avatar,
  face,
  hint,
  starters,
  onSend,
}: {
  avatar: string;
  face?: ReactNode;
  hint?: string;
  starters?: ReadonlyArray<{ label: string; prompt: string }>;
  onSend?: (prompt: string) => void;
}) {
  const petPx = usePetScale();
  return (
    <div className="c-rise flex min-h-full flex-col items-center justify-center px-6 pb-8 text-center">
      {face ?? <PetStage value={avatar} size={petPx(120)} />}
      <h2 className="mt-4 text-[22px] font-semibold">{hello()}</h2>
      {hint && <p className="c-muted mt-1.5 max-w-xs text-[14.5px] leading-relaxed">{hint}</p>}
      {starters && starters.length > 0 && onSend && (
        <div className="mt-5 flex max-w-md flex-wrap justify-center gap-2">
          {starters.map((starter) => (
            <button
              key={starter.label}
              type="button"
              onClick={() => onSend(starter.prompt)}
              className="c-card c-pressable rounded-full px-3.5 py-2 text-[13.5px] leading-snug"
            >
              {starter.label}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

export function TypingBubble() {
  return (
    <div className="c-rise flex" role="status" aria-label="正在输入">
      <div className="c-bubble-agent flex items-center gap-1 rounded-[20px] rounded-bl-[6px] px-4 py-3.5">
        <span className="c-dot h-1.5 w-1.5 rounded-full bg-current opacity-60" />
        <span className="c-dot h-1.5 w-1.5 rounded-full bg-current opacity-60" />
        <span className="c-dot h-1.5 w-1.5 rounded-full bg-current opacity-60" />
      </div>
    </div>
  );
}

function viewerItems(media: UIMediaAttachment[]): ViewerItem[] {
  return media.flatMap((item, index) =>
    item.url
      ? [{
          key: `${item.url}-${index}`,
          kind: item.kind === "image" || item.kind === "video" ? item.kind : ("document" as const),
          url: item.url,
          name: item.name ?? (item.kind === "image" ? "图片.png" : item.kind === "video" ? "视频.mp4" : "文件"),
          title: item.name ?? (item.kind === "image" ? "图片" : item.kind === "video" ? "视频" : "文件"),
        }]
      : [],
  );
}

function MediaCards({ media, align }: { media: UIMediaAttachment[]; align: "start" | "end" }) {
  const openViewer = useOpenViewer();
  const viewable = viewerItems(media);
  /** Opens the full-screen viewer when there is one; links still work without it. */
  const open = (url: string | undefined) => (event: MouseEvent) => {
    const index = viewable.findIndex((item) => item.url === url);
    if (!openViewer || index < 0) return;
    event.preventDefault();
    openViewer(viewable, index);
  };
  return (
    <div className={`flex flex-col gap-1.5 ${align === "end" ? "items-end" : "items-start"}`}>
      {media.map((item, index) => {
        const key = `${item.url ?? item.name ?? "media"}-${index}`;
        if (item.kind === "image" && item.url) {
          return (
            <a key={key} href={item.url} target="_blank" rel="noreferrer" onClick={open(item.url)} className="block max-w-[78%]">
              <img
                src={item.url}
                alt={item.name ?? "图片"}
                loading="lazy"
                className="max-h-80 w-auto rounded-[20px] object-cover"
              />
            </a>
          );
        }
        if (item.kind === "video" && item.url) {
          return <video key={key} src={item.url} controls playsInline className="max-h-80 max-w-[78%] rounded-[20px]" />;
        }
        return <FileCard key={key} item={item} onOpen={open(item.url)} />;
      })}
    </div>
  );
}

function mediaOf(message: UIMessage): UIMediaAttachment[] {
  const images: UIMediaAttachment[] = (message.images ?? [])
    .filter((image) => image.url)
    .map((image) => ({ kind: "image", url: image.url, name: image.name }));
  return [...images, ...(message.media ?? [])];
}

function UserTurn({
  message,
  me,
  relay,
  editing,
}: {
  message: UIMessage;
  me?: ReactNode;
  relay?: Speaker;
  editing?: {
    draft: string;
    busy: boolean;
    error: string | null;
    onDraftChange: (value: string) => void;
    onConfirm: () => void;
    onCancel: () => void;
  };
}) {
  const media = mediaOf(message);
  const failed = message.deliveryStatus === "failed";
  const goal = isGoalMessage(message.content);
  // A message answering an earlier one (long-press → 引用): the quote shows above it.
  const parsed = parseQuotedUserMessage(goal ? goalText(message.content) : message.content);
  const text = parsed.content;
  if (relay && message.sessionMessage) {
    // Chat-app style, work the companion handed over is its message, not the user's.
    return (
      <div className="c-rise flex items-start gap-2">
        {relay.face}
        <div className="flex min-w-0 flex-1 flex-col items-start gap-1">
          <span className="c-muted px-1 text-[12.5px]">
            {relay.name} 转来 · {message.sessionMessage.session.name}
          </span>
          <div className="c-bubble-agent c-bubble-text max-w-[85%] whitespace-pre-wrap rounded-[20px] rounded-tl-[6px] px-4 py-2.5">{text}</div>
        </div>
      </div>
    );
  }
  const turn = (
    <div className={`c-rise flex flex-col items-end gap-1 ${me ? "min-w-0 flex-1" : ""}`}>
      {goal && (
        <span className="c-muted flex items-center gap-1 px-1 text-xs">
          <Target className="h-3.5 w-3.5" aria-hidden="true" />
          交给它的目标
        </span>
      )}
      {message.sessionMessage && !relay && (
        // Work another chat handed this helper.
        <span className="c-muted px-1 text-xs">来自{message.sessionMessage.session.name}</span>
      )}
      {media.length > 0 && <MediaCards media={media} align="end" />}
      {parsed.quotedContext && (
        <div className="c-muted max-w-[80%] border-l-2 px-2.5 py-0.5 text-[13px] leading-snug" style={{ borderColor: "var(--c-border)" }}>
          <span className="line-clamp-3 whitespace-pre-wrap">{parsed.quotedContext}</span>
        </div>
      )}
      {editing ? (
        <div className="c-card flex w-full min-w-[240px] max-w-[92%] flex-col gap-2 rounded-2xl p-3">
          <textarea
            value={editing.draft}
            onChange={(event) => editing.onDraftChange(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Escape") {
                event.preventDefault();
                editing.onCancel();
              } else if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) {
                event.preventDefault();
                if (editing.draft.trim() && !editing.busy) editing.onConfirm();
              }
            }}
            rows={3}
            autoFocus
            aria-label="编辑消息内容"
            disabled={editing.busy}
            className="w-full resize-y rounded-xl bg-transparent p-1.5 text-[15px] leading-snug outline-none ring-1 ring-[var(--c-border)] focus:ring-2 focus:ring-[var(--c-primary)]"
          />
          {editing.error && (
            <p className="c-danger text-[12px]" role="alert">
              {editing.error}
            </p>
          )}
          <div className="flex items-center justify-end gap-2">
            <button
              type="button"
              disabled={editing.busy}
              onClick={editing.onCancel}
              className="c-pressable rounded-full px-3 py-1 text-[13px]"
              style={{ background: "var(--c-fill)" }}
            >
              取消
            </button>
            <button
              type="button"
              disabled={editing.busy || !editing.draft.trim()}
              onClick={editing.onConfirm}
              className="c-primary rounded-full px-3.5 py-1 text-[13px] font-semibold disabled:opacity-45"
            >
              {editing.busy ? "重发中…" : "确认并重发"}
            </button>
          </div>
        </div>
      ) : (
        text.trim() && (
          <div
            className={`c-bubble-user c-bubble-text max-w-[80%] whitespace-pre-wrap rounded-[20px] px-4 py-2.5 ${me ? "rounded-tr-[6px]" : "rounded-br-[6px]"}`}
          >
            {text}
          </div>
        )
      )}
      {failed && <span className="c-danger px-1 text-xs">未发送</span>}
    </div>
  );
  if (!me) return turn;
  // As in a chat app: the user's face on the right of their message.
  return (
    <div className="flex items-start justify-end gap-2">
      {turn}
      {me}
    </div>
  );
}

/** In a chat with a helper or a group: who said a reply, as a face (and a name in a group). */
export interface Speaker {
  face: ReactNode;
  name?: string;
}

/** "由 pro 回答"; tapping shows why the router picked it. */
function AnsweredBy({ route }: { route: ReplyRoute }) {
  const [open, setOpen] = useState(false);
  return (
    <button
      type="button"
      onClick={() => setOpen((value) => !value)}
      aria-expanded={open}
      className="c-muted px-1 text-left text-[12px] leading-snug"
    >
      由 {route.answeredBy} 回答{route.fallback ? "（原模型出错，换了备用）" : ""}
      {open && route.why && <span className="block">{route.why}</span>}
    </button>
  );
}

/** Under a reply that took steps: how many and how long; a tap lists them (as 做过的事 does). */
function TaskChip({ task }: { task: TurnTask }) {
  const [open, setOpen] = useState(false);
  const failed = task.steps.some((step) => step.failed);
  return (
    <div className="flex flex-col items-start gap-1 px-1">
      <button
        type="button"
        onClick={() => setOpen((value) => !value)}
        aria-expanded={open}
        className="c-muted c-pressable inline-flex items-center gap-1 rounded-full px-2.5 py-1 text-[12px]"
        style={{ background: "var(--c-fill)" }}
      >
        <ListChecks className="h-3.5 w-3.5" aria-hidden="true" />
        做了 {task.steps.length} 步{task.ms >= 1000 ? ` · 用时 ${elapsedWords(task.ms)}` : ""}
        {failed ? " · 有一步出错" : ""}
        <ChevronDown className={`h-3 w-3 transition-transform ${open ? "rotate-180" : ""}`} aria-hidden="true" />
      </button>
      {open && (
        <ol className="c-muted ml-2 flex list-decimal flex-col gap-0.5 pl-4 text-[13px] leading-snug">
          {task.steps.map((step) => (
            <li key={step.id} className={step.failed ? "c-danger" : ""}>
              {step.title}
              {step.failed ? "（出错了）" : ""}
            </li>
          ))}
        </ol>
      )}
    </div>
  );
}

export interface MarkdownHeading {
  level: 2 | 3;
  text: string;
  index: number;
}

const LONG_REPLY_CHAR_THRESHOLD = 1800;

/** Extract level 2/3 headings from completed Markdown replies while ignoring fenced code blocks. */
export function extractMarkdownHeadings(markdown: string): MarkdownHeading[] {
  const out: MarkdownHeading[] = [];
  let inFence: string | null = null;
  for (const rawLine of markdown.split(/\r?\n/)) {
    const trimmed = rawLine.trim();
    const fenceMatch = /^(`{3,}|~{3,})/.exec(trimmed);
    if (fenceMatch) {
      const marker = fenceMatch[1][0];
      if (!inFence) inFence = marker;
      else if (inFence === marker) inFence = null;
      continue;
    }
    if (inFence) continue;
    const match = /^(#{2,3})\s+(.+?)\s*#*$/.exec(trimmed);
    if (!match) continue;
    const level = match[1].length as 2 | 3;
    const clean = match[2]
      .replace(/\[([^\]]+)\]\([^)]*\)/g, "$1")
      .replace(/[*_`~]/g, "")
      .trim();
    if (!clean) continue;
    out.push({ level, text: clean, index: out.length });
  }
  return out;
}

function ReplyTurn({
  message,
  blocks,
  linked,
  route,
  send,
  speaker,
  byline,
  task,
}: {
  message: UIMessage;
  blocks: ReplyBlock[];
  linked: UIMediaAttachment[];
  route: ReplyRoute | null;
  /** Chat-app style: a face beside the reply, and a name over it in a group. */
  speaker?: Speaker;
  /** Set while this is the latest reply: its cards can still be answered. */
  send?: (text: string) => void;
  /** Who is speaking, when it isn't plain (the companion right after a helper's answer). */
  byline?: string;
  /** Its turn's steps, once the turn is over. */
  task?: TurnTask;
}) {
  const attached = mediaOf(message);
  const media = [...attached, ...linked.filter((item) => !attached.some((own) => own.url === item.url))];
  const proactive = isProactive(message);
  const visible = blocks;
  const replyContainerRef = useRef<HTMLDivElement>(null);
  const [outlineOpen, setOutlineOpen] = useState(false);
  const [collapsed, setCollapsed] = useState(false);

  const fullText = useMemo(
    () =>
      visible
        .filter((b): b is Extract<ReplyBlock, { type: "bubble" }> => b.type === "bubble")
        .map((b) => b.text)
        .join("\n\n"),
    [visible],
  );
  const headings = useMemo(
    () => (message.isStreaming ? [] : extractMarkdownHeadings(fullText)),
    [fullText, message.isStreaming],
  );
  const hasOutline = headings.length >= 3;
  const isLongReply = !message.isStreaming && fullText.length > LONG_REPLY_CHAR_THRESHOLD;
  const showLongControls = !message.isStreaming && (hasOutline || isLongReply);

  const jumpToHeading = (headingIndex: number) => {
    const scrollTarget = () => {
      const nodes = replyContainerRef.current?.querySelectorAll<HTMLElement>("h2, h3");
      const target = nodes?.[headingIndex];
      if (!target) return;
      const reduced = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
      target.scrollIntoView?.({ behavior: reduced ? "auto" : "smooth", block: "start" });
    };
    if (collapsed) {
      setCollapsed(false);
      window.requestAnimationFrame(scrollTarget);
    } else {
      scrollTarget();
    }
  };

  const body = (
    <div ref={replyContainerRef} className={`c-rise flex flex-col items-start gap-1 ${speaker ? "min-w-0 flex-1" : ""}`}>
      {speaker ? (
        speaker.name && <span className="c-muted px-1 text-[12.5px]">{speaker.name}</span>
      ) : message.source?.kind === "helper" ? (
        // A helper speaking for itself (in a chat that handed it work): its face and name.
        <span className="flex items-center gap-1.5 px-1 text-[13px] font-semibold">
          <EmojiIcon emoji={(message.source.label ?? "").split(" ")[0] || "🤖"} size={20} />
          {(message.source.label ?? "").split(" ").slice(1).join(" ") || message.source.label}
        </span>
      ) : byline && !proactive ? (
        <span className="c-muted px-1 text-[12.5px] font-medium">{byline}</span>
      ) : (
        proactive && (
          <span className="c-muted flex items-center gap-1 px-1 text-xs">
            <BellRing className="h-3.5 w-3.5" aria-hidden="true" />
            {message.source?.label || "主动消息"}
          </span>
        )
      )}
      {showLongControls && (
        <div
          data-testid="reply-outline-bar"
          className="flex max-w-[85%] flex-col items-start gap-1.5 pb-0.5"
        >
          <div className="flex flex-wrap items-center gap-1.5">
            {hasOutline && (
              <button
                type="button"
                aria-expanded={outlineOpen}
                onClick={() => setOutlineOpen((v) => !v)}
                className="c-pressable inline-flex items-center gap-1 rounded-full px-2.5 py-1 text-[12px] font-medium"
                style={{ background: "var(--c-fill)", color: "var(--c-text)" }}
              >
                <ListTree className="h-3.5 w-3.5" style={{ color: "var(--c-primary)" }} aria-hidden="true" />
                目录 ({headings.length} 节)
                <ChevronDown
                  className={`h-3 w-3 transition-transform ${outlineOpen ? "rotate-180" : ""}`}
                  aria-hidden="true"
                />
              </button>
            )}
            <button
              type="button"
              aria-expanded={!collapsed}
              onClick={() => setCollapsed((v) => !v)}
              className="c-muted c-pressable inline-flex items-center gap-1 rounded-full px-2.5 py-1 text-[12px] font-medium"
              style={{ background: "var(--c-fill)" }}
            >
              {collapsed ? (
                <>
                  <ChevronDown className="h-3.5 w-3.5" aria-hidden="true" />
                  展开全文
                </>
              ) : (
                <>
                  <ChevronUp className="h-3.5 w-3.5" aria-hidden="true" />
                  收起长文
                </>
              )}
            </button>
          </div>
          {hasOutline && outlineOpen && (
            <nav
              aria-label="回复章节目录"
              data-testid="reply-outline-nav"
              className="c-card flex w-full flex-col gap-0.5 rounded-2xl px-2.5 py-2"
            >
              {headings.map((h) => (
                <button
                  key={`${h.index}-${h.text}`}
                  type="button"
                  onClick={() => jumpToHeading(h.index)}
                  className={`c-pressable truncate rounded-lg px-2 py-1 text-left text-[12.5px] leading-snug hover:underline ${
                    h.level === 3 ? "c-muted pl-5" : "font-medium"
                  }`}
                >
                  {h.text}
                </button>
              ))}
            </nav>
          )}
        </div>
      )}
      <div className={collapsed ? "relative max-h-[220px] w-full overflow-hidden" : "contents"}>
        {visible.map((block, index) => {
          // Cards keep their position in the full reply, so a saved answer stays attached to them.
          const key = blocks.indexOf(block) >= 0 ? blocks.indexOf(block) : index;
          if (block.type === "card") return <CardView key={key} card={block.card} send={send} stateKey={`${message.id}:${key}`} />;
          if (block.type === "pending") return <CardPlaceholder key={key} kind={block.kind} />;
          // Consecutive bubbles read as one message: tight corners where they meet, a tail on the last.
          const joined = index > 0 && visible[index - 1].type === "bubble";
          const streaming = Boolean(message.isStreaming && index === visible.length - 1);
          // Chat-app style the tail is at the top, by the face.
          const corners = speaker ? "rounded-tl-[6px]" : `rounded-bl-[6px] ${joined ? "rounded-tl-[6px]" : ""}`;
          return (
            <div
              key={key}
              data-reply-body="true"
              className={`c-bubble-agent c-bubble-text max-w-[85%] rounded-[20px] px-4 py-2.5 ${corners} ${
                streaming
                  ? "[&>div:first-child:not(:has(*))]:inline [&>div:first-child:has(>p:last-child)]:contents [&>div:first-child>p:has(+p:last-child)]:mb-[0.5em] [&>div:first-child>p:last-child]:inline"
                  : ""
              }`}
            >
              <MarkdownText streaming={streaming}>{block.text}</MarkdownText>
              {streaming && (
                <span
                  aria-hidden="true"
                  data-streaming-cursor="true"
                  className="ml-0.5 inline-block h-[1em] w-[2px] translate-y-[2px] animate-pulse rounded-full align-baseline motion-reduce:animate-none"
                  style={{ background: "var(--c-primary)" }}
                />
              )}
            </div>
          );
        })}
        {collapsed && (
          <div
            data-testid="reply-collapsed-fade"
            className="pointer-events-none absolute inset-x-0 bottom-0 flex max-w-[85%] items-end justify-center pb-2 pt-12"
            style={{ background: "linear-gradient(to top, var(--c-bg) 35%, transparent)" }}
          >
            <button
              type="button"
              onClick={() => setCollapsed(false)}
              className="c-float c-pressable pointer-events-auto inline-flex items-center gap-1 rounded-full px-3 py-1 text-[12px] font-medium"
              style={{ color: "var(--c-primary)" }}
            >
              <ChevronDown className="h-3.5 w-3.5" aria-hidden="true" />
              展开全文
            </button>
          </div>
        )}
      </div>
      {media.length > 0 && <MediaCards media={media} align="start" />}
      {!message.isStreaming && message.sources && message.sources.length > 0 && <SourcesBar sources={message.sources} />}
      {task && !message.isStreaming && <TaskChip task={task} />}
      {route && !message.isStreaming && <AnsweredBy route={route} />}
    </div>
  );
  if (!speaker) return body;
  return (
    <div className="flex items-start gap-2">
      {speaker.face}
      {body}
    </div>
  );
}

/** While it works on a reply: the pet acting out the current step, and what that step is. */
export interface WorkingPet {
  avatar: string | null;
  animation: PetAnimation;
  loop: boolean;
  onDone: () => void;
  /** E.g. "正在运行命令…"; dots when nothing is known yet. */
  label: string | null;
  /** Someone else working (a helper in its own chat): its face instead of the pet. */
  face?: ReactNode;
}

function WorkingRow({ avatar, animation, loop, onDone, label, face }: WorkingPet) {
  const petPx = usePetScale();
  return (
    <div className="c-rise flex items-end gap-2" role="status" aria-label={label ?? "正在输入"}>
      {face ?? <PetView value={avatar} animation={animation} loop={loop} onDone={onDone} size={petPx(72)} />}
      <div className={`c-bubble-agent flex min-h-10 items-center gap-1 rounded-[20px] px-4 py-2 text-[14px] ${face ? "self-start rounded-tl-[6px]" : "mb-2"}`}>
        {label ?? (
          <>
            <span className="c-dot h-1.5 w-1.5 rounded-full bg-current opacity-60" />
            <span className="c-dot h-1.5 w-1.5 rounded-full bg-current opacity-60" />
            <span className="c-dot h-1.5 w-1.5 rounded-full bg-current opacity-60" />
          </>
        )}
      </div>
    </div>
  );
}

export const ChatThread = memo(function ChatThread({
  items,
  typing,
  routes,
  onDecide,
  working,
  onSend,
  messenger,
  onMessageAction,
  onEditMessage,
  onForkMessage,
  onQuoteSelection,
  speakingMessageId,
  speechStatus,
  companionName,
  draft,
  highlightedTurnId,
  onOpenMemory,
}: {
  items: ChatItem[];
  typing: boolean;
  /** A document it is writing now, shown as it will look before it is sent. */
  draft?: FileDraft | null;
  /** Names the companion's reply right after a helper's, so the two aren't read as one. */
  companionName?: string;
  /** Sends what a card's button or form answers, as the user's message. */
  onSend?: (text: string) => void;
  /** Router decisions by turn id. */
  routes: ReadonlyMap<string, RoutingDecision>;
  onDecide: (approval: ApprovalRecord, decision: "approve" | "reject", scope?: ApprovalScope) => Promise<void>;
  /** Shown instead of the typing dots while it works. */
  working?: WorkingPet;
  /**
   * A helper's or a group's chat, laid out as in a chat app: faces beside the messages (who said
   * each reply, and the user's own), with what it did and cards lined up after the faces.
   */
  messenger?: { speaker: (message: UIMessage) => Speaker; me: ReactNode; relay?: Speaker };
  /** Long-press a message: 引用 / 复制 / 删除 (``reply`` is the companion's whole reply). */
  onMessageAction?: (action: MessageAction, message: UIMessage, role: "user" | "reply") => void;
  /** Inline edit and resubmit a committed user message. */
  onEditMessage?: (message: UIMessage, content: string) => Promise<void> | void;
  /** Fork a side chat branching up to the target message's turn. */
  onForkMessage?: (message: UIMessage) => Promise<void> | void;
  /** Quote a user-selected snippet from inside an assistant reply into the composer. */
  onQuoteSelection?: (text: string) => void;
  /** Message ID currently targeted by neural or fallback TTS playback. */
  speakingMessageId?: string | null;
  /** Playback status for the active TTS message. */
  speechStatus?: SpeechPlayerStatus;
  /** Temporary turn highlight pulse when jumping from a search result. */
  highlightedTurnId?: string | null;
  /** Opens StatusSheet's 身份与记忆 pane when a memory pill is tapped. */
  onOpenMemory?: () => void;
}) {
  const indent = messenger ? "pl-11" : "";
  const threadRef = useRef<HTMLDivElement>(null);
  const [editing, setEditing] = useState<{
    messageId: string;
    message: UIMessage;
    quotedPrefix: string | null;
    draft: string;
    busy: boolean;
    error: string | null;
  } | null>(null);
  const [selectionQuote, setSelectionQuote] = useState<{
    text: string;
    top: number;
    left: number;
    positioned: boolean;
  } | null>(null);

  useEffect(() => {
    if (!onQuoteSelection) {
      setSelectionQuote(null);
      return;
    }
    const updateSelection = () => {
      const sel = window.getSelection?.();
      if (!sel || sel.isCollapsed || sel.rangeCount === 0) {
        setSelectionQuote(null);
        return;
      }
      const selectedText = sel.toString().trim();
      if (!selectedText) {
        setSelectionQuote(null);
        return;
      }
      const range = sel.getRangeAt(0);
      const node = range.commonAncestorContainer;
      const el = node.nodeType === 1 ? (node as Element) : node.parentElement;
      if (!el || !threadRef.current?.contains(el) || !el.closest('[data-reply-body="true"]')) {
        setSelectionQuote(null);
        return;
      }
      const rect = range.getBoundingClientRect?.();
      const hasCoords = Boolean(rect && (rect.width > 0 || rect.height > 0));
      const vw = typeof window !== "undefined" ? window.innerWidth : 390;
      const top = hasCoords ? Math.max(48, rect.top - 10) : 0;
      const left = hasCoords ? Math.max(72, Math.min(vw - 72, rect.left + rect.width / 2)) : 0;
      setSelectionQuote({ text: selectedText.slice(0, 300), top, left, positioned: hasCoords });
    };
    document.addEventListener("selectionchange", updateSelection);
    document.addEventListener("mouseup", updateSelection);
    document.addEventListener("keyup", updateSelection);
    return () => {
      document.removeEventListener("selectionchange", updateSelection);
      document.removeEventListener("mouseup", updateSelection);
      document.removeEventListener("keyup", updateSelection);
    };
  }, [onQuoteSelection]);

  const startEdit = (message: UIMessage) => {
    const goal = isGoalMessage(message.content);
    const parsed = parseQuotedUserMessage(goal ? goalText(message.content) : message.content);
    setEditing({
      messageId: message.id,
      message,
      quotedPrefix: parsed.quotedContext,
      draft: parsed.content,
      busy: false,
      error: null,
    });
  };

  const confirmEdit = async () => {
    if (!editing || editing.busy) return;
    const rawDraft = editing.draft.trim();
    if (!rawDraft) return;
    const nextContent = editing.quotedPrefix ? formatQuotedUserMessage(rawDraft, editing.quotedPrefix) : rawDraft;
    setEditing((prev) => (prev ? { ...prev, busy: true, error: null } : null));
    try {
      await onEditMessage?.(editing.message, nextContent);
      setEditing(null);
    } catch (error) {
      const msg = error instanceof Error ? error.message : "编辑重发失败";
      setEditing((prev) => (prev ? { ...prev, busy: false, error: msg } : null));
    }
  };

  // Screen-reader completion announcement: announce once when streaming finishes rather than on every delta.
  let latestReplyItem: Extract<ChatItem, { type: "reply" }> | undefined;
  for (let i = items.length - 1; i >= 0; i -= 1) {
    const candidate = items[i];
    if (candidate.type === "reply") {
      latestReplyItem = candidate;
      break;
    }
  }
  const replyBusy = typing || Boolean(latestReplyItem?.message.isStreaming);
  const wasReplyBusyRef = useRef(replyBusy);
  const [srAnnouncement, setSrAnnouncement] = useState("");
  useEffect(() => {
    if (replyBusy) {
      wasReplyBusyRef.current = true;
      return;
    }
    if (!wasReplyBusyRef.current) return;
    wasReplyBusyRef.current = false;
    if (!latestReplyItem) return;
    const plain = spokenText(latestReplyItem.message.content).trim();
    if (!plain) return;
    const speakerPrefix = companionName ? `${companionName}回复：` : "新回复：";
    setSrAnnouncement(`${speakerPrefix}${plain.slice(0, 200)}`);
  }, [replyBusy, latestReplyItem, companionName]);

  // Only the latest reply's cards still take answers; once the user moves on, older ones just show.
  // A message it sent on its own (a reminder, 主动关心) landing after a card doesn't count: the
  // card's question is still open.
  let lastReply = -1;
  items.forEach((item, index) => {
    const scheduled = item.type === "reply" && (item.message.source?.kind === "cron" || item.message.source?.kind === "checkin");
    if (item.type === "reply" && !scheduled) lastReply = index;
  });
  const answerable = lastReply >= 0 && !items.slice(lastReply + 1).some((item) => item.type === "user") ? lastReply : -1;
  // 重新生成 is for the newest answer to a plain question (it is asked again; files can't be sent again).
  let asked: UIMessage | undefined;
  let askedAt = -1;
  for (let index = 0; index < items.length; index += 1) {
    const item = items[index];
    if (item.type === "user") {
      asked = item.message;
      askedAt = index;
    }
  }
  let regenerable = -1;
  if (asked?.turnId && !typing && mediaOf(asked).length === 0) {
    for (let index = askedAt + 1; index < items.length; index += 1) {
      const item = items[index];
      if (item.type === "reply" && item.message.turnId === asked.turnId && !item.message.source) regenerable = index;
    }
  }
  return (
    <div ref={threadRef} className="relative flex flex-col gap-3 px-4">
      <div className="sr-only" role="status" aria-live="polite" aria-atomic="true">
        {srAnnouncement}
      </div>
      {items.map((item, itemIndex) => {
        if (item.type === "day") {
          return (
            <p key={item.id} className="c-muted pt-2 text-center text-xs">
              {item.label}
            </p>
          );
        }
        if (item.type === "notice") {
          if (item.kind === "memory") {
            if (onOpenMemory) {
              return (
                <button
                  key={item.id}
                  type="button"
                  onClick={onOpenMemory}
                  aria-label={`${item.text}，查看记忆`}
                  className="c-card c-pressable c-rise mx-auto inline-flex max-w-[88%] items-center gap-1.5 self-center rounded-full px-3 py-1.5 text-left text-[12.5px] leading-snug transition-transform active:scale-[0.98] motion-reduce:transition-none"
                >
                  <Brain className="h-3.5 w-3.5 shrink-0" style={{ color: "var(--c-primary)" }} aria-hidden="true" />
                  <span className="c-muted truncate">{item.text}</span>
                  <ChevronRight className="c-muted h-3.5 w-3.5 shrink-0 opacity-70" aria-hidden="true" />
                </button>
              );
            }
            return (
              <div
                key={item.id}
                className="c-card c-rise mx-auto inline-flex max-w-[88%] items-center gap-1.5 self-center rounded-full px-3 py-1 text-[12.5px] leading-snug"
              >
                <Brain className="h-3.5 w-3.5 shrink-0" style={{ color: "var(--c-primary)" }} aria-hidden="true" />
                <span className="c-muted truncate">{item.text}</span>
              </div>
            );
          }
          return (
            <p key={item.id} className="c-muted c-rise mx-auto max-w-[85%] text-center text-[12.5px] leading-snug">
              {item.text}
            </p>
          );
        }
        if (item.type === "user") {
          const isHighlighted = Boolean(highlightedTurnId && item.message.turnId === highlightedTurnId);
          const isEditing = editing?.messageId === item.message.id;
          const userExtra: MessageAction[] = [
            ...(onEditMessage && !item.message.sessionMessage && !isGoalMessage(item.message.content) ? ["edit" as const] : []),
            ...(onForkMessage ? ["fork" as const] : []),
          ];
          const userHasActions = Boolean(onMessageAction || userExtra.length > 0);
          return (
            <div
              key={item.id}
              data-turn-id={item.message.turnId || undefined}
              data-highlighted={isHighlighted ? "true" : undefined}
              className={
                isHighlighted
                  ? "rounded-[22px] ring-2 ring-[var(--c-primary)] ring-offset-2 ring-offset-[var(--c-bg)] transition-shadow duration-500 motion-reduce:transition-none"
                  : undefined
              }
            >
              <Pressable
                align="end"
                timestamp={item.message.createdAt}
                extra={userExtra}
                onAction={
                  isEditing || !userHasActions
                    ? undefined
                    : (action) => {
                        if (action === "edit") startEdit(item.message);
                        else if (action === "fork") void onForkMessage?.(item.message);
                        else onMessageAction?.(action, item.message, "user");
                      }
                }
              >
                <UserTurn
                  message={item.message}
                  me={messenger?.me}
                  relay={messenger?.relay}
                  editing={
                    isEditing && editing
                      ? {
                          draft: editing.draft,
                          busy: editing.busy,
                          error: editing.error,
                          onDraftChange: (value) => setEditing((prev) => (prev ? { ...prev, draft: value } : null)),
                          onConfirm: () => void confirmEdit(),
                          onCancel: () => setEditing(null),
                        }
                      : undefined
                  }
                />
              </Pressable>
            </div>
          );
        }
        if (item.type === "approval")
          return (
            <div key={item.id} className={indent}>
              <ApprovalCard approval={item.approval} onDecide={onDecide} />
            </div>
          );
        const isHighlighted = Boolean(highlightedTurnId && item.message.turnId === highlightedTurnId);
        const replyExtra: MessageAction[] = [
          "speak",
          ...(itemIndex === regenerable ? ["regenerate" as const] : []),
          ...(onForkMessage ? ["fork" as const] : []),
        ];
        const isSpeakingTarget = speakingMessageId === item.message.id;
        return (
          <div
            key={item.id}
            data-turn-id={item.message.turnId || undefined}
            data-highlighted={isHighlighted ? "true" : undefined}
            className={
              isHighlighted
                ? "rounded-[22px] ring-2 ring-[var(--c-primary)] ring-offset-2 ring-offset-[var(--c-bg)] transition-shadow duration-500 motion-reduce:transition-none"
                : undefined
            }
          >
            <Pressable
              align="start"
              timestamp={item.message.createdAt}
              extra={replyExtra}
              speaking={isSpeakingTarget && speechStatus === "playing"}
              speechLoading={isSpeakingTarget && speechStatus === "loading"}
              onAction={
                !onMessageAction && !onForkMessage
                  ? undefined
                  : (action) => {
                      if (action === "fork") void onForkMessage?.(item.message);
                      else onMessageAction?.(action, item.message, "reply");
                    }
              }
            >
              <ReplyTurn
                message={item.message}
                blocks={item.blocks}
                linked={item.media}
                route={replyRoute(item.message, routes)}
                send={itemIndex === answerable && !typing ? onSend : undefined}
                task={typing && itemIndex > askedAt ? undefined : item.task}
                speaker={messenger?.speaker(item.message)}
                byline={
                  !item.message.source && (() => {
                    const before = items[itemIndex - 1];
                    return before?.type === "reply" && before.message.source?.kind === "helper";
                  })()
                    ? companionName
                    : undefined
                }
              />
            </Pressable>
          </div>
        );
      })}
      {typing && draft && <DraftCard key={draft.id} draft={draft} />}
      {typing && (working ? <WorkingRow {...working} /> : <TypingBubble />)}
      {selectionQuote && onQuoteSelection && (
        <div
          className="pointer-events-none sticky bottom-3 z-20 flex justify-center"
          style={
            selectionQuote.positioned
              ? {
                  position: "fixed",
                  top: `${selectionQuote.top}px`,
                  left: `${selectionQuote.left}px`,
                  bottom: "auto",
                  transform: "translate(-50%, -100%)",
                }
              : undefined
          }
        >
          <button
            type="button"
            onPointerDown={(event) => event.preventDefault()}
            onClick={() => {
              onQuoteSelection(selectionQuote.text);
              setSelectionQuote(null);
              window.getSelection?.()?.removeAllRanges?.();
              androidShell()?.haptic?.("light");
            }}
            className="c-float c-primary pointer-events-auto flex items-center gap-1.5 rounded-full px-3 py-1.5 text-[12.5px] font-medium shadow-lg"
          >
            <Quote className="h-3.5 w-3.5" aria-hidden="true" />
            引用追问
          </button>
        </div>
      )}
    </div>
  );
});
