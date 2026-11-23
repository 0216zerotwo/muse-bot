/*
 * The companion's browser, live (Muse's "computer"). The picture is a screencast over
 * /ws/computer; the take-over follows nanoMuse's BrowserViewer (vendor/nanomuse-web, GPL-3.0-or-later).
 *
 * In the status sheet it is only watched. 打开浏览器 (bottom right) opens it full screen, still
 * watched: the companion keeps control. 接管控制 gives it to the user — the companion's browser
 * actions wait, a tap clicks the page, a tap on a field brings up the keyboard and typing goes
 * straight into the page, a drag scrolls. 交还 (or leaving full screen) hands it back and, when
 * the companion had asked for it, tells it to go on. 关闭浏览器 shuts it until it is next needed.
 */
import { ArrowLeft, Check, Globe, GraduationCap, Hand, Loader2, Maximize2, Minimize2, Power, RotateCw } from "lucide-react";
import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";
import { createPortal } from "react-dom";

import { deriveWsUrl } from "@/lib/bootstrap";
import { useClient } from "@/providers/ClientProvider";

import type { ComputerStatus, TaughtStep } from "../companion-api";
import { useBackKey } from "../Screen";

type Frame = { data: string; w: number; h: number };

/** One live connection to the browser: the latest frame, the page, and a way to send input. */
function useBrowserStream(onFocus?: (editable: boolean) => void, onTaught?: (steps: TaughtStep[]) => void) {
  const { token } = useClient();
  const tokenRef = useRef(token);
  tokenRef.current = token;
  const socket = useRef<WebSocket | null>(null);
  const focusRef = useRef(onFocus);
  focusRef.current = onFocus;
  const taughtRef = useRef(onTaught);
  taughtRef.current = onTaught;
  // 教它做: how many steps are written down so far (null: not recording).
  const [teachCount, setTeachCount] = useState<number | null>(null);
  const [frame, setFrame] = useState<Frame | null>(null);
  const [page, setPage] = useState({ title: "", url: "" });
  const [hold, setHold] = useState<ComputerStatus["hold"]>(null);
  const [error, setError] = useState<string | null>(null);
  // Closed: no pictures until the companion (or 打开浏览器) starts it again.
  const [idle, setIdle] = useState(false);

  useEffect(() => {
    let closed = false;
    let retry: ReturnType<typeof setTimeout> | undefined;
    const open = () => {
      const ws = new WebSocket(deriveWsUrl("/ws/computer", tokenRef.current));
      socket.current = ws;
      ws.onmessage = (event) => {
        const msg = JSON.parse(event.data as string) as Record<string, unknown>;
        if (msg.type === "frame") {
          setFrame({ data: msg.data as string, w: msg.w as number, h: msg.h as number });
          setIdle(false);
        } else if (msg.type === "idle") {
          setFrame(null);
          setIdle(true);
          setPage({ title: "", url: "" });
        } else if (msg.type === "page") {
          setPage({ title: (msg.title as string) ?? "", url: (msg.url as string) ?? "" });
          if ("hold" in msg) setHold((msg.hold as ComputerStatus["hold"]) ?? null);
        } else if (msg.type === "hold") setHold((msg.hold as ComputerStatus["hold"]) ?? null);
        else if (msg.type === "focus") focusRef.current?.(Boolean(msg.editable));
        else if (msg.type === "error") setError(msg.message as string);
        else if (msg.type === "teach") setTeachCount(msg.on ? Number(msg.count) || 0 : null);
        else if (msg.type === "teach_steps") taughtRef.current?.((msg.steps as TaughtStep[]) ?? []);
      };
      ws.onclose = () => {
        if (!closed) retry = setTimeout(open, 1500);
      };
    };
    open();
    return () => {
      closed = true;
      clearTimeout(retry);
      socket.current?.close();
    };
  }, []);

  const send = useCallback((msg: Record<string, unknown>) => {
    if (socket.current?.readyState === WebSocket.OPEN) socket.current.send(JSON.stringify(msg));
  }, []);
  const whenOpen = useCallback((msg: Record<string, unknown>) => {
    const ws = socket.current;
    if (!ws) return;
    if (ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(msg));
    else ws.addEventListener("open", () => ws.send(JSON.stringify(msg)), { once: true });
  }, []);
  return { frame, page, hold, error, idle, send, whenOpen, teachCount };
}

