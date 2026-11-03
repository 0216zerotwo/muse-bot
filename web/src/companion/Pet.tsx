import { createContext, useContext, useEffect, useReducer, useRef, useState, type ComponentType, type ReactNode } from "react";
import { BookOpen, Laptop, Moon, Palette, PenLine, Search } from "lucide-react";

import { useClient } from "@/providers/ClientProvider";

import { fetchAvatars, type CustomAvatar } from "./companion-api";
import { Mascot, MOCHI_VARIANTS, type MochiVariant } from "./Mascot";
import {
  ACTION_ANIMATIONS,
  ACTION_FALLBACK,
  ATLAS_COLUMNS,
  CELL_HEIGHT,
  CELL_WIDTH,
  isAction,
  ROWS,
  rowDuration,
  type ActionAnimation,
  type PetAnimation,
} from "./petAnimations";

/** Pets that ship with the app, served from ``public/pets/<id>/``; ``accent`` is the color the whole app takes on while it is the companion. */
export const BUILTIN_PETS: Record<string, { name: string; sheet: string; actions: string | null; accent: string }> = {
  "orange-cat": {
    name: "橘猫",
    sheet: "/pets/orange-cat/spritesheet.webp",
    actions: "/pets/orange-cat/actions.webp",
    accent: "#E07A35",
  },
};

/** Must match ``DEFAULT_COMPANION_AVATAR`` in the server's companion_state.py. */
export const DEFAULT_AVATAR = "pet:orange-cat";

/**
 * What an avatar value shows. Values are ``pet:<built-in id>``, ``custom:<id>``
 * (an imported Codex pet), ``image:<id>`` (an uploaded picture), the classic
 * ``mochi:<variant>`` drawings, or an emoji.
 */
export type PetSource =
  | { type: "sprite"; name: string; sheet: string; actions: string | null; accent?: string }
  | {
      type: "image";
      name: string;
      url: string;
      character?: boolean;
      working?: string | null;
      happy?: string | null;
      motions?: CustomAvatar["motions"];
    }
  | { type: "mochi"; variant: MochiVariant }
  | { type: "emoji"; value: string }
  /** Its own look isn't known yet (still loading): an empty space its size, never another look. */
  | { type: "blank" };

/** ``PetView`` value for "nothing to show yet" (the connecting screen before any look is known). */
export const NO_LOOK = "none";

const LOOK_KEY = "companion.look";

/** The look it wore last time, as this phone remembers it: shown at once on opening, before the
 * server answers (its picture links are signed, so they load without signing in). */
function cachedLook(): CustomAvatar | null {
  try {
    const raw = localStorage.getItem(LOOK_KEY);
    const item = raw ? (JSON.parse(raw) as CustomAvatar) : null;
    return item && typeof item.id === "string" ? item : null;
  } catch {
    return null;
  }
}

function rememberLook(item: CustomAvatar): void {
  try {
    localStorage.setItem(LOOK_KEY, JSON.stringify(item));
  } catch {
    // Only a head start for next time.
  }
}

/** The ``PetView`` value of the look remembered on this phone, if any. */
export function lastLook(): string | null {
  const item = cachedLook();
  return item ? `image:${item.id}` : null;
}

export function resolvePet(value: string | null | undefined, library: CustomAvatar[] | null): PetSource {
  const [kind, id] = value?.includes(":") ? [value.slice(0, value.indexOf(":")), value.slice(value.indexOf(":") + 1)] : ["", ""];
  if (value === NO_LOOK) return { type: "blank" };
  if (!value || value === "🐱") return resolvePet(DEFAULT_AVATAR, library);
  if (kind === "pet" && BUILTIN_PETS[id]) return { type: "sprite", ...BUILTIN_PETS[id] };
  if (kind === "mochi" && id in MOCHI_VARIANTS) return { type: "mochi", variant: id as MochiVariant };
  if (kind === "custom" || kind === "image") {
    // Before the list arrives, the look remembered on this phone stands in (it is usually this one).
    const remembered = library === null ? cachedLook() : null;
    const custom = library?.find((item) => item.id === id) ?? (remembered?.id === id ? remembered : undefined);
    if (custom?.kind === "pet" && custom.sheet_url) {
      return { type: "sprite", name: custom.name, sheet: custom.sheet_url, actions: custom.actions_url };
    }
    if (custom?.kind === "image" && custom.image_url) {
      return {
        type: "image", name: custom.name, url: custom.image_url, character: custom.character,
        working: custom.working_url, happy: custom.happy_url, motions: custom.motions,
      };
    }
    // Not loaded yet: nothing rather than another look (the default cat flashed up on opening).
    // Gone from the list (deleted): the default.
    return library === null ? { type: "blank" } : resolvePet(DEFAULT_AVATAR, library);
  }
  if (kind === "pet" || kind === "mochi") return resolvePet(DEFAULT_AVATAR, library);
  return { type: "emoji", value };
}

