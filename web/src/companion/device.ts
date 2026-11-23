/**
 * This device's connection to the companion's server (see the server's ``devices.py``).
 *
 * Paired once with a code, the device keeps a long-lived device token and signs in with it on
 * every start; the short-lived session tokens are renewed with it in the background. The token
 * lives in this page's storage and, in the Android app, also in the app's own preferences (which
 * survive a cleared web cache). It goes only when the user disconnects this device.
 */
import { fetchWithTimeout } from "@/lib/http";

import { androidShell } from "./android-shell";

const STORAGE_KEY = "companion.device";

export function loadDeviceToken(): string {
  const fromApp = androidShell()?.deviceToken?.() ?? "";
  if (fromApp) return fromApp;
  try {
    return window.localStorage.getItem(STORAGE_KEY) ?? "";
  } catch {
    return "";
  }
}

export function saveDeviceToken(token: string): void {
  try {
    window.localStorage.setItem(STORAGE_KEY, token);
  } catch {
    // Storage blocked (private browsing): the Android copy, if any, still holds it.
  }
  androidShell()?.saveDevice?.(token);
}

export function clearDeviceToken(): void {
  try {
    window.localStorage.removeItem(STORAGE_KEY);
  } catch {
    // Nothing stored.
  }
  androidShell()?.saveDevice?.("");
}

/** A pairing code passed in the link (``…/companion.html#pair=ABCD-2345``), taken out of the URL. */
export function consumePairCode(): string {
  const rawHash = window.location.hash;
  if (!rawHash) return "";
  const body = rawHash.startsWith("#") ? rawHash.slice(1) : rawHash;
  const parts = body.split("&").filter(Boolean);
  let rawCode: string | null = null;
  const kept: string[] = [];
  for (const part of parts) {
    const eq = part.indexOf("=");
    const key = eq >= 0 ? part.slice(0, eq) : part;
    if (key === "pair" && rawCode === null) {
      rawCode = eq >= 0 ? part.slice(eq + 1) : "";
    } else {
      kept.push(part);
    }
  }
  if (rawCode === null) return "";
  const nextHash = kept.length > 0 ? `#${kept.join("&")}` : "";
  try {
    window.history.replaceState(null, "", `${window.location.pathname}${window.location.search}${nextHash}`);
  } catch {
    // History replacement can fail in restricted frames; continue with the extracted code.
  }
  try {
    return decodeURIComponent(rawCode);
  } catch {
    return rawCode;
  }
}

/** A name for the device list: "iPhone", "Android 手机", "Windows 电脑"… */
export function deviceName(): string {
  const agent = navigator.userAgent;
  if (/iPhone/.test(agent)) return "iPhone";
  if (/iPad/.test(agent)) return "iPad";
  if (/PersonalBotAndroid/.test(agent)) return "安卓 App";
  if (/Android/.test(agent)) return "安卓手机";
  if (/Macintosh/.test(agent)) return "Mac";
  if (/Windows/.test(agent)) return "Windows 电脑";
  return "浏览器";
}

export class PairError extends Error {
  constructor(public readonly reason: "wrong_code" | "locked" | "offline") {
    super(reason);
  }
}

/** Trade a pairing code for this device's token. */
export async function pairDevice(code: string): Promise<{ device_id: string; token: string }> {
  let res: Response;
  try {
    res = await fetchWithTimeout(
      "/api/pair",
      { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ code, name: deviceName() }) },
      15_000,
    );
  } catch {
    throw new PairError("offline");
  }
  if (res.ok) return (await res.json()) as { device_id: string; token: string };
  if (res.status >= 500 || res.status === 0) throw new PairError("offline");
  const body = (await res.json().catch(() => ({}))) as { error?: string };
  if (res.status === 429 || body.error === "locked") throw new PairError("locked");
  throw new PairError("wrong_code");
}

export interface PairedDevice {
  id: string;
  name: string;
  paired_at: number;
  last_seen: number;
}

export async function fetchDevices(token: string): Promise<PairedDevice[]> {
  const res = await fetchWithTimeout(
    "/api/webui/devices",
    { headers: { Authorization: `Bearer ${token}` }, credentials: "same-origin" },
    15_000,
  );
  if (!res.ok) throw new Error(`devices failed: HTTP ${res.status}`);
  return ((await res.json()) as { devices: PairedDevice[] }).devices;
}

/** A code another device can pair with (this one is already connected). */
export async function newPairCode(token: string): Promise<{ code: string; expires_at: number; link: string }> {
  const res = await fetchWithTimeout(
    "/api/pair/code",
    { method: "POST", headers: { Authorization: `Bearer ${token}` }, credentials: "same-origin" },
    15_000,
  );
  if (!res.ok) throw new Error(`pair code failed: HTTP ${res.status}`);
  return (await res.json()) as { code: string; expires_at: number; link: string };
}
