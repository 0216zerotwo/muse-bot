import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  ArrowUpRight,
  Check,
  ChevronLeft,
  Copy,
  Eye,
  Loader2,
  LogIn,
  Plus,
  RefreshCw,
  Sparkles,
} from "lucide-react";

import { copyTextToClipboard } from "@/lib/clipboard";
import { useClient } from "@/providers/ClientProvider";

import {
  addModel,
  checkVendorModels,
  fetchModelCatalog,
  fetchVendorModels,
  importLogin,
  loginStatus,
  startLogin,
  type LoginState,
  type ModelKind,
  type ModelsPayload,
  type ModelVendor,
  type VendorModel,
  type VendorModels,
} from "./companion-api";
import { MuseSheet } from "./muse/Sheet";
import { priceSummary, priceUnitHint } from "./pricing";
import { SkeletonGrid, SkeletonRows } from "./Skeleton";

export const KIND_LABELS: Record<ModelKind, string> = {
  text: "对话",
  image: "图像",
  video: "视频",
};
const GROUP_LABELS: Record<ModelVendor["group"], string> = {
  sub: "用会员账号登录",
  cn: "国内",
  global: "海外",
  hub: "聚合平台",
};
const ACCOUNT: Record<string, string> = { chatgpt: "ChatGPT", grok: "Grok" };
const COLLAPSED = 6;
/** The registry describes models in English: in this Chinese list only a Chinese note is shown. */
const CJK = /[一-鿿]/;

/** A company's mark: its icon, or its first letter on its color when the icon cannot load. */
export function VendorLogo({
  vendor,
  size = 28,
}: {
  vendor: Pick<ModelVendor, "name" | "logo_url" | "color"> | null;
  size?: number;
}) {
  const [failed, setFailed] = useState(false);
  const box = {
    width: size,
    height: size,
    borderRadius: Math.round(size * 0.26),
  };
  if (vendor?.logo_url && !failed) {
    return (
      <img
        src={vendor.logo_url}
        alt=""
        onError={() => setFailed(true)}
        className="shrink-0 bg-white object-contain p-[3px]"
        style={box}
      />
    );
  }
  const initial = (vendor?.name ?? "").trim().charAt(0).toUpperCase();
  return (
    <span
      className="grid shrink-0 place-items-center font-semibold"
      style={{
        ...box,
        background: vendor?.color ?? "var(--c-fill)",
        color: vendor?.color ? "#fff" : undefined,
        fontSize: Math.round(size * 0.46),
      }}
      aria-hidden="true"
    >
      {initial || <Sparkles className="h-1/2 w-1/2" />}
    </span>
  );
}

/** "128K", "1M" */
function context(tokens: number | null): string | null {
  if (!tokens) return null;
  return tokens >= 1_000_000
    ? `${Math.round(tokens / 100_000) / 10}M`
    : `${Math.round(tokens / 1024)}K`;
}

/** "刚刚", "5 分钟前", "3 天前" */
function ago(seconds: number): string {
  const minutes = Math.floor((Date.now() / 1000 - seconds) / 60);
  if (minutes < 1) return "刚刚";
  if (minutes < 60) return `${minutes} 分钟前`;
  if (minutes < 60 * 24) return `${Math.floor(minutes / 60)} 小时前`;
  return `${Math.floor(minutes / 60 / 24)} 天前`;
}