function hostOf(url: string): string {
  try {
    return url ? new URL(url).host : "";
  } catch {
    return url;
  }
}

/** In the status sheet: the live picture, and 打开浏览器 in its corner. */
export function BrowserPane({
  name,
  status,
  onOpen,
}: {
  name: string;
  status: ComputerStatus | null;
  onOpen: () => void;
}) {
  const { frame, page, error, idle } = useBrowserStream();
  const title = page.title || hostOf(page.url) || status?.page?.title || "浏览器";
  return (
    <div className="pb-2">
      {status?.handoff && (
        <div className="mb-3 rounded-2xl bg-om-accent-soft px-3.5 py-2.5 text-[14px]">
          <span className="font-semibold">{name}需要你来一下：</span>
          {status.handoff.reason}
        </div>
      )}
      <div className="overflow-hidden rounded-2xl bg-[#1e1e1e] text-[#f1efe8]">
        <div className="flex items-center gap-2 px-3 py-2 text-[13px]">
          <Globe size={15} className="shrink-0 opacity-80" />
          <span className="min-w-0 flex-1 truncate">{title}</span>
          {frame && (
            <span className="flex shrink-0 items-center gap-1 rounded-full bg-white/10 px-2 py-0.5 text-[10.5px] font-semibold text-rose-300">
              <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-rose-400" /> 实时
            </span>
          )}
        </div>
        <div className="relative bg-black">
          {frame ? (
            <img
              src={`data:image/jpeg;base64,${frame.data}`}
              alt={title}
              draggable={false}
              className="block w-full select-none"
              style={{ aspectRatio: `${frame.w} / ${frame.h}` }}
            />
          ) : (
            <div className="flex aspect-[16/10] items-center justify-center gap-2 text-[13px] text-white/60">
              {error ??
                (idle ? (
                  `浏览器已关闭，${name}要用时会自动打开`
                ) : (
                  <>
                    <Loader2 size={15} className="animate-spin" /> 正在连接浏览器…
                  </>
                ))}
            </div>
          )}
          <button
            type="button"
            onClick={onOpen}
            className="absolute bottom-2.5 right-2.5 flex items-center gap-1.5 rounded-full bg-black/60 px-3 py-1.5 text-[12.5px] font-semibold text-white backdrop-blur-md active:scale-95"
          >
            <Maximize2 size={13} /> 打开浏览器
          </button>
        </div>
      </div>
      <p className="mt-2 truncate text-[12.5px] text-om-muted">
        {status?.last_action
          ? `刚才：${actionLabel(status.last_action.verb)}${status.last_action.detail ? `「${status.last_action.detail}」` : ""}`
          : `${name}上网时会在这里，打开浏览器就能自己操作`}
      </p>
    </div>
  );
}

/** Taps shorter than this are clicks; longer drags scroll the page. */
const TAP_SLOP_PX = 8;

/**
 * The browser full screen: the user has it. A tap clicks the page; when the page then has a
 * field focused, a hidden input brings up the keyboard and every keystroke goes to the page.
 */
