import { Component, useCallback, useEffect, useRef, useState, type FormEvent, type ReactNode } from "react";

import { useAppViewport } from "@/hooks/useAppViewport";
import { BootstrapAuthRequiredError, deriveWsUrl, fetchBootstrap } from "@/lib/bootstrap";
import { NanobotClient } from "@/lib/nanobot-client";
import { activateReloadCache } from "@/lib/reload-cache";
import type { WebUIIngressLimits } from "@/lib/types";
import { ClientProvider } from "@/providers/ClientProvider";

import { androidShell } from "./android-shell";
import { CompanionShell } from "./CompanionShell";
import { setCurrentDevice } from "./DevicesPanel";
import { clearDeviceToken, consumePairCode, loadDeviceToken, pairDevice, PairError, saveDeviceToken } from "./device";
import { lastLook, NO_LOOK, PetView } from "./Pet";

type BootState =
  | { status: "loading" }
  /** Not connected to this server yet (or this device was removed): pair with a code. */
  | { status: "pair"; code: string; removed: boolean }
  | { status: "error"; message: string }
  | {
      status: "ready";
      client: NanobotClient;
      token: string;
      deviceId: string | null;
      expiresAt: number | null;
      modelName: string | null;
      ingressLimits: WebUIIngressLimits | null;
    };

const MIN_REFRESH_DELAY_MS = 5_000;
const REFRESH_LEAD_MS = 60_000;
const MAX_REFRESH_RETRY_MS = 60_000;
/** Hidden longer than this, a socket that still looks open is replaced on return (see reconnectNow). */
const STALE_SOCKET_MS = 15_000;

function isAuthError(error: unknown): boolean {
  if (error instanceof BootstrapAuthRequiredError) return true;
  const message = error instanceof Error ? error.message : String(error);
  return message.includes("HTTP 401") || message.includes("HTTP 403");
}

function expiresAtFrom(expiresIn: number | undefined): number | null {
  return expiresIn ? Date.now() + Math.max(0, expiresIn) * 1000 : null;
}

class CompanionErrorBoundary extends Component<
  { children: ReactNode },
  { error: Error | null }
> {
  state: { error: Error | null } = { error: null };

  static getDerivedStateFromError(error: Error) {
    return { error };
  }

  render() {
    if (!this.state.error) return this.props.children;
    return (
      <CenteredMessage text={`页面出了一点小问题：${this.state.error.message || "未知错误"}`}>
        <button
          type="button"
          className="c-primary mt-5 min-h-12 rounded-full px-8 text-[16px] font-semibold"
          onClick={() => this.setState({ error: null })}
        >
          重试
        </button>
        {androidShell() && (
          <button
            type="button"
            className="c-muted mt-3 min-h-11 text-[14px]"
            onClick={() => androidShell()?.changeServer()}
          >
            更换服务器地址
          </button>
        )}
      </CenteredMessage>
    );
  }
}

/**
 * Connects to the companion's server. The first time, the device pairs with a code (see
 * ``device.ts``); after that it signs in with its device token on its own, also when session
 * tokens lapse or the server restarts, until the user disconnects this device.
 */
export function CompanionApp() {
  return (
    <CompanionErrorBoundary>
      <CompanionAppInner />
    </CompanionErrorBoundary>
  );
}