function ModelOption({
  model,
  kind,
  selected,
  subscription,
  onSelect,
}: {
  model: VendorModel;
  kind: ModelKind;
  selected: boolean;
  subscription: boolean;
  onSelect: () => void;
}) {
  const facts = [
    model.vision ? (kind === "text" ? "看图" : "改图") : null,
    model.reasoning ? "深度思考" : null,
    kind === "text" ? context(model.context) : null,
  ].filter(Boolean);
  const summary = subscription ? null : priceSummary(model.price);
  const cost = subscription ? "会员额度" : summary?.main ?? null;
  return (
    <button
      type="button"
      role="radio"
      aria-checked={selected}
      onClick={onSelect}
      className="c-pressable flex w-full items-start gap-3 px-3.5 py-3 text-left"
    >
      <span
        className="mt-0.5 grid h-5 w-5 shrink-0 place-items-center rounded-full border-[1.5px]"
        style={
          selected
            ? {
                background: "var(--c-primary)",
                borderColor: "var(--c-primary)",
              }
            : { borderColor: "var(--c-border)" }
        }
        aria-hidden="true"
      >
        {selected && (
          <Check
            className="h-3 w-3"
            style={{ color: "var(--c-primary-text)" }}
            strokeWidth={3}
          />
        )}
      </span>
      <span className="min-w-0 flex-1">
        <span className="flex items-baseline gap-2">
          <span className="truncate text-[15px] font-medium">{model.name}</span>
          {cost && (
            <span className="c-muted ml-auto shrink-0 text-[12px] tabular-nums">
              {cost}
            </span>
          )}
        </span>
        <span className="c-muted mt-0.5 block truncate text-[12.5px]">
          {[
            model.missing ? "官网上没找到了" : null,
            ...facts,
            (CJK.test(model.note) ? model.note : null) ||
              (model.name !== model.id ? model.id : null),
          ]
            .filter(Boolean)
            .join(" · ")}
        </span>
        {summary && summary.rows.length > 0 && (
          // The price per spec (resolution, length, tier): the headline is only the cheapest.
          <span className="c-muted mt-0.5 block text-[12px] leading-snug tabular-nums">
            {summary.rows.join(" · ")}
          </span>
        )}
      </span>
    </button>
  );
}

/**
 * Sign in with a subscription: the server asks for a short code, the user enters it on the
 * company's sign-in page (on this phone), and the server notices when that is done.
 */
