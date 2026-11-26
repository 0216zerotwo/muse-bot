import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { ArrowDown, Menu, Settings2, SlidersHorizontal, X } from "lucide-react";

import { useNanobotStream, type SendAttachment } from "@/hooks/useNanobotStream";
import { useSessionHistory } from "@/hooks/useSessions";
import { copyTextToClipboard } from "@/lib/clipboard";
import type { StreamError } from "@/lib/nanobot-client";
import type { ChatSummary, ConnectionStatus, UIMessage } from "@/lib/types";
import { parseQuotedUserMessage } from "@/lib/user-message-quote";
import { useClient } from "@/providers/ClientProvider";

import { ChatThread, EmptyThread } from "./ChatThread";
import {
  decideApproval,
  deleteMessage,
  fetchAllApprovals,
  fetchApprovals,
  changeFeed,
  fetchComputer,
  fetchFeed,
  fetchModels,
  regenerateReply,
  type ApprovalRecord,
  type ApprovalScope,
  type CompanionChanges,
  type CompanionState,
  type ComputerStatus,
  type FeedPayload,
  type FeedPost,
  type TaughtStep,
  type RoutingDecision,
} from "./companion-api";
import { ApprovalCard } from "./ApprovalCard";
import { Composer } from "./Composer";
import { androidShell } from "./android-shell";
import { readsAfterVoice, speakAloud, type MessageAction } from "./MessageActions";
import { fileDraft } from "./fileKinds";
import { textWithoutCards } from "./replyCards";
import { GOAL_COMMAND, GoalBanner } from "./GoalBanner";
import { BrowserFullscreen } from "./muse/BrowserPane";
import { NameLookRow } from "./muse/Identity";
import { MessengerBar, MessengerEmpty, type ChatApp } from "./muse/Messenger";
import { MuseHeader, MuseRoundButton } from "./muse/MuseHeader";
import { MuseSheet } from "./muse/Sheet";
import { MuseTabBar, StatusSheet, type MuseTab } from "./muse/Shell";
import { PetView } from "./Pet";
import { approvalCallIds, awaitingReply, chatItems, currentActivity } from "./presentation";
import { IdeasView } from "./TabViews";
import { usePetMood } from "./usePetMood";
import { usePetScale } from "./appearance";
import { LazyBoundary, lazyScreen } from "./Screen";
import { ViewerProvider } from "./Viewer";
import { ArtifactProvider, useArtifact } from "./muse/ArtifactContext";

const FeedScreen = lazyScreen(() => import("./muse/Feed").then((m) => ({ default: m.FeedScreen })));
const FeedSettingsSheet = lazyScreen(() => import("./muse/Feed").then((m) => ({ default: m.FeedSettingsSheet })));
const GoalsScreen = lazyScreen(() => import("./muse/GoalsScreen").then((m) => ({ default: m.GoalsScreen })));
const HelpersScreen = lazyScreen(() => import("./muse/Helpers").then((m) => ({ default: m.HelpersScreen })));
const TeachSheet = lazyScreen(() => import("./muse/TeachSheet").then((m) => ({ default: m.TeachSheet })));
const CreationsView = lazyScreen(() => import("./Creations").then((m) => ({ default: m.CreationsView })));
const ArtifactPane = lazyScreen(() => import("./muse/ArtifactPane").then((m) => ({ default: m.ArtifactPane })));

function ArtifactPaneSlot() {
  const ctx = useArtifact();
  if (!ctx?.artifact) return null;
  return (
    <LazyBoundary onClose={ctx.closeArtifact}>
      <ArtifactPane />
    </LazyBoundary>
  );
}

export type CompanionTab = MuseTab;


const FEED_SEEN_KEY = "companion.feed-seen-at";
/** While a run of 动态 was asked for: look for its posts this often, for at most this long. */
const FEED_POLL_MS = 15_000;
const FEED_WAIT_MS = 5 * 60_000;
/** Below this distance from the bottom, new messages keep the thread pinned. */
const STICKY_BOTTOM_PX = 160;
/** Reload history after the app was in the background this long (iOS drops the socket). */
const RESUME_REFRESH_MS = 15_000;
const HOME_STARTERS = [
  { label: "帮我理一下今天要做的事", prompt: "帮我理一下今天最重要的几件事，排个轻重缓急。" },
  { label: "看看最近有什么值得关注的新动态", prompt: "帮我看看最近我关心的领域有什么值得留意的新消息。" },
  { label: "帮我拆解一个近期目标", prompt: "我想定一个近期目标，你先问我几个关键问题，再帮我拆成可执行的步骤。" },
] as const;

function useConnectionStatus(): ConnectionStatus {
  const { client } = useClient();
  const [status, setStatus] = useState<ConnectionStatus>(client.status);
  useEffect(() => client.onStatus(setStatus), [client]);
  return status;
}

function streamErrorText(error: StreamError): string {
  switch (error.kind) {
    case "message_too_big":
      return "消息太大了，图片少放几张试试";
    case "model_request_failed":
      return "模型没有回应，检查一下模型设置或稍后再试";
    case "turn_rejected":
      return error.detail || "这条消息没有被接受";
    default:
      return "出了点问题，再试一次";
  }
}

