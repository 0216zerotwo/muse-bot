import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { useSessions } from "@/hooks/useSessions";
import type { ChatSummary, UIMessage } from "@/lib/types";
import { useClient } from "@/providers/ClientProvider";

import {
  changeChat,
  fetchCompanionState,
  fetchHelpers,
  forkSideChat,
  searchChats,
  type HelpersPayload,
  updateCompanionState,
  type CompanionChanges,
  type CompanionState,
} from "./companion-api";
import { CompanionScreen } from "./CompanionScreen";
import { CompanionTheme } from "./CompanionTheme";
import { exportThread } from "./exportThread";
import { MuseDrawer } from "./muse/Drawer";
import { Avatar, GroupFace, markSeen } from "./muse/helperPrimitives";
import { MeFace, type ChatApp } from "./muse/Messenger";
import { androidShell } from "./android-shell";
import { adoptAppearance, followSystemTheme, setAppearanceSaver, setThemeMode, themeMode } from "./appearance";
import { useTabOrder, type MuseTab } from "./muse/Shell";
import { CompanionAvatarContext, lastLook, NO_LOOK, PetLibraryProvider, PetView } from "./Pet";
import { LazyBoundary, lazyScreen, useEdgeSwipe } from "./Screen";
import type { SettingsPage } from "./Settings";

const Onboarding = lazyScreen(() => import("./Onboarding").then((m) => ({ default: m.Onboarding })));
const SettingsScreen = lazyScreen(() => import("./Settings").then((m) => ({ default: m.SettingsScreen })));
const AppearanceDock = lazyScreen(() => import("./AppearanceDock").then((m) => ({ default: m.AppearanceDock })));
const SideChatSheet = lazyScreen(() => import("./muse/SideChatSheet").then((m) => ({ default: m.SideChatSheet })));
const HelperProfileSheet = lazyScreen(() => import("./muse/Helpers").then((m) => ({ default: m.HelperProfileSheet })));
const GroupSheet = lazyScreen(() => import("./muse/Helpers").then((m) => ({ default: m.GroupSheet })));
const CommandPalette = lazyScreen(() => import("./CommandPalette").then((m) => ({ default: m.CommandPalette })));

/** Session summary for the companion chat, even before its first message is persisted. */
function companionSession(chatId: string, sessions: ChatSummary[]): ChatSummary {
  return (
    sessions.find((session) => session.chatId === chatId) ?? {
      key: `websocket:${chatId}`,
      channel: "websocket",
      chatId,
      createdAt: null,
      updatedAt: null,
      preview: "",
    }
  );
}

