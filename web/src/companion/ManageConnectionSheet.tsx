import { useEffect, useState } from "react";

import { runMcpPresetAction, updateMcpServerTools } from "@/lib/api";
import type { McpPresetInfo, McpPresetsPayload } from "@/lib/types";
import { useClient } from "@/providers/ClientProvider";

import { connectFailure, ServiceLogo } from "./ConnectSheet";
import { isReadOnlyTool, rawToolName, toolLabel } from "./presentation";
import { Switch } from "./Menu";
import { Sheet } from "./Sheet";
import { SkeletonLines } from "./Skeleton";

/** Only looking tools are switched on (a list that merely leaves out unsafe tools is not view-only). */
export function isViewOnly(preset: McpPresetInfo): boolean {
  const enabled = preset.enabled_tools ?? ["*"];
  return !enabled.includes("*") && enabled.every((name) => isReadOnlyTool(rawToolName(name, preset.name)));
}

/** Everything the connection offers, even while some of it is switched off. */
async function allTools(
  client: Parameters<typeof runMcpPresetAction>[0],
  preset: McpPresetInfo,
): Promise<string[]> {
  const restricted = preset.enabled_tools && !preset.enabled_tools.includes("*");
  if (!restricted && preset.tool_names?.length) return preset.tool_names;
  // Starting the server once lists all of its tools regardless of the allowlist.
  const payload = await runMcpPresetAction(client, "test", preset.name);
  return payload.last_action?.tool_names ?? preset.tool_names ?? [];
}

/**
 * Per-connection control, like Muse's connector settings: what it can do,
 * view-only or full access, pause, and disconnect. Approval cards still guard
 * sending and deleting when full access is on.
 */
export function ManageConnectionSheet({
  preset,
  onClose,
  onChanged,
  onEditCredentials,
}: {
  preset: McpPresetInfo | null;
  onClose: () => void;
  onChanged: (payload: McpPresetsPayload) => void;
  onEditCredentials: (preset: McpPresetInfo) => void;
}) {
  const { client } = useClient();
  const [tools, setTools] = useState<string[] | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [confirmRemove, setConfirmRemove] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setTools(null);
    setConfirmRemove(false);
    setError(null);
    if (!preset) return;
    let cancelled = false;
    allTools(client, preset)
      .then((names) => {
        if (!cancelled) setTools(names);
      })
      .catch((e) => {
        if (!cancelled) {
          setTools([]);
          setError(e instanceof Error ? e.message : String(e));
        }
      });
    return () => {
      cancelled = true;
    };
    // Re-read only when a different connection opens, not after each change below.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [client, preset?.name]);

  if (!preset) return null;
  const enabled = preset.enabled_tools ?? ["*"];
  const isEnabled = (name: string) =>
    enabled.includes("*") || enabled.includes(name) || enabled.includes(rawToolName(name, preset.name));
  // Tools marked unsafe stay out of the list unless someone switched them on deliberately.
  const raw = (tools ?? [])
    .filter((name) => !name.endsWith("_unsafe") || isEnabled(name))
    .map((name) => rawToolName(name, preset.name));
  const reading = raw.filter(isReadOnlyTool);
  const acting = raw.filter((name) => !isReadOnlyTool(name));
  const viewOnly = isViewOnly(preset);
  const paused = preset.enabled === false;

  const run = async (key: string, action: () => Promise<McpPresetsPayload>, close = false) => {
    setBusy(key);
    setError(null);
    try {
      const payload = await action();
      onChanged(payload);
      const failure = connectFailure(payload);
      if (failure) setError(failure);
      else if (close) onClose();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  };

  const setViewOnly = (on: boolean) => {
    const all = tools ?? [];
    // Full access never turns on tools a connection marks unsafe (e.g. browser_run_code_unsafe).
    const safe = all.filter((name) => !name.endsWith("_unsafe"));
    return run("tools", () =>
      updateMcpServerTools(
        client,
        preset.name,
        on
          ? all.filter((name) => isReadOnlyTool(rawToolName(name, preset.name)))
          : safe.length === all.length
            ? ["*"]
            : safe,
      ),
    );
  };

  return (
    <Sheet open onOpenChange={(open) => !open && !busy && onClose()} title={preset.display_name || preset.name}>
      <div className="flex flex-col gap-4">
        <div className="flex items-center gap-3">
          <ServiceLogo preset={preset} />
          <p className="c-muted min-w-0 flex-1 text-[14px] leading-snug">{preset.description}</p>
        </div>

        <section className="c-card rounded-[22px] p-4">
          <p className="text-[15px] font-semibold">它能做的事</p>
          {tools === null ? (
            <SkeletonLines lines={2} label="正在读取连接能力" className="mt-2.5" />
          ) : (
            <div className="mt-2 flex flex-col gap-2 text-[14px] leading-relaxed">
              {reading.length > 0 && (
                <p>
                  <span className="c-muted">查看：</span>
                  {reading.map(toolLabel).join("、")}
                </p>
              )}
              {acting.length > 0 && (
                <p style={viewOnly ? { opacity: 0.45, textDecoration: "line-through" } : undefined}>
                  <span className="c-muted">操作：</span>
                  {acting.map(toolLabel).join("、")}
                </p>
              )}
              {raw.length === 0 && <p className="c-muted">暂时读不到，连接正常后再看。</p>}
            </div>
          )}
        </section>

        {acting.length > 0 && reading.length > 0 && (
          <div className="c-card flex items-center gap-3 rounded-[22px] p-4">
            <div className="min-w-0 flex-1">
              <p className="text-[15px] font-semibold">只允许查看</p>
              <p className="c-muted mt-0.5 text-[13px] leading-snug">
                开启后它只能看，不能替你添加、修改、发送或删除。关闭时，发送和删除前仍会先问你。
              </p>
            </div>
            <Switch checked={viewOnly} disabled={busy !== null} label="只允许查看" onChange={setViewOnly} />
          </div>
        )}

        {error && (
          <p className="c-danger text-sm" role="alert">
            {error}
          </p>
        )}

        <div className="flex flex-col gap-2">
          {preset.required_fields.length > 0 && (
            <button
              type="button"
              onClick={() => onEditCredentials(preset)}
              className="c-pressable min-h-12 rounded-full text-[15px] font-medium"
              style={{ background: "var(--c-fill)" }}
            >
              重新填写凭据
            </button>
          )}
          <button
            type="button"
            disabled={busy !== null}
            onClick={() => run("pause", () => runMcpPresetAction(client, paused ? "enable" : "disable", preset.name))}
            className="c-pressable min-h-12 rounded-full text-[15px] font-medium disabled:opacity-50"
            style={{ background: "var(--c-fill)" }}
          >
            {busy === "pause" ? "处理中…" : paused ? "恢复使用" : "暂停使用"}
          </button>
          <button
            type="button"
            disabled={busy !== null}
            onClick={() =>
              confirmRemove
                ? run("remove", () => runMcpPresetAction(client, "remove", preset.name), true)
                : setConfirmRemove(true)
            }
            className="min-h-12 rounded-full text-[15px] font-semibold disabled:opacity-50"
            style={confirmRemove ? { background: "var(--c-danger-fill)", color: "#fff" } : { color: "var(--c-danger)" }}
          >
            {busy === "remove" ? "断开中…" : confirmRemove ? "确定断开？凭据会一起删除" : "断开连接"}
          </button>
        </div>
      </div>
    </Sheet>
  );
}
