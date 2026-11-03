/**
 * The app's colors follow the companion's look: one accent color (the orange cat's amber, a
 * mochi's shade, or the dominant color of an uploaded picture) becomes a full light and dark
 * palette — tinted neutrals, ink bubbles, an accent for buttons — computed in OKLCH so every
 * accent gets the same lightness steps, with button text checked for contrast.
 */

type Oklch = { l: number; c: number; h: number };
type Rgb = [number, number, number];

export type ThemeTokens = Record<string, string>;

/** The orange cat's amber: the default look (方案 A 暖瓷). */
export const DEFAULT_ACCENT = "#E07A35";

/* ---------- color math ---------- */

function toLinear(v: number): number {
  return v <= 0.04045 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4;
}

function fromLinear(v: number): number {
  return v <= 0.0031308 ? 12.92 * v : 1.055 * v ** (1 / 2.4) - 0.055;
}

export function hexToRgb(hex: string): Rgb {
  const value = hex.replace("#", "");
  const full = value.length === 3 ? value.split("").map((ch) => ch + ch).join("") : value;
  return [0, 2, 4].map((i) => parseInt(full.slice(i, i + 2), 16) / 255) as Rgb;
}

function rgbToHex(rgb: Rgb): string {
  return `#${rgb.map((v) => Math.round(Math.min(1, Math.max(0, v)) * 255).toString(16).padStart(2, "0")).join("")}`;
}

function rgbToOklch([r, g, b]: Rgb): Oklch {
  const [lr, lg, lb] = [toLinear(r), toLinear(g), toLinear(b)];
  const l = Math.cbrt(0.4122214708 * lr + 0.5363325363 * lg + 0.0514459929 * lb);
  const m = Math.cbrt(0.2119034982 * lr + 0.6806995451 * lg + 0.1073969566 * lb);
  const s = Math.cbrt(0.0883024619 * lr + 0.2817188376 * lg + 0.6299787005 * lb);
  const L = 0.2104542553 * l + 0.793617785 * m - 0.0040720468 * s;
  const A = 1.9779984951 * l - 2.428592205 * m + 0.4505937099 * s;
  const B = 0.0259040371 * l + 0.7827717662 * m - 0.808675766 * s;
  return { l: L, c: Math.hypot(A, B), h: ((Math.atan2(B, A) * 180) / Math.PI + 360) % 360 };
}

function oklchToRgbRaw({ l, c, h }: Oklch): Rgb {
  const a = c * Math.cos((h * Math.PI) / 180);
  const b = c * Math.sin((h * Math.PI) / 180);
  const l_ = (l + 0.3963377774 * a + 0.2158037573 * b) ** 3;
  const m_ = (l - 0.1055613458 * a - 0.0638541728 * b) ** 3;
  const s_ = (l - 0.0894841775 * a - 1.291485548 * b) ** 3;
  return [
    fromLinear(4.0767416621 * l_ - 3.3077115913 * m_ + 0.2309699292 * s_),
    fromLinear(-1.2684380046 * l_ + 2.6097574011 * m_ - 0.3413193965 * s_),
    fromLinear(-0.0041960863 * l_ - 0.7034186147 * m_ + 1.707614701 * s_),
  ];
}

/** An sRGB color for an OKLCH one, lowering chroma until it fits the gamut. */
function oklch(l: number, c: number, h: number): string {
  let chroma = c;
  for (let i = 0; i < 24; i += 1) {
    const rgb = oklchToRgbRaw({ l, c: chroma, h });
    if (rgb.every((v) => v >= -0.001 && v <= 1.001)) return rgbToHex(rgb);
    chroma *= 0.85;
  }
  return rgbToHex(oklchToRgbRaw({ l, c: 0, h }));
}

function luminance(hex: string): number {
  const [r, g, b] = hexToRgb(hex).map(toLinear);
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}

export function contrast(a: string, b: string): number {
  const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x);
  return (hi + 0.05) / (lo + 0.05);
}

function rgba(hex: string, alpha: number): string {
  const [r, g, b] = hexToRgb(hex).map((v) => Math.round(v * 255));
  return `rgb(${r} ${g} ${b} / ${alpha})`;
}

