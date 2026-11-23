/*
 * The sidebar, as Muse's: the companion's name; 选项卡 (the five tabs, long-press to reorder);
 * 旁聊 (side chats on a topic of their own, kept apart from the main chat); and at the bottom
 * settings, search and a new side chat.
 */
import { MoreHorizontal, Search, Settings, SquarePen } from "lucide-react";
import { useEffect, useRef, useState, type PointerEvent as ReactPointerEvent, type ReactNode } from "react";

import type { ChatSummary } from "@/lib/types";

import type { ChatSearchHit } from "../companion-api";
import { useBackKey } from "../Screen";

import { useSheetDrag } from "../sheetDrag";

import { TABS, type MuseTab } from "./Shell";

const HOLD_MS = 450;
const ROW_PX = 56;

/** Tab rows: a tap opens one; holding one picks it up, dragging moves it, letting go puts it there. */
function TabList({
  order,
  current,
  onTab,
  onReorder,
}: {
  order: MuseTab[];
  current: MuseTab | null;
  onTab: (tab: MuseTab) => void;
  onReorder: (order: MuseTab[]) => void;
}) {
  const [drag, setDrag] = useState<{ id: MuseTab; from: number; to: number; startY: number } | null>(null);
  const press = useRef<{ id: MuseTab; y: number; timer: number; held: boolean } | null>(null);

  const shown = (() => {
    if (!drag) return order;
    const next = order.filter((id) => id !== drag.id);
    next.splice(drag.to, 0, drag.id);
    return next;
  })();

  const down = (event: ReactPointerEvent<HTMLButtonElement>, id: MuseTab) => {
    const target = event.currentTarget;
    const pointer = event.pointerId;
    const y = event.clientY;
    press.current = {
      id,
      y,
      held: false,
      timer: window.setTimeout(() => {
        if (!press.current) return;
        press.current.held = true;
        target.setPointerCapture(pointer);
        navigator.vibrate?.(10);
        const from = order.indexOf(id);
        setDrag({ id, from, to: from, startY: y });
      }, HOLD_MS),
    };
  };
  const move = (event: ReactPointerEvent<HTMLButtonElement>) => {
    const held = press.current;
    if (!held) return;
    if (!held.held) {
      // Moving before the hold is a scroll, not a pick-up.
      if (Math.abs(event.clientY - held.y) > 8) {
        window.clearTimeout(held.timer);
        press.current = null;
      }
      return;
    }
    setDrag((d) => d && { ...d, to: Math.max(0, Math.min(order.length - 1, d.from + Math.round((event.clientY - d.startY) / ROW_PX))) });
  };
  // A hold ends in a drop, and the click that follows it is not a tap.
  const dropped = useRef(false);
  const up = () => {
    const held = press.current;
    press.current = null;
    if (held) window.clearTimeout(held.timer);
    if (held?.held && drag) {
      dropped.current = true;
      onReorder(shown);
      setDrag(null);
    }
  };

  return (
    <ul className="flex flex-col gap-0.5" aria-label="选项卡">
      {shown.map((id) => {
        const meta = TABS.find((t) => t.id === id)!;
        const active = current === id;
        const lifted = drag?.id === id;
        return (
          <li key={id}>
            <button
              type="button"
              onPointerDown={(event) => down(event, id)}
              onPointerMove={move}
              onPointerUp={up}
              onClick={() => {
                if (dropped.current) dropped.current = false;
                else onTab(id);
              }}
              onPointerCancel={() => {
                if (press.current) window.clearTimeout(press.current.timer);
                press.current = null;
                setDrag(null);
              }}
              onContextMenu={(event) => event.preventDefault()}
              aria-current={active ? "page" : undefined}
              className="flex h-14 w-full select-none items-center gap-4 rounded-full px-4 text-left text-[18px] transition-[background,box-shadow,transform] duration-150"
              style={{
                background: active || lifted ? "var(--c-fill)" : undefined,
                boxShadow: lifted ? "var(--c-shadow)" : undefined,
                transform: lifted ? "scale(1.02)" : undefined,
                WebkitTouchCallout: "none",
                touchAction: drag ? "none" : "pan-y",
              }}
            >
              <span className="grid h-7 w-7 place-items-center">{meta.icon(false)}</span>
              {meta.label}
            </button>
          </li>
        );
      })}
    </ul>
  );
}

const REVEAL_PX = 84;

