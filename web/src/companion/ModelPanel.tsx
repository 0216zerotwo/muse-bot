import { useCallback, useEffect, useState } from "react";
import { Check, ChevronRight, Clapperboard, Eye, Footprints, ImageIcon, LifeBuoy, Plus, Sparkles, Trash2, Wand2 } from "lucide-react";

import { useClient } from "@/providers/ClientProvider";

import { AddModelSheet, KIND_LABELS, VendorLogo } from "./AddModelSheet";
import {
  checkModels,
  checkVendorModels,
  fetchModelCatalog,
  fetchModels,
  removeModel,
  updateModels,
  type MediaKind,
  type ModelInfo,
  type ModelKind,
  type ModelVendor,
  type ModelLimits,
  type ModelPreset,
  type ModelsChanges,
  type ModelsPayload,
  type ModelTier,
  type ReasoningEffort,
} from "./companion-api";
import { MenuGroup, MenuRow, Segmented, SelectRow, Switch } from "./Menu";
import { priceLine } from "./pricing";
import { dayLabel } from "./presentation";
import { Sheet } from "./Sheet";
import { SkeletonRows } from "./Skeleton";

const TIERS: { value: ModelTier; label: string }[] = [
  { value: "cheap", label: "便宜快速" },
  { value: "balanced", label: "均衡" },
  { value: "strong", label: "最强" },
];

/** "default" stands for no setting: the provider's own default thinking. */
type EffortChoice = ReasoningEffort | "default";

const EFFORTS: { value: EffortChoice; label: string }[] = [
  { value: "default", label: "默认" },
  { value: "none", label: "关闭" },
  { value: "low", label: "低" },
  { value: "medium", label: "中" },
  { value: "high", label: "高" },
  { value: "max", label: "最高" },
];

const REPLY_LENGTHS = [1024, 2048, 4096, 8192, 16384, 32768, 65536];

const STYLES: { value: string; label: string }[] = [
  { value: "0.1", label: "严谨" },
  { value: "0.5", label: "平衡" },
  { value: "0.9", label: "发散" },
];

const TOOL_STEPS = [20, 50, 100, 200, 500];

const EMPTY: ModelInfo = { description: "", tier: "balanced", vision: false, price_in: null, price_out: null };

function tokens(count: number): string {
  return count >= 1024 ? `${Math.round(count / 1024)}K` : String(count);
}

export function effortLabel(effort: ReasoningEffort | null): string {
  if (effort === null) return "默认";
  return EFFORTS.find((option) => option.value === effort)?.label ?? effort;
}

/** The models payload, reloaded when shown, with save/check helpers that keep it current. */
function useModels(active: boolean) {
  const { client, token } = useClient();
  const [data, setData] = useState<ModelsPayload | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!active) return;
    let cancelled = false;
    setError(null);
    fetchModels(token)
      .then((next) => !cancelled && setData(next))
      .catch((e) => !cancelled && setError(e instanceof Error ? e.message : String(e)));
    return () => {
      cancelled = true;
    };
  }, [active, token]);

  const run = useCallback(async (task: () => Promise<ModelsPayload>) => {
    setBusy(true);
    setError(null);
    try {
      setData(await task());
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }, []);

  const save = useCallback((changes: ModelsChanges) => run(() => updateModels(client, changes)), [client, run]);
  const check = useCallback((names?: string[]) => run(() => checkModels(client, names)), [client, run]);
  const remove = useCallback((name: string) => run(() => removeModel(client, name)), [client, run]);
  // 检测价格: read a company's platform; the models added from it take the prices found.
  const checkPrice = useCallback(
    (vendor: string, kind: ModelKind) => run(async () => {
      await checkVendorModels(client, vendor, kind);
      return fetchModels(token);
    }),
    [client, run, token],
  );
  return { data, busy, error, setError, save, check, remove, checkPrice, setData };
}

function StatusDot({ health }: { health: ModelsPayload["health"][string] | undefined }) {
  const color = !health ? "var(--c-text-muted)" : health.ok ? "var(--c-online)" : "var(--c-danger)";
  return <span className="h-2.5 w-2.5 rounded-full" style={{ background: color }} aria-hidden="true" />;
}

function Errors({ error }: { error: string | null }) {
  if (!error) return null;
  return (
    <p className="c-danger px-3.5 text-[13px]" role="alert">
      {error}
    </p>
  );
}

