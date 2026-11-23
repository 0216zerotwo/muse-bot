/*
 * A login or an API key the task needs, asked for on a card: the user types it here and it is
 * saved encrypted on the server (see VaultPanel) — it never goes into the chat. Once saved, the
 * companion is told to carry on.
 */
import { Check, CircleAlert, Eye, EyeOff, Loader2, Lock } from "lucide-react";
import { useState, type FormEvent } from "react";

import { useClient } from "@/providers/ClientProvider";

import { saveKey, saveLogin } from "./companion-api";
import type { Card } from "./replyCards";

const FOCUS_RING =
  "focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[color:var(--c-primary)]";
const FIELD_CLASS =
  "c-card min-h-11 w-full rounded-2xl px-3.5 text-[16px] outline-none focus-visible:outline focus-visible:outline-2 focus-visible:outline-[color:var(--c-primary)]";

export function CredentialCard({ card, send }: { card: Extract<Card, { type: "credential" }>; send?: (text: string) => void }) {
  const { client } = useClient();
  const [saved, setSaved] = useState(false);
  const [label, setLabel] = useState(card.service);
  const [site, setSite] = useState(card.origin ?? "");
  const [identifier, setIdentifier] = useState("");
  const [variable, setVariable] = useState(card.env ?? "");
  const [secret, setSecret] = useState("");
  const [showSecret, setShowSecret] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const what = card.kind === "login" ? "登录" : "密钥";
  const secretLabel = card.kind === "login" ? "密码" : "密钥";
  const ready = card.kind === "login" ? Boolean(site.trim() && identifier.trim() && secret) : Boolean(variable.trim() && secret.trim());

  const save = async (event: FormEvent) => {
    event.preventDefault();
    if (!ready || busy) return;
    setBusy(true);
    setError(null);
    try {
      if (card.kind === "login") {
        await saveLogin(client, {
          label: label.trim(),
          origin: site.trim(),
          identifier: identifier.trim(),
          password: secret,
        });
      } else {
        await saveKey(client, {
          label: label.trim(),
          env: variable.trim(),
          value: secret.trim(),
        });
      }
      setSecret("");
      setShowSecret(false);
      setSaved(true);
      send?.(`${card.service} 的${what}存好了，接着做`);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="c-card w-full max-w-[340px] rounded-2xl p-3.5" aria-busy={busy}>
      <p className="flex items-center gap-1.5 text-[15px] font-semibold">
        <Lock className="h-4 w-4 shrink-0" aria-hidden="true" /> {card.service} 的{what}
      </p>
      {card.reason && <p className="c-muted mt-0.5 text-[13px] leading-snug">{card.reason}</p>}
      <div className="mt-3">
        {saved ? (
          <p className="flex items-center gap-1.5 text-[14px] font-medium" style={{ color: "var(--c-online)" }}>
            <Check className="h-4 w-4" aria-hidden="true" /> 已加密保存
          </p>
        ) : (
          <form className="flex flex-col gap-2.5" onSubmit={(event) => void save(event)}>
            {card.kind === "login" ? (
              <>
                <input
                  value={site}
                  onChange={(e) => setSite(e.target.value)}
                  placeholder="网站地址，比如 github.com"
                  aria-label="网站地址"
                  inputMode="url"
                  autoComplete="off"
                  className={FIELD_CLASS}
                />
                <input
                  value={identifier}
                  onChange={(e) => setIdentifier(e.target.value)}
                  placeholder="账号（邮箱、手机号或用户名）"
                  aria-label="账号"
                  autoComplete="username"
                  className={FIELD_CLASS}
                />
              </>
            ) : (
              <>
                <input
                  value={label}
                  onChange={(e) => setLabel(e.target.value)}
                  placeholder="名称，比如 GitHub"
                  aria-label="名称"
                  className={FIELD_CLASS}
                />
                <input
                  value={variable}
                  onChange={(e) => setVariable(e.target.value.toUpperCase())}
                  placeholder="变量名，比如 GITHUB_TOKEN"
                  aria-label="变量名"
                  autoCapitalize="characters"
                  autoComplete="off"
                  className={`${FIELD_CLASS} font-mono`}
                />
              </>
            )}
            <div className="relative flex items-center">
              <input
                value={secret}
                onChange={(e) => setSecret(e.target.value)}
                type={showSecret ? "text" : "password"}
                placeholder={secretLabel}
                aria-label={secretLabel}
                autoComplete={card.kind === "login" ? "current-password" : "off"}
                className={`${FIELD_CLASS} pr-12`}
              />
              <button
                type="button"
                aria-label={showSecret ? `隐藏${secretLabel}` : `显示${secretLabel}`}
                aria-pressed={showSecret}
                onClick={() => setShowSecret((shown) => !shown)}
                className={`c-muted c-pressable absolute right-0 top-1/2 grid min-h-11 min-w-11 -translate-y-1/2 place-items-center rounded-full motion-reduce:transition-none ${FOCUS_RING}`}
              >
                {showSecret ? <EyeOff className="h-4 w-4" aria-hidden="true" /> : <Eye className="h-4 w-4" aria-hidden="true" />}
              </button>
            </div>
            {error && (
              <div className="flex items-start gap-2 rounded-xl px-3 py-2 text-[13px]" style={{ background: "var(--c-fill)" }} role="alert">
                <CircleAlert className="c-danger mt-0.5 h-4 w-4 shrink-0" aria-hidden="true" />
                <span className="c-danger min-w-0 flex-1 leading-snug">{error}</span>
              </div>
            )}
            <button
              type="submit"
              disabled={!ready || busy}
              className={`c-primary inline-flex min-h-11 items-center justify-center gap-1.5 rounded-full text-[15px] font-semibold transition active:scale-[0.98] disabled:opacity-40 motion-reduce:transition-none motion-reduce:active:scale-100 ${FOCUS_RING}`}
            >
              {busy && <Loader2 className="h-4 w-4 animate-spin motion-reduce:animate-none" aria-hidden="true" />}
              {busy ? "保存中…" : "加密保存"}
            </button>
            <p className="c-muted text-[12px] leading-snug">只存在你的服务器上，加密保存。它只能拿来帮你在对应的网站或服务上用，看不到内容。</p>
          </form>
        )}
      </div>
    </div>
  );
}