/**
 * A row in a list (a side chat, a helper). Tap opens it; hold opens its settings; swipe left slides
 * 删除 in from the right (as in Mail or WeChat), and a tap on the row puts it away.
 */
export function SwipeRow({
  children,
  className = "min-h-12 flex-col justify-center rounded-[22px] px-3 py-1.5",
  rounded = "rounded-[22px]",
  active = false,
  swiped,
  onSwipe,
  onOpen,
  onEdit,
  onDelete,
}: {
  children: ReactNode;
  /** Layout of the row itself. */
  className?: string;
  /** Corners of the row and of 删除. */
  rounded?: string;
  active?: boolean;
  swiped: boolean;
  onSwipe: (open: boolean) => void;
  onOpen: () => void;
  onEdit: () => void;
  onDelete: () => void;
}) {
  const press = useRef<{ x: number; y: number; timer: number | null; dragging: boolean; done: boolean } | null>(null);
  const [drag, setDragState] = useState<number | null>(null);
  // The live distance, for the finger lifting before the last move has rendered (a quick swipe).
  const dragRef = useRef<number | null>(null);
  const setDrag = (value: number | null) => {
    dragRef.current = value;
    setDragState(value);
  };
  const offset = drag ?? (swiped ? -REVEAL_PX : 0);

  const end = () => {
    const p = press.current;
    if (p?.timer) window.clearTimeout(p.timer);
    if (p?.dragging) onSwipe((dragRef.current ?? 0) < -REVEAL_PX / 2);
    setDrag(null);
  };

  return (
    <li data-no-sheet-drag className={`group relative overflow-hidden ${rounded}`}>
      <button
        type="button"
        tabIndex={swiped ? 0 : -1}
        aria-hidden={!swiped}
        onClick={onDelete}
        className={`absolute inset-y-0 right-0 z-10 grid place-items-center ${rounded} text-[15px] font-semibold text-white`}
        style={{
          width: REVEAL_PX,
          background: "var(--c-danger-fill)",
          // Slides in from the right edge as the row is swiped; the row itself stays put.
          transform: `translateX(${REVEAL_PX + offset}px)`,
          transition: drag === null ? "transform 200ms ease-out" : undefined,
        }}
      >
        删除
      </button>
      <button
        type="button"
        onKeyDown={(event) => {
          if (event.key === "Delete") {
            event.preventDefault();
            onDelete();
          } else if (event.key === "F2" || event.key === "ContextMenu" || (event.shiftKey && event.key === "F10")) {
            event.preventDefault();
            onEdit();
          } else if (event.key === "ArrowLeft" && !swiped) {
            event.preventDefault();
            onSwipe(true);
          } else if ((event.key === "ArrowRight" || event.key === "Escape") && swiped) {
            event.preventDefault();
            event.stopPropagation();
            onSwipe(false);
          }
        }}
        onPointerDown={(event) => {
          press.current = {
            x: event.clientX,
            y: event.clientY,
            dragging: false,
            done: false,
            timer: window.setTimeout(() => {
              if (!press.current) return;
              press.current.timer = null;
              press.current.done = true;
              navigator.vibrate?.(10);
              onEdit();
            }, HOLD_MS),
          };
        }}
        onPointerMove={(event) => {
          const p = press.current;
          if (!p || p.done) return;
          const dx = event.clientX - p.x;
          const dy = event.clientY - p.y;
          if (!p.dragging && Math.abs(dx) > 8 && Math.abs(dx) > Math.abs(dy)) {
            p.dragging = true;
            event.currentTarget.setPointerCapture?.(event.pointerId);
          }
          if (p.dragging || Math.abs(dy) > 8) {
            if (p.timer) window.clearTimeout(p.timer);
            p.timer = null;
          }
          if (p.dragging) setDrag(Math.max(-REVEAL_PX - 16, Math.min(0, (swiped ? -REVEAL_PX : 0) + dx)));
        }}
        onPointerUp={end}
        onPointerCancel={end}
        onClick={() => {
          const p = press.current;
          press.current = null;
          if (p?.dragging || p?.done) return; // a swipe or a hold, not a tap
          if (swiped) onSwipe(false);
          else onOpen();
        }}
        onContextMenu={(event) => {
          event.preventDefault();
          onEdit();
        }}
        className={`relative flex w-full select-none text-left ${className}`}
        style={{
          background: active ? "var(--c-fill)" : undefined,
          paddingRight: swiped ? REVEAL_PX + 8 : undefined,
          touchAction: "pan-y",
          WebkitTouchCallout: "none",
        }}
      >
        {children}
      </button>
      {!swiped && (
        <button
          type="button"
          aria-label="编辑"
          onClick={(event) => {
            event.stopPropagation();
            onEdit();
          }}
          className="c-muted c-pressable absolute right-2 top-1/2 z-10 grid h-8 w-8 -translate-y-1/2 place-items-center rounded-full opacity-0 transition-opacity focus:opacity-100 group-hover:opacity-100 group-focus-within:opacity-100"
        >
          <MoreHorizontal className="h-4 w-4" aria-hidden="true" />
        </button>
      )}
    </li>
  );
}