/** Loads the thread history, then mounts the live screen on top of it. */
export function CompanionScreen({
  session,
  companion,
  tab,
  onTab,
  order,
  side,
  onEditSide,
  sideName,
  sideFace,
  chatApp,
  onOpenChat,
  draftRequest,
  jumpRequest,
  teachRequest,
  onOpenMenu,
  onUpdateCompanion,
  onChangeLook,
  onTurnEnd,
}: {
  session: ChatSummary;
  companion: CompanionState;
  tab: CompanionTab;
  onTab: (tab: CompanionTab) => void;
  order: CompanionTab[];
  /** A side chat (旁聊) is open: what the header says about it (a tab, 主要聊天 too, leaves it). */
  side: string | null;
  /** Its settings: name, delete (a helper's profile, a group's members). */
  onEditSide: () => void;
  /** A helper's or group's chat: its name and face in the header instead of the companion's. */
  sideName?: string;
  sideFace?: ReactNode;
  chatApp?: ChatApp;
  /** Open a helper's or a group's chat (from the 帮手 page). */
  onOpenChat: (chatId: string) => void;
  /** A message to start in the box (a skill's 用一次 / 定时做 from settings). */
  draftRequest: { text: string; at: number } | null;
  /** Jump to and briefly highlight a turn in this chat (from 查找聊天记录). */
  jumpRequest?: { turnId: string; at: number } | null;
  /** Changes when 教它做 was asked for: open the browser, recording. */
  teachRequest: number;
  onOpenMenu: () => void;
  onUpdateCompanion: (changes: CompanionChanges) => Promise<void>;
  /** 换形象: to the main chat with “把虚拟形象改为” in the box. */
  onChangeLook: () => void;
  onTurnEnd: () => void;
}) {
  const history = useSessionHistory(session.key);
  const [draft, setDraft] = useState("");
  const [localFocusAt, setLocalFocusAt] = useState<number | undefined>(undefined);
  useEffect(() => {
    if (draftRequest) setDraft(draftRequest.text);
  }, [draftRequest]);
  const handleRefineDraft = useCallback(
    (text: string) => {
      setDraft(text);
      setLocalFocusAt(Date.now());
      onTab("chat");
    },
    [onTab],
  );
  const { refresh } = history;

  useEffect(() => {
    let hiddenAt = 0;
    const onVisibility = () => {
      if (document.visibilityState === "hidden") hiddenAt = Date.now();
      else if (hiddenAt && Date.now() - hiddenAt > RESUME_REFRESH_MS) refresh();
    };
    document.addEventListener("visibilitychange", onVisibility);
    return () => document.removeEventListener("visibilitychange", onVisibility);
  }, [refresh]);

  const ready = !history.loading || history.messages.length > 0;
  return (
    <LiveScreen
      // Keep LiveScreen mounted across history refreshes so sheets, 2FA handoffs, composer
      // attachments, and live streaming deltas survive background resume and regeneration.
      key={session.key}
      ready={ready}
      historyVersion={history.version}
      chatId={session.chatId}
      sessionKey={session.key}
      onReload={refresh}
      initial={history.messages}
      pending={history.hasPendingToolCalls}
      companion={companion}
      tab={tab}
      onTab={onTab}
      order={order}
      side={side}
      onEditSide={onEditSide}
      sideName={sideName}
      sideFace={sideFace}
      chatApp={chatApp}
      onOpenChat={onOpenChat}
      teachRequest={teachRequest}
      jumpRequest={jumpRequest}
      draft={draft}
      focusRequest={Math.max(draftRequest?.at ?? 0, localFocusAt ?? 0) || undefined}
      onDraft={setDraft}
      onRefineDraft={handleRefineDraft}
      onOpenMenu={onOpenMenu}
      onUpdateCompanion={onUpdateCompanion}
      onChangeLook={onChangeLook}
      onTurnEnd={onTurnEnd}
    />
  );
}

