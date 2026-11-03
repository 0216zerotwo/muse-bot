import { useEffect, useState, type FormEvent, type ReactNode } from "react";
import { ExternalLink, PlugZap } from "lucide-react";

import { runMcpPresetAction } from "@/lib/api";
import type { McpPresetInfo, McpPresetsPayload } from "@/lib/types";
import { useClient } from "@/providers/ClientProvider";

import { Sheet } from "./Sheet";

/**
 * Service logo. The favicon host is unreachable from mainland China, so the
 * fallback is the service's first character on its brand color (or a plug).
 */
export function ServiceLogo({ preset, size = 40 }: { preset: McpPresetInfo; size?: number }) {
  const [failed, setFailed] = useState(false);
  const box = { width: size, height: size, borderRadius: Math.round(size * 0.28) };
  if (preset.logo_url && !failed) {
    return <img src={preset.logo_url} alt="" onError={() => setFailed(true)} className="shrink-0 object-contain" style={box} />;
  }
  const initial = (preset.display_name || preset.name).trim().charAt(0).toUpperCase();
  return (
    <span
      className="grid shrink-0 place-items-center font-semibold"
      style={{
        ...box,
        background: preset.brand_color || "var(--c-fill)",
        color: preset.brand_color ? "#fff" : undefined,
        fontSize: Math.round(size * 0.45),
      }}
      aria-hidden="true"
    >
      {preset.brand_color && initial ? initial : <PlugZap className="h-1/2 w-1/2" />}
    </span>
  );
}

/** Everyday services the 连接 page suggests, by what they help with. */
export const RECOMMENDED_GROUPS: { title: string; names: string[] }[] = [
  { title: "日常", names: ["email", "icloud-calendar", "kuaidi100"] },
  { title: "出行", names: ["amap", "train-12306"] },
  { title: "替你动手", names: ["browser", "minimax"] },
  { title: "家里", names: ["home-assistant"] },
];

/** The first few, offered during first-run setup. */
export const RECOMMENDED = ["email", "icloud-calendar", "amap", "browser"];

export function recommendedPresets(presets: McpPresetInfo[] | null, names: string[] = RECOMMENDED): McpPresetInfo[] {
  return names
    .map((name) => presets?.find((preset) => preset.name === name))
    .filter((preset): preset is McpPresetInfo => Boolean(preset?.install_supported));
}

export function isConnected(preset: McpPresetInfo): boolean {
  return preset.installed || preset.configured;
}

export function ServiceRow({ preset, detail, trailing }: { preset: McpPresetInfo; detail: string; trailing: ReactNode }) {
  return (
    <div className="flex items-center gap-3 text-left">
      <ServiceLogo preset={preset} />
      <div className="min-w-0 flex-1">
        <p className="truncate text-[16px] font-semibold">{preset.display_name || preset.name}</p>
        <p className="c-muted line-clamp-2 text-[13px]">{detail}</p>
      </div>
      {trailing}
    </div>
  );
}

/** Cards with a 连接 button; services already connected say so instead. */
export function RecommendedServices({
  presets,
  onConnect,
}: {
  presets: McpPresetInfo[];
  onConnect: (preset: McpPresetInfo) => void;
}) {
  return (
    <ul className="flex flex-col gap-3">
      {presets.map((preset) => (
        <li key={preset.name} className="c-float rounded-[22px] p-4">
          <ServiceRow
            preset={preset}
            detail={preset.description}
            trailing={
              isConnected(preset) ? (
                <span className="c-muted shrink-0 text-[13px]">已连接</span>
              ) : (
                <button
                  type="button"
                  onClick={() => onConnect(preset)}
                  className="c-primary min-h-9 shrink-0 rounded-full px-4 text-[14px] font-semibold"
                  aria-label={connectTitle(preset)}
                >
                  连接
                </button>
              )
            }
          />
        </li>
      ))}
    </ul>
  );
}

/** "连接通用邮箱", "连接 iCloud 日历": a space only before a Latin name. */
export function connectTitle(preset: McpPresetInfo): string {
  const name = preset.display_name || preset.name;
  return /^[A-Za-z0-9]/.test(name) ? `连接 ${name}` : `连接${name}`;
}

/** The reason an enable action did not take effect, or ``null`` when it did. */
export function connectFailure(payload: McpPresetsPayload): string | null {
  if (payload.last_action?.ok === false) return payload.last_action.error || payload.last_action.message;
  if (payload.hot_reload?.ok === false) return payload.hot_reload.message;
  return null;
}

/**
 * Credentials form for one service. Values go straight to the gateway's own
 * MCP settings (the same action the console uses) and are never shown again;
 * fields already saved can be left blank to keep them.
 */
export function ConnectSheet({
  preset,
  onClose,
  onChanged,
}: {
  preset: McpPresetInfo | null;
  onClose: () => void;
  onChanged: (payload: McpPresetsPayload) => void;
}) {
  const { client } = useClient();
  const [values, setValues] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setValues({});
    setError(null);
  }, [preset?.name]);

  if (!preset) return null;
  const fields = preset.required_fields;
  const missing = fields.some((field) => field.required && !field.configured && !values[field.name]?.trim());

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (missing || busy) return;
    setBusy(true);
    setError(null);
    try {
      const payload = await runMcpPresetAction(client, "enable", preset.name, values);
      onChanged(payload);
      const failure = connectFailure(payload);
      if (failure) setError(failure);
      else onClose();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Sheet
      open
      onOpenChange={(open) => !open && !busy && onClose()}
      title={connectTitle(preset)}
      description={preset.description}
    >
      <form onSubmit={submit} className="flex flex-col gap-4">
        {preset.note && <p className="c-muted text-[14px] leading-relaxed">{preset.note}</p>}
        {fields.map((field) => (
          <label key={field.name} className="flex flex-col gap-1.5">
            <span className="text-[14px] font-medium">
              {field.label}
              {!field.required && <span className="c-muted font-normal">（可选）</span>}
            </span>
            <input
              type={field.secret ? "password" : "text"}
              name={field.name}
              value={values[field.name] ?? ""}
              onChange={(event) => setValues((prev) => ({ ...prev, [field.name]: event.target.value }))}
              placeholder={field.configured ? "已保存，留空不变" : field.placeholder || ""}
              autoComplete={field.secret ? "new-password" : "off"}
              autoCapitalize="off"
              autoCorrect="off"
              spellCheck={false}
              className="c-card min-h-11 rounded-2xl px-4 text-[16px] outline-none focus-visible:outline-2 focus-visible:outline-[color:var(--c-primary)]"
            />
          </label>
        ))}
        {error && (
          <p className="c-danger text-[14px]" role="alert">
            {error}
          </p>
        )}
        <button
          type="submit"
          disabled={missing || busy}
          className="c-primary min-h-12 rounded-full text-[16px] font-semibold disabled:opacity-50"
        >
          {busy ? "连接中…" : "连接"}
        </button>
        {preset.docs_url && fields.length > 0 && (
          <a
            href={preset.docs_url}
            target="_blank"
            rel="noreferrer"
            className="c-muted flex min-h-11 items-center justify-center gap-1.5 text-[14px]"
          >
            怎么获取这些信息
            <ExternalLink className="h-3.5 w-3.5" aria-hidden="true" />
          </a>
        )}
      </form>
    </Sheet>
  );
}
