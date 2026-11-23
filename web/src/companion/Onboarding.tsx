import { useEffect, useMemo, useState, type ReactNode } from "react";
import { Bell, Check, ChevronLeft, Plus } from "lucide-react";

import { fetchMcpPresets } from "@/lib/api";
import type { McpPresetInfo } from "@/lib/types";
import { useClient } from "@/providers/ClientProvider";

import { AddModelSheet } from "./AddModelSheet";
import { deviceTimezone, fetchModels, updateCompanionState, type CompanionState, type Routines } from "./companion-api";
import { ConnectSheet, recommendedPresets, RecommendedServices } from "./ConnectSheet";
import { NameAndLookFields, PersonalityFields, personaChanges, personaDraft } from "./IdentityPanel";
import { PetView } from "./Pet";
import { DEFAULT_BRIEF_TIME, RoutineFields, useSaveRoutines } from "./ProactivePanel";

const BASE_STEPS = ["welcome", "look", "personality", "proactive", "connect"] as const;
const MODEL_STEPS = ["welcome", "look", "personality", "model", "proactive", "connect"] as const;
type Step = (typeof MODEL_STEPS)[number];

/**
 * First-run setup, like Muse's: name it, pick how it talks, decide when it may
 * reach out, and connect the first services. Every step can be changed later
 * from the menu, so "跳过" finishes right away without changing anything.
 */
