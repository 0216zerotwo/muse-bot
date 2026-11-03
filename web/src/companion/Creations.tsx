import { useCallback, useEffect, useMemo, useState } from "react";
import { FileText, Music, Play } from "lucide-react";

import { useClient } from "@/providers/ClientProvider";

import {
  fetchCreations,
  hideCreation,
  type Creation,
  type CreationKind,
  type CreationsPayload,
} from "./companion-api";
import { CompanionPet } from "./Pet";
import { PageTitle } from "./TabViews";
import { Viewer, type ViewerItem } from "./Viewer";

type Filter = "all" | CreationKind;

const FILTERS: { key: Filter; label: string }[] = [
  { key: "all", label: "全部" },
  { key: "image", label: "图片" },
  { key: "video", label: "视频" },
  { key: "document", label: "文档" },
  { key: "audio", label: "音频" },
];

const EMPTY_TEXT: Record<Filter, string> = {
  all: "做好的图片、视频和文档都在这里",
  image: "还没有图片",
  video: "还没有视频",
  document: "还没有文档",
  audio: "还没有音频",
};

export function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

/** "今天", "昨天", "10月3日", or with the year when it is not this year. */
export function creationDay(iso: string, now = new Date()): string {
  const date = new Date(iso);
  const start = new Date(now);
  start.setHours(0, 0, 0, 0);
  const dayMs = 24 * 60 * 60 * 1000;
  if (date.getTime() >= start.getTime()) return "今天";
  if (date.getTime() >= start.getTime() - dayMs) return "昨天";
  const label = `${date.getMonth() + 1}月${date.getDate()}日`;
  return date.getFullYear() === now.getFullYear() ? label : `${date.getFullYear()}年${label}`;
}

function extensionLabel(name: string): string {
  const dot = name.lastIndexOf(".");
  return dot > 0 ? name.slice(dot + 1).toUpperCase() : "文件";
}

function toViewerItem(item: Creation): ViewerItem | null {
  if (!item.url) return null;
  return {
    key: item.id,
    kind: item.kind,
    url: item.url,
    name: item.name,
    title: item.title,
    caption: item.prompt && item.prompt !== item.title ? item.prompt : creationDay(item.created_at),
    downloadUrl: `${item.url}?download=1`,
  };
}

/** Group consecutive items (already newest first) by day. */
function byDay(items: Creation[]): { day: string; items: Creation[] }[] {
  const groups: { day: string; items: Creation[] }[] = [];
  for (const item of items) {
    const day = creationDay(item.created_at);
    const last = groups[groups.length - 1];
    if (last?.day === day) last.items.push(item);
    else groups.push({ day, items: [item] });
  }
  return groups;
}

/**
 * 创意: everything it made for you, filed by kind. Pictures and videos sit in
 * a grid, documents and audio in a list; tapping one opens it full screen.
 */