export interface PetLibrary {
  /** The user's own pictures and pets; null until loaded. */
  items: CustomAvatar[] | null;
  /** Replace the list after an upload or delete (the server returns the new list). */
  update: (items: CustomAvatar[]) => void;
}

export const PetLibraryContext = createContext<PetLibrary>({ items: null, update: () => {} });

/**
 * Loads the user's own avatars for every pet on screen: once, and again when the companion wears
 * one not loaded yet (``avatar``: it just drew itself a new look).
 */
export function PetLibraryProvider({ avatar = null, children }: { avatar?: string | null; children: ReactNode }) {
  const { token } = useClient();
  const [items, setItems] = useState<CustomAvatar[] | null>(null);
  const wornId = /^(?:image|custom):(\w+)$/.exec(avatar ?? "")?.[1] ?? null;
  const missing = wornId !== null && items !== null && !items.some((item) => item.id === wornId);
  // Its motion clips are still being made: look again now and then, so it starts moving by itself.
  const pending = !!items?.find((item) => item.id === wornId)?.motion_pending;
  useEffect(() => {
    if (!pending) return;
    const timer = window.setInterval(() => {
      fetchAvatars(token).then((payload) => setItems(payload.items)).catch(() => undefined);
    }, 20_000);
    return () => window.clearInterval(timer);
  }, [pending, token]);
  useEffect(() => {
    let cancelled = false;
    fetchAvatars(token)
      .then((payload) => !cancelled && setItems(payload.items))
      .catch(() => !cancelled && setItems((current) => current ?? []));
    return () => {
      cancelled = true;
    };
  }, [token, missing ? wornId : null]); // eslint-disable-line react-hooks/exhaustive-deps
  // Remember the look it wears, for next time it opens.
  const worn = items?.find((item) => item.id === wornId);
  useEffect(() => {
    if (worn) rememberLook(worn);
  }, [worn]);
  return <PetLibraryContext.Provider value={{ items, update: setItems }}>{children}</PetLibraryContext.Provider>;
}

export function usePetSource(value: string | null | undefined): PetSource {
  return resolvePet(value, useContext(PetLibraryContext).items);
}

/** The companion's current look, for decorative pets on other screens. */
export const CompanionAvatarContext = createContext<string | null>(null);

/** The companion itself, in an empty state or while something loads. */
export function CompanionPet({ size, animation = "idle" }: { size: number; animation?: PetAnimation }) {
  return <PetView value={useContext(CompanionAvatarContext)} animation={animation} size={size} />;
}

const BADGES: Record<ActionAnimation, ComponentType<{ className?: string }>> = {
  typing: Laptop,
  reading: BookOpen,
  drawing: Palette,
  writing: PenLine,
  sleeping: Moon,
  searching: Search,
};

/** Motion for pictures without frames, by what it is doing. */
const MOTION: Record<PetAnimation, string> = {
  idle: "c-pet-breathe",
  waiting: "c-pet-breathe",
  "running-right": "c-pet-bob",
  "running-left": "c-pet-bob",
  waving: "c-pet-wiggle",
  jumping: "c-pet-hop",
  failed: "c-pet-droop",
  running: "c-pet-bob",
  review: "c-pet-sway",
  typing: "c-pet-jitter",
  reading: "c-pet-sway",
  drawing: "c-pet-bob",
  writing: "c-pet-bob",
  sleeping: "c-pet-sleep",
  searching: "c-pet-sway",
};

