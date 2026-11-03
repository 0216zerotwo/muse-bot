/*
 * Ported from nanoMuse (vendor/nanomuse-web/web/src/components/MuseHeader.tsx, GPL-3.0-or-later):
 * Muse's page header — the face centred, the name in a small tag hanging off the chin with what
 * the companion is doing as its second line, and a round button in each top corner. The face is
 * our pet; the colors are the companion's (the om-* tokens alias its theme).
 */
import type { ReactNode, Ref } from "react";

import { cn } from "@/lib/utils";

export function MuseHeader({
  face,
  name,
  statusLine,
  statusTone = "muted",
  spinning = false,
  badge,
  onAvatar,
  leading,
  trailing,
  under,
  scrim = false,
  className,
  headerRef,
  faceBadge,
}: {
  /** A control on the face's corner (the pen). */
  faceBadge?: ReactNode;
  /** Measured by the screen: content scrolls under the header. */
  headerRef?: Ref<HTMLElement>;
  /** The companion's face, already sized. */
  face: ReactNode;
  name: string;
  statusLine?: string | null;
  /** The second line's color: muted at rest, accent while working or waiting. */
  statusTone?: "muted" | "accent";
  /** A small spinner before the status line. */
  spinning?: boolean;
  /** A count on the face's shoulder (approvals waiting). */
  badge?: number;
  onAvatar?: () => void;
  leading?: ReactNode;
  trailing?: ReactNode;
  /** Under the tag (the goal banner on the chat). */
  under?: ReactNode;
  /** A shade over a list that scrolls under the header (the chat), instead of the canvas color. */
  scrim?: boolean;
  className?: string;
}) {
  return (
    <header
      ref={headerRef}
      className={cn("relative shrink-0 pb-1", scrim ? "c-fade-top pointer-events-none" : "bg-om-bg", className)}
      style={{ paddingTop: "max(0.375rem, env(safe-area-inset-top))" }}
    >
      <div className="flex flex-col items-center px-16">
        <div className="relative">
          <button type="button" onClick={onAvatar} aria-label={`${name}${statusLine ? `，${statusLine}` : ""}`} className="pointer-events-auto relative rounded-full">
            {face}
            {badge !== undefined && badge > 0 && (
              <span className="pointer-events-none absolute -right-0.5 top-0 flex h-[18px] min-w-[18px] items-center justify-center rounded-full border-2 border-om-bg bg-om-danger px-1 text-[10.5px] font-bold text-white">
                {badge}
              </span>
            )}
          </button>
          {/* Beside the face, not inside its button: the pen (改名字 / 换形象). */}
          {faceBadge}
        </div>
        {/* The tag rides up over the chin; its area is fixed so the bar keeps its height. */}
        <div className="relative z-10 -mt-2.5 flex min-h-[44px] flex-col items-center">
          <MuseNamePill name={name} statusLine={statusLine} statusTone={statusTone} spinning={spinning} onClick={onAvatar} />
          {under && <div className="pointer-events-auto mt-1.5 flex w-full max-w-md items-center">{under}</div>}
        </div>
      </div>
      {leading && (
        <div className="pointer-events-auto absolute left-4" style={{ top: "max(0.375rem, env(safe-area-inset-top))" }}>
          {leading}
        </div>
      )}
      {trailing && (
        <div className="pointer-events-auto absolute right-4 flex items-center gap-2" style={{ top: "max(0.375rem, env(safe-area-inset-top))" }}>
          {trailing}
        </div>
      )}
    </header>
  );
}

/** Muse's name tag: the name in regular weight, the status as a smaller line inside the same tag. */
export function MuseNamePill({
  name,
  statusLine,
  statusTone = "muted",
  spinning = false,
  onClick,
}: {
  name: string;
  statusLine?: string | null;
  statusTone?: "muted" | "accent";
  spinning?: boolean;
  onClick?: () => void;
}) {
  const inner = (
    <>
      <span className="max-w-[240px] truncate text-[14px] leading-[18px] text-om-fg">{name}</span>
      {statusLine && (
        <span
          className={cn(
            "mt-0.5 flex max-w-[240px] items-center gap-1 text-[11.5px] leading-[14px]",
            statusTone === "accent" ? "text-om-accent" : "text-om-muted",
          )}
          aria-live="polite"
        >
          {spinning && <span className="h-2.5 w-2.5 shrink-0 animate-spin rounded-full border-[1.5px] border-current border-t-transparent" />}
          <span className="truncate">{statusLine}</span>
        </span>
      )}
    </>
  );
  const cls =
    "pointer-events-auto flex flex-col items-center rounded-[14px] bg-om-surface px-3 py-1 shadow-[0_1px_4px_rgba(0,0,0,0.16)] dark:border dark:border-om-border dark:shadow-none";
  return onClick ? (
    <button type="button" onClick={onClick} className={cls} tabIndex={-1} aria-hidden="true">
      {inner}
    </button>
  ) : (
    <div className={cls}>{inner}</div>
  );
}

/** Muse's corner button: a disc with one glyph, lifted by a soft shadow (a hairline in the dark). */
export function MuseRoundButton({
  onClick,
  label,
  children,
  dot = false,
  className,
  expanded,
}: {
  onClick: () => void;
  label: string;
  children: ReactNode;
  /** A small accent dot at the top right (something is waiting behind it). */
  dot?: boolean;
  className?: string;
  /** For a button that opens a menu. */
  expanded?: boolean;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-label={label}
      aria-haspopup={expanded === undefined ? undefined : "menu"}
      aria-expanded={expanded}
      className={cn(
        "relative flex h-11 w-11 items-center justify-center rounded-full bg-om-surface text-om-fg shadow-[0_1px_4px_rgba(0,0,0,0.14)] active:bg-om-surface-2 dark:border dark:border-om-border dark:shadow-none",
        className,
      )}
    >
      {children}
      {dot && <span className="absolute right-[3px] top-[3px] h-[9px] w-[9px] rounded-full border-[1.5px] border-om-surface bg-om-accent" />}
    </button>
  );
}

/** Muse's big left-aligned page title (目标), the same size as the other tabs' titles. */
export function MusePageTitle({ children, className }: { children: ReactNode; className?: string }) {
  return <h1 className={cn("px-5 pb-1.5 pt-1.5 text-[32px] font-bold leading-tight", className)}>{children}</h1>;
}
