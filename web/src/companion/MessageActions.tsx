/*
 * Long-press a message (or right-click it) for what to do with it: 引用 (answer it, quoted above the
 * box), 复制, 删除. As in a chat app: a small bar floats by the message until a choice or a tap
 * elsewhere.
 */
import { Copy, Quote, RotateCcw, Trash2, Volume2 } from "lucide-react";
import { useEffect, useRef, useState, type ReactNode } from "react";

import { androidShell } from "./android-shell";
import { spokenText } from "./fileKinds";

export type MessageAction = "quote" | "copy" | "delete" | "speak" | "regenerate";

const HOLD_MS = 450;

export function Pressable({
  children,
  align,
  onAction,
  extra = [],
}: {
  children: ReactNode;
  /** Which side the message sits on (the bar lines up with it). */
  align: "start" | "end";
  /** A reply's own: 朗读, and 重新生成 on the newest one. */
  extra?: MessageAction[];
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
    onAction(action);
  };

  return (
    <div
      ref={box}
      tabIndex={0}
      className="flex flex-col gap-1.5 rounded-2xl outline-none focus-visible:ring-2 focus-visible:ring-[var(--c-primary)]"
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
      {open && (
        <div
          className={`c-float c-rise flex items-center gap-0.5 rounded-full p-1 ${align === "end" ? "self-end" : "self-start"}`}
          role="menu"
          aria-label="这条消息"
        >
          <ActionButton icon={<Quote className="h-4 w-4" />} label="引用" onClick={() => choose("quote")} />
          <ActionButton icon={<Copy className="h-4 w-4" />} label="复制" onClick={() => choose("copy")} />
          {extra.includes("speak") && <ActionButton icon={<Volume2 className="h-4 w-4" />} label="朗读" onClick={() => choose("speak")} />}
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
      className={`c-pressable flex min-h-9 items-center gap-1.5 rounded-full px-3 text-[13.5px] ${danger ? "c-danger" : ""}`}
    >
      {icon}
      {label}
    </button>
  );
}
