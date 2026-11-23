/*
 * 动态, as Muse's feed: posts the companion writes about what the user cares about, grouped by
 * day; the sentence that steers them on top (tap to edit); each post a topic tile, a title, a
 * short read with its sources linked, maybe a picture, and ❤ · 讨论 · time · ⓘ. Ported in shape
 * from nanoMuse (vendor/nanomuse-web/web/src/screens/FeedScreen.tsx, GPL-3.0-or-later).
 */
import { AlertCircle, Heart, Info, Loader2, MessageCircle, PenLine } from "lucide-react";
import { useEffect, useState } from "react";

import { MarkdownText } from "@/components/MarkdownText";
import { useClient } from "@/providers/ClientProvider";

import { changeFeed, type FeedPayload, type FeedPost } from "../companion-api";
import { Switch } from "../Menu";
import { MuseSheet } from "./Sheet";
import { EmojiIcon } from "../EmojiIcon";

const AREA_EMOJI: Record<string, string> = {
  tech: "💻", ai: "🤖", finance: "📈", news: "📰", health: "🌿", food: "🍳", travel: "✈️", learning: "📚",
  entertainment: "🎬", sports: "⚽", home: "🏠", people: "👥", goals: "🎯", other: "📝",
};
const AREA_LABEL: Record<string, string> = {
  tech: "科技", ai: "AI", finance: "财经", news: "新闻", health: "健康", food: "美食", travel: "出行", learning: "学习",
  entertainment: "娱乐", sports: "体育", home: "家居", people: "人物", goals: "目标", other: "其他",
};

function ago(at: number): string {
  const minutes = Math.max(0, Math.round((Date.now() - at) / 60_000));
  if (minutes < 1) return "刚刚";
  if (minutes < 60) return `${minutes}分钟`;
  if (minutes < 60 * 24) return `${Math.round(minutes / 60)}小时`;
  return `${Math.round(minutes / 1440)}天`;
}


function PostCard({
  post,
  liked,
  onLike,
  onDiscuss,
  onInfo,
}: {
  post: FeedPost;
  liked: boolean;
  onLike: () => void;
  onDiscuss: () => void;
  onInfo: () => void;
}) {
  return (
    <article className="flex gap-3.5 px-4 pb-2 pt-5">
      <span className="w-10 shrink-0 self-start pt-0.5" aria-hidden="true">
        <EmojiIcon emoji={AREA_EMOJI[post.area] ?? "📝"} size={40} />
      </span>
      <div className="min-w-0 flex-1">
        <h3 className="text-[18px] font-bold leading-snug">{post.title}</h3>
        <div className="feed-body mt-1 text-[16px] leading-relaxed">
          <MarkdownText>{post.body}</MarkdownText>
        </div>
        {post.image && (
          <img
            src={post.image}
            alt=""
            loading="lazy"
            referrerPolicy="no-referrer"
            onError={(event) => (event.currentTarget.style.display = "none")}
            className="mt-3 aspect-[16/9] w-full rounded-2xl object-cover"
            style={{ background: "var(--c-fill)" }}
          />
        )}
        <div className="-ml-2 mt-2 flex items-center">
          <button type="button" onClick={onLike} aria-pressed={liked} aria-label={liked ? "取消喜欢" : "喜欢"} className="c-pressable grid h-10 w-10 place-items-center rounded-full">
            <Heart className="h-[22px] w-[22px]" strokeWidth={1.8} fill={liked ? "var(--c-primary)" : "none"} style={liked ? { color: "var(--c-primary)" } : undefined} />
          </button>
          <button type="button" onClick={onDiscuss} className="c-pressable flex min-h-10 items-center gap-1.5 rounded-full px-2 text-[15px]">
            <MessageCircle className="h-[22px] w-[22px]" strokeWidth={1.8} aria-hidden="true" /> 讨论
          </button>
          <span className="c-muted ml-auto text-[15px]">{ago(post.ts)}</span>
          <button type="button" onClick={onInfo} aria-label="关于这条动态" className="c-pressable grid h-10 w-10 place-items-center rounded-full">
            <Info className="h-[22px] w-[22px]" strokeWidth={1.8} />
          </button>
        </div>
      </div>
    </article>
  );
}

