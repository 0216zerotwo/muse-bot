/*
 * 按需连接: a task needs a service that is not connected yet, so the reply carries a card for it
 * (as Grok Bot asks for a one-click connection mid-task). Credentials can be entered inline with
 * secret visibility toggles; once connected the companion is told to carry on.
 */
import { Check, CircleAlert, ExternalLink, Eye, EyeOff, Loader2 } from "lucide-react";
import { useCallback, useEffect, useState, type FormEvent } from "react";

import { fetchMcpPresets, runMcpPresetAction } from "@/lib/api";
import type { McpPresetInfo } from "@/lib/types";
import { useClient } from "@/providers/ClientProvider";

import { connectFailure, isConnected, ServiceLogo } from "./ConnectSheet";

const FOCUS_RING =
  "focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[color:var(--c-primary)]";
const FIELD_CLASS =
  "c-card min-h-11 w-full rounded-2xl px-3.5 text-[16px] outline-none focus-visible:outline focus-visible:outline-2 focus-visible:outline-[color:var(--c-primary)]";

export function ConnectCard({ service, reason, send }: { service: string; reason?: string; send?: (text: string) => void }) {
  const { client, token } = useClient();
  const [preset, setPreset] = useState<McpPresetInfo | null>(null);
  const [missing, setMissing] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [expanded, setExpanded] = useState(false);
  const [values, setValues] = useState<Record<string, string>>({});
  const [revealed, setRevealed] = useState<Record<string, boolean>>({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(
    (isCancelled?: () => boolean) => {
      setLoadError(null);
      setMissing(false);
      fetchMcpPresets(token)
        .then((payload) => {
          if (isCancelled?.()) return;
          const found = payload.presets.find((p) => p.name === service) ?? null;
          setPreset(found);
          setMissing(!found);
        })
        .catch((e: unknown) => {
          if (isCancelled?.()) return;
          setLoadError(e instanceof Error ? e.message : "服务信息加载失败");
        });
    },
    [service, token],
  );

  useEffect(() => {
    let cancelled = false;
    setValues({});
    setRevealed({});
    setExpanded(false);
    setError(null);
    load(() => cancelled);
    return () => {
      cancelled = true;
    };
  }, [load]);

  if (missing) return null;
  if (loadError) {
    return (
      <div className="c-card flex w-full max-w-[340px] items-center gap-3 rounded-2xl p-3" role="alert">
        <CircleAlert className="c-danger h-5 w-5 shrink-0" aria-hidden="true" />
        <div className="min-w-0 flex-1">
          <p className="text-[14px] font-semibold">无法读取连接信息</p>
          <p className="c-muted truncate text-[12.5px]">{loadError}</p>
        </div>
        <button
          type="button"
          onClick={() => load()}
          className={`c-pressable min-h-11 shrink-0 rounded-full px-4 text-[13.5px] font-semibold motion-reduce:transition-none ${FOCUS_RING}`}
          style={{ background: "var(--c-fill)" }}
        >
          重试
        </button>
      </div>
    );
  }
  if (!preset) {
    return (
      <div
        className="c-card flex h-[72px] w-full max-w-[340px] items-center gap-3 rounded-2xl p-3"
        role="status"
        aria-label="正在加载服务信息"
      >
        <span className="h-10 w-10 shrink-0 animate-pulse rounded-xl motion-reduce:animate-none" style={{ background: "var(--c-fill)" }} aria-hidden="true" />
        <span className="flex min-w-0 flex-1 flex-col gap-2" aria-hidden="true">
          <span className="h-3.5 w-24 animate-pulse rounded-full motion-reduce:animate-none" style={{ background: "var(--c-fill)" }} />
          <span className="h-3 w-36 animate-pulse rounded-full motion-reduce:animate-none" style={{ background: "var(--c-fill)" }} />
        </span>
      </div>
    );
  }

  const connected = isConnected(preset);
  const fields = preset.required_fields;
  const formIncomplete = fields.some((field) => field.required && !field.configured && !values[field.name]?.trim());

  const enableService = async () => {
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      const payload = await runMcpPresetAction(client, "enable", preset.name, values);
      const next = payload.presets.find((p) => p.name === service) ?? preset;
      setPreset(next);
      const failure = connectFailure(payload);
      if (failure) {
        setError(failure);
      } else if (isConnected(next) && !connected) {
        setExpanded(false);
        setValues({});
        setRevealed({});
        // Connected: pick the task up where it stopped.
        send?.(`${next.display_name} 连好了，接着做刚才的事`);
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const handleSubmit = (event: FormEvent) => {
    event.preventDefault();
    if (!formIncomplete && !busy) void enableService();
  };

  return (
    <div className="c-card flex w-full max-w-[340px] flex-col gap-3 rounded-2xl p-3.5" aria-busy={busy}>
      <div className="flex items-center gap-3">
        <ServiceLogo preset={preset} size={40} />
        <div className="min-w-0 flex-1">
          <p className="truncate text-[15px] font-semibold">{preset.display_name}</p>
          <p className="c-muted truncate text-[13px]">{connected ? "已连接" : reason || "连上后才能接着做"}</p>
        </div>
        {connected ? (
          <span
            className="inline-flex shrink-0 items-center gap-1 rounded-full px-2.5 py-1 text-[13px] font-medium"
            style={{ color: "var(--c-online)", background: "var(--c-fill)" }}
          >
            <Check className="h-4 w-4" aria-hidden="true" /> 已连接
          </span>
        ) : !expanded ? (
          <button
            type="button"
            disabled={busy}
            onClick={() => {
              setError(null);
              if (fields.length > 0) setExpanded(true);
              else void enableService();
            }}
            className={`c-primary inline-flex min-h-11 shrink-0 items-center justify-center gap-1.5 rounded-full px-4 text-[14px] font-semibold transition active:scale-[0.98] disabled:opacity-50 motion-reduce:transition-none motion-reduce:active:scale-100 ${FOCUS_RING}`}
          >
            {busy && <Loader2 className="h-4 w-4 animate-spin motion-reduce:animate-none" aria-hidden="true" />}
            {busy ? "连接中…" : "连接"}
          </button>
        ) : null}
      </div>

      {!connected && expanded && (
        <form onSubmit={handleSubmit} className="c-divider flex flex-col gap-2.5 border-t pt-3">
          {preset.note && <p className="c-muted text-[12.5px] leading-relaxed">{preset.note}</p>}
          {fields.map((field) => {
            const isShown = Boolean(revealed[field.name]);
            return (
              <div key={field.name} className="flex flex-col gap-1">
                <label htmlFor={`connect-${preset.name}-${field.name}`} className="text-[13px] font-medium">
                  {field.label}
                  {!field.required && <span className="c-muted font-normal">（可选）</span>}
                </label>
                <div className="relative flex items-center">
                  <input
                    id={`connect-${preset.name}-${field.name}`}
                    type={field.secret && !isShown ? "password" : "text"}
                    name={field.name}
                    value={values[field.name] ?? ""}
                    onChange={(event) => setValues((prev) => ({ ...prev, [field.name]: event.target.value }))}
                    placeholder={field.configured ? "已保存，留空不变" : field.placeholder || ""}
                    autoComplete={field.secret ? "new-password" : "off"}
                    autoCapitalize="off"
                    autoCorrect="off"
                    spellCheck={false}
                    className={`${FIELD_CLASS} ${field.secret ? "pr-12" : ""}`}
                  />
                  {field.secret && (
                    <button
                      type="button"
                      aria-label={isShown ? `隐藏${field.label}` : `显示${field.label}`}
                      aria-pressed={isShown}
                      onClick={() => setRevealed((prev) => ({ ...prev, [field.name]: !prev[field.name] }))}
                      className={`c-muted c-pressable absolute right-0 top-1/2 grid min-h-11 min-w-11 -translate-y-1/2 place-items-center rounded-full motion-reduce:transition-none ${FOCUS_RING}`}
                    >
                      {isShown ? <EyeOff className="h-4 w-4" aria-hidden="true" /> : <Eye className="h-4 w-4" aria-hidden="true" />}
                    </button>
                  )}
                </div>
              </div>
            );
          })}
          {error && (
            <div className="flex items-start gap-2 rounded-xl px-3 py-2 text-[13px]" style={{ background: "var(--c-fill)" }} role="alert">
              <CircleAlert className="c-danger mt-0.5 h-4 w-4 shrink-0" aria-hidden="true" />
              <span className="c-danger min-w-0 flex-1 leading-snug">{error}</span>
            </div>
          )}
          <div className="flex gap-2 pt-0.5">
            <button
              type="button"
              disabled={busy}
              onClick={() => {
                setExpanded(false);
                setError(null);
              }}
              className={`c-pressable min-h-11 flex-1 rounded-full text-[14px] font-medium disabled:opacity-50 motion-reduce:transition-none ${FOCUS_RING}`}
              style={{ background: "var(--c-fill)" }}
            >
              取消
            </button>
            <button
              type="submit"
              disabled={formIncomplete || busy}
              className={`c-primary inline-flex min-h-11 flex-1 items-center justify-center gap-1.5 rounded-full text-[14px] font-semibold transition active:scale-[0.98] disabled:opacity-40 motion-reduce:transition-none motion-reduce:active:scale-100 ${FOCUS_RING}`}
            >
              {busy && <Loader2 className="h-4 w-4 animate-spin motion-reduce:animate-none" aria-hidden="true" />}
              {busy ? "连接中…" : "确认连接"}
            </button>
          </div>
          {preset.docs_url && (
            <a
              href={preset.docs_url}
              target="_blank"
              rel="noreferrer"
              className={`c-muted inline-flex min-h-11 items-center justify-center gap-1.5 rounded-full text-[13px] motion-reduce:transition-none ${FOCUS_RING}`}
            >
              怎么获取这些信息
              <ExternalLink className="h-3.5 w-3.5" aria-hidden="true" />
            </a>
          )}
        </form>
      )}

      {!connected && !expanded && error && (
        <div className="flex items-start gap-2 rounded-xl px-3 py-2 text-[13px]" style={{ background: "var(--c-fill)" }} role="alert">
          <CircleAlert className="c-danger mt-0.5 h-4 w-4 shrink-0" aria-hidden="true" />
          <span className="c-danger min-w-0 flex-1 leading-snug">{error}</span>
        </div>
      )}
    </div>
  );
}