/** Company marks for the rows, by catalog id (loaded once; rows fall back to a plain dot). */
function useVendors(active: boolean) {
  const { token } = useClient();
  const [vendors, setVendors] = useState<Record<string, ModelVendor>>({});
  useEffect(() => {
    if (!active) return;
    fetchModelCatalog(token)
      .then((list) => setVendors(Object.fromEntries(list.map((v) => [v.id, v]))))
      .catch(() => setVendors({}));
  }, [active, token]);
  return vendors;
}

const MEDIA_COPY: Record<MediaKind, { use: string; empty: string; hint: string }> = {
  image: { use: "画图时用", empty: "还没有图像模型", hint: "添加一个，伙伴就能画图、给自己换形象。" },
  video: { use: "做视频时用", empty: "还没有视频模型", hint: "添加一个，伙伴就能把想法做成短视频。" },
};

/** 图像 / 视频: the models of that kind; tapping one puts it in use (one at a time). */
function MediaList({
  kind,
  data,
  vendors,
  busy,
  onUse,
  onRemove,
  onAdd,
  onCheckPrice,
}: {
  kind: MediaKind;
  data: ModelsPayload;
  vendors: Record<string, ModelVendor>;
  busy: boolean;
  /** 检测价格: read its company's platform (one model without a price yet). */
  onCheckPrice: (vendor: string) => void;
  onUse: (name: string) => void;
  onRemove: (name: string) => void;
  onAdd: () => void;
}) {
  const [confirm, setConfirm] = useState<string | null>(null);
  const { active, models } = data.media[kind];
  const copy = MEDIA_COPY[kind];
  if (models.length === 0) {
    return (
      <div className="c-card flex flex-col items-center rounded-[22px] px-6 py-8 text-center">
        <span className="grid h-12 w-12 place-items-center rounded-2xl" style={{ background: "var(--c-fill)" }}>
          {kind === "image" ? <ImageIcon className="h-6 w-6" /> : <Clapperboard className="h-6 w-6" />}
        </span>
        <p className="mt-3 text-[16px] font-semibold">{copy.empty}</p>
        <p className="c-muted mt-1 text-[13.5px]">{copy.hint}</p>
        <button type="button" onClick={onAdd} className="c-primary mt-4 min-h-11 rounded-full px-6 text-[15px] font-semibold">
          添加{KIND_LABELS[kind]}模型
        </button>
      </div>
    );
  }
  return (
    <section className="flex flex-col gap-1.5">
      <MenuGroup footnote={`${copy.use}打勾的那个；点另一个就换成它。`}>
        {models.map((model) => {
          const vendor = model.vendor ? vendors[model.vendor] : undefined;
          const inUse = model.name === active;
          return (
            <MenuRow
              key={model.name}
              icon={<VendorLogo vendor={vendor ?? null} size={28} />}
              label={model.name}
              detail={
                [vendor?.name, model.model !== model.name ? model.model : null, priceLine(model.price)]
                  .filter(Boolean)
                  .join(" · ")
              }
              trailing={
                <span className="flex shrink-0 items-center gap-2">
                  {/* A subscription (会员账号登录) has no price list to read: no 检测价格 for it. */}
                  {!model.price && model.vendor && vendor && !vendor.login && (
                    <button
                      type="button"
                      disabled={busy}
                      onClick={(event) => {
                        event.stopPropagation();
                        onCheckPrice(model.vendor!);
                      }}
                      className="shrink-0 px-1 text-[13px] disabled:opacity-50"
                      style={{ color: "var(--c-primary)" }}
                    >
                      {busy ? "检测中…" : "检测价格"}
                    </button>
                  )}
                  {inUse && <Check className="h-4 w-4 shrink-0" style={{ color: "var(--c-primary)" }} aria-label="正在用" />}
                  {(!inUse || models.length === 1) && (
                    <button
                      type="button"
                      disabled={busy}
                      onClick={(event) => {
                        event.stopPropagation();
                        if (confirm === model.name) {
                          setConfirm(null);
                          onRemove(model.name);
                        } else {
                          setConfirm(model.name);
                        }
                      }}
                      className="c-danger shrink-0 px-1 text-[13px] disabled:opacity-50"
                    >
                      {confirm === model.name ? "确认删除" : "删除"}
                    </button>
                  )}
                </span>
              }
              onSelect={busy || inUse ? undefined : () => onUse(model.name)}
            />
          );
        })}
      </MenuGroup>
      <MenuGroup>
        <MenuRow icon={<Plus className="h-4 w-4" />} label={`添加${KIND_LABELS[kind]}模型`} onSelect={onAdd} />
      </MenuGroup>
    </section>
  );
}

