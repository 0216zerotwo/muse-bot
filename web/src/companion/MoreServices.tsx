import { useMemo, useState, type FormEvent } from "react";

import type { McpPresetInfo, McpPresetsPayload } from "@/lib/types";
import { useClient } from "@/providers/ClientProvider";

import { addService, type NewService } from "./companion-api";
import { connectTitle, isConnected, RECOMMENDED_GROUPS, ServiceLogo } from "./ConnectSheet";
import { Segmented } from "./Menu";
import { Sheet } from "./Sheet";

const FIELD =
  "c-card w-full rounded-2xl px-3.5 text-[15px] outline-none focus-visible:outline-2 focus-visible:outline-[color:var(--c-primary)]";
const SUGGESTED = new Set(RECOMMENDED_GROUPS.flatMap((group) => group.names));

/**
 * 添加其他服务: every service in the catalog beyond the suggested ones (searchable), and a
 * form for one the user sets up themselves — by its web address or the command that starts it.
 */
export function MoreServicesSheet({
  open,
  presets,
  onClose,
  onConnect,
  onChanged,
}: {
  open: boolean;
  presets: McpPresetInfo[] | null;
  onClose: () => void;
  /** Opens the usual connect form for a catalog service. */
  onConnect: (preset: McpPresetInfo) => void;
  onChanged: (payload: McpPresetsPayload) => void;
}) {
  const [query, setQuery] = useState("");
  const [adding, setAdding] = useState(false);
  const catalog = useMemo(() => {
    const words = query.trim().toLowerCase();
    return (presets ?? []).filter(
      (preset) =>
        preset.source !== "custom" &&
        preset.install_supported &&
        !isConnected(preset) &&
        !SUGGESTED.has(preset.name) &&
        (!words || `${preset.display_name} ${preset.name} ${preset.description}`.toLowerCase().includes(words)),
    );
  }, [presets, query]);

  return (
    <Sheet
      open={open}
      onOpenChange={(next) => !next && onClose()}
      title={adding ? "自己添加一个服务" : "添加其他服务"}
      description={adding ? "填它的网址，或者在服务器上启动它的命令（MCP 服务）。" : "Notion、GitHub、飞书等更多服务，或者自己添加一个。"}
    >
      {adding ? (
        <CustomServiceForm
          onBack={() => setAdding(false)}
          onAdded={(payload) => {
            onChanged(payload);
            setAdding(false);
            onClose();
          }}
        />
      ) : (
        <div className="flex flex-col gap-3">
          <input
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="搜索服务"
            aria-label="搜索服务"
            className={`${FIELD} min-h-11`}
          />
          <ul className="flex max-h-[50vh] flex-col overflow-y-auto">
            {catalog.map((preset) => (
              <li key={preset.name} className="flex min-h-14 items-center gap-3 py-1.5">
                <ServiceLogo preset={preset} size={28} />
                <span className="min-w-0 flex-1">
                  <span className="block truncate text-[15px]">{preset.display_name || preset.name}</span>
                  <span className="c-muted block truncate text-[12px]">{preset.description}</span>
                </span>
                <button
                  type="button"
                  onClick={() => onConnect(preset)}
                  className="c-primary min-h-8 shrink-0 rounded-full px-3.5 text-[13px] font-semibold"
                  aria-label={connectTitle(preset)}
                >
                  连接
                </button>
              </li>
            ))}
            {catalog.length === 0 && <li className="c-muted py-3 text-[14px]">没有找到，可以自己添加一个。</li>}
          </ul>
          <button
            type="button"
            onClick={() => setAdding(true)}
            className="c-pressable min-h-11 rounded-full text-[15px] font-medium"
            style={{ background: "var(--c-fill)" }}
          >
            自己添加一个
          </button>
        </div>
      )}
    </Sheet>
  );
}

function CustomServiceForm({ onBack, onAdded }: { onBack: () => void; onAdded: (payload: McpPresetsPayload) => void }) {
  const { client } = useClient();
  const [kind, setKind] = useState<NewService["kind"]>("url");
  const [name, setName] = useState("");
  const [url, setUrl] = useState("");
  const [token, setToken] = useState("");
  const [command, setCommand] = useState("");
  const [env, setEnv] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const missing = !name.trim() || (kind === "url" ? !url.trim() : !command.trim());

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (missing || busy) return;
    setBusy(true);
    setError(null);
    try {
      const values: NewService =
        kind === "url" ? { kind, name, url, token } : { kind, name, command, env };
      const payload = await addService(client, values);
      if (payload.hot_reload?.ok === false) setError(`已添加，但${payload.hot_reload.message}`);
      else onAdded(payload);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <form onSubmit={submit} className="flex flex-col gap-4">
      <Segmented
        label="怎么连"
        value={kind}
        options={[
          { value: "url", label: "网址" },
          { value: "command", label: "命令" },
        ]}
        onChange={setKind}
      />
      <label className="flex flex-col gap-1.5">
        <span className="text-[14px] font-medium">名称</span>
        <input value={name} onChange={(e) => setName(e.target.value)} placeholder="比如：notes" autoComplete="off"
          spellCheck={false} className={`${FIELD} min-h-11`} />
        <span className="c-muted px-1 text-[12px]">小写英文字母、数字和横线</span>
      </label>
      {kind === "url" ? (
        <>
          <label className="flex flex-col gap-1.5">
            <span className="text-[14px] font-medium">网址</span>
            <input value={url} onChange={(e) => setUrl(e.target.value)} placeholder="https://example.com/mcp"
              autoComplete="off" spellCheck={false} className={`${FIELD} min-h-11`} />
          </label>
          <label className="flex flex-col gap-1.5">
            <span className="text-[14px] font-medium">
              密钥<span className="c-muted font-normal">（可选）</span>
            </span>
            <input value={token} onChange={(e) => setToken(e.target.value)} type="password" autoComplete="off"
              className={`${FIELD} min-h-11`} />
          </label>
        </>
      ) : (
        <>
          <label className="flex flex-col gap-1.5">
            <span className="text-[14px] font-medium">启动命令</span>
            <input value={command} onChange={(e) => setCommand(e.target.value)} placeholder="npx -y some-mcp-server"
              autoComplete="off" spellCheck={false} className={`${FIELD} min-h-11`} />
          </label>
          <label className="flex flex-col gap-1.5">
            <span className="text-[14px] font-medium">
              设置<span className="c-muted font-normal">（可选，每行一个 名字=值）</span>
            </span>
            <textarea value={env} onChange={(e) => setEnv(e.target.value)} rows={3} placeholder="API_KEY=..."
              spellCheck={false} className={`${FIELD} py-2.5`} />
          </label>
        </>
      )}
      {error && (
        <p className="c-danger text-[13px]" role="alert">
          {error}
        </p>
      )}
      <div className="flex gap-2">
        <button type="button" onClick={onBack} className="c-pressable min-h-11 flex-1 rounded-full text-[15px]"
          style={{ background: "var(--c-fill)" }}>
          返回
        </button>
        <button type="submit" disabled={missing || busy}
          className="c-primary min-h-11 flex-1 rounded-full text-[15px] font-semibold disabled:opacity-50">
          {busy ? "连接中…" : "添加"}
        </button>
      </div>
    </form>
  );
}
