import { useId } from "react";

/** Illustration palettes for the companion character (light → shade). */
export const MOCHI_VARIANTS = {
  cream: ["#fff7ec", "#e6cdb0"],
  peach: ["#ffe6d8", "#f2a184"],
  mint: ["#e4f8ee", "#93d3b5"],
  sky: ["#e5f0ff", "#93b8ef"],
  lilac: ["#f1e8ff", "#b79de9"],
  cocoa: ["#f2e2d5", "#b28465"],
} as const;

export type MochiVariant = keyof typeof MOCHI_VARIANTS;

export type AvatarValue = { type: "mochi"; variant: MochiVariant } | { type: "emoji"; value: string };

/** Avatars are stored as ``mochi:<variant>`` or an emoji; the old 🐱 default maps to the character. */
export function resolveAvatar(value: string | null | undefined): AvatarValue {
  const variant = value?.startsWith("mochi:") ? value.slice(6) : null;
  if (variant && variant in MOCHI_VARIANTS) return { type: "mochi", variant: variant as MochiVariant };
  if (!value || value === "🐱") return { type: "mochi", variant: "cream" };
  return { type: "emoji", value };
}

export function Mascot({ variant, size, busy = false }: { variant: MochiVariant; size: number; busy?: boolean }) {
  const id = useId();
  const [light, shade] = MOCHI_VARIANTS[variant];
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 64 64"
      aria-hidden="true"
      className={busy ? "c-mascot-busy" : undefined}
    >
      <defs>
        <radialGradient id={`${id}-body`} cx="38%" cy="30%" r="75%">
          <stop offset="0%" stopColor={light} />
          <stop offset="100%" stopColor={shade} />
        </radialGradient>
      </defs>
      <ellipse cx="32" cy="58" rx="16" ry="2.5" fill="#000" opacity="0.08" />
      <path
        d="M32 7c13 0 23 9.5 23 24.5C55 47 45 57 32 57S9 47 9 31.5C9 16.5 19 7 32 7Z"
        fill={`url(#${id}-body)`}
      />
      <ellipse cx="24" cy="18" rx="6" ry="3.5" fill="#fff" opacity="0.55" transform="rotate(-20 24 18)" />
      <ellipse className="c-mascot-eye" cx="25.5" cy="33" rx="2.4" ry="3.2" fill="#3a2d27" />
      <ellipse className="c-mascot-eye" cx="38.5" cy="33" rx="2.4" ry="3.2" fill="#3a2d27" />
      <ellipse cx="20" cy="40" rx="4" ry="2.3" fill="#ff8a8a" opacity="0.35" />
      <ellipse cx="44" cy="40" rx="4" ry="2.3" fill="#ff8a8a" opacity="0.35" />
      <path d="M29.5 39.5q2.5 2 5 0" stroke="#3a2d27" strokeWidth="1.4" strokeLinecap="round" fill="none" />
    </svg>
  );
}

export function Avatar({ value, size, busy = false }: { value: string | null | undefined; size: number; busy?: boolean }) {
  const avatar = resolveAvatar(value);
  if (avatar.type === "mochi") return <Mascot variant={avatar.variant} size={size} busy={busy} />;
  return (
    <span
      aria-hidden="true"
      className={`grid place-items-center rounded-full c-float ${busy ? "c-mascot-busy" : ""}`}
      style={{ width: size, height: size, fontSize: size * 0.55 }}
    >
      {avatar.value}
    </span>
  );
}