/** 动态版块说明: the sentence that drives every post, and when it writes. */
export function FeedSettingsSheet({
  open,
  data,
  onClose,
  onSaved,
}: {
  open: boolean;
  data: FeedPayload | null;
  onClose: () => void;
  onSaved: (data: FeedPayload, wroteNow?: boolean) => void;
}) {
  const { client } = useClient();
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    if (open && data) {
      setText(data.instructions);
      setError(null);
    }
  }, [open, data]);
  if (!data) return null;
  const run = async (task: () => Promise<FeedPayload>, wroteNow = false) => {
    setBusy(true);
    setError(null);
    try {
      onSaved(await task(), wroteNow);
      onClose();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };
  const changed = text.trim() !== data.instructions;
  return (
    <MuseSheet
      open={open}
      onClose={onClose}
      title="动态版块说明"
      footer={
        <div className="flex gap-2 pb-1">
          <button type="button" onClick={onClose} className="c-pressable min-h-12 flex-1 rounded-full text-[16px]" style={{ background: "var(--c-fill)" }}>
            取消
          </button>
          <button
            type="button"
            disabled={!changed || busy || !text.trim()}
            onClick={() => void run(() => changeFeed(client, "update", { changes: { instructions: text.trim() } }))}
            className="c-primary min-h-12 flex-1 rounded-full text-[16px] font-semibold disabled:opacity-40"
          >
            保存
          </button>
        </div>
      }
    >
      <p className="c-muted text-[14px] leading-relaxed">你的动态版块由以下指示驱动。你对此提示做出的任何编辑都将应用于今后的动态版块帖子。</p>
      <textarea
        value={text}
        onChange={(event) => setText(event.target.value)}
        rows={5}
        maxLength={500}
        aria-label="动态版块说明"
        className="c-card mt-3 w-full resize-none rounded-2xl p-3.5 text-[16px] leading-relaxed outline-none focus-visible:outline-2 focus-visible:outline-[color:var(--c-primary)]"
      />
      <div className="c-card mt-3 flex flex-col overflow-hidden rounded-2xl">
        <label className="flex min-h-12 items-center gap-3 px-3.5">
          <span className="flex-1 text-[15px]">每天自动更新</span>
          <input
            type="time"
            value={data.time}
            disabled={busy || !data.daily}
            onChange={(event) => event.target.value && void run(() => changeFeed(client, "update", { changes: { time: event.target.value } }))}
            className="c-muted bg-transparent text-[15px] outline-none disabled:opacity-40"
            aria-label="更新时间"
          />
          <Switch checked={data.daily} label="每天自动更新" disabled={busy} onChange={(daily) => void run(() => changeFeed(client, "update", { changes: { daily } }))} />
        </label>
        <button
          type="button"
          disabled={busy}
          onClick={() => void run(() => changeFeed(client, "write_now"), true)}
          className="c-pressable c-divider flex min-h-12 items-center gap-3 border-t px-3.5 text-left text-[15px] disabled:opacity-50"
          style={{ color: "var(--c-primary)" }}
        >
          <PenLine className="h-4 w-4" /> 现在就写一期
        </button>
      </div>
      {error && (
        <p className="c-danger mt-2 text-[13px]" role="alert">
          {error}
        </p>
      )}
    </MuseSheet>
  );
}

