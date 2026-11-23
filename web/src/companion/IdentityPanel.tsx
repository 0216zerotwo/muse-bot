import type { CompanionChanges, CompanionState, CompanionTone } from "./companion-api";

/** Same keys as ``TONES`` on the server; the sample shows how a reply would sound. */
export const TONE_OPTIONS: { key: CompanionTone; label: string; sample: string }[] = [
  { key: "warm", label: "温暖贴心", sample: "今天辛苦啦，晚饭吃了吗？" },
  { key: "concise", label: "简洁干脆", sample: "已订好：周六 10:00。" },
  { key: "playful", label: "活泼有趣", sample: "搞定！周六爬山，记得带水哦 🎒" },
  { key: "steady", label: "沉稳靠谱", sample: "已为你预约周六 10:00，详情如下。" },
];

const INPUT =
  "c-card min-h-12 rounded-2xl px-4 text-[16px] outline-none focus-visible:outline-2 focus-visible:outline-[color:var(--c-primary)]";

/** Editable copy of the persona fields, shared by the sheet and first-run setup. */
export interface PersonaDraft {
  name: string;
  /** The whole avatar value, e.g. ``pet:orange-cat`` or ``image:<id>``. */
  avatar: string;
  tone: CompanionTone;
  userName: string;
  styleNote: string;
}

export function personaDraft(companion: CompanionState): PersonaDraft {
  return {
    name: companion.name,
    avatar: companion.avatar,
    tone: companion.tone,
    userName: companion.user_name ?? "",
    styleNote: companion.style_note ?? "",
  };
}

export function personaChanges(draft: PersonaDraft): CompanionChanges {
  return {
    name: draft.name.trim(),
    avatar: draft.avatar,
    tone: draft.tone,
    user_name: draft.userName.trim() || null,
    style_note: draft.styleNote.trim() || null,
  };
}

type DraftProps = { draft: PersonaDraft; onChange: (draft: PersonaDraft) => void };

/** Its name. (Its look starts as Adam; to change it, the user describes a new one in the chat.) */
export function NameAndLookFields({ draft, onChange }: DraftProps) {
  return (
    <>
      <label className="flex flex-col gap-1.5">
        <span className="text-[14px] font-medium">名字</span>
        <input
          value={draft.name}
          onChange={(event) => onChange({ ...draft, name: event.target.value })}
          maxLength={40}
          className={INPUT}
        />
      </label>
    </>
  );
}

export function PersonalityFields({ draft, onChange, showNote = true }: DraftProps & { showNote?: boolean }) {
  return (
    <>
      <fieldset>
        <legend className="mb-2 text-[14px] font-medium">说话方式</legend>
        <div className="grid grid-cols-2 gap-2" role="radiogroup">
          {TONE_OPTIONS.map((option) => (
            <button
              key={option.key}
              type="button"
              role="radio"
              aria-checked={draft.tone === option.key}
              onClick={() => onChange({ ...draft, tone: option.key })}
              className="c-pressable flex min-h-[76px] flex-col items-start gap-1 rounded-2xl px-3.5 py-3 text-left"
              style={{
                background: "var(--c-fill)",
                boxShadow: draft.tone === option.key ? "inset 0 0 0 2px var(--c-primary)" : undefined,
              }}
            >
              <span className="text-[15px] font-semibold">{option.label}</span>
              <span className="c-muted text-[13px] leading-snug">“{option.sample}”</span>
            </button>
          ))}
        </div>
      </fieldset>
      <label className="flex flex-col gap-1.5">
        <span className="text-[14px] font-medium">
          它怎么称呼你<span className="c-muted font-normal">（可选）</span>
        </span>
        <input
          value={draft.userName}
          onChange={(event) => onChange({ ...draft, userName: event.target.value })}
          maxLength={20}
          placeholder="比如：东东"
          className={INPUT}
        />
      </label>
      {showNote && (
        <label className="flex flex-col gap-1.5">
          <span className="text-[14px] font-medium">
            还想让它注意什么<span className="c-muted font-normal">（可选）</span>
          </span>
          <textarea
            value={draft.styleNote}
            onChange={(event) => onChange({ ...draft, styleNote: event.target.value })}
            maxLength={300}
            rows={3}
            placeholder="比如：回复短一点；别用英文缩写"
            className="c-card rounded-2xl px-4 py-3 text-[16px] leading-relaxed outline-none focus-visible:outline-2 focus-visible:outline-[color:var(--c-primary)]"
          />
        </label>
      )}
    </>
  );
}
