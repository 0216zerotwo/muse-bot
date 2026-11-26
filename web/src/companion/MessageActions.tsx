/*
 * Long-press a message (or right-click it) for what to do with it: 引用 (answer it, quoted above the
 * box), 复制, 删除. As in a chat app: a small bar floats by the message until a choice or a tap
 * elsewhere.
 */
import { Copy, GitBranch, Loader2, Pencil, Quote, RotateCcw, Square, Trash2, Volume2 } from "lucide-react";
import { useEffect, useRef, useState, type ReactNode } from "react";

import { androidShell } from "./android-shell";
import { spokenText } from "./fileKinds";
import { dayLabel } from "./presentation";

export type MessageAction = "quote" | "copy" | "delete" | "speak" | "regenerate" | "edit" | "fork";

const HOLD_MS = 450;

export function Pressable({
  children,
  align,
  onAction,
  extra = [],
  timestamp,
  speaking = false,
  speechLoading = false,
}: {
  children: ReactNode;
  /** Which side the message sits on (the bar lines up with it). */
  align: "start" | "end";
  /** Optional contextual actions: edit, fork, speak, regenerate. */
  extra?: MessageAction[];
  /** Epoch ms when the message was sent, shown softly in the floating bar. */
  timestamp?: number;
  /** True while neural or fallback TTS is actively speaking this message. */
  speaking?: boolean;
  /** True while neural TTS audio for this message is loading. */
  speechLoading?: boolean;
  onAction?: (action: MessageAction) => void;
}) {
  const [open, setOpen] = useState(false);
  const press = useRef<{ x: number; y: number; timer: number } | null>(null);
  const box = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const away = (event: PointerEvent) => {
      if (!box.current?.contains(event.target as Node)) setOpen(false);
    };
    const onKey = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      event.preventDefault();
      event.stopPropagation();
      setOpen(false);
    };
    document.addEventListener("pointerdown", away);
    window.addEventListener("keydown", onKey, true);
    return () => {
      document.removeEventListener("pointerdown", away);
      window.removeEventListener("keydown", onKey, true);
    };
  }, [open]);

  if (!onAction) return <>{children}</>;
  const cancel = () => {
    if (press.current) window.clearTimeout(press.current.timer);
    press.current = null;
  };
  const choose = (action: MessageAction) => {
    setOpen(false);
    if (action === "copy" || action === "fork" || action === "edit") {
      androidShell()?.haptic?.("light");
    }
    onAction(action);
  };

  const speakLabel = speechLoading ? "合成中…" : speaking ? "停止朗读" : "朗读";
  const speakIcon = speechLoading ? (
    <Loader2 className="h-4 w-4 animate-spin motion-reduce:animate-none" />
  ) : speaking ? (
    <Square className="h-3.5 w-3.5 fill-current" />
  ) : (
    <Volume2 className="h-4 w-4" />
  );

  return (
    <div
      ref={box}
      tabIndex={0}
      className="group/msg flex flex-col gap-1.5 rounded-2xl outline-none focus-visible:ring-2 focus-visible:ring-[var(--c-primary)]"
      onKeyDown={(event) => {
        if (event.key === "ContextMenu" || (event.shiftKey && event.key === "F10")) {
          event.preventDefault();
          setOpen(true);
        }
      }}
      onPointerDown={(event) => {
        if (event.button !== 0) return;
        press.current = {
          x: event.clientX,
          y: event.clientY,
          timer: window.setTimeout(() => {
            press.current = null;
            navigator.vibrate?.(10);
            setOpen(true);
          }, HOLD_MS),
        };
      }}
      onPointerMove={(event) => {
        const p = press.current;
        if (p && Math.hypot(event.clientX - p.x, event.clientY - p.y) > 8) cancel(); // scrolling, not holding
      }}
      onPointerUp={cancel}
      onPointerCancel={cancel}
      onContextMenu={(event) => {
        event.preventDefault();
        setOpen(true);
      }}
      style={{ WebkitTouchCallout: "none", WebkitUserSelect: open ? "none" : undefined }}
    >
      {children}
      {!open && (
        <div
          role="toolbar"
          aria-label="快捷操作"
          className={`c-float pointer-events-none hidden items-center gap-0.5 rounded-full px-1 py-0.5 opacity-0 transition-opacity duration-150 motion-reduce:transition-none [@media(hover:hover)]:flex [@media(hover:hover)]:group-hover/msg:pointer-events-auto [@media(hover:hover)]:group-hover/msg:opacity-100 [@media(hover:hover)]:group-focus-within/msg:pointer-events-auto [@media(hover:hover)]:group-focus-within/msg:opacity-100 ${
            align === "end" ? "self-end" : "self-start"
          }`}
        >
          <ToolbarButton icon={<Quote className="h-3.5 w-3.5" />} label="引用" onClick={() => choose("quote")} />
          {extra.includes("edit") && (
            <ToolbarButton icon={<Pencil className="h-3.5 w-3.5" />} label="编辑并重发" onClick={() => choose("edit")} />
          )}
          {extra.includes("fork") && (
            <ToolbarButton icon={<GitBranch className="h-3.5 w-3.5" />} label="从这里分叉旁聊" onClick={() => choose("fork")} />
          )}
          <ToolbarButton icon={<Copy className="h-3.5 w-3.5" />} label="复制" onClick={() => choose("copy")} />
          {extra.includes("speak") && (
            <ToolbarButton icon={speakIcon} label={speakLabel} onClick={() => choose("speak")} />
          )}
          {extra.includes("regenerate") && (
            <ToolbarButton icon={<RotateCcw className="h-3.5 w-3.5" />} label="重新生成" onClick={() => choose("regenerate")} />
          )}
          <ToolbarButton icon={<Trash2 className="h-3.5 w-3.5" />} label="删除" danger onClick={() => choose("delete")} />
        </div>
      )}
      {open && (
        <div
          className={`c-float c-rise flex flex-wrap items-center gap-0.5 rounded-full p-1 ${align === "end" ? "self-end" : "self-start"}`}
          role="menu"
          aria-label="这条消息"
        >
          {timestamp !== undefined && timestamp > 0 && (
            <time
              dateTime={new Date(timestamp).toISOString()}
              data-testid="message-timestamp"
              className="c-muted mr-0.5 select-none border-r px-2.5 py-1 text-[12px] tabular-nums"
              style={{ borderColor: "var(--c-hairline)" }}
            >
              {dayLabel(timestamp)}
            </time>
          )}
          <ActionButton icon={<Quote className="h-4 w-4" />} label="引用" onClick={() => choose("quote")} />
          {extra.includes("edit") && (
            <ActionButton icon={<Pencil className="h-4 w-4" />} label="编辑并重发" onClick={() => choose("edit")} />
          )}
          {extra.includes("fork") && (
            <ActionButton icon={<GitBranch className="h-4 w-4" />} label="从这里分叉旁聊" onClick={() => choose("fork")} />
          )}
          <ActionButton icon={<Copy className="h-4 w-4" />} label="复制" onClick={() => choose("copy")} />
          {extra.includes("speak") && <ActionButton icon={speakIcon} label={speakLabel} onClick={() => choose("speak")} />}
          {extra.includes("regenerate") && (
            <ActionButton icon={<RotateCcw className="h-4 w-4" />} label="重新生成" onClick={() => choose("regenerate")} />
          )}
          <ActionButton icon={<Trash2 className="h-4 w-4" />} label="删除" danger onClick={() => choose("delete")} />
        </div>
      )}
    </div>
  );
}