export function CompanionShell({ onLogout }: { onLogout: () => void }) {
  const { client, token } = useClient();
  const { sessions, refresh, createChat } = useSessions();
  const [companion, setCompanion] = useState<CompanionState | null>(null);
  const [error, setError] = useState<string | null>(null);
  // The settings screen and the page it opened on; null while chatting.
  const [settings, setSettings] = useState<SettingsPage | null>(null);
  const creatingRef = useRef(false);
  // The sidebar; the tab showing; the side chat (旁聊) open instead of the tabs, if any.
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [tab, setTab] = useState<MuseTab>("chat");
  const [sideChatId, setSideChatId] = useState<string | null>(null);
  // The side chat whose settings (name, delete) are open.
  const [editingChat, setEditingChat] = useState<ChatSummary | null>(null);
  // Helpers and group chats: whose chat is open decides the header and its settings sheet.
  const [helpersData, setHelpersData] = useState<HelpersPayload | null>(null);
  const [editingHelper, setEditingHelper] = useState<string | null>(null);
  const [editingGroup, setEditingGroup] = useState(false);
  // From settings: a message to start in the main chat, and 教它做 (the browser, recording).
  const [draftRequest, setDraftRequest] = useState<{ text: string; at: number } | null>(null);
  const [jumpRequest, setJumpRequest] = useState<{ chatId: string; turnId: string; at: number } | null>(null);
  const [teachRequest, setTeachRequest] = useState(0);
  const [order, setOrder] = useTabOrder();
  const [appearance, setAppearance] = useState(false);
  const [paletteOpen, setPaletteOpen] = useState(false);
  const [composerFocusRequest, setComposerFocusRequest] = useState(0);
  const [memoryOpenRequest, setMemoryOpenRequest] = useState(0);
  // 外观 → 跟随系统: switch along with the phone while the app is open.
  useEffect(followSystemTheme, []);
  // A swipe in from the left edge opens the side menu (swiped back, it closes); inside a page over
  // the main screen, the same swipe goes back instead.
  useEdgeSwipe(() => setDrawerOpen(true), !drawerOpen && !appearance);

  // 外观 changes go to the server; what it keeps applies on opening.
  useEffect(() => {
    setAppearanceSaver((changes) => {
      updateCompanionState(client, changes).then(setCompanion).catch(() => undefined);
    });
    return () => setAppearanceSaver(null);
  }, [client]);

  useEffect(() => {
    let cancelled = false;
    fetchCompanionState(token)
      .then((state) => {
        if (cancelled) return;
        setCompanion(state);
        adoptAppearance(state);
      })
      .catch((e: unknown) => {
        if (!cancelled) setError(e instanceof Error ? e.message : String(e));
      });
    return () => {
      cancelled = true;
    };
  }, [token]);

  // First launch: create the companion thread once and remember it on the
  // server, so every device opens the same conversation.
  useEffect(() => {
    if (!companion || companion.chat_id || creatingRef.current) return;
    creatingRef.current = true;
    (async () => {
      try {
        const chatId = await createChat();
        setCompanion(await updateCompanionState(client, { chat_id: chatId }));
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e));
      } finally {
        creatingRef.current = false;
      }
    })();
  }, [client, companion, createChat]);

  const chatId = companion?.chat_id ?? null;
  const shownId = sideChatId ?? chatId;
  const session = useMemo(() => (shownId ? companionSession(shownId, sessions) : null), [shownId, sessions]);
  const sideChats = useMemo(
    // Helpers' chats and group chats have their own places (the 帮手 page, 群聊), not 旁聊.
    () => sessions.filter((s) => s.channel === "websocket" && s.chatId !== chatId && !s.helper && !s.group),
    [sessions, chatId],
  );
  // A tab, from the bar or the sidebar: leaves a side chat for it (主要聊天 is the main chat).
  const openTab = useCallback((next: MuseTab) => {
    setSideChatId(null);
    setTab(next);
  }, []);
  // Text shared to the Android app from another app (a link, a passage): into the main chat's box.
  useEffect(() => {
    const take = () => {
      const shared = androidShell()?.takeShared?.();
      if (!shared) return;
      openTab("chat");
      setDraftRequest({ text: shared, at: Date.now() });
    };
    take();
    window.addEventListener("companion:shared", take);
    return () => window.removeEventListener("companion:shared", take);
  }, [openTab]);
  const searchHistory = useCallback((words: string) => searchChats(token, words), [token]);
  const loadHelpers = useCallback(() => {
    fetchHelpers(token).then(setHelpersData).catch(() => undefined);
  }, [token]);
  useEffect(loadHelpers, [loadHelpers, sessions]);
  const openHelper = helpersData?.helpers.find((h) => h.chat_id === sideChatId) ?? null;
  const openGroup = helpersData?.groups.find((g) => g.chat_id === sideChatId) ?? null;
  const groupNames = openGroup
    ? openGroup.members.map((id) => helpersData?.helpers.find((h) => h.id === id)?.name).filter(Boolean).join("、")
    : "";
  // A helper's or a group's chat reads as a conversation in a chat app; ‹ goes back to the 帮手 list.
  const chatApp = useMemo((): ChatApp | undefined => {
    if (!sideChatId || (!openHelper && !openGroup) || !companion) return undefined;
    const back = () => {
      markSeen(sideChatId);
      setSideChatId(null);
      setTab("helpers");
    };
    const host = <PetView value={companion.avatar} size={36} />;
    if (openHelper) {
      const face = <Avatar emoji={openHelper.emoji} avatar={openHelper.avatar} size={36} />;
      return {
        title: openHelper.name,
        subtitle: openHelper.setup ? "正在设定" : openHelper.title || "帮手",
        speaker: () => ({ face }),
        me: <MeFace />,
        relay: { face: host, name: companion.name },
        workingFace: face,
        bigFace: <Avatar emoji={openHelper.emoji} avatar={openHelper.avatar} size={72} />,
        hint: openHelper.title ? `${openHelper.title}。有事直接说。` : "跟它说说你想让它负责什么。",
        // A default one still doing its default job: a few things to try.
        starters: openHelper.is_default ? helpersData?.templates.find((t) => t.name === openHelper.preset)?.starters : undefined,
        onBack: back,
        onProfile: () => setEditingHelper(openHelper.chat_id),
      };
    }
    const group = openGroup!;
    const members = group.members.map((id) => helpersData?.helpers.find((h) => h.id === id)).filter((h) => h !== undefined);
    return {
      title: `${group.name}（${members.length + 1}）`,
      subtitle: groupNames,
      speaker: (message) => {
        // A helper speaking for itself; otherwise the companion, hosting the group.
        if (message.source?.kind === "helper") {
          const helper = members.find((h) => h.id === message.source?.id || message.source?.label === `${h.emoji} ${h.name}`);
          if (helper) return { face: <Avatar emoji={helper.emoji} avatar={helper.avatar} size={36} />, name: helper.name };
          return { face: <Avatar emoji="🤖" size={36} />, name: message.source.label?.replace(/^\S+\s/, "") };
        }
        return { face: host, name: companion.name };
      },
      me: <MeFace />,
      workingFace: host,
      bigFace: <GroupFace members={members} size={72} />,
      hint: "说一件事，大家一起做。@ 谁就交给谁，不 @ 就由主助理来分派。",
      onBack: back,
      onProfile: () => setEditingGroup(true),
    };
  }, [sideChatId, openHelper, openGroup, companion, helpersData, groupNames]);

  const newSideChat = useCallback(async () => {
    try {
      setSideChatId(await createChat());
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, [createChat]);

  const handleForkChat = useCallback(
    async (message: UIMessage) => {
      if (!shownId) return;
      const forked = await forkSideChat(client, shownId, message.turnId || undefined);
      await refresh();
      if (forked?.chat_id) {
        setSideChatId(forked.chat_id);
        setTab("chat");
      }
      androidShell()?.haptic?.("light");
    },
    [client, refresh, shownId],
  );

  const handleExportChat = useCallback(
    async (targetId?: string, title?: string) => {
      const exportId = targetId || shownId;
      if (!exportId) return;
      await exportThread(token, exportId, { title: title ?? session?.title ?? companion?.name });
    },
    [companion?.name, session?.title, shownId, token],
  );

  // Global desktop shortcuts: Cmd/Ctrl+K (command palette), Cmd/Ctrl+Shift+O (new side chat), "/" (focus input).
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (!companion?.onboarded || !chatId) return;
      const key = event.key.toLowerCase();
      if ((event.metaKey || event.ctrlKey) && !event.altKey && !event.shiftKey && key === "k") {
        event.preventDefault();
        setPaletteOpen((open) => !open);
        return;
      }
      if ((event.metaKey || event.ctrlKey) && event.shiftKey && !event.altKey && key === "o") {
        event.preventDefault();
        setPaletteOpen(false);
        void newSideChat();
        return;
      }
      if (
        event.key === "/" &&
        !event.metaKey &&
        !event.ctrlKey &&
        !event.altKey &&
        !paletteOpen &&
        !drawerOpen &&
        !settings &&
        !appearance &&
        !editingChat &&
        !editingHelper &&
        !editingGroup
      ) {
        const target = event.target as HTMLElement | null;
        if (
          target &&
          (target.tagName === "INPUT" ||
            target.tagName === "TEXTAREA" ||
            target.tagName === "SELECT" ||
            target.isContentEditable)
        ) {
          return;
        }
        if (document.querySelector('[role="dialog"], [aria-modal="true"]')) return;
        event.preventDefault();
        openTab("chat");
        setComposerFocusRequest((n) => n + 1);
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [
    appearance,
    chatId,
    companion?.onboarded,
    drawerOpen,
    editingChat,
    editingGroup,
    editingHelper,
    newSideChat,
    openTab,
    paletteOpen,
    settings,
  ]);

  // A turn can change its look (it drew a new one): pick that up (the library loads the drawing).
  const reloadLook = useCallback(() => {
    fetchCompanionState(token).then(setCompanion).catch(() => undefined);
  }, [token]);

  const saveIdentity = useCallback(
    async (changes: CompanionChanges) => {
      setCompanion(await updateCompanionState(client, changes));
    },
    [client],
  );

  return (
    <PetLibraryProvider avatar={companion?.avatar ?? null}>
    <CompanionAvatarContext.Provider value={companion?.avatar ?? null}>
    <CompanionTheme avatar={companion?.avatar ?? null} />
    <div className="companion h-full">
      {session && companion && !companion.onboarded ? (
        <LazyBoundary>
          <Onboarding companion={companion} onDone={setCompanion} />
        </LazyBoundary>
      ) : session && companion ? (
        <CompanionScreen
          key={session.key}
          session={session}
          companion={companion}
          tab={sideChatId ? "chat" : tab}
          onTab={openTab}
          order={order}
          side={
            !sideChatId
              ? null
              : openHelper
                ? `帮手 · ${openHelper.setup ? "正在设定" : openHelper.title || "没写职责"}`
                : openGroup
                  ? `群聊 · ${groupNames}`
                  : `旁聊 · ${session.title || "新的旁聊"}`
          }
          sideName={openHelper?.name ?? openGroup?.name}
          chatApp={chatApp}
          sideFace={
            openHelper ? <Avatar emoji={openHelper.emoji} avatar={openHelper.avatar} size={64} /> : undefined
          }
          onEditSide={() => (openHelper ? setEditingHelper(openHelper.chat_id) : openGroup ? setEditingGroup(true) : setEditingChat(session))}
          onOpenChat={(id) => {
            markSeen(id);
            setSideChatId(id);
            loadHelpers();
          }}
          draftRequest={sideChatId ? null : draftRequest}
          composerFocusRequest={composerFocusRequest}
          memoryOpenRequest={memoryOpenRequest}
          onMemoryRequestHandled={() => setMemoryOpenRequest(0)}
          onForkChat={handleForkChat}
          jumpRequest={jumpRequest && jumpRequest.chatId === session.chatId ? { turnId: jumpRequest.turnId, at: jumpRequest.at } : null}
          teachRequest={teachRequest}
          onOpenMenu={() => setDrawerOpen(true)}
          onUpdateCompanion={saveIdentity}
          onChangeLook={() => {
            openTab("chat");
            setDraftRequest({ text: "把虚拟形象改为", at: Date.now() });
          }}
          onTurnEnd={() => {
            void refresh();
            reloadLook();
            // A reply may have set or moved a reminder: the Android app sets its notifications again.
            androidShell()?.planReminders?.();
            // A new side chat is named just after its first reply: pick the name up then.
            if (sideChatId && !session.title) window.setTimeout(() => void refresh(), 5_000);
          }}
        />
      ) : (
        <div className="flex h-full flex-col items-center justify-center gap-4 px-8 text-center">
          {/* Before its settings arrive: the look this phone remembers, or nothing (not the default cat). */}
          <PetView value={companion?.avatar ?? lastLook() ?? NO_LOOK} animation={error ? "failed" : "running"} size={72} />
          <p className={error ? "c-danger text-sm" : "c-muted text-sm"}>{error ?? "正在准备…"}</p>
        </div>
      )}

      {companion && chatId && (
        <MuseDrawer
          open={drawerOpen}
          onClose={() => setDrawerOpen(false)}
          name={companion.name}
          current={tab}
          activeChatId={sideChatId}
          order={order}
          onReorder={setOrder}
          onTab={openTab}
          sideChats={sideChats}
          onOpenChat={setSideChatId}
          onNewChat={() => void newSideChat()}
          onOpenSettings={() => {
            setDrawerOpen(false);
            setSettings("root");
          }}
          onEditChat={setEditingChat}
          search={searchHistory}
          onOpenHit={(hit) => {
            if (hit.chat_id === chatId) openTab("chat");
            else setSideChatId(hit.chat_id);
            if (hit.turn_id) setJumpRequest({ chatId: hit.chat_id, turnId: hit.turn_id, at: Date.now() });
            else setJumpRequest(null);
          }}
          onDeleteChat={(id) => {
            if (id === sideChatId) setSideChatId(null);
            void changeChat(client, id, "delete").then(() => refresh());
          }}
          onExportCurrentChat={shownId ? () => void handleExportChat(shownId, session?.title || companion.name) : undefined}
        />
      )}

      {editingHelper && (
        <LazyBoundary onClose={() => setEditingHelper(null)}>
          <HelperProfileSheet
            chatId={editingHelper}
            onClose={() => setEditingHelper(null)}
            onChanged={loadHelpers}
            onDeleted={() => {
              setEditingHelper(null);
              setSideChatId(null);
              openTab("helpers");
              void refresh();
            }}
          />
        </LazyBoundary>
      )}
      {editingGroup && (
        <LazyBoundary onClose={() => setEditingGroup(false)}>
          <GroupSheet
            open={editingGroup}
            group={openGroup}
            onClose={() => setEditingGroup(false)}
            onSaved={loadHelpers}
            onDeleted={() => {
              setSideChatId(null);
              openTab("helpers");
              void refresh();
            }}
          />
        </LazyBoundary>
      )}

      {editingChat && (
        <LazyBoundary onClose={() => setEditingChat(null)}>
          <SideChatSheet
            chat={editingChat}
            onClose={() => setEditingChat(null)}
            onRename={(id, title) => void changeChat(client, id, "rename", title).then(() => refresh())}
            onDelete={(id) => {
              if (id === sideChatId) setSideChatId(null);
              void changeChat(client, id, "delete").then(() => refresh());
            }}
            onExport={(chat) => handleExportChat(chat.chatId, chat.title)}
          />
        </LazyBoundary>
      )}

      {settings && companion && (
        <LazyBoundary onClose={() => setSettings(null)}>
          <SettingsScreen
            start={settings}
            onClose={() => setSettings(null)}
            onLogout={onLogout}
            onDraft={(text) => {
              setSettings(null);
              openTab("chat");
              setDraftRequest({ text, at: Date.now() });
            }}
            onTeach={() => {
              setSettings(null);
              openTab("chat");
              setTeachRequest(Date.now());
            }}
            onAppearance={() => {
              setSettings(null);
              openTab("chat");
              setAppearance(true);
            }}
          />
        </LazyBoundary>
      )}
      {appearance && (
        <LazyBoundary onClose={() => setAppearance(false)}>
          <AppearanceDock onDone={() => setAppearance(false)} />
        </LazyBoundary>
      )}
      {paletteOpen && companion && chatId && companion.onboarded && (
        <LazyBoundary onClose={() => setPaletteOpen(false)}>
          <CommandPalette
            token={token}
            activeChatId={shownId ?? chatId}
            activeChatTitle={session?.title || companion.name}
            sideChats={sideChats}
            helpers={helpersData?.helpers ?? []}
            canExport={Boolean(shownId)}
            onClose={() => setPaletteOpen(false)}
            onSelectTab={openTab}
            onSelectSideChat={(id) => {
              setSideChatId(id);
              setTab("chat");
            }}
            onNewSideChat={() => void newSideChat()}
            onSelectHelper={(helperId) => {
              const helper = helpersData?.helpers.find((h) => h.id === helperId);
              if (helper) {
                markSeen(helper.chat_id);
                setSideChatId(helper.chat_id);
                loadHelpers();
              }
            }}
            onOpenSettings={(page) => setSettings(page ?? "root")}
            onOpenMemory={() => {
              openTab("chat");
              setMemoryOpenRequest((n) => n + 1);
            }}
            onExportChat={() => void handleExportChat(shownId ?? chatId, session?.title || companion.name)}
            onToggleTheme={() => {
              const mode = themeMode();
              const currentlyDark =
                mode === "dark" ||
                (mode === "system" &&
                  (document.documentElement.classList.contains("dark") ||
                    Boolean(window.matchMedia?.("(prefers-color-scheme: dark)").matches)));
              setThemeMode(currentlyDark ? "light" : "dark");
            }}
            onJumpToHit={(hit) => {
              if (hit.chat_id === chatId) openTab("chat");
              else setSideChatId(hit.chat_id);
              if (hit.turn_id) setJumpRequest({ chatId: hit.chat_id, turnId: hit.turn_id, at: Date.now() });
              else setJumpRequest(null);
            }}
          />
        </LazyBoundary>
      )}
    </div>
    </CompanionAvatarContext.Provider>
    </PetLibraryProvider>
  );
}