export function BrowserFullscreen({
  name,
  take = false,
  teach = false,
  onTaught,
  onClose,
}: {
  name: string;
  take?: boolean;
  /** Opened to teach it a task: take control and start writing the steps down. */
  teach?: boolean;
  /** 做完了: the steps the user did, to turn into a skill. */
  onTaught?: (steps: TaughtStep[]) => void;
  onClose: () => void;
}) {
  const keyboard = useRef<HTMLInputElement>(null);
  const composing = useRef(false);
  const [typing, setTyping] = useState(false);
  const onFocus = useCallback((editable: boolean) => {
    setTyping(editable);
    if (!editable) keyboard.current?.blur();
  }, []);
  // What happens to the steps when recording stops: kept (做完了) or dropped (不记了).
  const keepSteps = useRef(true);
  // 做完了 waits for the steps to arrive before closing (closing drops the connection they come on).
  const finishing = useRef(false);
  const { frame, page, hold, idle, send, whenOpen, teachCount } = useBrowserStream(onFocus, (steps) => {
    if (keepSteps.current && steps.length) onTaught?.(steps);
    if (finishing.current) {
      finishing.current = false;
      send({ type: "done" });
      onClose();
    }
  });
  const recording = teachCount !== null;
  const [editingUrl, setEditingUrl] = useState(false);
  const [url, setUrl] = useState("");
  const drag = useRef<{ x: number; y: number; lastY: number; moved: boolean } | null>(null);
  // The user has control only after 接管控制; until then the companion does, and this only watches.
  const controlling = hold === "user";
  const controllingRef = useRef(controlling);
  controllingRef.current = controlling;

  // 去接手 (the companion asked for the user), or 教它做: open already in control.
  useEffect(() => {
    if (take || teach) whenOpen({ type: "take" });
  }, [take, teach, whenOpen]);
  // 教它做: once the user has the page, start writing down what they do.
  const teachStarted = useRef(false);
  useEffect(() => {
    if (teach && controlling && !teachStarted.current) {
      teachStarted.current = true;
      send({ type: "teach_start" });
    }
  }, [teach, controlling, send]);
  const stopTeaching = (keep: boolean) => {
    keepSteps.current = keep;
    finishing.current = keep;
    send({ type: "teach_stop" });
  };
  const handBack = () => {
    send({ type: "done" });
    setTyping(false);
    keyboard.current?.blur();
  };
  // Leaving full screen while in control hands it back: the companion must not wait on nobody.
  const recordingRef = useRef(recording);
  recordingRef.current = recording;
  const leave = useCallback(() => {
    if (recordingRef.current) {
      keepSteps.current = false;
      send({ type: "teach_stop" });
    }
    if (controllingRef.current) send({ type: "done" });
    onClose();
  }, [onClose, send]);
  const closeBrowser = () => {
    send({ type: "close" });
    onClose();
  };
  useBackKey(leave, !typing);

  const point = (event: React.PointerEvent<HTMLImageElement>) => {
    const box = event.currentTarget.getBoundingClientRect();
    return { x: (event.clientX - box.left) / box.width, y: (event.clientY - box.top) / box.height, height: box.height };
  };
  const onPointerDown = (event: React.PointerEvent<HTMLImageElement>) => {
    if (!controlling) return;
    event.currentTarget.setPointerCapture(event.pointerId);
    drag.current = { x: event.clientX, y: event.clientY, lastY: event.clientY, moved: false };
  };
  const onPointerMove = (event: React.PointerEvent<HTMLImageElement>) => {
    const d = drag.current;
    if (!d) return;
    if (Math.hypot(event.clientX - d.x, event.clientY - d.y) > TAP_SLOP_PX) d.moved = true;
    if (d.moved) {
      const dy = d.lastY - event.clientY;
      if (Math.abs(dy) > 4) {
        send({ type: "scroll", dy: dy / point(event).height });
        d.lastY = event.clientY;
      }
    }
  };
  const onPointerUp = (event: React.PointerEvent<HTMLImageElement>) => {
    const d = drag.current;
    drag.current = null;
    if (!d || d.moved) return;
    const { x, y } = point(event);
    // Focus now, inside the tap: phones open the keyboard only from a user gesture. When the page
    // says the tap did not land on a field, the input lets go again.
    keyboard.current?.focus({ preventScroll: true });
    send({ type: "click", x, y });
  };

  const onInput = (event: FormEvent<HTMLInputElement>) => {
    if (composing.current) return;
    const value = event.currentTarget.value;
    if (value) send({ type: "text", text: value });
    event.currentTarget.value = "";
  };
  const go = (event: FormEvent) => {
    event.preventDefault();
    if (url.trim()) send({ type: "navigate", url: url.trim() });
    setEditingUrl(false);
  };

  const host = hostOf(page.url);
  return createPortal(
    <div className="companion">
      <div data-sheet className="fixed inset-0 z-[60] flex flex-col bg-black text-white" role="dialog" aria-modal="true" aria-label="浏览器">
        <div className="flex items-center gap-2 px-2 pb-2" style={{ paddingTop: "max(0.5rem, env(safe-area-inset-top))" }}>
          <button type="button" onClick={leave} aria-label="收起浏览器" className="shrink-0 rounded-full p-2 active:bg-white/15">
            <Minimize2 size={19} />
          </button>
          {editingUrl && controlling ? (
            <form onSubmit={go} className="min-w-0 flex-1">
              <input
                autoFocus
                value={url}
                onChange={(e) => setUrl(e.target.value)}
                onBlur={() => setEditingUrl(false)}
                inputMode="url"
                enterKeyHint="go"
                aria-label="网址"
                placeholder="输入网址"
                className="w-full rounded-full bg-white/15 px-3.5 py-2 text-[16px] text-white outline-none placeholder:text-white/40"
              />
            </form>
          ) : (
            <button
              type="button"
              disabled={!controlling}
              onClick={() => {
                setUrl(page.url);
                setEditingUrl(true);
              }}
              className="min-w-0 flex-1 truncate rounded-full bg-white/10 px-3.5 py-2 text-left text-[13.5px] text-white/85"
            >
              {host || page.title || (idle ? "浏览器已关闭" : controlling ? "输入网址" : "")}
            </button>
          )}
          {controlling && (
            <>
              <button type="button" aria-label="后退" onClick={() => send({ type: "back" })} className="shrink-0 rounded-full p-2 active:bg-white/15">
                <ArrowLeft size={19} />
              </button>
              <button type="button" aria-label="刷新" onClick={() => send({ type: "reload" })} className="shrink-0 rounded-full p-2 active:bg-white/15">
                <RotateCw size={18} />
              </button>
            </>
          )}
        </div>

        <div className="relative flex min-h-0 flex-1 items-center justify-center overflow-hidden">
          {frame ? (
            <img
              src={`data:image/jpeg;base64,${frame.data}`}
              alt={page.title || "浏览器画面"}
              draggable={false}
              onPointerDown={onPointerDown}
              onPointerMove={onPointerMove}
              onPointerUp={onPointerUp}
              onWheel={(event) => send({ type: "scroll", dy: event.deltaY / event.currentTarget.getBoundingClientRect().height })}
              className="max-h-full max-w-full touch-none select-none object-contain"
            />
          ) : idle ? (
            <div className="flex flex-col items-center gap-3 text-[13px] text-white/60">
              浏览器已关闭，{name}要用时会自动打开
              <button
                type="button"
                onClick={() => whenOpen({ type: "start" })}
                className="rounded-full bg-white/15 px-4 py-2 text-[14px] font-semibold text-white active:bg-white/25"
              >
                现在打开
              </button>
            </div>
          ) : (
            <div className="flex items-center gap-2 text-[13px] text-white/60">
              <Loader2 size={15} className="animate-spin" /> 正在连接浏览器…
            </div>
          )}
          {/* The keyboard's way into the page: focused on a tap, typed into, emptied after each key. */}
          <input
            ref={keyboard}
            aria-label="在网页里输入"
            autoCapitalize="off"
            autoCorrect="off"
            autoComplete="off"
            spellCheck={false}
            enterKeyHint="enter"
            onInput={onInput}
            onCompositionStart={() => (composing.current = true)}
            onCompositionEnd={(event) => {
              composing.current = false;
              const value = event.currentTarget.value;
              if (value) send({ type: "text", text: value });
              event.currentTarget.value = "";
            }}
            onKeyDown={(event) => {
              if (composing.current) return;
              if (event.key === "Enter" || event.key === "Backspace" || event.key === "Tab" || event.key.startsWith("Arrow")) {
                if (event.key !== "Backspace" || !event.currentTarget.value) {
                  event.preventDefault();
                  send({ type: "key", key: event.key });
                }
              }
            }}
            onBlur={() => setTyping(false)}
            className="pointer-events-none absolute bottom-0 left-0 h-px w-px opacity-0"
            style={{ fontSize: 16 }}
          />
        </div>

        <div className="flex flex-col gap-2 px-4 pt-2" style={{ paddingBottom: "max(0.75rem, env(safe-area-inset-bottom))" }}>
          {recording && (
            <p className="flex items-center justify-center gap-2 text-[13px] font-medium text-rose-300" role="status">
              <span className="h-2 w-2 animate-pulse rounded-full bg-rose-400" aria-hidden="true" />
              正在记下你的操作 · {teachCount} 步
            </p>
          )}
          <p className="text-center text-[12px] text-white/55">
            {recording
              ? `把这件事完整做一遍，${name}会照着学。输入的密码不会被记下`
              : controlling
              ? typing
                ? "直接打字，就会输进网页"
                : `现在由你操作：点网页就是点击，拖动翻页。${name}会等你`
              : hold === "handoff"
                ? `${name}在等你接手`
                : `现在由${name}操作，你在旁边看`}
          </p>
          {!idle && recording && (
            <div className="flex gap-2">
              <button
                type="button"
                onClick={() => stopTeaching(false)}
                className="flex h-11 flex-1 items-center justify-center rounded-full bg-white/10 text-[14.5px] font-medium active:bg-white/20"
              >
                不记了
              </button>
              <button
                type="button"
                onClick={() => stopTeaching(true)}
                className="flex h-11 flex-[1.4] items-center justify-center gap-1.5 rounded-full bg-om-accent text-[14.5px] font-semibold text-om-accent-fg active:opacity-80"
              >
                <Check size={16} /> 做完了
              </button>
            </div>
          )}
          {!idle && !recording && controlling && onTaught && (
            <button
              type="button"
              onClick={() => send({ type: "teach_start" })}
              className="flex h-10 items-center justify-center gap-1.5 rounded-full text-[13.5px] text-white/80 active:bg-white/10"
            >
              <GraduationCap size={15} /> 把接下来的操作教给{name}
            </button>
          )}
          {!idle && !recording && (
            <div className="flex gap-2">
              <button
                type="button"
                onClick={closeBrowser}
                className="flex h-11 flex-1 items-center justify-center gap-1.5 rounded-full bg-white/10 text-[14.5px] font-medium active:bg-white/20"
              >
                <Power size={16} /> 关闭浏览器
              </button>
              <button
                type="button"
                onClick={controlling ? handBack : () => send({ type: "take" })}
                className="flex h-11 flex-[1.4] items-center justify-center gap-1.5 rounded-full bg-om-accent text-[14.5px] font-semibold text-om-accent-fg active:opacity-80"
              >
                {controlling ? (
                  `交还给${name}`
                ) : (
                  <>
                    <Hand size={16} /> 接管控制
                  </>
                )}
              </button>
            </div>
          )}
        </div>
      </div>
    </div>,
    document.body,
  );
}

const VERBS: Record<string, string> = {
  navigate: "打开",
  click: "点击",
  type: "输入",
  fill_form: "填写表单",
  press_key: "按键",
  snapshot: "看了看页面",
  take_screenshot: "截图",
  navigate_back: "后退",
  select_option: "选择",
  hover: "指向",
  file_upload: "上传文件",
  tabs: "切换标签页",
  wait_for: "等待页面",
};

function actionLabel(verb: string): string {
  return VERBS[verb.replace(/^browser_/, "")] ?? "操作浏览器";
}
