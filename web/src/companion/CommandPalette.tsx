import {
  BarChart3,
  BookOpen,
  Bot,
  CornerDownLeft,
  Download,
  MessageCircle,
  Moon,
  Palette,
  Pin,
  Plus,
  Search,
  Settings,
  Sparkles,
  Sun,
  Trash2,
  X,
} from "lucide-react";
import { Fragment, useEffect, useMemo, useRef, useState, type ReactNode } from "react";

import type { ChatSummary } from "@/lib/types";

import { setThemeMode, themeMode } from "./appearance";
import { searchChats, type ChatSearchHit, type Helper, type SearchScope } from "./companion-api";
import { TABS, type MuseTab } from "./muse/Shell";
import { useBackKey, useDialogFocus } from "./Screen";
import type { SettingsPage } from "./Settings";

export interface CommandPaletteProps {
  token: string;
  activeChatId: string;
  activeChatTitle: string;
  sideChats: ChatSummary[];
  helpers: Helper[];
  canExport?: boolean;
  onClose: () => void;
  onSelectTab: (tab: MuseTab) => void;
  onSelectSideChat: (chatId: string) => void;
  onNewSideChat: () => void;
  onSelectHelper: (helperId: string) => void;
  onOpenSettings: (page?: SettingsPage) => void;
  onOpenAppearance?: () => void;
  onOpenKnowledge?: () => void;
  onPruneEmptyChats?: () => void;
  emptySideChatCount?: number;
  onOpenMemory: () => void;
  onExportChat: () => void;
  onToggleTheme?: () => void;
  onJumpToHit: (hit: ChatSearchHit) => void;
}

const SCOPE_OPTIONS: Array<{ id: SearchScope; label: string }> = [
  { id: "all", label: "全部" },
  { id: "main", label: "主对话" },
  { id: "sides", label: "旁聊" },
  { id: "helpers", label: "帮手" },
];

interface PaletteItem {
  id: string;
  section: string;
  label: string;
  detail?: string;
  keywords: string;
  icon: ReactNode;
  run: () => void;
  hit?: ChatSearchHit;
}