function SubscriptionSignIn({
  vendor,
  onSignedIn,
}: {
  vendor: ModelVendor;
  onSignedIn: () => void;
}) {
  const { client } = useClient();
  const [login, setLogin] = useState<LoginState | null>(null);
  const [busy, setBusy] = useState(false);
  const [copied, setCopied] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const account = ACCOUNT[vendor.login ?? ""] ?? vendor.name;
  const waiting = login?.status === "waiting";

  useEffect(() => {
    if (!login || !waiting) return;
    const timer = window.setInterval(() => {
      loginStatus(client, login.id)
        .then((next) => {
          setLogin(next);
          if (next.status === "done") onSignedIn();
          if (next.status === "failed")
            setError(next.error ?? "登录没有完成，再试一次");
        })
        .catch(() => undefined);
    }, 2000);
    return () => window.clearInterval(timer);
  }, [client, login, waiting, onSignedIn]);

  const begin = async () => {
    setBusy(true);
    setError(null);
    try {
      setLogin(await startLogin(client, vendor.login ?? ""));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const importHere = async () => {
    setBusy(true);
    setError(null);
    try {
      await importLogin(client, vendor.login ?? "");
      onSignedIn();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  if (vendor.has_key || login?.status === "done") {
    return (
      <p
        className="flex items-center gap-1.5 text-[14px] font-medium"
        style={{ color: "var(--c-online)" }}
      >
        <Check className="h-4 w-4" /> 已登录 {account} 账号
      </p>
    );
  }
  return (
    <section className="c-card flex flex-col gap-3 rounded-2xl p-4">
      {waiting && login ? (
        <>
          <p className="text-[13.5px] leading-relaxed">
            打开 {account} 的登录页，登录你的会员账号，再输入这串代码：
          </p>
          <div className="flex items-center gap-2">
            <span className="flex-1 select-all text-center font-mono text-[26px] font-semibold tracking-[0.18em]">
              {login.user_code}
            </span>
            <button
              type="button"
              onClick={() =>
                void copyTextToClipboard(login.user_code).then((ok) => ok && setCopied(true))
              }
              className="c-pressable grid h-10 w-10 place-items-center rounded-full"
              style={{ background: "var(--c-fill)" }}
              aria-label="复制代码"
            >
              {copied ? (
                <Check className="h-4 w-4" />
              ) : (
                <Copy className="h-4 w-4" />
              )}
            </button>
          </div>
          <a
            href={login.url}
            target="_blank"
            rel="noreferrer"
            className="c-primary flex min-h-11 items-center justify-center gap-1 rounded-full text-[15px] font-semibold"
          >
            打开登录页 <ArrowUpRight className="h-4 w-4" />
          </a>
          <p className="c-muted flex items-center justify-center gap-1.5 text-[12.5px]">
            <Loader2 className="h-3.5 w-3.5 animate-spin" />{" "}
            登录好了这里会自动继续
          </p>
        </>
      ) : (
        <>
          <p className="c-muted text-[13.5px] leading-relaxed">{vendor.note}</p>
          {vendor.local_login ? (
            <>
              {/* Signed in on this machine already (Codex CLI / app): use that, no second sign-in. */}
              <button
                type="button"
                disabled={busy}
                onClick={() => void importHere()}
                className="c-primary flex min-h-11 items-center justify-center gap-1.5 rounded-full text-[15px] font-semibold disabled:opacity-50"
              >
                {busy ? <Loader2 className="h-4 w-4 animate-spin" /> : <Check className="h-4 w-4" />} 用这台电脑上已登录的 {account}
              </button>
              <button type="button" disabled={busy} onClick={() => void begin()} className="c-muted min-h-9 text-[13px]">
                换一个账号登录
              </button>
            </>
          ) : (
            <button
              type="button"
              disabled={busy}
              onClick={() => void begin()}
              className="c-primary flex min-h-11 items-center justify-center gap-1.5 rounded-full text-[15px] font-semibold disabled:opacity-50"
            >
              {busy ? <Loader2 className="h-4 w-4 animate-spin" /> : <LogIn className="h-4 w-4" />} 用 {account} 账号登录
            </button>
          )}
        </>
      )}
      {error && (
        <p className="c-danger text-[13px]" role="alert">
          {error}
        </p>
      )}
    </section>
  );
}

/**
 * 添加模型 for one kind: pick a company (only those with that kind), paste its key once, pick a
 * model from its catalog (recommended first, prices per million tokens), or type a model id.
 */
export function AddModelSheet({
  open,
  kind,
  onClose,
  onAdded,
}: {
  open: boolean;
  kind: ModelKind;
  onClose: () => void;
  onAdded: (payload: ModelsPayload) => void;
}) {
  const { client, token } = useClient();
  const [vendors, setVendors] = useState<ModelVendor[] | null>(null);
  const [vendor, setVendor] = useState<ModelVendor | null>(null);
  const [models, setModels] = useState<VendorModel[] | null>(null);
  // 检测: when the company's platform was last read, and whether it is being read now.
  const [checkedAt, setCheckedAt] = useState<VendorModels["checked"]>(null);
  const [canCheck, setCanCheck] = useState(false);
  const [checking, setChecking] = useState(false);
  const [showAll, setShowAll] = useState(false);
  const [picked, setPicked] = useState<string>("");
  const [ownId, setOwnId] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [baseUrl, setBaseUrl] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const vendorReqSeq = useRef(0);

  useEffect(() => {
    if (!open) return;
    vendorReqSeq.current += 1;
    setVendor(null);
    setError(null);
    fetchModelCatalog(token)
      .then(setVendors)
      .catch((e) => setError(e instanceof Error ? e.message : String(e)));
  }, [open, token]);

  const choose = (next: ModelVendor) => {
    vendorReqSeq.current += 1;
    setVendor(next);
    setModels(null);
    setChecking(false);
    setShowAll(false);
    setPicked("");
    setOwnId("");
    setApiKey("");
    setBaseUrl("");
    setError(null);
    if (next.id === "custom") {
      setModels([]);
      return;
    }
    loadModels(next);
  };

  const loadModels = (next: ModelVendor) => {
    const seq = ++vendorReqSeq.current;
    if (next.login && !next.has_key) {
      setModels([]);
      return;
    }
    setModels(null);
    setCheckedAt(null);
    fetchVendorModels(client, next.id, kind)
      .then((body) => {
        if (seq !== vendorReqSeq.current) return;
        setModels(body.models);
        setCheckedAt(body.checked ?? null);
        setCanCheck(!!body.can_check);
        setPicked(body.models.find((m) => m.recommended)?.id ?? "");
      })
      .catch((e) => {
        if (seq !== vendorReqSeq.current) return;
        setModels([]);
        setError(e instanceof Error ? e.message : String(e));
      });
  };

  /** 检测: read the company's platform now for its current model IDs and prices. */
  const checkNow = () => {
    if (!vendor || checking) return;
    const seq = ++vendorReqSeq.current;
    setChecking(true);
    setError(null);
    checkVendorModels(client, vendor.id, kind)
      .then((body) => {
        if (seq !== vendorReqSeq.current) return;
        setModels(body.models);
        setCheckedAt(body.checked);
        // Keep the pick when the platform still has it; else its newest.
        setPicked((current) =>
          current === "__own" || body.models.some((m) => m.id === current) ? current : body.models.find((m) => m.recommended)?.id ?? "",
        );
      })
      .catch((e) => {
        if (seq !== vendorReqSeq.current) return;
        setError(`检测没成功：${e instanceof Error ? e.message : String(e)}`);
      })
      .finally(() => {
        if (seq === vendorReqSeq.current) setChecking(false);
      });
  };

  const offered = useMemo(
    () => (vendors ?? []).filter((v) => v.kinds.includes(kind)),
    [vendors, kind],
  );
  const custom = vendor?.id === "custom";
  const modelId = picked === "__own" || custom ? ownId.trim() : picked;
  const subscription = !!vendor?.login;
  const ready =
    !!vendor &&
    !!modelId &&
    (vendor.has_key || (!subscription && !!apiKey.trim())) &&
    (!custom || /^https?:\/\/\S+$/.test(baseUrl.trim()));
  // Signed in: the plan's models can be listed now (ChatGPT's come from the account).
  const signedIn = useCallback(() => {
    if (!vendor) return;
    const next = { ...vendor, has_key: true };
    setVendor(next);
    setVendors((list) => list?.map((v) => (v.id === next.id ? next : v)) ?? list);
    loadModels(next);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [vendor, kind]);

  const submit = async () => {
    if (!vendor || !ready || busy) return;
    setBusy(true);
    setError(null);
    try {
      const payload = await addModel(client, {
        kind,
        vendor: vendor.id,
        model: modelId,
        api_key: apiKey.trim(),
        ...(custom ? { base_url: baseUrl.trim() } : {}),
      });
      const health = payload.added
        ? payload.health?.[payload.added]
        : undefined;
      if (health && !health.ok) {
        setError(`已添加，但现在用不了：${health.error ?? "出错了"}`);
        onAdded(payload);
      } else {
        onAdded(payload);
        onClose();
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const listed = models ?? [];
  const recommended = listed.filter((m) => m.recommended);
  const rest = listed.filter((m) => !m.recommended);
  const visibleRest = showAll
    ? rest
    : rest.slice(0, Math.max(0, COLLAPSED - recommended.length));

  return (
    <MuseSheet
      open={open}
      onClose={onClose}
      title={
        vendor ? (
          <span className="flex items-center gap-2">
            <button
              type="button"
              onClick={() => setVendor(null)}
              aria-label="换一个服务商"
              className="c-pressable -ml-2 grid h-9 w-9 place-items-center rounded-full"
            >
              <ChevronLeft className="h-5 w-5" />
            </button>
            <VendorLogo vendor={vendor} size={24} />
            <span className="truncate">{vendor.name}</span>
          </span>
        ) : (
          `添加${KIND_LABELS[kind]}模型`
        )
      }
      footer={
        // A subscription shows its add button once signed in.
        vendor && !(subscription && !vendor.has_key) ? (
          <div className="flex flex-col gap-2 pb-1">
            {error && (
              <p className="c-danger text-[13px]" role="alert">
                {error}
              </p>
            )}
            <button
              type="button"
              disabled={!ready || busy}
              onClick={() => void submit()}
              className="c-primary min-h-12 w-full rounded-full text-[16px] font-semibold disabled:opacity-40"
            >
              {busy ? (kind === "text" ? "添加并检查中…" : "添加中…") : "添加"}
            </button>
          </div>
        ) : undefined
      }
    >
      {!vendor ? (
        <div className="pb-2">
          <p className="c-muted mb-3 text-[13.5px] leading-relaxed">
            {kind === "text"
              ? "用会员账号登录，或选一家服务商填上 API key，再挑一个模型。"
              : kind === "image"
                ? "伙伴画图、做头像时会用它。"
                : "伙伴做视频时会用它。"}
          </p>
          {vendors === null ? (
            <SkeletonGrid count={6} variant="vendor" label="正在读取服务商" />
          ) : (
            <div className="flex flex-col gap-4">
              {(Object.keys(GROUP_LABELS) as ModelVendor["group"][]).map(
                (group) => {
                  const members = offered.filter((v) => v.group === group);
                  if (!members.length) return null;
                  return (
                    <section key={group} className="flex flex-col gap-2">
                      <h3 className="c-muted px-1 text-[12px] font-medium">
                        {GROUP_LABELS[group]}
                      </h3>
                      <div className="grid grid-cols-3 gap-2">
                        {members.map((v) => (
                          <button
                            key={v.id}
                            type="button"
                            onClick={() => choose(v)}
                            className="c-card c-pressable flex flex-col items-center gap-2 rounded-2xl px-1 pb-2.5 pt-3.5"
                          >
                            {v.id === "custom" ? (
                              <span
                                className="grid h-10 w-10 place-items-center rounded-[11px]"
                                style={{ background: "var(--c-fill)" }}
                              >
                                <Plus className="h-5 w-5" />
                              </span>
                            ) : (
                              <VendorLogo vendor={v} size={40} />
                            )}
                            <span className="w-full truncate text-center text-[12.5px] leading-tight">
                              {v.name}
                            </span>
                            {v.has_key && (
                              <span className="c-muted -mt-1.5 text-[10.5px]">
                                {v.login ? "已登录" : "已填 key"}
                              </span>
                            )}
                          </button>
                        ))}
                      </div>
                    </section>
                  );
                },
              )}
            </div>
          )}
          {error && (
            <p className="c-danger mt-3 text-[13px]" role="alert">
              {error}
            </p>
          )}
        </div>
      ) : (
        <div className="flex flex-col gap-5 pb-3">
          {vendor.note && !subscription && (
            <p className="c-muted text-[13px] leading-relaxed">{vendor.note}</p>
          )}

          {custom && (
            <label className="flex flex-col gap-1.5">
              <span className="text-[13px] font-medium">服务地址</span>
              <input
                value={baseUrl}
                onChange={(e) => setBaseUrl(e.target.value)}
                placeholder="https://api.example.com/v1"
                inputMode="url"
                autoComplete="off"
                spellCheck={false}
                className={FIELD}
              />
            </label>
          )}

          {subscription ? (
            <SubscriptionSignIn vendor={vendor} onSignedIn={signedIn} />
          ) : (
            <label className="flex flex-col gap-1.5">
              <span className="flex items-center text-[13px] font-medium">
                API key
                {vendor.console && (
                  <a
                    href={vendor.console}
                    target="_blank"
                    rel="noreferrer"
                    className="ml-auto flex items-center gap-0.5 font-normal"
                    style={{ color: "var(--c-primary)" }}
                  >
                    去获取 <ArrowUpRight className="h-3.5 w-3.5" />
                  </a>
                )}
              </span>
              <input
                value={apiKey}
                onChange={(e) => setApiKey(e.target.value)}
                placeholder={
                  vendor.has_key ? "已保存，不换就留空" : "粘贴 API key"
                }
                type="password"
                autoComplete="off"
                spellCheck={false}
                className={FIELD}
              />
            </label>
          )}

          {subscription && !vendor.has_key ? null : custom ? (
            <label className="flex flex-col gap-1.5">
              <span className="text-[13px] font-medium">模型 ID</span>
              <input
                value={ownId}
                onChange={(e) => setOwnId(e.target.value)}
                placeholder="比如 qwen-plus"
                autoComplete="off"
                spellCheck={false}
                className={FIELD}
              />
            </label>
          ) : (
            <section
              className="flex flex-col gap-1.5"
              role="radiogroup"
              aria-label="选择模型"
            >
              <div className="flex items-center">
                <span className="text-[13px] font-medium">选择模型</span>
                {canCheck && !subscription && (
                  <button
                    type="button"
                    onClick={checkNow}
                    disabled={checking || models === null}
                    className="ml-auto flex items-center gap-1 text-[13px] disabled:opacity-60"
                    style={{ color: "var(--c-primary)" }}
                  >
                    <RefreshCw className={`h-3.5 w-3.5 ${checking ? "animate-spin" : ""}`} />
                    {checking ? "检测中" : "检测 ID 和价格"}
                  </button>
                )}
              </div>
              {!subscription && (checking || checkedAt || canCheck || priceUnitHint(listed.map((m) => m.price))) && (
                <p className="c-muted -mt-0.5 text-[11.5px] leading-snug">
                  {checking
                    ? `正在读${vendor.name}官网的模型和价格，大约半分钟…`
                    : [
                        checkedAt ? `已按官网核对 · ${ago(checkedAt.at)}` : canCheck ? "还没和官网核对过，模型和价格可能不是最新的" : null,
                        priceUnitHint(listed.map((m) => m.price)),
                      ]
                        .filter(Boolean)
                        .join(" · ")}
                </p>
              )}
              {models === null ? (
                <SkeletonRows rows={4} withIcon={false} label="正在读取模型" />
              ) : (
                <div className="c-card divide-y divide-[color:var(--c-border)] overflow-hidden rounded-2xl">
                  {recommended.length > 0 && (
                    <p className="c-muted flex items-center gap-1 px-3.5 pb-1 pt-2.5 text-[11.5px] font-medium">
                      <Sparkles className="h-3 w-3" /> 推荐：最新的几款
                    </p>
                  )}
                  {[...recommended, ...visibleRest].map((model) => (
                    <ModelOption
                      key={model.id}
                      model={model}
                      kind={kind}
                      subscription={subscription}
                      selected={picked === model.id}
                      onSelect={() => setPicked(model.id)}
                    />
                  ))}
                  {!showAll && rest.length > visibleRest.length && (
                    <button
                      type="button"
                      onClick={() => setShowAll(true)}
                      className="c-pressable w-full px-3.5 py-2.5 text-left text-[13.5px]"
                      style={{ color: "var(--c-primary)" }}
                    >
                      显示全部 {listed.length} 个
                    </button>
                  )}
                  <div className="px-3.5 py-2.5">
                    <button
                      type="button"
                      role="radio"
                      aria-checked={picked === "__own"}
                      onClick={() => setPicked("__own")}
                      className="flex w-full items-center gap-3 text-left text-[14px]"
                    >
                      <span
                        className="grid h-5 w-5 shrink-0 place-items-center rounded-full border-[1.5px]"
                        style={
                          picked === "__own"
                            ? {
                                background: "var(--c-primary)",
                                borderColor: "var(--c-primary)",
                              }
                            : { borderColor: "var(--c-border)" }
                        }
                        aria-hidden="true"
                      >
                        {picked === "__own" && (
                          <Check
                            className="h-3 w-3"
                            style={{ color: "var(--c-primary-text)" }}
                            strokeWidth={3}
                          />
                        )}
                      </span>
                      {listed.length
                        ? "列表里没有？自己填模型 ID"
                        : "填写模型 ID"}
                    </button>
                    {picked === "__own" && (
                      <input
                        autoFocus
                        value={ownId}
                        onChange={(e) => setOwnId(e.target.value)}
                        placeholder="模型 ID"
                        autoComplete="off"
                        spellCheck={false}
                        className={`${FIELD} mt-2`}
                      />
                    )}
                  </div>
                </div>
              )}
              {kind === "text" && listed.some((m) => m.vision) && (
                <p className="c-muted flex items-center gap-1 px-1 text-[11.5px]">
                  <Eye className="h-3 w-3" /> 能看图的模型可以直接看你发的照片
                </p>
              )}
            </section>
          )}
        </div>
      )}
    </MuseSheet>
  );
}

const FIELD =
  "c-card min-h-11 w-full rounded-2xl px-3.5 text-[15px] outline-none focus-visible:outline-2 focus-visible:outline-[color:var(--c-primary)]";