/** The 动态 tab: the steering sentence, then the posts by day. */
export function FeedScreen({
  data,
  writing,
  onEditInstructions,
  onWriteNow,
  onDiscuss,
  onChange,
}: {
  data: FeedPayload | null;
  /** A run was asked for and has not delivered yet. */
  writing: boolean;
  onEditInstructions: () => void;
  onWriteNow: () => void;
  onDiscuss: (post: FeedPost) => void;
  onChange: (data: FeedPayload) => void;
}) {
  const { client } = useClient();
  const [info, setInfo] = useState<FeedPost | null>(null);
  const [actionError, setActionError] = useState<{ message: string; retry: () => void } | null>(null);
  // ❤ is kept on the server: the next run writes more of what was liked.
  const [pending, setPending] = useState<Record<string, boolean>>({});
  const liked = (id: string) => pending[id] ?? (data?.liked ?? []).includes(id);
  const toggleLike = (id: string, desired?: boolean) => {
    const on = desired ?? !liked(id);
    setActionError(null);
    setPending((current) => ({ ...current, [id]: on }));
    changeFeed(client, "like", { id, on })
      .then(onChange)
      .catch((e: unknown) =>
        setActionError({
          message: e instanceof Error ? e.message : "操作没保存成功，请稍后再试",
          retry: () => toggleLike(id, on),
        }),
      )
      .finally(() =>
        setPending((current) => {
          const rest = { ...current };
          delete rest[id];
          return rest;
        }),
      );
  };
  const removePost = (id: string) => {
    setActionError(null);
    void changeFeed(client, "delete", { id })
      .then(onChange)
      .catch((e: unknown) =>
        setActionError({
          message: e instanceof Error ? e.message : "删除失败，请稍后再试",
          retry: () => removePost(id),
        }),
      );
  };

  return (
    <div className="pb-4">
      <div className="px-5 pb-2">
        <h2 className="text-[32px] font-bold leading-tight">动态</h2>
        <button
          type="button"
          onClick={onEditInstructions}
          className="c-pressable c-divider c-muted mt-3 block w-full truncate rounded-full border px-5 py-3 text-left text-[16px]"
        >
          {data?.instructions ?? "…"}
        </button>
      </div>

      {actionError && (
        <div className="c-card mx-4 my-2 flex items-center gap-3 rounded-2xl px-4 py-3" role="alert">
          <AlertCircle className="c-danger h-4 w-4 shrink-0" aria-hidden="true" />
          <span className="min-w-0 flex-1 truncate text-[13.5px]">{actionError.message}</span>
          <button
            type="button"
            onClick={actionError.retry}
            className="c-pressable shrink-0 rounded-full px-3 py-1 text-[13px] font-semibold"
            style={{ background: "var(--c-fill)" }}
          >
            重试
          </button>
        </div>
      )}

      {writing && (
        <div className="c-card mx-4 my-2 flex items-center gap-3 rounded-2xl px-4 py-3" role="status">
          <Loader2 className="h-4 w-4 shrink-0 animate-spin" style={{ color: "var(--c-primary)" }} aria-hidden="true" />
          <div className="min-w-0 flex-1">
            <p className="text-[14px] font-medium">正在写新一期动态…</p>
            <p className="c-muted text-[12px]">根据你的兴趣与最近聊过的话题整理中</p>
          </div>
        </div>
      )}

      {data && data.posts.length === 0 && !writing ? (
        <div className="flex flex-col items-center px-8 pt-10 text-center">
          <span aria-hidden="true">
            <EmojiIcon emoji="🗞️" size={72} />
          </span>
          <p className="mt-3 text-[16px] font-semibold">还没有动态</p>
          <p className="c-muted mt-1 max-w-xs text-[14px] leading-relaxed">
            {data.daily ? `每天 ${data.time} 自动更新，也可以现在先写一期看看` : "自动更新已关闭，随时可以手动让它写一期"}
          </p>
          <button type="button" onClick={onWriteNow} className="c-primary mt-4 min-h-11 rounded-full px-6 text-[15px] font-semibold">
            现在就写一期
          </button>
        </div>
      ) : (
        <ul className="c-divider divide-y">
          {(data?.posts ?? []).map((post) => (
            <li key={post.id}>
              <PostCard
                post={post}
                liked={liked(post.id)}
                onLike={() => toggleLike(post.id)}
                onDiscuss={() => onDiscuss(post)}
                onInfo={() => setInfo(post)}
              />
            </li>
          ))}
        </ul>
      )}

      <MuseSheet open={info !== null} onClose={() => setInfo(null)} title={info?.title}>
        {info && (
          <div className="flex flex-col gap-3 pb-2 text-[15px]">
            <div className="flex justify-between gap-4">
              <span className="c-muted">话题</span>
              <span className="flex items-center gap-1.5">
                <EmojiIcon emoji={AREA_EMOJI[info.area] ?? "📝"} size={22} /> {AREA_LABEL[info.area] ?? "其他"}
              </span>
            </div>
            <div className="flex justify-between gap-4">
              <span className="c-muted">写于</span>
              <span>{new Date(info.ts).toLocaleString("zh-CN", { dateStyle: "medium", timeStyle: "short" })}</span>
            </div>
            <p className="c-muted text-[13px] leading-relaxed">按你们聊过的事和你的目标挑话题。想换方向，改顶部那句说明。</p>
            <button
              type="button"
              onClick={() => {
                const id = info.id;
                setInfo(null);
                removePost(id);
              }}
              className="c-danger c-pressable min-h-11 rounded-full text-[15px]"
              style={{ background: "var(--c-fill)" }}
            >
              不想看这条，删掉
            </button>
          </div>
        )}
      </MuseSheet>
    </div>
  );
}
