/* A side chat's settings: its name, export, and deleting it. */
import { useEffect, useState } from "react";

import type { ChatSummary } from "@/lib/types";

import { MuseSheet } from "./Sheet";

export function SideChatSheet({
  chat,
  onClose,
  onRename,
  onDelete,
  onExport,
}: {
  chat: ChatSummary | null;
  onClose: () => void;
  onRename: (chatId: string, title: string) => void;
  onDelete: (chatId: string) => void;
  onExport?: (chat: ChatSummary) => Promise<void> | void;
}) {
  const [name, setName] = useState("");
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [exporting, setExporting] = useState(false);
  useEffect(() => {
    if (!chat) return;
    setName(chat.title || "");
    setConfirmDelete(false);
    setExporting(false);
  }, [chat]);

  return (
    <MuseSheet open={chat !== null} onClose={onClose} title="旁聊设置">
      {chat && (
        <div className="flex flex-col gap-3 pb-2">
          <input
            value={name}
            onChange={(e) => setName(e.target.value)}
            maxLength={30}
            placeholder="名字"
            aria-label="旁聊的名字"
            className="c-card min-h-11 w-full rounded-2xl px-3.5 text-[16px] outline-none"
          />
          <button
            type="button"
            disabled={!name.trim() || name.trim() === (chat.title || "")}
            onClick={() => {
              onRename(chat.chatId, name.trim());
              onClose();
            }}
            className="c-primary min-h-11 rounded-full text-[15px] font-semibold disabled:opacity-40"
          >
            改名
          </button>
          {onExport && (
            <button
              type="button"
              disabled={exporting}
              onClick={async () => {
                setExporting(true);
                try {
                  await onExport(chat);
                } finally {
                  setExporting(false);
                }
              }}
              className="c-pressable min-h-11 rounded-full text-[15px] font-medium disabled:opacity-50"
              style={{ background: "var(--c-fill)" }}
            >
              {exporting ? "正在导出…" : "导出对话 (.md)"}
            </button>
          )}
          {confirmDelete ? (
            <div className="flex gap-2">
              <button type="button" onClick={() => setConfirmDelete(false)} className="c-pressable min-h-11 flex-1 rounded-full text-[15px]" style={{ background: "var(--c-fill)" }}>
                取消
              </button>
              <button
                type="button"
                onClick={() => {
                  onDelete(chat.chatId);
                  onClose();
                }}
                className="min-h-11 flex-1 rounded-full text-[15px] font-semibold text-white"
                style={{ background: "var(--c-danger-fill)" }}
              >
                删除
              </button>
            </div>
          ) : (
            <button type="button" onClick={() => setConfirmDelete(true)} className="c-danger min-h-10 text-[14px]">
              删除这个旁聊
            </button>
          )}
        </div>
      )}
    </MuseSheet>
  );
}
