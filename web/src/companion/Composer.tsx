import { useCallback, useEffect, useRef, useState, type KeyboardEvent } from "react";
import { ArrowUp, FileText, Loader2, Mic, Plus, Quote, Square, X } from "lucide-react";

import type { SendAttachment } from "@/hooks/useNanobotStream";
import { useVoiceRecorder, type VoiceRecorderErrorKey } from "@/hooks/useVoiceRecorder";
import { encodeImage } from "@/lib/imageEncode";

import { useClient } from "@/providers/ClientProvider";

import { androidShell, type PickedFile } from "./android-shell";
import { fetchHelpers, fetchSkills, type Helper, type SkillInfo } from "./companion-api";
import { GOAL_COMMAND } from "./GoalBanner";

/**
 * What "/" offers at the start of a message: a way of working (long task, tidying the context),
 * or a request it fills in for the user to finish. ``text`` replaces the box.
 */
export const SLASH_PRESETS: { key: string; label: string; detail: string; text: string }[] = [
  { key: "goal", label: "长任务", detail: "一直做到完成，中途不用你盯着", text: GOAL_COMMAND },
  { key: "compact", label: "整理上下文", detail: "聊得太长时压缩一下，更快也更省", text: "/compact" },
  { key: "draw", label: "画一张图", detail: "用生图模型画出来", text: "画一张图：" },
  { key: "search", label: "联网查", detail: "搜最新的信息，给出处", text: "联网查一下：" },
  { key: "routine", label: "设个例行", detail: "定时帮你做一件事", text: "帮我设一个定时任务：" },
];

const VOICE_ERRORS: Record<VoiceRecorderErrorKey, string> = {
  failed: "语音识别失败，再试一次",
  insecureContext: "需要 HTTPS 才能用语音",
  noDevice: "没有找到麦克风",
  notConfigured: "还没开启语音识别，先打字跟我说吧",
  permission: "没有麦克风权限",
  tooLong: "说得太久了，分几段说吧",
  tooShort: "多说一会儿再松开",
  unsupported: "这个浏览器不支持语音输入",
};

const MAX_TEXTAREA_PX = 132;
const MAX_FILES = 6;
const MAX_FILE_BYTES = 20 * 1024 * 1024;

function readAsDataUrl(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result));
    reader.onerror = () => reject(reader.error);
    reader.readAsDataURL(file);
  });
}
const BLUR_SETTLE_MS = 180;

/** Keep focus (and the iOS keyboard) in the field when tapping composer buttons. */
const keepFocus = (event: { preventDefault: () => void }) => event.preventDefault();

function tapHaptic(ms = 8): void {
  try {
    navigator.vibrate?.(ms);
  } catch {
    // Ignore environments where vibration is unsupported or blocked.
  }
}

