/*
 * 设置 → 外观: light or dark (or following the phone), and how big the companion's face is drawn
 * (header, empty chat, while it works). Kept on the server with the companion (so a phone that
 * loses its web storage, or another device, still has them); this phone's storage is only the
 * copy that applies before the server answers.
 */
import { useSyncExternalStore } from "react";

export type ThemeMode = "system" | "light" | "dark";
export type PetSize = "small" | "medium" | "large";

const THEME_KEY = "nanobot-webui.theme"; // the app's own theme switch reads this (useTheme)
const SIZE_KEY = "companion.pet-size";
const CHANGED = "companion-appearance";

// 小 is how it first was; each step up is about a quarter bigger again.
const SCALE: Record<PetSize, number> = { small: 1, medium: 1.25, large: 1.6 };

function read(key: string): string | null {
  try {
    return localStorage.getItem(key);
  } catch {
    return null;
  }
}

function write(key: string, value: string | null): void {
  try {
    if (value === null) localStorage.removeItem(key);
    else localStorage.setItem(key, value);
  } catch {
    // Private mode: it still applies for now.
  }
  window.dispatchEvent(new Event(CHANGED));
}

function systemDark(): boolean {
  return typeof window !== "undefined" && !!window.matchMedia?.("(prefers-color-scheme: dark)").matches;
}

function apply(dark: boolean): void {
  document.documentElement.classList.toggle("dark", dark);
  const meta = document.querySelector<HTMLMetaElement>('meta[name="theme-color"]');
  const color = dark ? meta?.dataset.themeColorDark : meta?.dataset.themeColorLight;
  if (meta && color) meta.content = color;
}

export function themeMode(): ThemeMode {
  const stored = read(THEME_KEY);
  return stored === "light" || stored === "dark" ? stored : "system";
}

/** Saves a change on the server; set once the app is connected (``useAppearanceSync``). */
let save: ((changes: { theme_mode?: ThemeMode; pet_size?: PetSize }) => void) | null = null;

export function setAppearanceSaver(saver: typeof save): void {
  save = saver;
}

function setThemeLocally(mode: ThemeMode): void {
  write(THEME_KEY, mode === "system" ? null : mode);
  apply(mode === "system" ? systemDark() : mode === "dark");
}

export function setThemeMode(mode: ThemeMode): void {
  setThemeLocally(mode);
  save?.({ theme_mode: mode });
}

/** While on 跟随系统, switch along with the phone (at sunset, say). Returns the unsubscribe. */
export function followSystemTheme(): () => void {
  const query = window.matchMedia?.("(prefers-color-scheme: dark)");
  if (!query) return () => undefined;
  const onChange = () => {
    if (themeMode() === "system") apply(query.matches);
  };
  query.addEventListener("change", onChange);
  return () => query.removeEventListener("change", onChange);
}

export function petSize(): PetSize {
  const stored = read(SIZE_KEY);
  return stored === "small" || stored === "large" ? stored : "medium";
}

export function setPetSize(size: PetSize): void {
  write(SIZE_KEY, size === "medium" ? null : size);
  save?.({ pet_size: size });
}

/**
 * What the server keeps, on opening the app. A choice made on this phone before the server kept
 * them (the server still has the defaults) goes up instead of being lost.
 */
export function adoptAppearance(server: { theme_mode?: ThemeMode; pet_size?: PetSize }): void {
  const local = { theme_mode: themeMode(), pet_size: petSize() };
  const serverMode = server.theme_mode ?? "system";
  const serverSize = server.pet_size ?? "medium";
  const changes: { theme_mode?: ThemeMode; pet_size?: PetSize } = {};
  if (serverMode === "system" && local.theme_mode !== "system") changes.theme_mode = local.theme_mode;
  else setThemeLocally(serverMode);
  if (serverSize === "medium" && local.pet_size !== "medium") changes.pet_size = local.pet_size;
  else write(SIZE_KEY, serverSize === "medium" ? null : serverSize);
  if (Object.keys(changes).length) save?.(changes);
}

function subscribe(callback: () => void): () => void {
  window.addEventListener(CHANGED, callback);
  window.addEventListener("storage", callback);
  return () => {
    window.removeEventListener(CHANGED, callback);
    window.removeEventListener("storage", callback);
  };
}

export function useThemeMode(): ThemeMode {
  return useSyncExternalStore(subscribe, themeMode, () => "system");
}

export function usePetSize(): PetSize {
  return useSyncExternalStore(subscribe, petSize, () => "medium");
}

/** A size in pixels for the companion's face, scaled to 外观 → 形象大小. */
export function usePetScale(): (px: number) => number {
  return scaleFor(usePetSize());
}

export function scaleFor(size: PetSize): (px: number) => number {
  return (px) => Math.round(px * SCALE[size]);
}