const BUSY: ReadonlySet<PetAnimation> = new Set(["running", "running-left", "running-right", "review", "typing", "reading", "drawing", "writing", "searching"]);

/** The motion clip for what it is doing (as Muse plays one per state), and the still picture of it. */
function moodPicture(source: Extract<PetSource, { type: "image" }>, animation: PetAnimation): { clip?: string; still: string } {
  const clips = source.motions ?? {};
  const clip = animation === "waiting" ? clips.waiting : animation === "review" ? clips.thinking ?? clips.working
    : BUSY.has(animation) ? clips.working : animation === "idle" ? clips.idle : undefined;
  const still = BUSY.has(animation) && source.working ? source.working
    : (animation === "jumping" || animation === "waving") && source.happy ? source.happy : source.url;
  return { clip, still };
}

/**
 * A drawn character in its mood. A clip (a couple of megabytes) shows once it has downloaded —
 * until then the still of that mood, so the face never blinks out on a slow connection; once
 * one clip is showing, the others load in the background, ready for when its state changes.
 */
function CharacterPicture({ source, animation, size, height }: {
  source: Extract<PetSource, { type: "image" }>; animation: PetAnimation; size: number; height: number;
}) {
  const { clip, still } = moodPicture(source, animation);
  const [, loaded] = useReducer((n: number) => n + 1, 0);
  useEffect(() => {
    if (!clip) return;
    let live = true;
    void loadSheet(clip).then((ok) => {
      if (!live || !ok) return;
      loaded();
      for (const other of Object.values(source.motions ?? {})) if (other) void loadSheet(other);
    });
    return () => {
      live = false;
    };
  }, [clip, source.motions]);
  const shown = clip && readySheets.has(clip) ? clip : still;
  return <img src={shown} alt="" draggable={false} className="h-full w-full object-contain" style={{ width: size, height }} />;
}

function prefersReducedMotion(): boolean {
  return typeof window !== "undefined" && !!window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
}

/* Sheets load once per URL; a failed sheet makes its pet fall back. */
const sheetStatus = new Map<string, Promise<boolean>>();
/** Sheets already decoded, so a new pet on screen shows its first frame right away. */
const readySheets = new Set<string>();

function loadSheet(url: string): Promise<boolean> {
  let status = sheetStatus.get(url);
  if (!status) {
    status = new Promise((resolve) => {
      const image = new Image();
      image.onload = () => {
        readySheets.add(url);
        resolve(true);
      };
      image.onerror = () => resolve(false);
      image.src = url;
    });
    sheetStatus.set(url, status);
  }
  return status;
}

function useSheet(url: string | null): "loading" | "ready" | "failed" {
  const [state, setState] = useState<"loading" | "ready" | "failed">(
    url ? (readySheets.has(url) ? "ready" : "loading") : "failed",
  );
  useEffect(() => {
    if (!url) {
      setState("failed");
      return;
    }
    let cancelled = false;
    setState(readySheets.has(url) ? "ready" : "loading");
    void loadSheet(url).then((ok) => !cancelled && setState(ok ? "ready" : "failed"));
    return () => {
      cancelled = true;
    };
  }, [url]);
  return state;
}

/**
 * Steps through one row of a sprite atlas. Loops, or plays once and calls
 * ``onDone`` (holding the last frame). Pauses while the page is hidden.
 */
