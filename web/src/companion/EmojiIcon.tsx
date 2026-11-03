import { iconFor } from "./icons";

/** An emoji as the app draws it: its drawing when there is one (see icons.ts), else the emoji. */
export function EmojiIcon({ emoji, size }: { emoji: string; size: number }) {
  const src = iconFor(emoji);
  if (src) return <img src={src} alt="" draggable={false} width={size} height={size} className="block shrink-0 object-contain" />;
  return (
    <span className="leading-none" style={{ fontSize: size * 0.9 }}>
      {emoji}
    </span>
  );
}