/**
 * 模型, in three kinds: 对话 (who answers: picked automatically or one fixed model, the chat
 * models, limits), 图像 and 视频 (the drawing / video model in use). Each kind adds its own
 * models from a company's catalog.
 */
export function ModelPanel({ active, onOpenModel }: { active: boolean; onOpenModel: (name: string) => void }) {
  const { data, busy, error, setError, save, check, remove, checkPrice, setData } = useModels(active);
  const vendors = useVendors(active);
  const [kind, setKind] = useState<ModelKind>("text");
  const [sheet, setSheet] = useState<"fallbacks" | "add" | null>(null);

  if (!data) return error ? <Errors error={error} /> : <SkeletonRows rows={4} label="正在读取模型" />;
  const settings = data.settings;
  const names = data.presets.map((preset) => preset.name);
  const enough = names.length >= 2;
  const fallbackText = data.fallbacks.length ? data.fallbacks.join("、") : "不换";
  const count = (k: ModelKind) => (k === "text" ? names.length : data.media[k].models.length);

  return (
    <div className="flex flex-col gap-6">
      <Segmented
        label="模型种类"
        value={kind}
        options={(["text", "image", "video"] as const).map((k) => ({ value: k, label: count(k) ? `${KIND_LABELS[k]} ${count(k)}` : KIND_LABELS[k] }))}
        onChange={(next) => {
          // An error belongs to the kind it happened on.
          setError(null);
          setKind(next);
        }}
      />

      {kind === "text" ? (
        names.length === 0 ? (
          <div className="c-card flex flex-col items-center rounded-[22px] px-6 py-8 text-center">
            <span className="grid h-12 w-12 place-items-center rounded-2xl" style={{ background: "var(--c-fill)" }}>
              <Sparkles className="h-6 w-6" />
            </span>
            <p className="mt-3 text-[16px] font-semibold">还没有对话模型</p>
            <p className="c-muted mt-1 text-[13.5px]">添加一个对话模型（填 API key 或用会员账号登录），伙伴就能开始陪你聊天做事。</p>
            <button
              type="button"
              onClick={() => setSheet("add")}
              className="c-primary mt-4 min-h-11 rounded-full px-6 text-[15px] font-semibold"
            >
              添加对话模型
            </button>
          </div>
        ) : (
        <>
          <section className="flex flex-col gap-1.5">
            <MenuGroup
              footnote={
                settings.enabled
                  ? "聊天一直用日常模型（同一段对话不来回换，回得更快也更省）；后台的小事交给最便宜的，日常模型看不了图时，先请能看图的模型看。"
                  : enough
                    ? "每条消息都用这个模型回答。"
                    : "至少有两个模型才能自动选择；先在下面多添加几个。"
              }
            >
              <div className="px-3.5 py-2.5">
                <Segmented
                  label="回答用的模型"
                  value={settings.enabled ? "auto" : "fixed"}
                  disabled={busy || !enough}
                  options={[
                    { value: "auto", label: "自动选择" },
                    { value: "fixed", label: "固定一个" },
                  ]}
                  onChange={(mode) => void save({ enabled: mode === "auto" })}
                />
              </div>
              {settings.enabled ? (
                <SelectRow
                  icon={<Wand2 className="h-4 w-4" />}
                  label="日常用"
                  value={data.default ?? ""}
                  disabled={busy}
                  options={names.map((name) => ({ value: name, label: name }))}
                  onChange={(name) => void save({ default: name })}
                />
              ) : (
                <SelectRow
                  icon={<Sparkles className="h-4 w-4" />}
                  label="一直使用"
                  value={settings.fixed ?? ""}
                  disabled={busy}
                  options={[
                    { value: "", label: data.default ? `默认（${data.default}）` : "默认" },
                    ...names.map((name) => ({ value: name, label: name })),
                  ]}
                  onChange={(fixed) => void save({ fixed: fixed || null })}
                />
              )}
            </MenuGroup>
          </section>

          <section className="flex flex-col gap-1.5">
            <div className="flex items-center justify-between px-3.5">
              <h2 className="c-muted text-[13px]">对话模型</h2>
              <button type="button" disabled={busy} onClick={() => void check()} className="c-pressable text-[13px] font-medium disabled:opacity-50" style={{ color: "var(--c-primary)" }}>
                {busy ? "检查中…" : "检查能否使用"}
              </button>
            </div>
            <MenuGroup>
              {data.presets.map((preset) => {
                const info = settings.models[preset.name];
                const health = data.health?.[preset.name];
                const vendor = preset.vendor ? vendors[preset.vendor] : undefined;
                const detail = [
                  health?.ok === false ? "用不了" : vendor?.name ?? preset.model,
                  info?.vision ? "看图" : null,
                  `思考${effortLabel(preset.reasoning_effort)}`,
                  health?.ok && health.latency_ms ? `响应 ${(health.latency_ms / 1000).toFixed(1)} 秒` : null,
                ]
                  .filter(Boolean)
                  .join(" · ");
                return (
                  <MenuRow
                    key={preset.name}
                    icon={vendor ? <VendorLogo vendor={vendor} size={28} /> : <StatusDot health={health} />}
                    label={preset.name}
                    detail={detail}
                    trailing={
                      <span className="flex items-center gap-2">
                        {vendor && <StatusDot health={health} />}
                        <ChevronRight className="c-muted h-4 w-4" aria-hidden="true" />
                      </span>
                    }
                    onSelect={() => onOpenModel(preset.name)}
                  />
                );
              })}
              <MenuRow icon={<Plus className="h-4 w-4" />} label="添加对话模型" onSelect={() => setSheet("add")} />
            </MenuGroup>
          </section>

          <section className="flex flex-col gap-1.5">
            <h2 className="c-muted px-3.5 text-[13px]">限制</h2>
            <MenuGroup footnote="一次回复里最多调用几次工具，防止任务一直转、花掉太多额度。">
              <SelectRow
                icon={<Footprints className="h-4 w-4" />}
                label="每次最多做几步"
                value={String(data.max_tool_iterations)}
                disabled={busy}
                options={[...new Set([...TOOL_STEPS, data.max_tool_iterations])]
                  .sort((a, b) => a - b)
                  .map((steps) => ({ value: String(steps), label: `${steps} 步` }))}
                onChange={(steps) => void save({ max_tool_iterations: Number(steps) })}
              />
              {names.length > 1 && (
                <MenuRow icon={<LifeBuoy className="h-4 w-4" />} label="出错时换用" value={fallbackText} onSelect={() => setSheet("fallbacks")} />
              )}
            </MenuGroup>
          </section>
        </>
        )
      ) : (
        <MediaList
          kind={kind}
          data={data}
          vendors={vendors}
          busy={busy}
          onUse={(name) => void save({ media_active: { [kind]: name } })}
          onRemove={(name) => void remove(name)}
          onAdd={() => setSheet("add")}
          onCheckPrice={(vendor) => void checkPrice(vendor, kind)}
        />
      )}
      <Errors error={error} />

      <AddModelSheet open={sheet === "add"} kind={kind} onClose={() => setSheet(null)} onAdded={setData} />

      <Sheet open={sheet === "fallbacks"} onOpenChange={(open) => !open && setSheet(null)} title="出错时换用" description="模型出错或超时，按顺序换下一个接着回答。">
        <MenuGroup>
          {names.map((name) => {
            const index = data.fallbacks.indexOf(name);
            return (
              <MenuRow
                key={name}
                icon={index >= 0 ? <span className="text-[13px] font-semibold">{index + 1}</span> : <span />}
                label={name}
                detail={data.health?.[name]?.ok === false ? "用不了" : undefined}
                trailing={index >= 0 ? <Check className="h-4 w-4" style={{ color: "var(--c-primary)" }} aria-hidden="true" /> : <span />}
                onSelect={() =>
                  void save({ fallbacks: index >= 0 ? data.fallbacks.filter((n) => n !== name) : [...data.fallbacks, name] })
                }
              />
            );
          })}
        </MenuGroup>
      </Sheet>
    </div>
  );
}