/** Walk lightness from ``start`` in ``step`` until ``text`` on the color reaches ``min`` contrast. */
function readableFill(start: number, c: number, h: number, text: string, step: number, min = 4.5): string {
  let l = start;
  let fill = oklch(l, c, h);
  for (let i = 0; i < 40 && contrast(fill, text) < min; i += 1) {
    l = Math.min(0.98, Math.max(0.05, l + step));
    fill = oklch(l, c, h);
  }
  return fill;
}

/* ---------- palettes ---------- */

/** A soft light from the top left and the bottom right, for the glass to refract. */
function wash(top: string, bottom: string): string {
  return `radial-gradient(120% 60% at 15% 0%, ${top}, transparent 60%), radial-gradient(90% 50% at 100% 100%, ${bottom}, transparent 65%)`;
}

export function themeFromAccent(accent: string): { light: ThemeTokens; dark: ThemeTokens } {
  const { c, h } = rgbToOklch(hexToRgb(accent));
  const tint = (k: number, max: number) => Math.min(c * k, max);

  const ink = oklch(0.215, tint(0.1, 0.012), h);
  const lightBg = oklch(0.968, tint(0.06, 0.0075), h + 12);
  const light: ThemeTokens = {
    "--c-bg": lightBg,
    "--c-surface": oklch(0.997, tint(0.03, 0.004), h),
    "--c-surface-pressed": oklch(0.945, tint(0.1, 0.011), h + 12),
    "--c-glass": rgba(oklch(0.995, tint(0.04, 0.006), h), 0.6),
    "--c-glass-soft": rgba(oklch(0.997, tint(0.03, 0.004), h), 0.68),
    "--c-text": ink,
    "--c-text-muted": oklch(0.5, tint(0.15, 0.024), h),
    "--c-border": rgba(ink, 0.085),
    "--c-hairline": rgba(ink, 0.06),
    "--c-bubble-agent": oklch(0.997, tint(0.03, 0.004), h),
    "--c-bubble-user": oklch(0.285, tint(0.12, 0.02), h),
    "--c-bubble-user-text": oklch(0.975, tint(0.05, 0.008), h),
    "--c-primary": readableFill(0.62, Math.min(c, 0.16), h, "#ffffff", -0.02),
    "--c-primary-text": "#ffffff",
    "--c-accent": oklch(0.7, Math.min(c, 0.17), h),
    "--c-wash": wash(rgba(oklch(0.7, Math.min(c, 0.17), h), 0.1), rgba(oklch(0.7, Math.min(c, 0.17), h), 0.07)),
    "--c-accent-soft": oklch(0.93, tint(0.3, 0.045), h),
    /* Secondary buttons, icon chips, inset tracks. */
    "--c-fill": oklch(0.935, tint(0.12, 0.013), h + 12),
    "--c-tab-selected": oklch(0.93, tint(0.12, 0.013), h + 12),
    "--c-segment": oklch(0.935, tint(0.12, 0.012), h + 12),
    "--c-segment-on": oklch(0.997, tint(0.03, 0.004), h),
    "--c-danger": "#b42318",
    "--c-danger-fill": "#c0392b",
    "--c-online": "#4f8a5b",
    "--c-shadow": `0 1px 1px ${rgba(ink, 0.04)}, 0 3px 8px ${rgba(ink, 0.05)}, 0 14px 32px ${rgba(ink, 0.07)}`,
    "--c-scrim": rgba(ink, 0.36),
  };

  const paper = oklch(0.95, tint(0.06, 0.008), h);
  const darkPrimaryText = oklch(0.2, tint(0.1, 0.012), h);
  const dark: ThemeTokens = {
    "--c-bg": oklch(0.165, tint(0.08, 0.01), h),
    "--c-surface": oklch(0.215, tint(0.08, 0.011), h),
    "--c-surface-pressed": oklch(0.265, tint(0.1, 0.013), h),
    "--c-glass": rgba(oklch(0.22, tint(0.08, 0.011), h), 0.58),
    "--c-glass-soft": rgba(oklch(0.235, tint(0.08, 0.011), h), 0.66),
    "--c-text": paper,
    "--c-text-muted": oklch(0.74, tint(0.12, 0.02), h),
    "--c-border": rgba(paper, 0.1),
    "--c-hairline": rgba(paper, 0.07),
    "--c-bubble-agent": oklch(0.235, tint(0.08, 0.011), h),
    "--c-bubble-user": readableFill(0.42, tint(0.55, 0.09), h, oklch(0.97, tint(0.05, 0.008), h), -0.02),
    "--c-bubble-user-text": oklch(0.97, tint(0.05, 0.008), h),
    "--c-primary": readableFill(0.72, Math.min(c, 0.15), h, darkPrimaryText, 0.02),
    "--c-primary-text": darkPrimaryText,
    "--c-accent": oklch(0.76, Math.min(c, 0.15), h),
    "--c-wash": wash(rgba(oklch(0.76, Math.min(c, 0.15), h), 0.1), rgba(oklch(0.76, Math.min(c, 0.15), h), 0.06)),
    "--c-accent-soft": oklch(0.3, tint(0.4, 0.05), h),
    "--c-fill": oklch(0.275, tint(0.1, 0.014), h),
    "--c-tab-selected": oklch(0.28, tint(0.1, 0.014), h),
    "--c-segment": oklch(0.14, tint(0.08, 0.01), h),
    "--c-segment-on": oklch(0.28, tint(0.1, 0.014), h),
    "--c-danger": "#ff8a7a",
    "--c-danger-fill": "#d93636",
    "--c-online": "#7fbf8a",
    "--c-shadow": "0 1px 1px rgb(0 0 0 / 0.3), 0 4px 10px rgb(0 0 0 / 0.28), 0 16px 36px rgb(0 0 0 / 0.32)",
    "--c-scrim": "rgb(0 0 0 / 0.55)",
  };
  return { light, dark };
}

