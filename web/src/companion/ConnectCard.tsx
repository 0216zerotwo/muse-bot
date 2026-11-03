/*
 * 按需连接: a task needs a service that is not connected yet, so the reply carries a card for it
 * (as Grok Bot asks for a one-click connection mid-task). Connecting opens the usual connect sheet
 * right here; once it is connected the companion is told to carry on, without a trip to settings.
 */
import { Check } from "lucide-react";
import { useEffect, useState } from "react";

import { fetchMcpPresets } from "@/lib/api";
import type { McpPresetInfo } from "@/lib/types";
import { useClient } from "@/providers/ClientProvider";

import { ConnectSheet, isConnected, ServiceLogo } from "./ConnectSheet";

export function ConnectCard({ service, reason, send }: { service: string; reason?: string; send?: (text: string) => void }) {
  const { token } = useClient();
  const [preset, setPreset] = useState<McpPresetInfo | null>(null);
  const [missing, setMissing] = useState(false);
  const [open, setOpen] = useState(false);

  useEffect(() => {
    let cancelled = false;
    fetchMcpPresets(token)
      .then((payload) => {
        if (cancelled) return;
        const found = payload.presets.find((p) => p.name === service) ?? null;
        setPreset(found);
        setMissing(!found);
      })
      .catch(() => !cancelled && setMissing(true));
    return () => {
      cancelled = true;
    };
  }, [service, token]);

  if (missing) return null;
  if (!preset) return <div className="c-card h-[72px] w-full max-w-[340px] rounded-2xl" aria-hidden="true" />;
  const connected = isConnected(preset);
  return (
    <div className="c-card flex w-full max-w-[340px] items-center gap-3 rounded-2xl p-3">
      <ServiceLogo preset={preset} size={40} />
      <div className="min-w-0 flex-1">
        <p className="truncate text-[15px] font-semibold">{preset.display_name}</p>
        <p className="c-muted truncate text-[13px]">{connected ? "已连接" : reason || "连上后才能接着做"}</p>
      </div>
      {connected ? (
        <span className="c-muted flex shrink-0 items-center gap-1 text-[13px]">
          <Check className="h-4 w-4" /> 已连接
        </span>
      ) : (
        <button type="button" onClick={() => setOpen(true)} className="c-primary min-h-9 shrink-0 rounded-full px-4 text-[14px] font-semibold">
          连接
        </button>
      )}
      <ConnectSheet
        preset={open ? preset : null}
        onClose={() => setOpen(false)}
        onChanged={(payload) => {
          const next = payload.presets.find((p) => p.name === service) ?? preset;
          setPreset(next);
          if (isConnected(next) && !connected) {
            setOpen(false);
            // Connected: pick the task up where it stopped.
            send?.(`${next.display_name} 连好了，接着做刚才的事`);
          }
        }}
      />
    </div>
  );
}