export function CreationsView({ active }: { active: boolean }) {
  const { client, token } = useClient();
  const [payload, setPayload] = useState<CreationsPayload | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [filter, setFilter] = useState<Filter>("all");
  const [viewing, setViewing] = useState<number | null>(null);

  const load = useCallback(() => {
    fetchCreations(token)
      .then((next) => {
        setPayload(next);
        setError(null);
      })
      .catch((e: unknown) => setError(e instanceof Error ? e.message : String(e)));
  }, [token]);

  useEffect(() => {
    if (active) load();
  }, [active, load]);

  const shown = useMemo(
    () => (payload?.items ?? []).filter((item) => item.url && (filter === "all" || item.kind === filter)),
    [payload, filter],
  );
  const viewerItems = useMemo(() => shown.map(toViewerItem).filter((item): item is ViewerItem => item !== null), [shown]);
  const total = payload?.items.length ?? 0;
  const filters = FILTERS.filter(({ key }) => key !== "audio" || (payload?.counts.audio ?? 0) > 0 || filter === "audio");

  const remove = useCallback(
    async (item: ViewerItem) => {
      setPayload(await hideCreation(client, item.key));
    },
    [client],
  );

  return (
    <div>
      <PageTitle title="资源库" />

      <div className="flex gap-2 overflow-x-auto px-4 pb-4" role="tablist" aria-label="按类型查看">
        {filters.map(({ key, label }) => {
          const count = key === "all" ? total : payload?.counts[key] ?? 0;
          const selected = filter === key;
          return (
            <button
              key={key}
              type="button"
              role="tab"
              aria-selected={selected}
              onClick={() => setFilter(key)}
              className="c-pressable min-h-9 shrink-0 rounded-full px-3.5 text-[14px]"
              style={
                selected
                  ? { background: "var(--c-text)", color: "var(--c-bg)" }
                  : { background: "var(--c-segment)", color: "var(--c-text)" }
              }
            >
              {label}
              {payload && count > 0 && <span className="ml-1 opacity-60">{count}</span>}
            </button>
          );
        })}
      </div>

      {error && !payload ? (
        <p className="c-danger px-5 text-sm" role="alert">
          没能读取：{error}
        </p>
      ) : !payload ? (
        <p className="c-muted px-5 text-sm">正在读取…</p>
      ) : shown.length === 0 ? (
        <div className="flex flex-col items-center px-8 pt-10 text-center">
          <CompanionPet size={64} />
          <p className="mt-4 text-[16px] font-medium">还没有作品</p>
          <p className="c-muted mt-1 text-[14px]">{EMPTY_TEXT[filter]}</p>
        </div>
      ) : (
        <div className="flex flex-col gap-5">
          {byDay(shown).map((group) => {
            const media = group.items.filter((item) => item.kind === "image" || item.kind === "video");
            const files = group.items.filter((item) => item.kind === "document" || item.kind === "audio");
            return (
              <section key={group.day} aria-label={group.day}>
                <h3 className="c-muted px-5 pb-2 text-[13px] font-medium">{group.day}</h3>
                {media.length > 0 && (
                  <ul className="grid grid-cols-3 gap-1 px-4">
                    {media.map((item) => (
                      <li key={item.id}>
                        <MediaTile item={item} onOpen={() => setViewing(shown.indexOf(item))} />
                      </li>
                    ))}
                  </ul>
                )}
                {files.length > 0 && (
                  <ul className={`c-card mx-4 overflow-hidden rounded-2xl ${media.length ? "mt-2" : ""}`}>
                    {files.map((item) => (
                      <li key={item.id} className="border-b last:border-b-0" style={{ borderColor: "var(--c-border)" }}>
                        <FileRow item={item} onOpen={() => setViewing(shown.indexOf(item))} />
                      </li>
                    ))}
                  </ul>
                )}
              </section>
            );
          })}
        </div>
      )}

      {viewing !== null && viewerItems.length > 0 && (
        <Viewer items={viewerItems} start={Math.min(viewing, viewerItems.length - 1)} onClose={() => setViewing(null)} onRemove={remove} />
      )}
    </div>
  );
}

function MediaTile({ item, onOpen }: { item: Creation; onOpen: () => void }) {
  return (
    <button
      type="button"
      onClick={onOpen}
      aria-label={`${item.kind === "video" ? "视频" : "图片"}：${item.title}`}
      className="c-pressable relative block aspect-square w-full overflow-hidden rounded-lg"
      style={{ background: "var(--c-segment)" }}
    >
      {/* Pictures: a small copy from the server (the originals are 1–2 MB each). */}
      {item.kind === "image" ? (
        <img src={item.url ? `${item.url}?thumb=1` : undefined} alt="" loading="lazy" className="h-full w-full object-cover" />
      ) : (
        <>
          {/* The first frame stands in for a thumbnail. */}
          <video src={`${item.url}#t=0.1`} preload="metadata" muted playsInline className="h-full w-full object-cover" />
          {/* Videos look alike as a first frame (a look's motion clips most of all): its name
              underneath tells them apart, and the play mark stays out of the face. */}
          <span
            className="absolute inset-x-0 bottom-0 flex items-center gap-1 bg-gradient-to-t from-black/65 to-transparent px-1.5 pb-1 pt-4 text-left text-white"
            aria-hidden="true"
          >
            <Play className="h-3 w-3 shrink-0" fill="currentColor" />
            <span className="truncate text-[11px] font-medium leading-tight">{item.title.split("：")[0]}</span>
          </span>
        </>
      )}
    </button>
  );
}

function FileRow({ item, onOpen }: { item: Creation; onOpen: () => void }) {
  const Icon = item.kind === "audio" ? Music : FileText;
  return (
    <button type="button" onClick={onOpen} className="c-pressable flex min-h-16 w-full items-center gap-3 px-3.5 py-2.5 text-left">
      <span className="c-primary grid h-10 w-10 shrink-0 place-items-center rounded-xl" aria-hidden="true">
        <Icon className="h-5 w-5" />
      </span>
      <span className="min-w-0 flex-1">
        <span className="block truncate text-[15px] font-medium">{item.title}</span>
        <span className="c-muted block truncate text-[13px]">
          {extensionLabel(item.name)} · {formatSize(item.size)}
        </span>
      </span>
    </button>
  );
}