function SpriteFrames({
  url,
  rows,
  animation,
  width,
  loop,
  onDone,
}: {
  url: string;
  rows: number;
  animation: PetAnimation;
  width: number;
  loop: boolean;
  onDone?: () => void;
}) {
  const { row, durations } = ROWS[animation];
  const [frame, setFrame] = useState(0);
  const done = useRef(onDone);
  done.current = onDone;
  const reduced = prefersReducedMotion();

  useEffect(() => {
    setFrame(0);
    if (reduced) {
      // Still report the end of a one-off reaction so whoever waits for it moves on.
      if (!loop) {
        const timer = window.setTimeout(() => done.current?.(), rowDuration(animation));
        return () => window.clearTimeout(timer);
      }
      return;
    }
    let current = 0;
    let timer = 0;
    let finished = false;
    const schedule = () => {
      timer = window.setTimeout(() => {
        if (current === durations.length - 1) {
          if (!loop) {
            finished = true;
            done.current?.();
            return;
          }
          current = 0;
        } else {
          current += 1;
        }
        setFrame(current);
        schedule();
      }, durations[current]);
    };
    const onVisibility = () => {
      window.clearTimeout(timer);
      if (document.visibilityState === "visible" && !finished) schedule();
    };
    schedule();
    document.addEventListener("visibilitychange", onVisibility);
    return () => {
      window.clearTimeout(timer);
      document.removeEventListener("visibilitychange", onVisibility);
    };
  }, [animation, durations, loop, reduced]);

  const height = Math.round((width * CELL_HEIGHT) / CELL_WIDTH);
  // Reduced motion shows the first idle frame, which Codex pets keep calm on purpose.
  const shownRow = reduced ? ROWS.idle.row : row;
  const shownFrame = reduced ? 0 : frame;
  return (
    <span
      aria-hidden="true"
      className="block"
      style={{
        width,
        height,
        backgroundImage: `url("${url}")`,
        backgroundRepeat: "no-repeat",
        backgroundSize: `${width * ATLAS_COLUMNS}px ${height * rows}px`,
        backgroundPosition: `${-shownFrame * width}px ${-shownRow * height}px`,
        imageRendering: width >= CELL_WIDTH ? "pixelated" : "auto",
      }}
    />
  );
}

function Badge({ animation, size }: { animation: ActionAnimation; size: number }) {
  const Icon = BADGES[animation];
  const badge = Math.max(18, Math.round(size * 0.34));
  return (
    <span
      aria-hidden="true"
      className="c-float absolute bottom-0 right-0 grid place-items-center rounded-full"
      style={{ width: badge, height: badge }}
    >
      <Icon className="h-[60%] w-[60%]" />
    </span>
  );
}

/**
 * The companion, alive: a sprite pet playing ``animation`` (or the closest
 * thing it can), a picture or classic drawing moving to match, plus a badge
 * when the pet cannot act out an errand itself.
 */
export function PetView({
  value,
  animation = "idle",
  size,
  loop = true,
  onDone,
  label,
}: {
  value: string | null | undefined;
  animation?: PetAnimation;
  /** Width in CSS pixels; sprites are a little taller than wide. */
  size: number;
  loop?: boolean;
  onDone?: () => void;
  /** Accessible name; decorative when omitted. */
  label?: string;
}) {
  const source = usePetSource(value);
  return <PetSourceView source={source} animation={animation} size={size} loop={loop} onDone={onDone} label={label} />;
}

