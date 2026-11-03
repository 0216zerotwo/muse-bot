/*
 * A helper's or a group's chat, dressed as a chat app's conversation (WeChat, iMessage): a slim bar
 * on top with ‹ back, the name and what it is doing, and ··· for its profile; faces beside the
 * messages; no tab bar while inside. The list of these conversations is the 帮手 tab.
 */
import { ChevronLeft, MoreHorizontal } from "lucide-react";
import type { ReactNode, Ref } from "react";

import type { Speaker } from "../ChatThread";
import { useBackKey } from "../Screen";
import type { UIMessage } from "@/lib/types";

/** What the chat screen needs to show a conversation chat-app style. */
export interface ChatApp {
  title: string;
  /** Under the name at rest (its job, or the members); while it works, what it is doing. */
  subtitle: string;
  /** Who said a reply: their face, and in a group their name. */
  speaker: (message: UIMessage) => Speaker;
  /** The user's own face, beside their messages. */
  me: ReactNode;
  /** Who hands over work from another chat (the companion), shown as its message. */
  relay?: Speaker;
  /** The face shown while a reply is coming. */
  workingFace: ReactNode;
  /** Shown in an empty conversation: its face, large. */
  bigFace: ReactNode;
  /** An empty conversation's line under the name. */
  hint: string;
  /** Things to ask it, tapped to send, while the conversation is empty. */
  starters?: string[];
  onBack: () => void;
  onProfile: () => void;
}

export function MessengerBar({
  headerRef,
  title,
  subtitle,
  busy,
  onBack,
  onProfile,
}: {
  headerRef?: Ref<HTMLElement>;
  title: string;
  subtitle: string;
  busy: boolean;
  onBack: () => void;
  onProfile: () => void;
}) {
  // The back key, or a swipe in from the left edge, leaves the conversation.
  useBackKey(onBack);
  return (
    <header
      ref={headerRef}
      className="absolute inset-x-0 top-0 z-20 border-b"
      style={{
        paddingTop: "env(safe-area-inset-top)",
        background: "color-mix(in srgb, var(--c-bg) 92%, transparent)",
        borderColor: "var(--c-border)",
        backdropFilter: "blur(14px)",
        WebkitBackdropFilter: "blur(14px)",
      }}
    >
      <div className="mx-auto flex h-14 max-w-2xl items-center px-1">
        <button type="button" onClick={onBack} aria-label="返回帮手列表" className="c-pressable grid h-11 w-11 place-items-center rounded-full">
          <ChevronLeft className="h-6 w-6" />
        </button>
        <button type="button" onClick={onProfile} className="min-w-0 flex-1 px-2 text-center" aria-label={`${title}的资料`}>
          <span className="block truncate text-[16.5px] font-semibold leading-tight">{title}</span>
          <span className="block truncate text-[12px] leading-tight" style={{ color: busy ? "var(--c-primary)" : "var(--c-text-muted)" }}>
            {subtitle}
          </span>
        </button>
        <button type="button" onClick={onProfile} aria-label="资料与设置" className="c-pressable grid h-11 w-11 place-items-center rounded-full">
          <MoreHorizontal className="h-6 w-6" />
        </button>
      </div>
    </header>
  );
}

/** The user's face in a conversation: "我" on the theme color. */
export function MeFace({ size = 36 }: { size?: number }) {
  return (
    <span
      className="c-primary grid shrink-0 place-items-center rounded-full font-semibold"
      style={{ width: size, height: size, fontSize: size * 0.4 }}
      aria-hidden="true"
    >
      我
    </span>
  );
}

/** An empty conversation: its face, its name and a line to start with. */
export function MessengerEmpty({ face, title, hint, starters, onSend }: {
  face: ReactNode; title: string; hint: string; starters?: string[]; onSend?: (text: string) => void;
}) {
  return (
    <div className="c-rise flex min-h-full flex-col items-center justify-center px-8 pb-8 text-center">
      {face}
      <h2 className="mt-3 text-[19px] font-semibold">{title}</h2>
      <p className="c-muted mt-1 max-w-xs text-[14px] leading-relaxed">{hint}</p>
      {onSend && starters && starters.length > 0 && (
        <div className="mt-5 flex w-full max-w-xs flex-col gap-2" aria-label="可以这样问">
          {starters.map((text) => (
            <button
              key={text}
              type="button"
              onClick={() => onSend(text)}
              className="c-pressable c-float rounded-2xl px-4 py-2.5 text-left text-[14.5px]"
            >
              {text}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