export function MuseDrawer({
  open,
  onClose,
  name,
  current,
  activeChatId,
  order,
  onReorder,
  onTab,
  sideChats,
  onOpenChat,
  onNewChat,
  onOpenSettings,
  onEditChat,
  onDeleteChat,
  search,
  onOpenHit,
}: {
  open: boolean;
  onClose: () => void;
  name: string;
  /** The tab showing (null while a side chat is open). */
  current: MuseTab | null;
  activeChatId: string | null;
  order: MuseTab[];
  onReorder: (order: MuseTab[]) => void;
  onTab: (tab: MuseTab) => void;
  sideChats: ChatSummary[];
  onOpenChat: (chatId: string) => void;
  onNewChat: () => void;
  onOpenSettings: () => void;
  /** Long-pressed: its settings (name, delete). */
  onEditChat: (chat: ChatSummary) => void;
  /** Swiped left and 删除 tapped. */
  onDeleteChat: (chatId: string) => void;
  /** 查找聊天记录: messages in any chat that say the words (searched once typing pauses). */
  search?: (words: string) => Promise<ChatSearchHit[]>;
  onOpenHit?: (hit: ChatSearchHit) => void;
}) {
  const [query, setQuery] = useState("");
  const [found, setFound] = useState<{ words: string; hits: ChatSearchHit[] } | null>(null);
  useEffect(() => {
    const words = query.trim();
    if (!words || !search) {
      setFound(null);
      return;
    }
    let alive = true;
    const timer = window.setTimeout(() => {
      search(words)
        .then((hits) => alive && setFound({ words, hits }))
        .catch(() => alive && setFound({ words, hits: [] }));
    }, 350);
    return () => {
      alive = false;
      window.clearTimeout(timer);
    };
  }, [query, search]);
  // The one side chat swiped open to show 删除 (closes when the sidebar does).
  const [swiped, setSwiped] = useState<string | null>(null);
  // Swiped back to the left, it closes (a side chat's own swipe, to 删除, stays the row's).
  const drawer = useRef<HTMLElement>(null);
  useSheetDrag(drawer, onClose, open, "left");
  useEffect(() => {
    drawer.current?.toggleAttribute("inert", !open);
  }, [open]);
  useEffect(() => {
    if (!open) {
      setQuery("");
      setSwiped(null);
    }
  }, [open]);
  useBackKey(onClose, open);

  const needle = query.trim().toLowerCase();
  const chats = needle
    ? sideChats.filter((chat) => `${chat.title ?? ""} ${chat.preview}`.toLowerCase().includes(needle))
    : sideChats;
  const label = (chat: ChatSummary) => chat.title || chat.preview.slice(0, 16) || "新的旁聊";

  return (
    <>
      <div
        className="fixed inset-0 z-40 transition-opacity duration-300"
        style={{ background: "var(--c-scrim)", opacity: open ? 1 : 0, pointerEvents: open ? "auto" : "none" }}
        onClick={onClose}
        aria-hidden="true"
      />
      <aside
        ref={drawer}
        className="fixed inset-y-0 left-0 z-50 flex w-[82vw] max-w-[360px] flex-col transition-transform duration-300 ease-out"
        style={{ background: "var(--c-bg)", transform: open ? "translateX(0)" : "translateX(-102%)", paddingTop: "max(env(safe-area-inset-top), 16px)" }}
        aria-label="侧栏"
        aria-hidden={!open}
      >
        <div className="min-h-0 flex-1 overflow-y-auto px-3 pb-4">
          <h1 className="px-3 pb-6 pt-8 text-[32px] font-bold leading-tight">{name}</h1>
          <p className="c-muted px-3 text-[13px] font-medium">选项卡</p>
          <p className="c-muted px-3 pb-3 pt-1 text-[15px]">你可以长按来重新排序</p>
          <TabList
            order={order}
            current={activeChatId ? null : current}
            onTab={(tab) => {
              onTab(tab);
              onClose();
            }}
            onReorder={onReorder}
          />
          <div className="c-divider mx-3 my-4 border-t" />
          <p className="c-muted px-3 pb-2 text-[13px] font-medium">旁聊</p>
          {chats.length === 0 ? (
            <p className="c-muted px-3 py-2 text-[14px] leading-relaxed">
              {needle ? "没有找到" : "还没有旁聊"}
            </p>
          ) : (
            <ul className="flex flex-col gap-0.5">
              {chats.map((chat) => (
                <SwipeRow
                  key={chat.chatId}
                  active={activeChatId === chat.chatId}
                  swiped={swiped === chat.chatId}
                  onSwipe={(open) => setSwiped(open ? chat.chatId : null)}
                  onOpen={() => {
                    onOpenChat(chat.chatId);
                    onClose();
                  }}
                  onEdit={() => onEditChat(chat)}
                  onDelete={() => {
                    setSwiped(null);
                    onDeleteChat(chat.chatId);
                  }}
                >
                  <span className="block truncate text-[18px] leading-snug">{label(chat)}</span>
                </SwipeRow>
              ))}
            </ul>
          )}
          {found && (
            <>
              <div className="c-divider mx-3 my-4 border-t" />
              <p className="c-muted px-3 pb-2 text-[13px] font-medium">聊天记录</p>
              {found.hits.length === 0 ? (
                <p className="c-muted px-3 py-2 text-[14px] leading-relaxed">聊天记录里没有「{found.words}」</p>
              ) : (
                <ul className="flex flex-col gap-0.5">
                  {found.hits.map((hit, index) => (
                    <li key={`${hit.chat_id}-${hit.at_ms}-${index}`}>
                      <button
                        type="button"
                        className="c-pressable w-full rounded-2xl px-3 py-2 text-left"
                        onClick={() => {
                          onOpenHit?.(hit);
                          onClose();
                        }}
                      >
                        <span className="c-muted flex justify-between gap-2 text-[12px]">
                          <span className="truncate">
                            {hit.role === "user" ? "我" : name} · {hit.chat_title}
                          </span>
                          <span className="shrink-0">{hitTime(hit.at_ms)}</span>
                        </span>
                        <span className="line-clamp-3 block text-[15px] leading-snug">{marked(hit.text, found.words)}</span>
                      </button>
                    </li>
                  ))}
                </ul>
              )}
            </>
          )}
        </div>

        <div className="flex items-center gap-2 px-3 pt-2" style={{ paddingBottom: "max(env(safe-area-inset-bottom), 14px)" }}>
          <button type="button" onClick={onOpenSettings} aria-label="设置" className="c-float c-pressable grid h-12 w-12 shrink-0 place-items-center rounded-full">
            <Settings className="h-6 w-6" />
          </button>
          <label className="c-float flex h-12 min-w-0 flex-1 items-center gap-2 rounded-full px-4">
            <Search className="c-muted h-4 w-4 shrink-0" />
            <input
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder="搜索"
              aria-label="搜索旁聊和聊天记录"
              className="min-w-0 flex-1 bg-transparent text-[16px] outline-none"
            />
          </label>
          <button
            type="button"
            onClick={() => {
              onNewChat();
              onClose();
            }}
            aria-label="新开旁聊"
            className="c-float c-pressable grid h-12 w-12 shrink-0 place-items-center rounded-full"
          >
            <SquarePen className="h-6 w-6" />
          </button>
        </div>
      </aside>

    </>
  );
}

/** The words searched for, in the accent colour. */
function marked(text: string, words: string): ReactNode {
  const at = text.toLowerCase().indexOf(words.toLowerCase());
  if (at < 0) return text;
  return (
    <>
      {text.slice(0, at)}
      <b style={{ color: "var(--c-primary)" }}>{text.slice(at, at + words.length)}</b>
      {text.slice(at + words.length)}
    </>
  );
}

function hitTime(ms: number, now = new Date()): string {
  const date = new Date(ms);
  return date.toDateString() === now.toDateString()
    ? date.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit", hour12: false })
    : `${date.getMonth() + 1}月${date.getDate()}日`;
}