function escapeRegExp(text: string): string {
  return text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function resolveCurrentlyDark(): boolean {
  const mode = themeMode();
  if (mode === "dark") return true;
  if (mode === "light") return false;
  if (typeof document !== "undefined" && document.documentElement.classList.contains("dark")) return true;
  return typeof window !== "undefined" && Boolean(window.matchMedia?.("(prefers-color-scheme: dark)").matches);
}

/**
 * Render search result text with `<b>` highlights using server `matches: [[start, end], ...]`
 * intervals, falling back to token matching when precomputed offsets are absent.
 */
export function renderMatchedSnippet(
  text: string,
  matches?: Array<[number, number]>,
  fallbackQuery?: string,
): ReactNode {
  const source = text || "";
  if (!source) return null;

  let ranges: Array<[number, number]> = [];
  if (Array.isArray(matches) && matches.length > 0) {
    const sorted = matches
      .map(([s, e]) => [Math.max(0, Math.min(source.length, Number(s))), Math.max(0, Math.min(source.length, Number(e)))] as [number, number])
      .filter(([s, e]) => Number.isFinite(s) && Number.isFinite(e) && e > s)
      .sort((a, b) => a[0] - b[0] || a[1] - b[1]);
    for (const [start, end] of sorted) {
      const prev = ranges[ranges.length - 1];
      if (prev && start <= prev[1]) {
        prev[1] = Math.max(prev[1], end);
      } else {
        ranges.push([start, end]);
      }
    }
  } else if (fallbackQuery) {
    const tokens = fallbackQuery
      .trim()
      .split(/\s+/)
      .filter(Boolean)
      .sort((a, b) => b.length - a.length);
    if (tokens.length > 0) {
      const pattern = new RegExp(tokens.map(escapeRegExp).join("|"), "gi");
      for (const match of source.matchAll(pattern)) {
        const idx = match.index ?? 0;
        if (match[0]) ranges.push([idx, idx + match[0].length]);
      }
      ranges = ranges.slice(0, 24);
    }
  }

  if (ranges.length === 0) return source;

  const nodes: ReactNode[] = [];
  let cursor = 0;
  ranges.forEach(([start, end], index) => {
    if (start > cursor) {
      nodes.push(<Fragment key={`t-${index}`}>{source.slice(cursor, start)}</Fragment>);
    }
    nodes.push(
      <b
        key={`m-${index}`}
        className="rounded px-0.5 font-semibold"
        style={{
          color: "var(--c-primary)",
          background: "color-mix(in srgb, var(--c-primary) 16%, transparent)",
        }}
      >
        {source.slice(start, end)}
      </b>,
    );
    cursor = end;
  });
  if (cursor < source.length) {
    nodes.push(<Fragment key="t-tail">{source.slice(cursor)}</Fragment>);
  }
  return <>{nodes}</>;
}

function formatHitDate(ms: number): string {
  if (!ms) return "";
  const date = new Date(ms);
  const now = new Date();
  return date.toDateString() === now.toDateString()
    ? date.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit", hour12: false })
    : `${date.getMonth() + 1}月${date.getDate()}日`;
}

/**
 * Global Cmd/Ctrl+K command palette for navigation, quick actions, and scoped cross-session search.
 */
export function CommandPalette({
  token,
  activeChatTitle,
  sideChats,
  helpers,
  canExport = true,
  onClose,
  onSelectTab,
  onSelectSideChat,
  onNewSideChat,
  onSelectHelper,
  onOpenSettings,
  onOpenAppearance,
  onOpenKnowledge,
  onPruneEmptyChats,
  emptySideChatCount = 0,
  onOpenMemory,
  onExportChat,
  onToggleTheme,
  onJumpToHit,
}: CommandPaletteProps) {
  const panelRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const [query, setQuery] = useState("");
  const [scope, setScope] = useState<SearchScope>("all");
  const [hits, setHits] = useState<ChatSearchHit[]>([]);
  const [searching, setSearching] = useState(false);
  const [searchError, setSearchError] = useState<string | null>(null);
  const [activeIndex, setActiveIndex] = useState(0);
  const isDark = resolveCurrentlyDark();

  const screenId = useBackKey(onClose, true);
  useDialogFocus(panelRef, true, screenId);

  useEffect(() => {
    inputRef.current?.focus();
  }, []);

  const trimmed = query.trim();

  useEffect(() => {
    if (!trimmed) {
      setHits([]);
      setSearching(false);
      setSearchError(null);
      return;
    }
    if (!token) return;
    let cancelled = false;
    const controller = new AbortController();
    setSearching(true);
    setSearchError(null);
    const timer = window.setTimeout(() => {
      searchChats(token, trimmed, { scope, limit: 25, signal: controller.signal })
        .then((results) => {
          if (cancelled) return;
          setHits(results);
          setSearching(false);
        })
        .catch((err) => {
          if (cancelled) return;
          if (err instanceof DOMException && err.name === "AbortError") return;
          setHits([]);
          setSearching(false);
          setSearchError("搜索暂时不可用");
        });
    }, 150);

    return () => {
      cancelled = true;
      window.clearTimeout(timer);
      controller.abort();
    };
  }, [scope, token, trimmed]);

  const baseItems = useMemo<PaletteItem[]>(() => {
    const items: PaletteItem[] = [];

    for (const tab of TABS) {
      items.push({
        id: `tab:${tab.id}`,
        section: "主界面",
        label: tab.label,
        detail: "切换标签页",
        keywords: `${tab.label} ${tab.id} tab 标签`,
        icon: <span className="inline-flex scale-[0.72] opacity-80">{tab.icon(false)}</span>,
        run: () => {
          onSelectTab(tab.id);
          onClose();
        },
      });
    }

    const orderedSideChats = [...sideChats]
      .filter((c) => !c.archived)
      .sort((a, b) => Number(Boolean(b.pinned)) - Number(Boolean(a.pinned)));

    for (const chat of orderedSideChats.slice(0, 6)) {
      const title = chat.title || "未命名旁聊";
      items.push({
        id: `side:${chat.chatId}`,
        section: "最近旁聊",
        label: chat.pinned ? `📌 ${title}` : title,
        detail: chat.preview || "旁聊会话",
        keywords: `${title} 旁聊 side chat${chat.pinned ? " 置顶 pinned" : ""}`,
        icon: chat.pinned ? (
          <Pin className="h-4 w-4" style={{ color: "var(--c-primary)" }} aria-hidden="true" />
        ) : (
          <MessageCircle className="h-4 w-4" aria-hidden="true" />
        ),
        run: () => {
          onSelectSideChat(chat.chatId);
          onClose();
        },
      });
    }

    if (onOpenKnowledge) {
      items.push({
        id: "tab:knowledge",
        section: "主界面",
        label: "知识库",
        detail: "全文检索与分块阅读个人知识资料",
        keywords: "知识库 资料 文档 检索 knowledge vault rag fts",
        icon: <BookOpen className="h-4 w-4" aria-hidden="true" />,
        run: () => {
          onOpenKnowledge();
          onClose();
        },
      });
    }

    for (const helper of helpers) {
      items.push({
        id: `helper:${helper.id}`,
        section: "帮手",
        label: helper.name,
        detail: helper.title || "专长帮手",
        keywords: `${helper.name} ${helper.title} helper 帮手`,
        icon: helper.emoji ? (
          <span className="text-[16px] leading-none" aria-hidden="true">{helper.emoji}</span>
        ) : (
          <Bot className="h-4 w-4" aria-hidden="true" />
        ),
        run: () => {
          onSelectHelper(helper.id);
          onClose();
        },
      });
    }

    const settingShortcuts: Array<{ id: string; label: string; detail: string; keywords: string; icon?: ReactNode; run: () => void }> = [
      {
        id: "setting:models",
        label: "模型设置",
        detail: "切换默认或路由模型",
        keywords: "模型 models settings 设置",
        run: () => {
          onOpenSettings("models");
          onClose();
        },
      },
      {
        id: "setting:usage",
        label: "用量统计",
        detail: "查看近 14 天趋势、缓存省钱与花费估算",
        keywords: "用量 统计 费用 缓存 usage cost tokens analytics",
        icon: <BarChart3 className="h-4 w-4" aria-hidden="true" />,
        run: () => {
          onOpenSettings("usage");
          onClose();
        },
      },
      ...(onOpenAppearance
        ? [
            {
              id: "setting:appearance",
              label: "外观设置",
              detail: "调整深浅模式与宠物尺寸",
              keywords: "外观 主题 宠物 尺寸 appearance theme dock",
              icon: <Palette className="h-4 w-4" aria-hidden="true" />,
              run: () => {
                onClose();
                onOpenAppearance();
              },
            },
          ]
        : []),
      {
        id: "setting:memory",
        label: "记忆管理",
        detail: "查看、编辑和置顶长期记忆",
        keywords: "记忆 memory identity 身份 settings 设置",
        run: () => {
          onClose();
          onOpenMemory();
        },
      },
      {
        id: "setting:skills",
        label: "技能管理",
        detail: "查看伙伴已学会的技能",
        keywords: "技能 skills settings 设置",
        run: () => {
          onOpenSettings("skills");
          onClose();
        },
      },
      {
        id: "setting:connections",
        label: "连接设置",
        detail: "MCP 与外部工具连接",
        keywords: "连接 connections mcp settings 设置",
        run: () => {
          onOpenSettings("connections");
          onClose();
        },
      },
      {
        id: "setting:permissions",
        label: "权限设置",
        detail: "审批规则与沙盒限制",
        keywords: "权限 permissions approval settings 设置",
        run: () => {
          onOpenSettings("permissions");
          onClose();
        },
      },
    ];
    for (const entry of settingShortcuts) {
      items.push({
        id: entry.id,
        section: "常用设置",
        label: entry.label,
        detail: entry.detail,
        keywords: entry.keywords,
        icon: entry.icon ?? <Settings className="h-4 w-4" aria-hidden="true" />,
        run: entry.run,
      });
    }

    items.push({
      id: "action:new-side",
      section: "快捷操作",
      label: "新建旁聊",
      detail: "Cmd/Ctrl+Shift+O · 开一个独立主题对话",
      keywords: "新建 旁聊 new side chat",
      icon: <Plus className="h-4 w-4" aria-hidden="true" />,
      run: () => {
        onClose();
        onNewSideChat();
      },
    });
    if (onPruneEmptyChats) {
      items.push({
        id: "action:prune-empty",
        section: "快捷操作",
        label: "清理空旁聊",
        detail: emptySideChatCount > 0 ? `可清理 ${emptySideChatCount} 个未发消息的空旁聊` : "一键清理无消息的空旁聊",
        keywords: "清理 空 旁聊 prune empty chats",
        icon: <Trash2 className="h-4 w-4" aria-hidden="true" />,
        run: () => {
          onClose();
          onPruneEmptyChats();
        },
      });
    }
    if (canExport) {
      items.push({
        id: "action:export",
        section: "快捷操作",
        label: "导出当前对话",
        detail: `${activeChatTitle || "当前对话"} · Markdown`,
        keywords: "导出 对话 export markdown md 分享",
        icon: <Download className="h-4 w-4" aria-hidden="true" />,
        run: () => {
          onClose();
          onExportChat();
        },
      });
    }
    items.push({
      id: "action:theme",
      section: "快捷操作",
      label: isDark ? "切换到浅色主题" : "切换到深色主题",
      detail: isDark ? "当前：深色外观" : "当前：浅色外观",
      keywords: "主题 深色 浅色 theme dark light 外观",
      icon: isDark ? <Sun className="h-4 w-4" aria-hidden="true" /> : <Moon className="h-4 w-4" aria-hidden="true" />,
      run: () => {
        if (onToggleTheme) {
          onToggleTheme();
        } else {
          setThemeMode(isDark ? "light" : "dark");
        }
        onClose();
      },
    });

    return items;
  }, [
    activeChatTitle,
    canExport,
    emptySideChatCount,
    helpers,
    isDark,
    onClose,
    onExportChat,
    onNewSideChat,
    onOpenAppearance,
    onOpenKnowledge,
    onOpenMemory,
    onOpenSettings,
    onPruneEmptyChats,
    onSelectHelper,
    onSelectSideChat,
    onSelectTab,
    onToggleTheme,
    sideChats,
  ]);

  const visibleItems = useMemo<PaletteItem[]>(() => {
    if (!trimmed) return baseItems;
    const lowered = trimmed.toLowerCase();
    const matchedCommands = baseItems.filter(
      (item) =>
        item.label.toLowerCase().includes(lowered) ||
        (item.detail ?? "").toLowerCase().includes(lowered) ||
        item.keywords.toLowerCase().includes(lowered),
    );
    const searchItems: PaletteItem[] = hits.map((hit, idx) => ({
      id: `hit:${hit.chat_id}:${hit.turn_id ?? idx}:${hit.at_ms}`,
      section: "聊天记录命中",
      label: hit.title || hit.chat_title || "对话",
      detail: formatHitDate(hit.at_ms),
      keywords: "",
      icon: <Sparkles className="h-4 w-4" aria-hidden="true" />,
      hit,
      run: () => {
        onClose();
        onJumpToHit(hit);
      },
    }));
    return [...searchItems, ...matchedCommands];
  }, [baseItems, hits, onClose, onJumpToHit, trimmed]);

  useEffect(() => {
    setActiveIndex(0);
  }, [trimmed, scope, hits.length]);

  useEffect(() => {
    const activeEl = panelRef.current?.querySelector<HTMLElement>(`[data-palette-index="${activeIndex}"]`);
    activeEl?.scrollIntoView?.({ block: "nearest" });
  }, [activeIndex]);

  const groupedSections = useMemo(() => {
    const map = new Map<string, Array<{ item: PaletteItem; index: number }>>();
    visibleItems.forEach((item, index) => {
      const bucket = map.get(item.section);
      if (bucket) bucket.push({ item, index });
      else map.set(item.section, [{ item, index }]);
    });
    return Array.from(map.entries());
  }, [visibleItems]);

  const handleKeyDown = (event: React.KeyboardEvent) => {
    if (event.key === "ArrowDown") {
      event.preventDefault();
      if (visibleItems.length > 0) setActiveIndex((prev) => (prev + 1) % visibleItems.length);
      return;
    }
    if (event.key === "ArrowUp") {
      event.preventDefault();
      if (visibleItems.length > 0) setActiveIndex((prev) => (prev - 1 + visibleItems.length) % visibleItems.length);
      return;
    }
    if (event.key === "Enter" && !event.nativeEvent.isComposing) {
      const picked = visibleItems[activeIndex];
      if (picked) {
        event.preventDefault();
        picked.run();
      }
    }
  };

  return (
    <div
      className="fixed inset-0 z-50 flex items-start justify-center bg-black/45 px-3 pt-[10vh] backdrop-blur-[2px]"
      onClick={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <div
        ref={panelRef}
        role="dialog"
        aria-modal="true"
        aria-label="全局指令面板"
        onKeyDown={handleKeyDown}
        className="c-card c-rise flex max-h-[76vh] w-full max-w-xl flex-col overflow-hidden rounded-[24px] shadow-2xl"
      >
        <div className="flex items-center gap-2.5 border-b px-4 py-3" style={{ borderColor: "var(--c-hairline)" }}>
          <Search className="c-muted h-4.5 w-4.5 shrink-0" aria-hidden="true" />
          <input
            ref={inputRef}
            type="search"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="搜索全部对话、切换页面或执行指令…"
            aria-label="搜索对话或输入指令"
            className="min-w-0 flex-1 bg-transparent text-[15px] outline-none placeholder:text-[color:var(--c-text-muted)]"
          />
          {query && (
            <button
              type="button"
              onClick={() => setQuery("")}
              aria-label="清空搜索"
              className="c-pressable grid h-7 w-7 place-items-center rounded-full"
            >
              <X className="h-3.5 w-3.5" aria-hidden="true" />
            </button>
          )}
          <kbd className="c-muted hidden rounded-md border px-1.5 py-0.5 text-[11px] sm:inline-block" style={{ borderColor: "var(--c-border)" }}>
            Esc
          </kbd>
        </div>

        <div className="flex flex-wrap items-center gap-1.5 border-b px-4 py-2" style={{ borderColor: "var(--c-hairline)" }}>
          {SCOPE_OPTIONS.map((option) => {
            const selected = scope === option.id;
            return (
              <button
                key={option.id}
                type="button"
                aria-pressed={selected}
                onClick={() => setScope(option.id)}
                className={`rounded-full px-2.5 py-1 text-[12px] font-medium transition-colors ${
                  selected ? "c-primary" : "c-pressable c-muted"
                }`}
                style={selected ? undefined : { background: "var(--c-fill)" }}
              >
                {option.label}
              </button>
            );
          })}
          {searching && <span className="c-muted ml-auto text-[12px]">正在搜索…</span>}
        </div>

        <div className="min-h-0 flex-1 overflow-y-auto px-2 py-2" role="listbox" aria-label="指令与搜索结果">
          {searchError && <p className="c-muted px-3 py-2 text-[12.5px]">{searchError}</p>}
          {groupedSections.length === 0 ? (
            <p className="c-muted py-10 text-center text-[13.5px]">
              {searching ? "正在翻找记录…" : "没有匹配的对话或指令"}
            </p>
          ) : (
            groupedSections.map(([section, entries]) => (
              <div key={section} className="mb-2 last:mb-0">
                <p className="c-muted px-3 py-1 text-[11.5px] font-semibold uppercase tracking-wide">{section}</p>
                <div className="flex flex-col gap-0.5">
                  {entries.map(({ item, index }) => {
                    const active = index === activeIndex;
                    return (
                      <button
                        key={item.id}
                        type="button"
                        role="option"
                        aria-selected={active}
                        data-palette-index={index}
                        onMouseEnter={() => setActiveIndex(index)}
                        onClick={item.run}
                        className="flex w-full items-start gap-3 rounded-2xl px-3 py-2 text-left transition-colors"
                        style={
                          active
                            ? { background: "color-mix(in srgb, var(--c-primary) 13%, transparent)" }
                            : undefined
                        }
                      >
                        <span className="c-muted mt-0.5 grid h-7 w-7 shrink-0 place-items-center rounded-xl" style={{ background: "var(--c-fill)" }}>
                          {item.icon}
                        </span>
                        <span className="min-w-0 flex-1">
                          <span className="flex items-center justify-between gap-2 text-[14px] font-medium">
                            <span className="truncate">{item.label}</span>
                            {item.detail && <span className="c-muted shrink-0 text-[11.5px] font-normal">{item.detail}</span>}
                          </span>
                          {item.hit && (
                            <span className="c-muted mt-0.5 line-clamp-2 block text-[12.5px] leading-snug">
                              {item.hit.role === "user" ? "我：" : item.hit.helper ? "帮手：" : "伙伴："}
                              {renderMatchedSnippet(item.hit.snippet || item.hit.text, item.hit.matches, trimmed)}
                            </span>
                          )}
                        </span>
                        {active && <CornerDownLeft className="c-muted mt-1.5 h-3.5 w-3.5 shrink-0" aria-hidden="true" />}
                      </button>
                    );
                  })}
                </div>
              </div>
            ))
          )}
        </div>
      </div>
    </div>
  );
}