export function PetSourceView({
  source,
  animation = "idle",
  size,
  loop = true,
  onDone,
  label,
}: {
  source: PetSource;
  animation?: PetAnimation;
  size: number;
  loop?: boolean;
  onDone?: () => void;
  label?: string;
}) {
  const base = useSheet(source.type === "sprite" ? source.sheet : null);
  // Loaded up front, so the first errand plays right away instead of a stand-in pose.
  const actions = useSheet(source.type === "sprite" ? source.actions : null);
  const done = useRef(onDone);
  done.current = onDone;
  const spriteReady = source.type === "sprite" && base === "ready";
  const actionPlayable = isAction(animation) && actions === "ready";
  const frameless = source.type !== "sprite" || base === "failed";

  // Pictures and drawings have no frames: end one-off reactions on a timer
  // (a sprite still loading reports the end itself once it plays).
  useEffect(() => {
    if (!frameless || loop) return;
    const timer = window.setTimeout(() => done.current?.(), rowDuration(animation));
    return () => window.clearTimeout(timer);
  }, [animation, loop, frameless]);

  const height = Math.round((size * CELL_HEIGHT) / CELL_WIDTH);
  const showBadge = isAction(animation) && !(spriteReady && actionPlayable);
  const role = label ? { role: "img", "aria-label": label } : { "aria-hidden": true };

  let body: ReactNode;
  if (source.type === "blank") {
    body = <span className="block" style={{ width: size, height }} />;
  } else if (source.type === "sprite" && base !== "failed") {
    if (base === "loading") {
      body = <span className="block" style={{ width: size, height }} />;
    } else if (actionPlayable) {
      body = (
        <SpriteFrames url={source.actions as string} rows={ACTION_ANIMATIONS.length} animation={animation} width={size} loop={loop} onDone={onDone} />
      );
    } else {
      const shown = isAction(animation) ? ACTION_FALLBACK[animation] : animation;
      body = <SpriteFrames url={source.sheet} rows={9} animation={shown} width={size} loop={loop} onDone={onDone} />;
    }
  } else {
    // A look with its own motion clips moves by itself; the stills get a gentle sway.
    const clips = source.type === "image" && Object.keys(source.motions ?? {}).length > 0;
    const motion = prefersReducedMotion() || clips ? "" : MOTION[animation];
    body = (
      <span className={`grid place-items-center ${motion}`} style={{ width: size, height }}>
        {source.type === "image" && source.character ? (
          // A drawn character, whole: its busy picture while it works, its happy one when pleased.
          <CharacterPicture source={source} animation={animation} size={size} height={height} />
        ) : source.type === "image" ? (
          <img src={source.url} alt="" draggable={false} className="h-full w-full rounded-full object-cover" style={{ width: size, height: size }} />
        ) : source.type === "emoji" ? (
          <span className="c-float grid place-items-center rounded-full" style={{ width: size, height: size, fontSize: size * 0.55 }}>
            {source.value}
          </span>
        ) : (
          <Mascot variant={source.type === "mochi" ? source.variant : "cream"} size={size} />
        )}
      </span>
    );
  }

  return (
    <span className="relative inline-block shrink-0" style={{ width: size, height }} {...role}>
      {body}
      {animation === "sleeping" && <span className="c-pet-zz absolute -top-1 right-0 text-[12px] font-semibold" aria-hidden="true">z<sup>z</sup></span>}
      {showBadge && animation !== "sleeping" && <Badge animation={animation as ActionAnimation} size={size} />}
    </span>
  );
}

/** Time for one stroll across the stage. */
const WALK_MS = 1400;

/** Centre, right, centre, left: where it strolls to in turn. */
function strollOffsets(size: number): number[] {
  const stride = Math.round(size * 0.45);
  return [0, stride, 0, -stride];
}

/**
 * The pet on its own, larger: waves hello, idles, strolls a few steps now and
 * then, and jumps when tapped.
 */
export function PetStage({ value, size }: { value: string | null | undefined; size: number }) {
  const [reaction, setReaction] = useState<PetAnimation | null>("waving");
  const [spot, setSpot] = useState(0);
  const [walking, setWalking] = useState<"right" | "left" | null>(null);
  const offsets = strollOffsets(size);

  useEffect(() => {
    if (reaction || walking || prefersReducedMotion()) return;
    const timer = window.setTimeout(() => {
      const around = strollOffsets(size);
      const next = (spot + 1) % around.length;
      setWalking(around[next] > around[spot] ? "right" : "left");
      setSpot(next);
    }, 9_000 + Math.random() * 6_000);
    return () => window.clearTimeout(timer);
  }, [reaction, walking, spot, size]);

  useEffect(() => {
    if (!walking) return;
    const timer = window.setTimeout(() => setWalking(null), WALK_MS);
    return () => window.clearTimeout(timer);
  }, [walking]);

  const animation: PetAnimation = reaction ?? (walking ? (walking === "right" ? "running-right" : "running-left") : "idle");
  return (
    <button
      type="button"
      aria-label="戳它一下"
      onClick={() => setReaction("jumping")}
      className="c-pressable rounded-3xl"
      style={{ transform: `translateX(${offsets[spot]}px)`, transition: `transform ${WALK_MS}ms linear` }}
    >
      <PetView value={value} animation={animation} loop={!reaction} onDone={() => setReaction(null)} size={size} />
    </button>
  );
}