export function themeCss(accent: string): string {
  const { light, dark } = themeFromAccent(accent);
  const block = (tokens: ThemeTokens) => Object.entries(tokens).map(([k, v]) => `${k}: ${v};`).join(" ");
  return `.companion { ${block(light)} }\n.dark .companion { ${block(dark)} }`;
}

/* ---------- the accent of a picture ---------- */

/**
 * The dominant vivid color of a picture (an uploaded avatar or a pet's first frame): pixels
 * are bucketed by hue, weighted by chroma, ignoring near-white, near-black and transparent ones.
 */
export async function accentOfImage(url: string, crop?: { w: number; h: number }): Promise<string | null> {
  const image = new Image();
  image.crossOrigin = "anonymous";
  image.src = url;
  try {
    await image.decode();
  } catch {
    return null;
  }
  const size = 48;
  const canvas = document.createElement("canvas");
  canvas.width = size;
  canvas.height = size;
  const context = canvas.getContext("2d", { willReadFrequently: true });
  if (!context) return null;
  const sw = crop?.w ?? image.naturalWidth;
  const sh = crop?.h ?? image.naturalHeight;
  context.drawImage(image, 0, 0, sw, sh, 0, 0, size, size);
  let data: Uint8ClampedArray;
  try {
    data = context.getImageData(0, 0, size, size).data;
  } catch {
    return null;
  }
  const buckets = new Map<number, { weight: number; l: number; c: number; x: number; y: number }>();
  for (let i = 0; i < data.length; i += 4) {
    if (data[i + 3] < 200) continue;
    const color = rgbToOklch([data[i] / 255, data[i + 1] / 255, data[i + 2] / 255]);
    if (color.c < 0.04 || color.l < 0.25 || color.l > 0.95) continue;
    const key = Math.round(color.h / 15) % 24;
    const bucket = buckets.get(key) ?? { weight: 0, l: 0, c: 0, x: 0, y: 0 };
    bucket.weight += color.c;
    bucket.l += color.l * color.c;
    bucket.c += color.c * color.c;
    bucket.x += Math.cos((color.h * Math.PI) / 180) * color.c;
    bucket.y += Math.sin((color.h * Math.PI) / 180) * color.c;
    buckets.set(key, bucket);
  }
  const best = [...buckets.values()].sort((a, b) => b.weight - a.weight)[0];
  if (!best || best.weight < 0.5) return null;
  const hue = ((Math.atan2(best.y, best.x) * 180) / Math.PI + 360) % 360;
  return oklch(best.l / best.weight, best.c / best.weight, hue);
}
