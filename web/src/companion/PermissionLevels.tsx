/*
 * 设置 → 权限: for each kind of action, whether the companion (and its helpers) may do it on its
 * own — 禁止 / 询问我 / 允许, picked from a small dropdown on the row.
 */
import { Check, ChevronDown } from "lucide-react";
import { useEffect, useRef, useState } from "react";

import { useClient } from "@/providers/ClientProvider";

import { fetchPermissions, setPermission, type PermissionKind, type PermissionLevel } from "./companion-api";

const LEVEL_LABEL: Record<PermissionLevel, string> = { deny: "禁止", ask: "询问我", allow: "允许" };
const LEVEL_COLOR: Record<PermissionLevel, string | undefined> = { deny: "var(--c-danger)", ask: undefined, allow: "var(--c-primary)" };

function LevelPicker({ kind, onChange }: { kind: PermissionKind; onChange: (level: PermissionLevel) => void }) {
  const [open, setOpen] = useState(false);
  const box = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const away = (event: PointerEvent) => {
      if (!box.current?.contains(event.target as Node)) setOpen(false);
    };
    const onKey = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      event.preventDefault();
      event.stopPropagation();
      setOpen(false);
    };
    document.addEventListener("pointerdown", away);
    window.addEventListener("keydown", onKey, true);
    return () => {
      document.removeEventListener("pointerdown", away);
      window.removeEventListener("keydown", onKey, true);
    };
  }, [open]);

  return (
    <div ref={box} className="relative shrink-0">
      <button
        type="button"
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-label={`${kind.label}：${LEVEL_LABEL[kind.level]}`}
        onClick={() => setOpen((v) => !v)}
        className="c-pressable flex min-h-9 w-[5.5rem] items-center justify-between rounded-full pl-3.5 pr-2 text-[14px] font-medium"
        style={{ background: "var(--c-fill)", color: LEVEL_COLOR[kind.level] }}
      >
        {LEVEL_LABEL[kind.level]}
        <ChevronDown className={`h-4 w-4 transition-transform ${open ? "rotate-180" : ""}`} aria-hidden="true" />
      </button>
      {open && (
        <ul role="listbox" aria-label={kind.label} className="c-float absolute right-0 top-full z-30 mt-1.5 w-32 overflow-hidden rounded-2xl py-1 shadow-lg">
          {kind.levels.map((level) => (
            <li key={level}>
              <button
                type="button"
                role="option"
                aria-selected={kind.level === level}
                onClick={() => {
                  setOpen(false);
                  if (kind.level !== level) onChange(level);
                }}
                className="c-pressable flex min-h-11 w-full items-center gap-2 px-3.5 text-left text-[15px]"
                style={{ color: LEVEL_COLOR[level] }}
              >
                <span className="flex-1">{LEVEL_LABEL[level]}</span>
                {kind.level === level && <Check className="h-4 w-4" aria-hidden="true" />}
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

export function PermissionLevels() {
  const { client, token } = useClient();
  const [kinds, setKinds] = useState<PermissionKind[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    fetchPermissions(token)
      .then((p) => setKinds(p.kinds))
      .catch((e: unknown) => setError(e instanceof Error ? e.message : String(e)));
  }, [token]);

  const change = async (kind: PermissionKind, level: PermissionLevel) => {
    const previous = kinds;
    setError(null);
    setKinds((current) => current?.map((k) => (k.key === kind.key ? { ...k, level } : k)) ?? null);
    try {
      setKinds((await setPermission(client, kind.key, level)).kinds);
    } catch (e) {
      setKinds(previous);
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  return (
    <div className="flex flex-col gap-1.5">
      <p className="c-muted px-3.5 text-[13px] leading-snug">它和帮手做这些事之前，按这里的设置来。</p>
      {error && <p className="c-danger px-3.5 text-[13px]">{error}</p>}
      <div className="c-card divide-y rounded-2xl" style={{ borderColor: "var(--c-border)" }}>
        {(kinds ?? []).map((kind) => (
          <div key={kind.key} className="flex items-center gap-3 py-2.5 pl-4 pr-2.5">
            <div className="min-w-0 flex-1">
              <p className="text-[15px] font-medium">{kind.label}</p>
              <p className="c-muted line-clamp-2 text-[12.5px] leading-snug">{kind.detail}</p>
            </div>
            <LevelPicker kind={kind} onChange={(level) => void change(kind, level)} />
          </div>
        ))}
      </div>
    </div>
  );
}
