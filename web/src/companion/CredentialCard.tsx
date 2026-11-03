/*
 * A login or an API key the task needs, asked for on a card: the user types it here and it is
 * saved encrypted on the server (see VaultPanel) — it never goes into the chat. Once saved, the
 * companion is told to carry on.
 */
import { Check, Lock } from "lucide-react";
import { useState } from "react";

import type { Card } from "./replyCards";
import { CredentialForm } from "./VaultPanel";

export function CredentialCard({ card, send }: { card: Extract<Card, { type: "credential" }>; send?: (text: string) => void }) {
  const [saved, setSaved] = useState(false);
  const what = card.kind === "login" ? "登录" : "密钥";
  return (
    <div className="c-card w-full max-w-[340px] rounded-2xl p-3.5">
      <p className="flex items-center gap-1.5 text-[15px] font-semibold">
        <Lock className="h-4 w-4" aria-hidden="true" /> {card.service} 的{what}
      </p>
      {card.reason && <p className="c-muted mt-0.5 text-[13px]">{card.reason}</p>}
      <div className="mt-3">
        {saved ? (
          <p className="c-muted flex items-center gap-1.5 text-[14px]">
            <Check className="h-4 w-4" /> 已加密保存
          </p>
        ) : (
          <CredentialForm
            kind={card.kind}
            service={card.service}
            origin={card.origin}
            env={card.env}
            onSaved={() => {
              setSaved(true);
              send?.(`${card.service} 的${what}存好了，接着做`);
            }}
          />
        )}
      </div>
    </div>
  );
}