export function LiveScreen({
  ready,
  historyVersion,
  chatId,
  sessionKey,
  initial,
  pending,
  companion,
  tab,
  onTab,
  order,
  side,
  onEditSide,
  sideName,
  sideFace,
  chatApp,
  onOpenChat,
  teachRequest,
  jumpRequest,
  draft,
  focusRequest,
  onDraft,
  onRefineDraft,
  onOpenMenu,
  onUpdateCompanion,
  onChangeLook,
  onTurnEnd,
  onReload,
}: {
  ready: boolean;
  historyVersion: number;
  chatId: string;
  sessionKey: string;
  initial: Parameters<typeof useNanobotStream>[1];
  pending: boolean;
  companion: CompanionState;
  tab: CompanionTab;
  onTab: (tab: CompanionTab) => void;
  order: CompanionTab[];
  side: string | null;
  onEditSide: () => void;
  sideName?: string;
  sideFace?: ReactNode;
  /** A helper's or a group's chat: shown as a conversation in a chat app. */
  chatApp?: ChatApp;
  onOpenChat: (chatId: string) => void;
  teachRequest: number;
  jumpRequest?: { turnId: string; at: number } | null;
  draft: string;
  focusRequest?: number;
  onDraft: (draft: string) => void;
  onRefineDraft?: (draft: string) => void;
  onOpenMenu: () => void;
  onUpdateCompanion: (changes: CompanionChanges) => Promise<void>;
  /** 换形象: to the main chat with “把虚拟形象改为” in the box. */
  onChangeLook: () => void;
  onTurnEnd: () => void;
  /** Load the thread again from the server (after a message was deleted). */
  onReload: () => void;
}) {
  const { client, token } = useClient();
  const { messages, isStreaming, goalState, send, stop, transcribeAudio, streamError, dismissStreamError } =
    useNanobotStream(ready ? chatId : null, initial, pending, onTurnEnd, undefined, historyVersion);

  /* Stopping a goal: halt the current run, then ask the companion to cancel the goal. */
  const cancelGoalAfterStop = useRef(false);
  const stopGoal = useCallback(() => {
    if (isStreaming) {
      cancelGoalAfterStop.current = true;
      stop();
    } else {
      send(`${GOAL_COMMAND}stop`);
    }
  }, [isStreaming, send, stop]);
  useEffect(() => {
    if (isStreaming || !cancelGoalAfterStop.current) return;
    cancelGoalAfterStop.current = false;
    send(`${GOAL_COMMAND}stop`);
  }, [isStreaming, send]);
  const activeGoal = goalState?.active ? goalState : null;
  const connection = useConnectionStatus();

  /* Approval cards: the server's records are the source of truth for what will run. */
  const [approvals, setApprovals] = useState<ApprovalRecord[]>([]);
  const [allApprovals, setAllApprovals] = useState<ApprovalRecord[]>([]);
  const loadApprovals = useCallback(() => {
    fetchApprovals(token, sessionKey)
      .then(setApprovals)
      .catch(() => {
        // Cards stay as last loaded; the next turn or reload retries.
      });
    fetchAllApprovals(token)
      .then(setAllApprovals)
      .catch(() => {
        // Older servers or transient errors: session approvals still work.
      });
  }, [sessionKey, token]);
  // A stable key: refetch when the companion finishes asking for a new approval.
  const requestedKey = useMemo(() => approvalCallIds(messages).join(","), [messages]);
  const knownKey = approvals.map((approval) => approval.call_id).join(",");
  useEffect(() => {
    if (!ready) return;
    const known = new Set(knownKey.split(","));
    const missing = requestedKey.split(",").some((id) => id && !known.has(id));
    if (missing || !knownKey) loadApprovals();
  }, [ready, requestedKey, knownKey, loadApprovals]);
  useEffect(() => {
    if (!ready) return;
    return client.onApprovalUpdate?.(loadApprovals);
  }, [client, ready, loadApprovals]);
  useEffect(() => {
    if (ready && historyVersion > 0) loadApprovals();
  }, [ready, historyVersion, loadApprovals]);
  const decide = useCallback(
    async (approval: ApprovalRecord, decision: "approve" | "reject", scope?: ApprovalScope) => {
      try {
        const updated = await decideApproval(client, approval.id, decision, scope);
        setApprovals((current) => current.map((item) => (item.id === updated.id ? updated : item)));
        setAllApprovals((current) => current.map((item) => (item.id === updated.id ? updated : item)));
      } catch (error) {
        loadApprovals(); // e.g. decided on another device; show the real state.
        throw error;
      }
    },
    [client, loadApprovals],
  );
  const mergedApprovals = useMemo(() => {
    const byId = new Map<string, ApprovalRecord>();
    for (const item of approvals) byId.set(item.id, item);
    for (const item of allApprovals) {
      const prev = byId.get(item.id);
      if (!prev || prev.status !== item.status) byId.set(item.id, item);
    }
    return Array.from(byId.values());
  }, [approvals, allApprovals]);

  /* Which model answered each turn, when the router chose it. */
  const [routes, setRoutes] = useState<ReadonlyMap<string, RoutingDecision>>(new Map());
  useEffect(() => {
    if (!ready || isStreaming) return;
    fetchModels(token)
      .then((data) =>
        setRoutes(new Map(data.decisions.filter((d) => d.turn_id).map((d) => [d.turn_id as string, d]))),
      )
      .catch(() => {
        // Without decisions replies simply show no "由 … 回答" line.
      });
  }, [ready, isStreaming, token]);

  const items = useMemo(() => chatItems(messages, Date.now(), approvals), [messages, approvals]);
  // What it was last asked (a goal's page shows it working when this names the goal).
  const lastAsked = useMemo(() => [...messages].reverse().find((m) => m.role === "user")?.content ?? "", [messages]);
  const activity = currentActivity(messages, isStreaming);
  const typing = awaitingReply(messages, isStreaming);
  const readNext = useRef(false);
  const wasTyping = useRef(false);
  useEffect(() => {
    if (wasTyping.current && !typing && readNext.current) {
      readNext.current = false;
      const asked = messages.map((m) => m.role).lastIndexOf("user");
      const reply = messages.slice(asked + 1).reverse().find((m) => m.role === "assistant" && m.kind !== "trace" && m.content.trim());
      if (reply) speakAloud(reply.content);
    }
    wasTyping.current = typing;
  }, [typing, messages]);
  const writing = useMemo(() => (typing ? fileDraft(messages) : null), [typing, messages]);
  const [composerFocused, setComposerFocused] = useState(false);
  const [statusOpen, setStatusOpen] = useState(false);
  const [statusInitialPane, setStatusInitialPane] = useState<0 | 1 | 2 | 3 | undefined>(undefined);
  const openMemoryPane = useCallback(() => {
    setStatusInitialPane(3);
    setStatusOpen(true);
  }, []);
  const [visitedTabs, setVisitedTabs] = useState<ReadonlySet<MuseTab>>(() => new Set([tab]));
  useEffect(() => {
    setVisitedTabs((prev) => (prev.has(tab) ? prev : new Set(prev).add(tab)));
  }, [tab]);
  // A new request rises as a sheet; 稍后再说 puts it away (it stays in the chat and in 等你确认).
  const [laterIds, setLaterIds] = useState<ReadonlySet<string>>(new Set());
  /* The browser it works in: whether it is waiting on the user (a login, a captcha). */
  const [computer, setComputer] = useState<ComputerStatus | null>(null);
  useEffect(() => {
    if (!ready) return;
    const load = () => {
      if (document.visibilityState === "visible") fetchComputer(token).then(setComputer).catch(() => undefined);
    };
    load();
    const timer = setInterval(load, statusOpen || isStreaming ? 3_000 : 15_000);
    return () => clearInterval(timer);
  }, [ready, token, statusOpen, isStreaming]);
  useEffect(() => {
    if (ready && statusOpen) loadApprovals();
  }, [ready, statusOpen, loadApprovals]);
  const handoff = computer?.handoff ?? null;
  const pendingApprovals = mergedApprovals.filter((approval) => approval.status === "pending").length;
  const asking = statusOpen ? undefined : approvals.find((approval) => approval.status === "pending" && !laterIds.has(approval.id));
  const planProposals = mergedApprovals.filter((approval) => approval.status === "pending" && approval.action === "goal_plan").length;

  /* Layout: content scrolls beneath the floating header and dock. */
  const headerRef = useRef<HTMLElement>(null);
  const dockRef = useRef<HTMLDivElement>(null);
  const [insets, setInsets] = useState({ top: 120, bottom: 160 });
  useLayoutEffect(() => {
    const measure = () =>
      setInsets({
        top: headerRef.current?.offsetHeight ?? 120,
        bottom: dockRef.current?.offsetHeight ?? 160,
      });
    measure();
    const observer = new ResizeObserver(measure);
    if (headerRef.current) observer.observe(headerRef.current);
    if (dockRef.current) observer.observe(dockRef.current);
    return () => observer.disconnect();
  }, []);

  /* Keep the newest message in view unless the user scrolled up to read. */
  const chatScrollRef = useRef<HTMLDivElement>(null);
  const stickToBottom = useRef(true);
  const [atBottom, setAtBottom] = useState(true);
  const [unreadReplies, setUnreadReplies] = useState(0);
  const seenReplyIdsRef = useRef<Set<string>>(new Set());
  // Protects an active smooth search-jump from being re-pinned during its first frames near the bottom.
  const jumpGuardUntilRef = useRef(0);
  // Where it last put the thread: only a move up from there is the user scrolling back to read
  // (content growing under it also fires scroll events, which must not unpin it).
  const pinnedTop = useRef(0);
  const setPinned = useCallback((pinned: boolean) => {
    stickToBottom.current = pinned;
    setAtBottom(pinned);
    if (pinned) setUnreadReplies(0);
  }, []);
  const toBottom = (el: HTMLDivElement) => {
    el.scrollTop = el.scrollHeight;
    pinnedTop.current = el.scrollTop;
  };
  const onChatScroll = () => {
    const el = chatScrollRef.current;
    if (!el) return;
    const distanceFromBottom = el.scrollHeight - el.scrollTop - el.clientHeight;
    if (distanceFromBottom < STICKY_BOTTOM_PX) {
      if (Date.now() < jumpGuardUntilRef.current) return;
      pinnedTop.current = el.scrollTop;
      if (!stickToBottom.current) setPinned(true);
    } else if (el.scrollTop < pinnedTop.current - 4 || (pinnedTop.current === 0 && distanceFromBottom >= STICKY_BOTTOM_PX)) {
      if (stickToBottom.current) setPinned(false);
    }
  };
  const scrollToLatest = useCallback(() => {
    jumpGuardUntilRef.current = 0;
    setPinned(true);
    const el = chatScrollRef.current;
    if (!el) return;
    pinnedTop.current = el.scrollHeight;
    const reduced = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
    if (typeof el.scrollTo === "function") {
      el.scrollTo({ top: el.scrollHeight, behavior: reduced ? "auto" : "smooth" });
    } else {
      el.scrollTop = el.scrollHeight;
    }
  }, [setPinned]);
  useEffect(() => {
    if (atBottom) {
      const nextSeen = new Set<string>();
      for (const item of items) {
        if (item.type === "reply") nextSeen.add(item.id);
      }
      seenReplyIdsRef.current = nextSeen;
      setUnreadReplies(0);
      return;
    }
    let unseen = 0;
    for (const item of items) {
      if (item.type === "reply" && !seenReplyIdsRef.current.has(item.id)) unseen += 1;
    }
    setUnreadReplies(unseen);
  }, [atBottom, items]);

  /* Jump to a specific turn from search results and highlight it briefly. */
  const [highlightedTurnId, setHighlightedTurnId] = useState<string | null>(null);
  const handledJumpAtRef = useRef<number | null>(null);
  const highlightTimerRef = useRef<number | null>(null);
  useEffect(
    () => () => {
      if (highlightTimerRef.current !== null) window.clearTimeout(highlightTimerRef.current);
    },
    [],
  );
  useLayoutEffect(() => {
    const el = chatScrollRef.current;
    if (jumpRequest && handledJumpAtRef.current !== jumpRequest.at) return;
    if (el && stickToBottom.current) toBottom(el);
  }, [items, typing, insets.bottom, tab, jumpRequest]);
  const initialCount = initial?.length ?? 0;
  useEffect(() => {
    if (!jumpRequest || !ready || tab !== "chat" || handledJumpAtRef.current === jumpRequest.at) return;
    const container = chatScrollRef.current;
    if (!container) return;
    const target = Array.from(container.querySelectorAll<HTMLElement>("[data-turn-id]")).find(
      (node) => node.dataset.turnId === jumpRequest.turnId,
    );
    if (!target) {
      // Once persisted history has hydrated into messages, consume an absent/deleted turnId so
      // auto-scroll-to-bottom isn't permanently blocked for the rest of this chat session.
      if (historyVersion > 0 && messages.length >= initialCount) {
        handledJumpAtRef.current = jumpRequest.at;
        if (stickToBottom.current) toBottom(container);
      }
      return;
    }
    handledJumpAtRef.current = jumpRequest.at;
    const targetBottom = target.offsetTop + target.offsetHeight;
    const awayFromBottom =
      container.scrollHeight - targetBottom >= STICKY_BOTTOM_PX ||
      container.scrollHeight - container.scrollTop - container.clientHeight >= STICKY_BOTTOM_PX;
    if (awayFromBottom) {
      jumpGuardUntilRef.current = Date.now() + 700;
      setPinned(false);
    } else {
      jumpGuardUntilRef.current = 0;
    }
    const reduced = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
    target.scrollIntoView?.({ behavior: reduced ? "auto" : "smooth", block: "center" });
    setHighlightedTurnId(jumpRequest.turnId);
    if (highlightTimerRef.current !== null) window.clearTimeout(highlightTimerRef.current);
    highlightTimerRef.current = window.setTimeout(() => {
      setHighlightedTurnId(null);
      highlightTimerRef.current = null;
    }, 1200);
  }, [jumpRequest, ready, historyVersion, initialCount, messages.length, items, tab, setPinned]);
  // Pictures, cards and tables grow the thread after it is shown: stay at the bottom while they do.
  useEffect(() => {
    const el = chatScrollRef.current;
    const content = el?.firstElementChild;
    if (!el || !content) return;
    const observer = new ResizeObserver(() => {
      if (stickToBottom.current) toBottom(el);
    });
    observer.observe(content);
    observer.observe(el);
    return () => observer.disconnect();
  }, [tab]);

  /* Long-press a message: 引用 puts it above the box (sent with the next message), 复制, 删除. */
  const [quote, setQuote] = useState<string | null>(null);
  const petPx = usePetScale();
  const submit = useCallback(
    (text: string, images: SendAttachment[] = [], spoken = false) => {
      jumpGuardUntilRef.current = 0;
      setPinned(true);
      if (send(text, images.length ? images : undefined, quote ? { quotedContext: quote } : undefined)) {
        // Asked by voice in the Android app: the answer is read out when it is done (as on iPhone).
        readNext.current = spoken && !!androidShell()?.speak && readsAfterVoice();
        onDraft("");
        setQuote(null);
      }
    },
    [onDraft, send, quote, setPinned],
  );
  const onMessageAction = useCallback(
    (action: MessageAction, message: UIMessage, role: "user" | "reply") => {
      const words = (role === "user" ? parseQuotedUserMessage(message.content).content : textWithoutCards(message.content)).trim();
      if (action === "quote") {
        setQuote(words.slice(0, 300) || (role === "user" ? "（我发的）" : "（它的回复）"));
      } else if (action === "copy") {
        void copyTextToClipboard(words);
      } else if (action === "speak") {
        speakAloud(message.content);
      } else if (action === "regenerate") {
        if (message.turnId) void regenerateReply(client, chatId, message.turnId).then(onReload).catch(() => undefined);
      } else if (message.turnId) {
        void deleteMessage(client, chatId, message.turnId, role).then(onReload).catch(() => undefined);
      }
    },
    [client, chatId, onReload],
  );

  /* 教它做: the browser opened to record a task (from 设置 → 技能), then the steps to review. */
  const [teaching, setTeaching] = useState(false);
  const [taught, setTaught] = useState<TaughtStep[] | null>(null);
  useEffect(() => {
    if (teachRequest) setTeaching(true);
  }, [teachRequest]);

  /* 动态: the posts it writes; after 现在就写一期, look again until the new ones arrive. */
  const [feedData, setFeedData] = useState<FeedPayload | null>(null);
  const [feedSettings, setFeedSettings] = useState(false);
  const [writingSince, setWritingSince] = useState<number | null>(null);
  const loadFeed = useCallback(() => {
    fetchFeed(token).then(setFeedData).catch(() => undefined);
  }, [token]);
  useEffect(() => {
    if (ready) loadFeed();
  }, [ready, loadFeed, tab]);
  useEffect(() => {
    if (!ready) return;
    return client.onFeedUpdate?.(loadFeed);
  }, [client, ready, loadFeed]);
  useEffect(() => {
    if (writingSince === null) return;
    const timer = setInterval(loadFeed, FEED_POLL_MS);
    const giveUp = setTimeout(() => setWritingSince(null), FEED_WAIT_MS);
    return () => {
      clearInterval(timer);
      clearTimeout(giveUp);
    };
  }, [writingSince, loadFeed]);
  useEffect(() => {
    // Posts newer than the ask: the run is writing; give it a little longer to finish the rest.
    if (writingSince === null || (feedData?.generated_at ?? 0) <= writingSince) return;
    const settle = setTimeout(() => setWritingSince(null), 90_000);
    return () => clearTimeout(settle);
  }, [feedData, writingSince]);
  const writeNow = useCallback(() => {
    setWritingSince(Date.now());
    changeFeed(client, "write_now").then(setFeedData).catch(() => setWritingSince(null));
  }, [client]);

  const discuss = useCallback(
    (post: FeedPost) => {
      onDraft(post.prompt || `关于「${post.title}」，`);
      onTab("chat");
      // Asking about it says the topic is wanted: the next run leans that way.
      changeFeed(client, "discussed", { id: post.id }).then(setFeedData).catch(() => undefined);
    },
    [client, onDraft, onTab],
  );

  /* A count on 动态 of the posts since it was last opened. */
  // How far 动态 has been read: kept with the companion on the server (a phone that loses its web
  // storage would otherwise count every post ever as new), this phone's copy until it answers.
  const [feedSeenAt, setFeedSeenAt] = useState(() => {
    try {
      return Number(window.localStorage.getItem(FEED_SEEN_KEY)) || 0;
    } catch {
      return 0;
    }
  });
  const serverSeenAt = companion.feed_seen_at ?? 0;
  useEffect(() => {
    setFeedSeenAt((local) => Math.max(local, serverSeenAt));
  }, [serverSeenAt]);
  const newestPost = feedData?.posts[0]?.ts ?? 0;
  useEffect(() => {
    // Never read anywhere yet: what is already there isn't news; only what comes after counts.
    const firstTime = !feedSeenAt && !serverSeenAt;
    if ((tab !== "feed" && !firstTime) || !newestPost) return;
    setFeedSeenAt(newestPost);
    try {
      window.localStorage.setItem(FEED_SEEN_KEY, String(newestPost));
    } catch {
      // The server keeps it too.
    }
    if (newestPost > serverSeenAt) void onUpdateCompanion({ feed_seen_at: newestPost }).catch(() => undefined);
  }, [newestPost, tab, feedSeenAt, serverSeenAt, onUpdateCompanion]);
  const feedUnread = (feedData?.posts ?? []).filter((post) => post.ts > feedSeenAt).length;

  const connected = connection === "open";
  const pet = usePetMood({
    connected,
    activity: connected ? (activity?.kind ?? null) : null,
    approvals: mergedApprovals,
    failed: !!streamError,
  });
  const status = !connected
    ? connection === "reconnecting" || connection === "error" || connection === "closed"
      ? "重新连接中…"
      : "连接中…"
    : handoff
      ? `需要你来一下：${handoff.reason}`
      : pendingApprovals
      ? `${pendingApprovals} 件事等你确认`
      : activity
        ? `${activity.label}…`
        : "在线";
  const statusTone = connected && (handoff || pendingApprovals || activity) ? "accent" : "muted";
  /** From a goal or a feed post: say it in the chat and show the chat. */
  const sendFromTab = (text: string) => {
    onTab("chat");
    submit(text);
  };

  const refineDraft = useCallback(
    (text: string) => {
      if (onRefineDraft) onRefineDraft(text);
      else {
        onDraft(text);
        onTab("chat");
      }
    },
    [onDraft, onRefineDraft, onTab],
  );

  return (
    <ViewerProvider>
    <ArtifactProvider onRefine={refineDraft}>
    <div className="flex h-full overflow-hidden">
    <div className="relative min-w-0 flex-1 overflow-hidden">
      <div
        ref={chatScrollRef}
        onScroll={onChatScroll}
        hidden={tab !== "chat"}
        className="absolute inset-0 overflow-y-auto overscroll-contain"
        style={{ paddingTop: insets.top, paddingBottom: insets.bottom + 8 }}
      >
        <div className="mx-auto min-h-full max-w-2xl">
          {!ready ? null : items.length === 0 && !typing && chatApp ? (
            <MessengerEmpty face={chatApp.bigFace} title={chatApp.title} hint={chatApp.hint} starters={chatApp.starters} onSend={(text) => submit(text)} />
          ) : items.length === 0 && !typing ? (
            <EmptyThread
              avatar={companion.avatar}
              face={sideFace}
              hint={side?.startsWith("群聊") ? "说一件事，大家一起做。@ 谁就交给谁，不 @ 就由我来分派。" : undefined}
              starters={!side ? HOME_STARTERS : undefined}
              onSend={!side ? (text) => submit(text) : undefined}
            />
          ) : (
            <ChatThread
              items={items}
              typing={typing}
              routes={routes}
              onDecide={decide}
              onSend={(text) => submit(text)}
              onMessageAction={onMessageAction}
              highlightedTurnId={highlightedTurnId}
              onOpenMemory={openMemoryPane}
              draft={writing}
              companionName={chatApp ? undefined : companion.name}
              working={{
                avatar: companion.avatar,
                animation: pet.animation,
                loop: pet.loop,
                onDone: pet.onDone,
                label: activity ? `${activity.label}…` : null,
                face: chatApp?.workingFace ?? sideFace,
              }}
              messenger={chatApp ? { speaker: chatApp.speaker, me: chatApp.me, relay: chatApp.relay } : undefined}
            />
          )}
        </div>
      </div>
      {(["feed", "ideas", "goals", "helpers", "creations"] as const).map((key) =>
        visitedTabs.has(key) || tab === key ? (
          <div
            key={key}
            hidden={tab !== key}
            className="absolute inset-0 overflow-y-auto overscroll-contain"
            style={{ paddingTop: insets.top, paddingBottom: insets.bottom + 16 }}
          >
            <div className="mx-auto max-w-2xl">
              <LazyBoundary>
                {key === "feed" && (
                  <FeedScreen
                    data={feedData}
                    writing={writingSince !== null}
                    onEditInstructions={() => setFeedSettings(true)}
                    onWriteNow={writeNow}
                    onDiscuss={discuss}
                    onChange={setFeedData}
                  />
                )}
                {key === "ideas" && (
                  <IdeasView
                    active={tab === "ideas"}
                    onDo={(prompt) => {
                      onTab("chat");
                      submit(prompt);
                    }}
                  />
                )}
                {key === "goals" && (
                  <GoalsScreen
                    active={tab === "goals"}
                    name={companion.name}
                    approvals={mergedApprovals}
                    onDecide={decide}
                    onSend={sendFromTab}
                    working={isStreaming ? { label: activity?.label ?? null, asked: lastAsked } : null}
                    computer={computer}
                    onOpenCreations={() => onTab("creations")}
                  />
                )}
                {key === "helpers" && <HelpersScreen active={tab === "helpers"} onOpenChat={onOpenChat} />}
                {key === "creations" && <CreationsView active={tab === "creations"} />}
              </LazyBoundary>
            </div>
          </div>
        ) : null,
      )}

      {chatApp ? (
        <MessengerBar
          headerRef={headerRef}
          title={chatApp.title}
          subtitle={isStreaming ? (activity ? `${activity.label}…` : "正在输入…") : chatApp.subtitle}
          busy={isStreaming}
          onBack={chatApp.onBack}
          onProfile={chatApp.onProfile}
        />
      ) : (
      <MuseHeader
        headerRef={headerRef}
        scrim
        className="absolute inset-x-0 top-0 z-20 pb-4"
        face={sideFace ?? <PetView value={companion.avatar} animation={pet.animation} loop={pet.loop} onDone={pet.onDone} size={petPx(64)} />}
        name={sideName ?? companion.name}
        statusLine={side && status === "在线" ? side : status}
        statusTone={statusTone}
        spinning={connected && !!activity && !pendingApprovals}
        badge={pendingApprovals + (handoff ? 1 : 0)}
        // A helper's or a group's chat: its face opens its profile (the companion's status is the companion's).
        onAvatar={() => {
          if (sideName) {
            onEditSide();
          } else {
            setStatusInitialPane(undefined);
            setStatusOpen(true);
          }
        }}
        leading={
          <MuseRoundButton onClick={onOpenMenu} label="菜单">
            <Menu size={22} />
          </MuseRoundButton>
        }
        // Settings are in the sidebar; only a page with settings of its own (动态) has a button here.
        trailing={
          side ? (
            <MuseRoundButton onClick={onEditSide} label={sideName ? "资料与设置" : "旁聊设置"}>
              <Settings2 size={20} />
            </MuseRoundButton>
          ) : tab === "feed" ? (
            <MuseRoundButton onClick={() => setFeedSettings(true)} label="动态版块说明">
              <SlidersHorizontal size={20} />
            </MuseRoundButton>
          ) : null
        }
        under={tab === "chat" && activeGoal ? <GoalBanner goal={activeGoal} onStop={stopGoal} /> : null}
      />
      )}

      <div
        ref={dockRef}
        className="c-fade-bottom absolute inset-x-0 bottom-0 z-20 px-3 pt-6"
        style={{ paddingBottom: "max(0.75rem, env(safe-area-inset-bottom))" }}
      >
        <div className="mx-auto flex max-w-2xl flex-col gap-2">
          {tab === "chat" && !atBottom && items.length > 0 && (
            <div className="pointer-events-none flex justify-center">
              <button
                type="button"
                onClick={scrollToLatest}
                aria-label={unreadReplies > 0 ? `回到最新，${unreadReplies} 条新回复` : "回到最新"}
                className="c-float c-pressable c-rise pointer-events-auto inline-flex min-h-9 items-center gap-1.5 rounded-full px-3.5 py-1.5 text-[13px] font-medium shadow-sm transition-transform active:scale-95 motion-reduce:transition-none"
              >
                <ArrowDown className="h-3.5 w-3.5 shrink-0" style={{ color: "var(--c-primary)" }} aria-hidden="true" />
                <span style={unreadReplies > 0 ? { color: "var(--c-primary)" } : undefined}>
                  {unreadReplies > 0 ? `${unreadReplies} 条新回复` : "回到最新"}
                </span>
              </button>
            </div>
          )}
          {streamError && tab === "chat" && (
            <div className="c-float flex items-center gap-2 rounded-2xl py-1 pl-4 pr-1 text-[14px]" role="alert">
              <span className="flex-1">{streamErrorText(streamError)}</span>
              <button
                type="button"
                aria-label="关闭提示"
                onClick={dismissStreamError}
                className="c-pressable grid h-11 w-11 place-items-center rounded-full"
              >
                <X className="h-4 w-4" aria-hidden="true" />
              </button>
            </div>
          )}
          {tab === "chat" && (
            <Composer
              value={draft}
              onChange={onDraft}
              onSend={submit}
              onStop={stop}
              streaming={isStreaming}
              connected={connected && ready}
              transcribeAudio={transcribeAudio}
              voiceEnabled={companion.capabilities?.stt !== false}
              onFocusChange={setComposerFocused}
              quote={quote}
              onClearQuote={() => setQuote(null)}
              focusRequest={focusRequest}
            />
          )}
          {/* Inside a conversation with a helper there is no tab bar, as in a chat app (‹ goes back). */}
          {!(tab === "chat" && composerFocused) && !chatApp && (
            <MuseTabBar
              tab={side && /^(帮手|群聊)/.test(side) ? "helpers" : tab}
              onTab={onTab}
              order={order}
              badges={{ chat: tab === "chat" ? 0 : pendingApprovals, feed: tab === "feed" ? 0 : feedUnread, goals: planProposals }}
            />
          )}
        </div>
      </div>

      <MuseSheet
        open={!!asking}
        onClose={() => asking && setLaterIds((ids) => new Set(ids).add(asking.id))}
        title={`${companion.name} 想先问问你`}
      >
        {asking && (
          <ApprovalCard
            key={asking.id}
            bare
            approval={asking}
            onDecide={decide}
            onLater={() => setLaterIds((ids) => new Set(ids).add(asking.id))}
          />
        )}
      </MuseSheet>

      {feedSettings && (
        <LazyBoundary onClose={() => setFeedSettings(false)}>
          <FeedSettingsSheet
            open={feedSettings}
            data={feedData}
            onClose={() => setFeedSettings(false)}
            onSaved={(data, wroteNow) => {
              setFeedData(data);
              setFeedSettings(false);
              if (wroteNow) setWritingSince(Date.now());
            }}
          />
        </LazyBoundary>
      )}

      <StatusSheet
        open={statusOpen}
        initialPane={statusInitialPane}
        onClose={() => {
          setStatusOpen(false);
          setStatusInitialPane(undefined);
        }}
        face={<PetView value={companion.avatar} animation={pet.animation} loop={pet.loop} onDone={pet.onDone} size={72} />}
        name={companion.name}
        statusLine={status}
        working={isStreaming}
        onStop={() => {
          stop();
          setStatusOpen(false);
          setStatusInitialPane(undefined);
        }}
        approvals={mergedApprovals}
        sessionKey={sessionKey}
        history={messages}
        computer={computer}
        onDecide={decide}
        onTaught={setTaught}
        identity={
          <NameLookRow
            companion={companion}
            onUpdate={onUpdateCompanion}
            onChangeLook={() => {
              setStatusOpen(false);
              onChangeLook();
            }}
          />
        }
      />

      {teaching && (
        <BrowserFullscreen
          name={companion.name}
          teach
          onTaught={setTaught}
          onClose={() => setTeaching(false)}
        />
      )}
      {taught && (
        <LazyBoundary onClose={() => setTaught(null)}>
          <TeachSheet steps={taught} name={companion.name} onClose={() => setTaught(null)} />
        </LazyBoundary>
      )}
    </div>
    <ArtifactPaneSlot />
    </div>
    </ArtifactProvider>
    </ViewerProvider>
  );
}