export function Composer({
  value,
  onChange,
  onSend,
  onStop,
  streaming,
  connected,
  transcribeAudio,
  onFocusChange,
  focusRequest,
  quote,
  onClearQuote,
  voiceEnabled = true,
}: {
  value: string;
  onChange: (next: string) => void;
  /** ``spoken``: the text came from voice input (its answer may be read out). */
  onSend: (text: string, images: SendAttachment[], spoken?: boolean) => void;
  onStop: () => void;
  streaming: boolean;
  connected: boolean;
  transcribeAudio: (dataUrl: string, options?: { durationMs?: number }) => Promise<string>;
  onFocusChange?: (focused: boolean) => void;
  /** Changes when the box was filled in for the user to finish (换形象): focus it, the caret at the end. */
  focusRequest?: number;
  /** What this message answers (long-press → 引用), shown above the box until sent or cleared. */
  quote?: string | null;
  onClearQuote?: () => void;
  /** False when backend reports STT capability is unavailable; hides the guaranteed-to-fail mic button. */
  voiceEnabled?: boolean;
}) {
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  useEffect(() => {
    const box = textareaRef.current;
    if (!focusRequest || !box) return;
    box.focus();
    box.setSelectionRange(box.value.length, box.value.length);
  }, [focusRequest]);
  const fileRef = useRef<HTMLInputElement>(null);
  const blurTimerRef = useRef<number | null>(null);
  const [images, setImages] = useState<SendAttachment[]>([]);
  const [notice, setNotice] = useState<string | null>(null);

  /* @名字: name a helper to hand this to. Typing "@" lists them; picking one fills its name in. */
  const { token } = useClient();
  const [helpers, setHelpers] = useState<Helper[] | null>(null);
  const mention = /(?:^|\s)@([^\s@]{0,12})$/.exec(value)?.[1];
  const mentionOpen = mention !== undefined;
  useEffect(() => {
    if (!mentionOpen) return;
    let cancelled = false;
    fetchHelpers(token)
      .then((payload) => {
        if (!cancelled) setHelpers(payload.helpers);
      })
      .catch(() => {
        if (!cancelled) setHelpers((prev) => prev ?? []);
      });
    return () => {
      cancelled = true;
    };
  }, [mentionOpen, token]);
  const mentionable = mention === undefined ? [] : (helpers ?? []).filter((h) => h.name.includes(mention)).slice(0, 5);
  const pickHelper = (helper: Helper) => {
    onChange(value.replace(/@[^\s@]{0,12}$/, `@${helper.name} `));
    textareaRef.current?.focus();
  };

  /* "/" at the start: built-in presets plus skills the companion has learned. */
  const [skills, setSkills] = useState<SkillInfo[] | null>(null);
  const slash = /^\/(\S{0,20})$/.exec(value)?.[1];
  const slashOpen = slash !== undefined;
  useEffect(() => {
    if (!slashOpen) return;
    let cancelled = false;
    fetchSkills(token)
      .then((payload) => {
        if (!cancelled) setSkills(payload.learned);
      })
      .catch(() => {
        if (!cancelled) setSkills((prev) => prev ?? []);
      });
    return () => {
      cancelled = true;
    };
  }, [slashOpen, token]);
  const slashItems = [
    ...SLASH_PRESETS,
    ...(skills ?? [])
      .filter((s) => !SLASH_PRESETS.some((p) => p.key === s.name))
      .map((s) => ({
        key: s.name,
        label: s.name,
        detail: s.description || "学会的技能",
        text: `用「${s.name}」技能帮我：`,
      })),
  ];
  const pickable =
    slash === undefined
      ? []
      : slashItems.filter(
          (p) => !slash || p.key.toLowerCase().includes(slash.toLowerCase()) || p.label.toLowerCase().includes(slash.toLowerCase()),
        );
  const pickPreset = (preset: (typeof SLASH_PRESETS)[number]) => {
    onChange(preset.text);
    textareaRef.current?.focus();
  };

  useEffect(() => () => {
    if (blurTimerRef.current !== null) window.clearTimeout(blurTimerRef.current);
  }, []);

  // Grow with the text up to a few lines, like Messages.
  useEffect(() => {
    const el = textareaRef.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, MAX_TEXTAREA_PX)}px`;
  }, [value]);

  // Whether what is in the box was said rather than typed (cleared with the box).
  const dictated = useRef(false);
  useEffect(() => {
    if (!value) dictated.current = false;
  }, [value]);

  const voice = useVoiceRecorder({
    disabled: !connected,
    onClearError: () => setNotice(null),
    onError: (key) => setNotice(VOICE_ERRORS[key] ?? VOICE_ERRORS.failed),
    onTranscript: (text) => {
      dictated.current = true;
      onChange(value ? `${value} ${text}` : text);
    },
    onTranscribeAudio: transcribeAudio,
  });

  const canSend = connected && !streaming && (value.trim().length > 0 || images.length > 0);

  const submit = useCallback(() => {
    if (!canSend) return;
    tapHaptic(8);
    onSend(value.trim(), images, dictated.current);
    setImages([]);
    setNotice(null);
  }, [canSend, images, onSend, value]);

  const onKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    // Touch keyboards insert newlines; hardware keyboards send on Enter.
    const coarse = window.matchMedia?.("(any-pointer: coarse)").matches;
    if (event.key === "Enter" && !event.shiftKey && !coarse && !event.nativeEvent.isComposing) {
      event.preventDefault();
      submit();
    }
  };

  /** Any file: pictures are shrunk for the trip, everything else goes as it is (up to 20 MB). */
  const addFiles = async (files: FileList | File[] | null) => {
    if (!files?.length) return;
    const room = MAX_FILES - images.length;
    const picked = Array.from(files).slice(0, room);
    const encoded: SendAttachment[] = [];
    for (const file of picked) {
      if (file.type.startsWith("image/") && file.type !== "image/svg+xml") {
        const result = await encodeImage(file);
        if (result.ok) {
          encoded.push({
            media: { data_url: result.dataUrl, name: file.name },
            preview: { kind: "image", url: result.dataUrl, name: file.name },
          });
          continue;
        }
      }
      if (file.size > MAX_FILE_BYTES) {
        setNotice(`「${file.name}」超过 20 MB，发不了`);
        continue;
      }
      try {
        const dataUrl = await readAsDataUrl(file);
        // Some files come without a type: send them as plain bytes.
        const typed = dataUrl.startsWith("data:;") ? dataUrl.replace("data:;", "data:application/octet-stream;") : dataUrl;
        encoded.push({ media: { data_url: typed, name: file.name }, preview: { kind: "file", url: typed, name: file.name } });
      } catch {
        setNotice(`「${file.name}」读不了，换一个试试`);
      }
    }
    if (files.length > room) setNotice(`一次最多 ${MAX_FILES} 个文件`);
    setImages((current) => [...current, ...encoded]);
  };

  /* The Android app's ＋ sheet (camera, latest photos, files): it hands over what was picked, and
     the page fetches each file from the app itself. Browsers use the file input below. */
  const addFilesRef = useRef(addFiles);
  addFilesRef.current = addFiles;
  useEffect(() => {
    const take = (event: Event) => {
      const picked = (event as CustomEvent<PickedFile[]>).detail ?? [];
      void Promise.all(
        picked.map(async (file) => {
          const blob = await (await fetch(file.url)).blob();
          return new File([blob], file.name, { type: file.type || blob.type });
        }),
      )
        .then((files) => addFilesRef.current(files))
        .catch(() => setNotice("这个文件读不了，换一个试试"));
    };
    window.addEventListener("companion:attach", take);
    return () => window.removeEventListener("companion:attach", take);
  }, []);
  const pickFiles = () => {
    const android = androidShell();
    if (android?.attach) android.attach();
    else fileRef.current?.click();
  };

  const recording = voice.isRecording;
  const transcribing = voice.state === "transcribing";

  return (
    <div className="flex flex-col gap-1.5">
      {notice && (
        <p className="c-float mx-auto rounded-full px-3 py-1.5 text-[13px]" role="alert">
          {notice}
        </p>
      )}
      {pickable.length > 0 && (
        <ul className="c-float max-h-[40vh] overflow-y-auto rounded-[22px] py-1" aria-label="快捷指令">
          {pickable.map((preset) => (
            <li key={preset.key}>
              <button
                type="button"
                onPointerDown={keepFocus}
                onClick={() => pickPreset(preset)}
                className="c-pressable flex w-full items-baseline gap-2 px-4 py-2 text-left"
              >
                <span className="text-[15px] font-medium">{preset.label}</span>
                <span className="c-muted min-w-0 flex-1 truncate text-[12.5px]">{preset.detail}</span>
                <span className="c-muted shrink-0 font-mono text-[12px]">/{preset.key}</span>
              </button>
            </li>
          ))}
        </ul>
      )}
      {mentionable.length > 0 && (
        <ul className="c-float overflow-hidden rounded-[22px] py-1" aria-label="叫哪个帮手">
          {mentionable.map((helper) => (
            <li key={helper.id}>
              <button
                type="button"
                onPointerDown={keepFocus}
                onClick={() => pickHelper(helper)}
                className="c-pressable flex w-full items-center gap-3 px-4 py-2 text-left"
              >
                <span className="text-[20px]" aria-hidden="true">{helper.emoji}</span>
                <span className="min-w-0 flex-1">
                  <span className="block text-[15px] font-medium">{helper.name}</span>
                  {helper.title && <span className="c-muted block truncate text-[12.5px]">{helper.title}</span>}
                </span>
              </button>
            </li>
          ))}
        </ul>
      )}
      <div className="c-float flex flex-col rounded-[26px]">
        {quote && (
          <div className="flex items-center gap-2 px-3 pt-2.5">
            <Quote className="c-muted h-3.5 w-3.5 shrink-0" aria-hidden="true" />
            <span className="c-muted min-w-0 flex-1 truncate text-[13px]">{quote}</span>
            <button
              type="button"
              aria-label="不引用了"
              onPointerDown={keepFocus}
              onClick={onClearQuote}
              className="c-pressable grid h-8 w-8 place-items-center rounded-full"
            >
              <X className="h-3.5 w-3.5" aria-hidden="true" />
            </button>
          </div>
        )}
        {images.length > 0 && (
          <div className="flex gap-2 overflow-x-auto px-3 pt-3">
            {images.map((image, index) => (
              <div key={index} className="c-rise relative shrink-0 motion-reduce:animate-none">
                {image.preview.kind === "image" ? (
                  <img src={image.preview.url} alt={image.preview.name ?? "图片"} className="h-16 w-16 rounded-2xl object-cover" />
                ) : (
                  <span className="flex h-16 w-28 flex-col justify-center gap-1 rounded-2xl px-2.5" style={{ background: "var(--c-fill)" }}>
                    <FileText className="c-muted h-4 w-4" aria-hidden="true" />
                    <span className="line-clamp-2 text-[11.5px] leading-tight">{image.preview.name}</span>
                  </span>
                )}
                <button
                  type="button"
                  aria-label="移除"
                  title={image.preview.name ? `移除 ${image.preview.name}` : "移除"}
                  onClick={() => setImages((current) => current.filter((_, i) => i !== index))}
                  className="c-float absolute -right-1.5 -top-1.5 grid h-6 w-6 place-items-center rounded-full transition-transform active:scale-90 after:absolute after:-inset-2 after:content-[''] motion-reduce:transition-none"
                >
                  <X className="h-3.5 w-3.5" aria-hidden="true" />
                </button>
              </div>
            ))}
          </div>
        )}
        <div className="flex items-end gap-1 p-1">
          <button
            type="button"
            aria-label="添加文件"
            onClick={pickFiles}
            disabled={!connected}
            className="c-pressable grid h-11 w-11 shrink-0 place-items-center rounded-full transition-transform active:scale-95 disabled:opacity-40 motion-reduce:transition-none"
          >
            <Plus className="h-5 w-5" aria-hidden="true" />
          </button>
          <input
            ref={fileRef}
            type="file"
            multiple
            hidden
            onChange={(event) => {
              void addFiles(event.target.files);
              event.target.value = "";
            }}
          />
          {recording ? (
            <p className="c-danger flex h-11 flex-1 items-center gap-2 text-[15px]" role="status">
              <span className="c-dot h-2 w-2 rounded-full bg-current" aria-hidden="true" />
              正在听… {voice.elapsedLabel}
            </p>
          ) : (
            <textarea
              ref={textareaRef}
              rows={1}
              value={value}
              onChange={(event) => onChange(event.target.value)}
              onKeyDown={onKeyDown}
              onFocus={() => {
                if (blurTimerRef.current !== null) window.clearTimeout(blurTimerRef.current);
                onFocusChange?.(true);
              }}
              // Report blur a beat later: showing the tab bar immediately would
              // move the composer while a tap on it is still in progress.
              onBlur={() => {
                blurTimerRef.current = window.setTimeout(() => onFocusChange?.(false), BLUR_SETTLE_MS);
              }}
              placeholder={transcribing ? "正在转成文字…" : value.startsWith(GOAL_COMMAND) ? "想达成什么？说得越具体越好" : "发消息"}
              aria-label="发消息"
              // 16px keeps iOS from zooming into the field on focus.
              className="min-h-11 flex-1 resize-none bg-transparent px-1 py-[11px] text-[16px] leading-[22px] outline-none placeholder:text-[color:var(--c-text-muted)]"
            />
          )}
          {streaming ? (
            <button
              type="button"
              aria-label="停止"
              onPointerDown={keepFocus}
              onClick={() => {
                tapHaptic(10);
                onStop();
              }}
              className="c-pressable group grid h-11 w-11 shrink-0 place-items-center rounded-full"
            >
              <span
                className="grid h-8 w-8 place-items-center rounded-full transition-transform duration-150 group-active:scale-90 motion-reduce:transition-none"
                style={{ background: "var(--c-danger-fill)" }}
              >
                <Square className="h-3 w-3 text-white" style={{ fill: "currentColor" }} aria-hidden="true" />
              </span>
            </button>
          ) : canSend ? (
            <button
              type="button"
              aria-label="发送"
              onPointerDown={keepFocus}
              onClick={submit}
              className="c-pressable group grid h-11 w-11 shrink-0 place-items-center rounded-full"
            >
              <span className="c-primary grid h-8 w-8 place-items-center rounded-full shadow-sm transition-transform duration-150 group-active:scale-90 motion-reduce:transition-none">
                <ArrowUp className="h-[18px] w-[18px]" aria-hidden="true" />
              </span>
            </button>
          ) : voiceEnabled ? (
            <button
              type="button"
              aria-label={recording ? "停止录音" : "语音输入"}
              onPointerDown={keepFocus}
              aria-pressed={recording}
              onClick={voice.handleClick}
              disabled={voice.buttonDisabled}
              className={`c-pressable grid h-11 w-11 shrink-0 place-items-center rounded-full transition-transform active:scale-95 disabled:opacity-40 motion-reduce:transition-none ${recording ? "c-danger" : ""}`}
            >
              {transcribing ? (
                <Loader2 className="h-5 w-5 animate-spin motion-reduce:animate-none" aria-hidden="true" />
              ) : (
                <Mic className="h-5 w-5" aria-hidden="true" />
              )}
            </button>
          ) : null}
        </div>
      </div>
    </div>
  );
}
