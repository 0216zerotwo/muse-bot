/*
 * 设置 → 安全凭证: site logins and API keys for the companion. Passwords are stored encrypted on the
 * server and only ever filled into the matching site's login page; keys go into the server's
 * environment for the services that need them. Nothing typed here appears in a chat. The same
 * forms show up as a card in the chat when the companion needs a login or a key
 * (``CredentialForm``).
 */
import { Globe, KeyRound, Trash2 } from "lucide-react";
import { useEffect, useState } from "react";

import { useClient } from "@/providers/ClientProvider";

import {
  fetchVault,
  removeCredential,
  saveKey,
  saveLogin,
  type VaultPayload,
} from "./companion-api";
import { MuseSheet } from "./muse/Sheet";
import { SkeletonRows } from "./Skeleton";

const field = "c-card c-focus-ring min-h-11 w-full rounded-2xl px-3.5 text-[16px] outline-none";

/** A login or an API key, typed in by the user; shared by the settings page and the chat card. */
export function CredentialForm({
  kind,
  service = "",
  origin = "",
  env = "",
  onSaved,
}: {
  kind: "login" | "api_key";
  service?: string;
  origin?: string;
  env?: string;
  onSaved: (payload: VaultPayload) => void;
}) {
  const { client } = useClient();
  const [label, setLabel] = useState(service);
  const [site, setSite] = useState(origin);
  const [identifier, setIdentifier] = useState("");
  const [secret, setSecret] = useState("");
  const [variable, setVariable] = useState(env);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const ready = kind === "login" ? site.trim() && identifier.trim() && secret : variable.trim() && secret.trim();

  const save = async () => {
    setBusy(true);
    setError(null);
    try {
      const next =
        kind === "login"
          ? await saveLogin(client, { label: label.trim(), origin: site.trim(), identifier: identifier.trim(), password: secret })
          : await saveKey(client, { label: label.trim(), env: variable.trim(), value: secret.trim() });
      setSecret("");
      onSaved(next);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <form
      className="flex flex-col gap-2.5"
      onSubmit={(event) => {
        event.preventDefault();
        if (ready && !busy) void save();
      }}
    >
      {kind === "login" ? (
        <>
          <input value={site} onChange={(e) => setSite(e.target.value)} placeholder="网站地址，比如 github.com" aria-label="网站地址" inputMode="url" autoComplete="off" className={field} />
          <input value={identifier} onChange={(e) => setIdentifier(e.target.value)} placeholder="账号（邮箱、手机号或用户名）" aria-label="账号" autoComplete="username" className={field} />
          <input value={secret} onChange={(e) => setSecret(e.target.value)} type="password" placeholder="密码" aria-label="密码" autoComplete="current-password" className={field} />
        </>
      ) : (
        <>
          <input value={label} onChange={(e) => setLabel(e.target.value)} placeholder="名称，比如 GitHub" aria-label="名称" className={field} />
          <input
            value={variable}
            onChange={(e) => setVariable(e.target.value.toUpperCase())}
            placeholder="变量名，比如 GITHUB_TOKEN"
            aria-label="变量名"
            autoCapitalize="characters"
            autoComplete="off"
            className={`${field} font-mono`}
          />
          <input value={secret} onChange={(e) => setSecret(e.target.value)} type="password" placeholder="密钥" aria-label="密钥" autoComplete="off" className={field} />
        </>
      )}
      {error && <p className="c-danger text-[13.5px]">{error}</p>}
      <button type="submit" disabled={!ready || busy} className="c-primary min-h-11 rounded-full text-[15px] font-semibold disabled:opacity-40">
        {busy ? "保存中…" : "加密保存"}
      </button>
      <p className="c-muted text-[12px] leading-snug">只存在你的服务器上，加密保存。它只能拿来帮你在对应的网站或服务上用，看不到内容。</p>
    </form>
  );
}

export function VaultPanel() {
  const { client, token } = useClient();
  const [data, setData] = useState<VaultPayload | null>(null);
  const [adding, setAdding] = useState<"login" | "api_key" | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    fetchVault(token)
      .then(setData)
      .catch((e: unknown) => setError(e instanceof Error ? e.message : String(e)));
  }, [token]);
  const remove = async (kind: "login" | "key", id: string) => {
    try {
      setData(await removeCredential(client, kind, id));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  return (
    <div className="flex flex-col gap-6">
      <p className="c-muted px-3.5 text-[13px] leading-snug">网站登录和服务密钥都加密存在你的服务器上。它需要时会在聊天里弹出卡片让你填，不用把密码发在聊天里。</p>
      {error && <p className="c-danger px-3.5 text-[13px]">{error}</p>}

      <section className="flex flex-col gap-1.5">
        <div className="flex items-center px-3.5">
          <h2 className="c-muted flex-1 text-[13px]">网站登录</h2>
          <button
            type="button"
            onClick={() => setAdding("login")}
            className="c-pressable -mr-2.5 min-h-11 rounded-xl px-2.5 text-[13.5px] font-medium"
            style={{ color: "var(--c-primary)" }}
          >
            添加
          </button>
        </div>
        {!data && !error ? (
          <SkeletonRows rows={2} label="正在读取网站登录" />
        ) : data && data.logins.length === 0 ? (
          <div className="c-card rounded-2xl px-4 py-4">
            <p className="c-muted text-[13.5px]">还没有。</p>
          </div>
        ) : (
          <ul className="c-card divide-y overflow-hidden rounded-2xl" style={{ borderColor: "var(--c-border)" }}>
            {(data?.logins ?? []).map((login) => (
              <li key={login.id} className="flex items-center gap-3 py-2 pl-4 pr-1.5">
                <Globe className="c-muted h-4 w-4 shrink-0" />
                <span className="min-w-0 flex-1">
                  <span className="block truncate text-[15px] font-medium">{login.label}</span>
                  <span className="c-muted block truncate text-[12.5px]">
                    {login.identifier} · {login.origin.replace(/^https?:\/\//, "")}
                  </span>
                </span>
                <span className="c-muted shrink-0 text-[13px] tracking-widest">••••••</span>
                <button
                  type="button"
                  aria-label={`删除 ${login.label} 的登录`}
                  onClick={() => void remove("login", login.id)}
                  className="c-muted c-pressable grid min-h-11 min-w-11 shrink-0 place-items-center rounded-xl"
                >
                  <Trash2 className="h-4 w-4" />
                </button>
              </li>
            ))}
          </ul>
        )}
      </section>

      <section className="flex flex-col gap-1.5">
        <div className="flex items-center px-3.5">
          <h2 className="c-muted flex-1 text-[13px]">API 密钥</h2>
          <button
            type="button"
            onClick={() => setAdding("api_key")}
            className="c-pressable -mr-2.5 min-h-11 rounded-xl px-2.5 text-[13.5px] font-medium"
            style={{ color: "var(--c-primary)" }}
          >
            添加
          </button>
        </div>
        {!data && !error ? (
          <SkeletonRows rows={2} label="正在读取密钥" />
        ) : data && data.keys.length === 0 ? (
          <div className="c-card rounded-2xl px-4 py-4">
            <p className="c-muted text-[13.5px] leading-snug">还没有。模型的密钥在「模型」里管理。</p>
          </div>
        ) : (
          <ul className="c-card divide-y overflow-hidden rounded-2xl" style={{ borderColor: "var(--c-border)" }}>
            {(data?.keys ?? []).map((key) => (
              <li key={key.env} className="flex items-center gap-3 py-2 pl-4 pr-1.5">
                <KeyRound className="c-muted h-4 w-4 shrink-0" />
                <span className="min-w-0 flex-1">
                  <span className="block truncate text-[15px] font-medium">{key.label}</span>
                  <span className="c-muted block truncate font-mono text-[12px]">{key.env}</span>
                </span>
                <span className="c-muted shrink-0 text-[12.5px]">{key.set ? "已设好" : "没有值"}</span>
                <button
                  type="button"
                  aria-label={`删除 ${key.label} 的密钥`}
                  onClick={() => void remove("key", key.env)}
                  className="c-muted c-pressable grid min-h-11 min-w-11 shrink-0 place-items-center rounded-xl"
                >
                  <Trash2 className="h-4 w-4" />
                </button>
              </li>
            ))}
          </ul>
        )}
      </section>

      <MuseSheet open={adding !== null} onClose={() => setAdding(null)} title={adding === "login" ? "添加网站登录" : "添加 API 密钥"}>
        {adding && (
          <div className="pb-2">
            <CredentialForm
              kind={adding}
              onSaved={(next) => {
                setData(next);
                setAdding(null);
              }}
            />
          </div>
        )}
      </MuseSheet>
    </div>
  );
}