export function Onboarding({
  companion,
  onDone,
}: {
  companion: CompanionState;
  onDone: (companion: CompanionState) => void;
}) {
  const { client, token } = useClient();
  const saveRoutines = useSaveRoutines();
  const [step, setStep] = useState<Step>("welcome");
  const [draft, setDraft] = useState(() => personaDraft(companion));
  const [routines, setRoutines] = useState<Routines>({ brief_time: DEFAULT_BRIEF_TIME, checkins: true });
  const [needsModel, setNeedsModel] = useState(false);
  const [addedModels, setAddedModels] = useState<string[]>([]);
  const [addingModel, setAddingModel] = useState(false);
  const [notifPerm, setNotifPerm] = useState<NotificationPermission | "unsupported">(() =>
    typeof Notification === "undefined" ? "unsupported" : Notification.permission,
  );
  const [presets, setPresets] = useState<McpPresetInfo[] | null>(null);
  const [connecting, setConnecting] = useState<McpPresetInfo | null>(null);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    fetchModels(token)
      .then((payload) => {
        if (cancelled) return;
        if (payload.presets.length === 0) setNeedsModel(true);
        else setAddedModels(payload.presets.map((m) => m.name));
      })
      .catch(() => {
        // Offline or mocked environment without models endpoint: keep standard steps.
      });
    return () => {
      cancelled = true;
    };
  }, [token]);

  useEffect(() => {
    if (step !== "connect") return;
    let cancelled = false;
    fetchMcpPresets(token)
      .then((payload) => {
        if (!cancelled) setPresets(payload.presets);
      })
      .catch(() => {
        if (!cancelled) setPresets([]);
      });
    return () => {
      cancelled = true;
    };
  }, [step, token]);

  const steps: readonly Step[] = useMemo(() => (needsModel ? MODEL_STEPS : BASE_STEPS), [needsModel]);
  const index = Math.max(0, steps.indexOf(step));
  const next = () => setStep(steps[Math.min(index + 1, steps.length - 1)]);
  const back = () => setStep(steps[Math.max(index - 1, 0)]);

  const finish = async (keep: boolean) => {
    setSaving(true);
    setError(null);
    try {
      if (keep) await saveRoutines(routines);
      const timezone = deviceTimezone();
      const saved = await updateCompanionState(client, {
        ...(keep ? personaChanges(draft) : {}),
        ...(timezone ? { timezone } : {}),
        onboarded: true,
      });
      onDone(saved);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setSaving(false);
    }
  };

  const name = draft.name.trim() || companion.name;

  return (
    <div className="flex h-full flex-col" style={{ background: "var(--c-bg)" }}>
      <header
        className="flex min-h-14 items-center justify-between px-3"
        style={{ paddingTop: "env(safe-area-inset-top)" }}
      >
        {index > 0 ? (
          <button type="button" onClick={back} className="c-pressable grid h-11 w-11 place-items-center rounded-full" aria-label="上一步">
            <ChevronLeft className="h-5 w-5" aria-hidden="true" />
          </button>
        ) : (
          <span className="h-11 w-11" />
        )}
        <div className="flex gap-1.5" aria-label={`第 ${index + 1} 步，共 ${steps.length} 步`}>
          {steps.map((key, i) => (
            <span
              key={key}
              className="h-1.5 w-5 rounded-full"
              style={{ background: i <= index ? "var(--c-primary)" : "var(--c-border)" }}
              aria-hidden="true"
            />
          ))}
        </div>
        <button
          type="button"
          onClick={() => void finish(false)}
          disabled={saving}
          className="c-muted c-pressable min-h-11 rounded-full px-3 text-[15px]"
        >
          跳过
        </button>
      </header>

      <main className="min-h-0 flex-1 overflow-y-auto px-5 pb-4">
        {step === "welcome" && (
          <StepIntro mascot={<PetView value={draft.avatar} animation="waving" size={120} />} title="嗨，我是你的私人伙伴">
            我能替你查资料、写邮件、管日程，会记住你在意的事，也会在合适的时候主动找你。先花一分钟认识一下？
          </StepIntro>
        )}
        {step === "look" && (
          <div className="flex flex-col gap-5">
            <StepTitle title="给我起个名字吧" subtitle="我会用这个名字称呼自己。想换个样子，以后跟我说“把虚拟形象改为……”就行。" />
            <NameAndLookFields draft={draft} onChange={setDraft} />
          </div>
        )}
        {step === "personality" && (
          <div className="flex flex-col gap-5">
            <StepTitle title={`你希望${name}怎么说话？`} subtitle="随时可以在菜单里改。" />
            <PersonalityFields draft={draft} onChange={setDraft} showNote={false} />
          </div>
        )}
        {step === "model" && (
          <div className="flex flex-col gap-5">
            <StepTitle
              title={`先给${name}配一个对话模型`}
              subtitle="还没有配置可用的对话模型。选一家常用平台填上 API key，或直接用会员账号登录。"
            />
            {addedModels.length > 0 && (
              <div className="c-card flex items-center gap-2.5 rounded-2xl px-4 py-3 text-[15px]">
                <Check className="h-4 w-4 shrink-0" style={{ color: "var(--c-primary)" }} aria-hidden="true" />
                <span className="truncate">已配好：{addedModels.join("、")}</span>
              </div>
            )}
            <button
              type="button"
              onClick={() => setAddingModel(true)}
              className="c-card c-pressable flex min-h-12 items-center justify-center gap-2 rounded-2xl px-4 py-3 text-[15.5px] font-medium"
            >
              <Plus className="h-4 w-4" aria-hidden="true" />
              {addedModels.length > 0 ? "再加一个模型" : "选择并添加模型"}
            </button>
          </div>
        )}
        {step === "proactive" && (
          <div className="flex flex-col gap-5">
            <StepTitle title="要我主动找你吗？" subtitle="消息会出现在「动态」里，开了通知还会推送到手机。" />
            <RoutineFields value={routines} onChange={(changes) => setRoutines({ ...routines, ...changes })} />
            {(routines.brief_time !== null || routines.checkins) &&
              companion.capabilities?.push !== false &&
              notifPerm === "default" && (
                <div className="c-card flex items-center justify-between gap-3 rounded-2xl px-4 py-3">
                  <div className="min-w-0">
                    <p className="text-[15px] font-medium">顺便开启系统通知</p>
                    <p className="c-muted text-[13px]">早报或提醒发来时，在锁屏上叫你一声。</p>
                  </div>
                  <button
                    type="button"
                    onClick={() => {
                      void Notification.requestPermission()
                        .then((perm) => setNotifPerm(perm))
                        .catch(() => undefined);
                    }}
                    className="c-primary shrink-0 rounded-full px-3.5 py-2 text-[14px] font-semibold"
                  >
                    开启通知
                  </button>
                </div>
              )}
            {notifPerm === "granted" && (routines.brief_time !== null || routines.checkins) && (
              <p className="c-muted flex items-center gap-1.5 px-1 text-[13px]">
                <Bell className="h-3.5 w-3.5" aria-hidden="true" /> 已开启通知提醒
              </p>
            )}
          </div>
        )}
        {step === "connect" && (
          <div className="flex flex-col gap-5">
            <StepTitle
              title="连上你常用的服务"
              subtitle={`连上之后${name}就能直接替你看邮件、排日程、做视频。凭据只保存在你自己的 bot 上，现在不连也可以。`}
            />
            {presets === null ? (
              <p className="c-muted text-sm">正在读取…</p>
            ) : (
              <RecommendedServices presets={recommendedPresets(presets)} onConnect={setConnecting} />
            )}
          </div>
        )}
        {error && (
          <p className="c-danger mt-4 text-sm" role="alert">
            {error}
          </p>
        )}
      </main>

      <footer className="px-5 pt-2" style={{ paddingBottom: "max(1.25rem, env(safe-area-inset-bottom))" }}>
        <button
          type="button"
          disabled={saving || (step === "look" && !draft.name.trim())}
          onClick={() => (step === "connect" ? void finish(true) : next())}
          className="c-primary min-h-12 w-full rounded-full text-[16px] font-semibold disabled:opacity-50"
        >
          {step === "welcome" ? "开始" : step === "connect" ? (saving ? "保存中…" : "完成") : "下一步"}
        </button>
      </footer>

      <AddModelSheet
        open={addingModel}
        kind="text"
        onClose={() => setAddingModel(false)}
        onAdded={(payload) => {
          setAddedModels(payload.presets.map((m) => m.name));
          setAddingModel(false);
        }}
      />

      <ConnectSheet
        preset={connecting}
        onClose={() => setConnecting(null)}
        onChanged={(payload) => setPresets(payload.presets)}
      />
    </div>
  );
}

function StepTitle({ title, subtitle }: { title: string; subtitle: string }) {
  return (
    <div className="pt-2">
      <h1 className="text-[26px] font-bold leading-tight">{title}</h1>
      <p className="c-muted mt-1.5 text-[15px]">{subtitle}</p>
    </div>
  );
}

function StepIntro({ mascot, title, children }: { mascot: ReactNode; title: string; children: ReactNode }) {
  return (
    <div className="flex min-h-full flex-col items-center justify-center gap-4 text-center">
      {mascot}
      <h1 className="text-[26px] font-bold leading-tight">{title}</h1>
      <p className="c-muted max-w-sm text-[16px] leading-relaxed">{children}</p>
    </div>
  );
}
