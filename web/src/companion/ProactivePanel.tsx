import { useEffect, useState } from "react";

import { useClient } from "@/providers/ClientProvider";

import {
  deviceTimezone,
  fetchRoutines,
  updateCompanionState,
  updateRoutines,
  type Routines,
} from "./companion-api";
import { Switch } from "./Menu";
import { SkeletonCard } from "./Skeleton";

export const DEFAULT_BRIEF_TIME = "08:00";

/** Daily brief and check-ins; shared by the menu sheet and first-run setup. */
export function RoutineFields({
  value,
  onChange,
  disabled,
}: {
  value: Routines;
  onChange: (changes: Partial<Routines>) => void;
  disabled?: boolean;
}) {
  const briefOn = value.brief_time !== null;
  return (
    <div className="flex flex-col gap-3">
      <div className="c-card rounded-[22px] p-4">
        <div className="flex items-center gap-3">
          <div className="min-w-0 flex-1">
            <p className="text-[16px] font-semibold">每日早报</p>
            <p className="c-muted mt-0.5 text-[13px] leading-snug">今天的日程、重要邮件、该记得的事</p>
          </div>
          <Switch
            checked={briefOn}
            disabled={disabled}
            label="每日早报"
            onChange={(on) => onChange({ brief_time: on ? DEFAULT_BRIEF_TIME : null })}
          />
        </div>
        {briefOn && (
          <label className="mt-3 flex items-center justify-between gap-3 border-t pt-3 c-divider">
            <span className="text-[15px]">每天几点</span>
            <input
              type="time"
              step={300}
              value={value.brief_time ?? DEFAULT_BRIEF_TIME}
              disabled={disabled}
              onChange={(event) => event.target.value && onChange({ brief_time: event.target.value })}
              className="c-bubble-agent c-focus-ring min-h-11 rounded-xl px-3 text-[16px]"
            />
          </label>
        )}
      </div>
      <div className="c-card flex items-center gap-3 rounded-[22px] p-4">
        <div className="min-w-0 flex-1">
          <p className="text-[16px] font-semibold">主动关心</p>
          <p className="c-muted mt-0.5 text-[13px] leading-snug">
            白天大约每 3 小时看看它记得的事和最近的聊天，有值得说的（生日、约好的事、该跟进的事）才找你；晚上 10 点到早上 9 点不打扰。每次会用一点模型额度。
          </p>
        </div>
        <Switch
          checked={value.checkins}
          disabled={disabled}
          label="主动关心"
          onChange={(on) => onChange({ checkins: on })}
        />
      </div>
    </div>
  );
}

/**
 * Saves routines; a brief time is stored with this phone's timezone first, so
 * "8 点" means the user's 8 点 even when the bot runs on a server elsewhere.
 */
export function useSaveRoutines() {
  const { client } = useClient();
  return async (changes: Partial<Routines>): Promise<Routines> => {
    const timezone = deviceTimezone();
    if (changes.brief_time && timezone) await updateCompanionState(client, { timezone });
    return updateRoutines(client, changes);
  };
}

/** Daily brief and check-ins, saved as soon as they change. */
export function ProactivePanel() {
  const { token } = useClient();
  const save = useSaveRoutines();
  const [routines, setRoutines] = useState<Routines | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    fetchRoutines(token)
      .then((value) => {
        if (!cancelled) setRoutines(value);
      })
      .catch((e) => {
        if (!cancelled) setError(e instanceof Error ? e.message : String(e));
      });
    return () => {
      cancelled = true;
    };
  }, [token]);

  const change = async (changes: Partial<Routines>) => {
    if (!routines) return;
    setRoutines({ ...routines, ...changes });
    setBusy(true);
    setError(null);
    try {
      setRoutines(await save(changes));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setRoutines(await fetchRoutines(token).catch(() => routines));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div>
      <p className="c-muted mb-3 px-1 text-[13px] leading-snug">
        它会自己留意，在合适的时候找你。消息出现在「动态」里，开了通知还会推送到手机。
      </p>
      {routines ? (
        <RoutineFields value={routines} onChange={change} disabled={busy} />
      ) : (
        !error && (
          <div role="status" aria-busy="true" aria-label="正在读取主动提醒" className="flex flex-col gap-3">
            <SkeletonCard lines={2} status={false} />
            <SkeletonCard lines={2} status={false} />
          </div>
        )
      )}
      {error && (
        <p className="c-danger mt-3 text-sm" role="alert">
          {error}
        </p>
      )}
    </div>
  );
}