function CompanionAppInner() {
  useAppViewport();
  const [state, setState] = useState<BootState>({ status: "loading" });
  const linkCode = useRef<string | null>(null);
  if (linkCode.current === null) {
    linkCode.current = consumePairCode();
  }
  const refreshInFlightRef = useRef<{ client: NanobotClient; promise: Promise<string> } | null>(null);

  /** Sign-in with this device's token; the device token itself is gone when the server no longer knows it. */
  const signIn = useCallback(async () => {
    const device = loadDeviceToken();
    try {
      return await fetchBootstrap("", "", undefined, device);
    } catch (e) {
      if (isAuthError(e) && device) clearDeviceToken();
      throw Object.assign(e instanceof Error ? e : new Error(String(e)), { hadDevice: !!device });
    }
  }, []);

  const toPairing = useCallback((removed: boolean) => {
    setState({ status: "pair", code: linkCode.current ?? "", removed });
    linkCode.current = "";
  }, []);

  /** Re-run bootstrap for a live client: new socket URL, upload token, and API token, same client. */
  const refresh = useCallback(
    (client: NanobotClient): Promise<string> => {
      if (refreshInFlightRef.current?.client === client) {
        return refreshInFlightRef.current.promise;
      }
      const promise = (async () => {
        const boot = await signIn();
        const url = deriveWsUrl(boot.ws_path, boot.token, boot.ws_url);
        client.updateUrl(url);
        client.updateUploadToken(boot.token || boot.api_token);
        client.updateMaxFrameBytes(boot.limits?.transport.max_frame_bytes);
        setState((current) =>
          current.status === "ready" && current.client === client
            ? {
                ...current,
                token: boot.api_token ?? "",
                expiresAt: expiresAtFrom(boot.expires_in),
                modelName: boot.model_name ?? current.modelName,
                ingressLimits: boot.limits ?? current.ingressLimits,
              }
            : current,
        );
        return url;
      })().finally(() => {
        if (refreshInFlightRef.current?.promise === promise) {
          refreshInFlightRef.current = null;
        }
      });
      refreshInFlightRef.current = { client, promise };
      return promise;
    },
    [signIn],
  );

  const connect = useCallback(() => {
    let cancelled = false;
    (async () => {
      // Opened with a connection link: pair with its code first (also on the server itself).
      if (linkCode.current) {
        toPairing(false);
        return;
      }
      setState({ status: "loading" });
      try {
        const boot = await signIn();
        if (cancelled) return;
        const url = deriveWsUrl(boot.ws_path, boot.token, boot.ws_url);
        activateReloadCache(url);
        const client: NanobotClient = new NanobotClient({
          url,
          maxFrameBytes: boot.limits?.transport.max_frame_bytes,
          onReauth: async () => {
            try {
              return await refresh(client);
            } catch (e) {
              // The device was removed (here or on another device): back to pairing.
              if (isAuthError(e)) toPairing(true);
              return null;
            }
          },
        });
        client.connect();
        setCurrentDevice(boot.device_id ?? null);
        setState({
          status: "ready",
          client,
          token: boot.api_token ?? "",
          deviceId: boot.device_id ?? null,
          expiresAt: expiresAtFrom(boot.expires_in),
          modelName: boot.model_name ?? null,
          ingressLimits: boot.limits ?? null,
        });
      } catch (e) {
        if (cancelled) return;
        if (isAuthError(e)) toPairing(Boolean((e as { hadDevice?: boolean }).hadDevice));
        else setState({ status: "error", message: e instanceof Error ? e.message : String(e) });
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [refresh, signIn, toPairing]);

  useEffect(() => connect(), [connect]);

  const readyClient = state.status === "ready" ? state.client : null;
  const readyExpiresAt = state.status === "ready" ? state.expiresAt : null;
  const expiresAtRef = useRef<number | null>(readyExpiresAt);
  expiresAtRef.current = readyExpiresAt;

  // Refresh the short-lived API token before it expires, retrying with backoff on transient network errors.
  useEffect(() => {
    if (!readyClient || readyExpiresAt === null) return;
    let cancelled = false;
    let timer: number | undefined;
    let attempt = 0;

    const schedule = (delayMs: number) => {
      timer = window.setTimeout(() => {
        refresh(readyClient).catch((e) => {
          if (cancelled) return;
          if (isAuthError(e)) {
            toPairing(true);
            return;
          }
          attempt += 1;
          const retryDelay = Math.min(MIN_REFRESH_DELAY_MS * 2 ** (attempt - 1), MAX_REFRESH_RETRY_MS);
          schedule(retryDelay);
        });
      }, delayMs);
    };

    const remaining = readyExpiresAt - Date.now();
    const delay = remaining <= REFRESH_LEAD_MS
      ? 0
      : Math.max(MIN_REFRESH_DELAY_MS, remaining - Math.min(REFRESH_LEAD_MS, remaining / 2));
    schedule(delay);
    return () => {
      cancelled = true;
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, [readyClient, readyExpiresAt, refresh, toPairing]);

  // The server keeps working while the app is in the background; coming back, reconnect and renew expiring tokens at once.
  useEffect(() => {
    if (!readyClient) return;
    let hiddenAt = 0;
    const maybeRefreshToken = () => {
      if (expiresAtRef.current !== null && expiresAtRef.current - Date.now() <= REFRESH_LEAD_MS) {
        refresh(readyClient).catch((e) => {
          if (isAuthError(e)) toPairing(true);
        });
      }
    };
    const onVisibility = () => {
      if (document.visibilityState === "hidden") {
        hiddenAt = Date.now();
      } else {
        readyClient.reconnectNow(hiddenAt > 0 && Date.now() - hiddenAt > STALE_SOCKET_MS);
        maybeRefreshToken();
      }
    };
    const onOnline = () => {
      readyClient.reconnectNow();
      maybeRefreshToken();
    };
    const onPageShow = (event: PageTransitionEvent) => {
      if (!event.persisted) return;
      readyClient.reconnectNow(true);
      maybeRefreshToken();
    };
    document.addEventListener("visibilitychange", onVisibility);
    window.addEventListener("online", onOnline);
    window.addEventListener("pageshow", onPageShow);
    return () => {
      document.removeEventListener("visibilitychange", onVisibility);
      window.removeEventListener("online", onOnline);
      window.removeEventListener("pageshow", onPageShow);
    };
  }, [readyClient, refresh, toPairing]);

  // Close a client once it is replaced (disconnect, re-pair) or unmounted.
  useEffect(() => () => readyClient?.close(), [readyClient]);

  /** 断开这台设备: the server forgets it, and so does this device. */
  const disconnect = useCallback(async () => {
    if (state.status === "ready" && state.deviceId) {
      await state.client.requestMutation("companion.devices.remove", { id: state.deviceId }).catch(() => undefined);
    }
    clearDeviceToken();
    toPairing(false);
  }, [state, toPairing]);

  if (state.status === "loading") {
    return <CenteredMessage text="正在连接…" busy />;
  }
  if (state.status === "error") {
    return (
      <CenteredMessage text={`连不上服务器：${state.message}`}>
        <button type="button" className="c-primary mt-5 min-h-12 rounded-full px-8 text-[16px] font-semibold" onClick={connect}>
          重试
        </button>
        {androidShell() && (
          <button type="button" className="c-muted mt-3 min-h-11 text-[14px]" onClick={() => androidShell()?.changeServer()}>
            更换服务器地址
          </button>
        )}
      </CenteredMessage>
    );
  }
  if (state.status === "pair") {
    return <Pairing initialCode={state.code} removed={state.removed} onPaired={connect} />;
  }
  return (
    <ClientProvider client={state.client} token={state.token} modelName={state.modelName} ingressLimits={state.ingressLimits}>
      <CompanionShell onLogout={() => void disconnect()} />
    </ClientProvider>
  );
}

function CenteredMessage({ text, busy = false, children }: { text: string; busy?: boolean; children?: React.ReactNode }) {
  return (
    <main className="companion flex h-full flex-col items-center justify-center px-8 text-center">
      {/* Its own look if this phone remembers it; never another one meanwhile. */}
      <PetView value={lastLook() ?? NO_LOOK} animation={busy ? "running" : "idle"} size={72} />
      <p className="c-muted mt-4 text-[15px]">{text}</p>
      {children}
    </main>
  );
}

/** ``abcd2345`` → ``ABCD-2345`` as the user types. */
function formatCode(value: string): string {
  const raw = value.toUpperCase().replace(/[^A-Z0-9]/g, "").slice(0, 8);
  return raw.length > 4 ? `${raw.slice(0, 4)}-${raw.slice(4)}` : raw;
}

const PAIR_ERRORS: Record<PairError["reason"], string> = {
  wrong_code: "配对码不对或已过期，在服务器上再生成一个",
  locked: "试错太多次了，过 10 分钟再试",
  offline: "连不上服务器，检查一下网络或服务器地址",
};

/** First start on this server: one pairing code, and this device stays connected. */
function Pairing({ initialCode, removed, onPaired }: { initialCode: string; removed: boolean; onPaired: () => void }) {
  const [code, setCode] = useState(formatCode(initialCode));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const complete = code.replace("-", "").length === 8;

  const submit = async (event?: FormEvent) => {
    event?.preventDefault();
    if (!complete || busy) return;
    setBusy(true);
    setError(null);
    try {
      const paired = await pairDevice(code);
      saveDeviceToken(paired.token);
      onPaired();
    } catch (e) {
      setError(PAIR_ERRORS[e instanceof PairError ? e.reason : "offline"]);
      setBusy(false);
    }
  };
  // A code from the link pairs right away.
  useEffect(() => {
    if (initialCode && complete) void submit();
    // Once, for the code the page was opened with.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return (
    <main className="companion flex h-full flex-col items-center justify-center px-6">
      <form onSubmit={submit} className="c-rise flex w-full max-w-sm flex-col items-center">
        <PetView value={lastLook()} animation="waving" size={96} />
        <h1 className="mt-5 text-center text-[26px] font-bold leading-tight">连接你的伙伴</h1>
        <p className="c-muted mt-2 text-center text-[14px] leading-relaxed">
          {removed ? "这台设备已经断开了，" : ""}
          只需要连一次，以后打开就能用。
          <br />
          服务器：{window.location.host}
        </p>
        <label className="mt-7 flex w-full flex-col gap-1.5">
          <span className="c-muted px-1 text-[14px]">配对码</span>
          <input
            value={code}
            onChange={(event) => setCode(formatCode(event.target.value))}
            placeholder="ABCD-2345"
            autoCapitalize="characters"
            autoComplete="one-time-code"
            autoCorrect="off"
            spellCheck={false}
            enterKeyHint="go"
            aria-invalid={!!error}
            aria-describedby={error ? "pair-error" : "pair-help"}
            className="c-float min-h-12 w-full rounded-full px-5 text-center font-mono text-[20px] tracking-[0.2em] outline-none focus-visible:outline-2 focus-visible:outline-[color:var(--c-primary)]"
          />
        </label>
        {error && (
          <p id="pair-error" className="c-danger mt-2 self-start px-1 text-[14px]" role="alert">
            {error}
          </p>
        )}
        <button
          type="submit"
          className="c-primary mt-4 min-h-12 w-full rounded-full text-[16px] font-semibold disabled:opacity-50"
          disabled={!complete || busy}
        >
          {busy ? "正在连接…" : "连接"}
        </button>
        <p id="pair-help" className="c-muted mt-4 px-2 text-center text-[13px] leading-relaxed">
          配对码在服务器上运行 <span className="font-mono">python tools/companion-pair.py</span> 获得，
          或在已连接的设备上打开 设置 → 设备 → 添加新设备。
        </p>
        {androidShell() && (
          <button type="button" className="c-muted mt-2 min-h-11 text-[14px]" onClick={() => androidShell()?.changeServer()}>
            更换服务器地址
          </button>
        )}
      </form>
    </main>
  );
}