const FIELD = "c-card w-full rounded-2xl px-3.5 text-[15px] outline-none focus-visible:outline-2 focus-visible:outline-[color:var(--c-primary)]";

/** One model: how hard it thinks, how long and how freely it answers, and how routing should see it. */
export function ModelDetail({ name }: { name: string }) {
  const { data, busy, error, save, check, remove } = useModels(true);
  const vendors = useVendors(true);
  const [confirmRemove, setConfirmRemove] = useState(false);
  const preset: ModelPreset | undefined = data?.presets.find((p) => p.name === name);
  const [info, setInfo] = useState<ModelInfo>(EMPTY);
  const savedInfo = data?.settings.models[name];
  useEffect(() => setInfo(savedInfo ?? EMPTY), [savedInfo]);

  if (!data) return error ? <Errors error={error} /> : <SkeletonRows rows={4} label="正在读取模型" />;
  if (!preset) return <p className="c-muted text-sm">这个模型已经不在了。</p>;

  const limits = (changes: ModelLimits) => void save({ presets: { [name]: changes } });
  const route = (changes: Partial<ModelInfo>) => {
    const next = { ...info, ...changes };
    setInfo(next);
    void save({ models: { [name]: next } });
  };
  const health = data.health?.[name];
  const style = STYLES.find((option) => Math.abs(Number(option.value) - preset.temperature) < 0.05)?.value ?? String(preset.temperature);

  return (
    <div className="flex flex-col gap-6">
      <section className="flex flex-col gap-1.5">
        <MenuGroup
          footnote={
            health?.ok === false
              ? `用不了：${health.error ?? "出错了"}。自动选择会先避开它。`
              : health
                ? `${dayLabel(health.checked_at)}检查过，可以用。`
                : undefined
          }
        >
          <MenuRow
            icon={preset.vendor && vendors[preset.vendor] ? <VendorLogo vendor={vendors[preset.vendor]} size={28} /> : <StatusDot health={health} />}
            label={preset.model}
            detail={(preset.vendor && vendors[preset.vendor]?.name) || preset.provider}
            trailing={
            <button type="button" disabled={busy} onClick={() => void check([name])} className="c-pressable shrink-0 text-[13px] font-medium disabled:opacity-50" style={{ color: "var(--c-primary)" }}>
              {busy ? "检查中…" : "检查"}
            </button>
          } />
        </MenuGroup>
      </section>

      <section className="flex flex-col gap-1.5">
        <h2 className="c-muted px-3.5 text-[13px]">思考强度</h2>
        <Segmented
          label="思考强度"
          value={preset.reasoning_effort ?? "default"}
          disabled={busy}
          options={EFFORTS}
          onChange={(effort) => limits({ reasoning_effort: effort === "default" ? null : effort })}
        />
        <p className="c-muted px-3.5 text-[12px] leading-snug">想得越多答得越准，但更慢、更费 token；「默认」按服务商的设置，不支持思考的模型会忽略。</p>
      </section>

      <section className="flex flex-col gap-1.5">
        <h2 className="c-muted px-3.5 text-[13px]">回答</h2>
        <MenuGroup>
          <SelectRow
            label="单次最多"
            value={String(preset.max_tokens)}
            disabled={busy}
            options={[...new Set([...REPLY_LENGTHS, preset.max_tokens])]
              .sort((a, b) => a - b)
              .map((count) => ({ value: String(count), label: `${tokens(count)} token` }))}
            onChange={(count) => limits({ max_tokens: Number(count) })}
          />
          <SelectRow
            label="风格"
            value={style}
            disabled={busy}
            options={STYLES.some((option) => option.value === style) ? STYLES : [...STYLES, { value: style, label: `自定义 ${style}` }]}
            onChange={(value) => limits({ temperature: Number(value) })}
          />
        </MenuGroup>
      </section>

      <section className="flex flex-col gap-1.5">
        <h2 className="c-muted px-3.5 text-[13px]">自动选择时</h2>
        <Segmented label="档位" value={info.tier} disabled={busy} options={TIERS} onChange={(tier) => route({ tier })} />
        <MenuGroup>
          <MenuRow
            icon={<Eye className="h-4 w-4" />}
            label="能看图"
            trailing={<Switch checked={info.vision} label="能看图" disabled={busy} onChange={(vision) => route({ vision })} />}
          />
        </MenuGroup>
        <div className="grid grid-cols-2 gap-2">
          {(["price_in", "price_out"] as const).map((key) => (
            <label key={key} className="flex flex-col gap-1">
              <span className="c-muted px-1 text-[12px]">{key === "price_in" ? "输入" : "输出"}价格（元/百万 token）</span>
              <input
                type="number"
                inputMode="decimal"
                min={0}
                step="0.01"
                value={info[key] ?? ""}
                placeholder="可不填"
                onChange={(event) => {
                  const value = Number.parseFloat(event.target.value);
                  setInfo({ ...info, [key]: Number.isFinite(value) && value >= 0 ? value : null });
                }}
                onBlur={() => info[key] !== (savedInfo?.[key] ?? null) && route({})}
                className={`${FIELD} min-h-11`}
              />
            </label>
          ))}
        </div>
      </section>
      {data.presets.length > 1 && (
        <MenuGroup>
          <MenuRow
            icon={<Trash2 className="h-4 w-4" />}
            label={confirmRemove ? "再点一次，确认删除" : "删除这个模型"}
            danger
            onSelect={() => (confirmRemove ? void remove(name) : setConfirmRemove(true))}
          />
        </MenuGroup>
      )}
      <Errors error={error} />
    </div>
  );
}