const READ_AFTER_VOICE = "companion.read-after-voice";

/** 设置 → 语音输入后朗读回复 (the Android app, as on iPhone): on unless turned off. */
export function readsAfterVoice(): boolean {
  try {
    return localStorage.getItem(READ_AFTER_VOICE) !== "off";
  } catch {
    return true;
  }
}

export function setReadsAfterVoice(on: boolean): void {
  try {
    localStorage.setItem(READ_AFTER_VOICE, on ? "on" : "off");
  } catch {
    // Private mode: it stays on for this visit.
  }
}

/** 朗读 with the browser's own voice; 朗读 again while it reads stops it. */
export function speakAloud(reply: string): void {
  const android = androidShell();
  if (android?.speaking?.()) return android.stopSpeaking?.();
  const synth = typeof window === "undefined" ? undefined : window.speechSynthesis;
  if (!android?.speak && synth?.speaking) {
    synth.cancel();
    return;
  }
  const words = spokenText(reply);
  if (!words) return;
  const lang = /[\u4e00-\u9fff]/.test(words) ? "zh-CN" : "en-US";
  // The Android app's WebView has no speechSynthesis: its own voice reads it.
  if (android?.speak) return android.speak(words, lang);
  if (!synth) return;
  const utterance = new SpeechSynthesisUtterance(words);
  utterance.lang = lang;
  synth.speak(utterance);
}

function ActionButton({ icon, label, danger, onClick }: { icon: ReactNode; label: string; danger?: boolean; onClick: () => void }) {
  return (
    <button
      type="button"
      role="menuitem"
      onClick={onClick}
      className={`c-pressable flex min-h-9 items-center gap-1.5 rounded-full px-3 text-[13.5px] transition-transform active:scale-95 motion-reduce:transition-none ${danger ? "c-danger" : ""}`}
    >
      {icon}
      {label}
    </button>
  );
}

function ToolbarButton({ icon, label, danger, onClick }: { icon: ReactNode; label: string; danger?: boolean; onClick: () => void }) {
  return (
    <button
      type="button"
      aria-label={label}
      title={label}
      onClick={onClick}
      className={`c-pressable grid h-7 w-7 place-items-center rounded-full transition-transform active:scale-90 motion-reduce:transition-none ${danger ? "c-danger" : ""}`}
    >
      {icon}
    </button>
  );
}
