import { useEffect, useState } from "react";

import { MOCHI_VARIANTS } from "./Mascot";
import { lastLook, NO_LOOK, usePetSource, type PetSource } from "./Pet";
import { accentOfImage, DEFAULT_ACCENT, themeCss } from "./theme";

const STYLE_ID = "companion-theme";
const CACHE_KEY = "companion.accent";
// One cell of a pet sprite sheet: the first frame is the pet at rest.
const SPRITE_CELL = { w: 192, h: 208 };

/** An accent known without looking at pixels; ``null`` means sample the picture. */
function knownAccent(source: PetSource): string | null {
  if (source.type === "sprite") return source.accent ?? null;
  if (source.type === "mochi") return MOCHI_VARIANTS[source.variant][1];
  if (source.type === "emoji") return DEFAULT_ACCENT;
  return null;
}

function cachedAccent(): string {
  try {
    return localStorage.getItem(CACHE_KEY) || DEFAULT_ACCENT;
  } catch {
    return DEFAULT_ACCENT;
  }
}

/**
 * Colors the whole app after the companion's look: the orange cat gives warm porcelain, a
 * mochi its own shade, an uploaded picture or imported pet its dominant color. The palette
 * is injected as one stylesheet so sheets rendered in portals pick it up too.
 */
export function CompanionTheme({ avatar }: { avatar: string | null }) {
  // Before the companion's settings arrive its look isn't known: the look this phone remembers,
  // else nothing (not the default cat, whose orange flashed up and overwrote the kept color).
  const source = usePetSource(avatar ?? lastLook() ?? NO_LOOK);
  const [accent, setAccent] = useState<string>(cachedAccent);
  const url = source.type === "image" ? source.url : source.type === "sprite" ? source.sheet : null;
  const known = knownAccent(source);

  useEffect(() => {
    if (known) {
      setAccent(known);
      return;
    }
    if (!url) return;
    let cancelled = false;
    void accentOfImage(url, source.type === "sprite" ? SPRITE_CELL : undefined).then((found) => {
      if (!cancelled) setAccent(found ?? DEFAULT_ACCENT);
    });
    return () => {
      cancelled = true;
    };
  }, [known, url, source.type]);

  useEffect(() => {
    let style = document.getElementById(STYLE_ID) as HTMLStyleElement | null;
    if (!style) {
      style = document.createElement("style");
      style.id = STYLE_ID;
      document.head.appendChild(style);
    }
    style.textContent = themeCss(accent);
    try {
      localStorage.setItem(CACHE_KEY, accent);
    } catch {
      // Private mode: the next load starts from the default palette.
    }
  }, [accent]);

  return null;
}
