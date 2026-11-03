import { useLayoutEffect, useRef, useState } from "react";
import { CalendarX, Check, CircleAlert, Globe, Mail, ShieldAlert, SquareTerminal, Target, Trash2, Upload, X } from "lucide-react";

import type { ApprovalRecord, ApprovalScope } from "./companion-api";
import { approvalKind, type ApprovalAction } from "./presentation";

const ACTIONS: Record<ApprovalAction, { title: string; confirm: string; done: string }> = {
  send_email: { title: "邮件", confirm: "发送", done: "已发送" },
  forward_email: { title: "转发邮件", confirm: "转发", done: "已转发" },
  delete_emails: { title: "删除邮件", confirm: "删除", done: "已删除" },
  email_command: { title: "邮件操作", confirm: "执行", done: "已执行" },
  delete_event: { title: "删除日程", confirm: "删除", done: "已删除" },
  browser_submit: { title: "在网页上点按钮", confirm: "点吧", done: "已点了" },
  browser_upload: { title: "上传文件到网页", confirm: "上传", done: "已上传" },
  run_command: { title: "运行命令", confirm: "运行", done: "已允许" },
  goal_plan: { title: "调整计划", confirm: "用新计划", done: "已换成新计划" },
  send_private: { title: "把信息发到网站", confirm: "允许发送", done: "已发送" },
  browse: { title: "打开网页", confirm: "允许", done: "已允许" },
  payment: { title: "付款或下单", confirm: "确认付款", done: "已确认" },
  connector_read: { title: "读取连接的服务", confirm: "允许", done: "已允许" },
  connector_write: { title: "在连接的服务里修改", confirm: "允许", done: "已允许" },
  schedule: { title: "设定时任务", confirm: "允许", done: "已允许" },
};

const SCOPE_LABELS: Record<ApprovalScope, string> = { once: "仅这次", "24h": "24 小时内", always: "一直允许" };

/** What a standing yes covers, in words: "发给 a@x.com" for mail, the reason for a command. */
function grantSubject(approval: ApprovalRecord, action: ApprovalAction, args: Record<string, unknown>): string {
  if (action === "send_email" || action === "forward_email") return `发给 ${list(args.recipients)}`;
  if (action === "send_private") return `发到 ${approval.context?.egress?.host ?? ""}`;
  if (action === "run_command") return text(args.reason);
  return "";
}

function standingLabel(scope: ApprovalScope, subject: string): string {
  const what = subject ? `「${subject}」` : "同样的事";
  return scope === "24h" ? `24 小时内${what}都允许` : `${what}一直允许`;
}

const DESTRUCTIVE: ReadonlySet<ApprovalAction> = new Set(["delete_emails", "delete_event"]);

function ActionIcon({ action }: { action: ApprovalAction }) {
  if (action === "delete_event") return <CalendarX className="h-4 w-4" />;
  if (action === "delete_emails") return <Trash2 className="h-4 w-4" />;
  if (action === "browser_submit") return <Globe className="h-4 w-4" />;
  if (action === "browser_upload") return <Upload className="h-4 w-4" />;
  if (action === "run_command") return <SquareTerminal className="h-4 w-4" />;
  if (action === "goal_plan") return <Target className="h-4 w-4" />;
  if (action === "send_private") return <ShieldAlert className="h-4 w-4" />;
  return <Mail className="h-4 w-4" />;
}

function list(value: unknown): string {
  return Array.isArray(value) ? value.filter((item) => typeof item === "string").join("、") : "";
}

function text(value: unknown): string {
  return typeof value === "string" ? value : "";
}

function Field({ label, value }: { label: string; value: string }) {
  if (!value) return null;
  return (
    <div className="flex gap-3 text-[14px] leading-5">
      <span className="c-muted w-12 shrink-0">{label}</span>
      <span className="min-w-0 flex-1 break-words">{value}</span>
    </div>
  );
}

/** The plan as it is (finished steps stay) next to the proposed remaining steps. */
function PlanChange({ current, proposed }: { current: { text: string; status: string }[]; proposed: string[] }) {
  const kept = current.filter((step) => step.status === "done");
  const replaced = current.filter((step) => step.status !== "done");
  return (
    <div className="mt-3 grid gap-3 text-[14px] leading-5">
      {replaced.length > 0 && (
        <div>
          <p className="c-muted mb-1 text-[12px]">现在的计划</p>
          <ol className="c-muted flex flex-col gap-1">
            {replaced.map((step) => (
              <li key={step.text} className="line-through">{step.text}</li>
            ))}
          </ol>
        </div>
      )}
      <div>
        <p className="c-muted mb-1 text-[12px]">{kept.length ? `新计划（已完成的 ${kept.length} 步保留）` : "新计划"}</p>
        <ol className="flex list-decimal flex-col gap-1 pl-5">
          {proposed.map((step) => (
            <li key={step}>{step}</li>
          ))}
        </ol>
      </div>
    </div>
  );
}

function StatusLine({ approval, done }: { approval: ApprovalRecord; done: string }) {
  if (approval.status === "executed") {
    return (
      <p className="flex items-center gap-1.5 text-[14px] font-medium" style={{ color: "var(--c-online)" }}>
        <Check className="h-4 w-4" aria-hidden="true" />
        {done}
      </p>
    );
  }
  if (approval.status === "rejected") {
    return (
      <p className="c-muted flex items-center gap-1.5 text-[14px]">
        <X className="h-4 w-4" aria-hidden="true" />
        {approval.action === "goal_plan" ? "保持原来的计划" : "已取消，没有执行"}
      </p>
    );
  }
  return (
    <p className="c-danger flex items-start gap-1.5 text-[14px]">
      <CircleAlert className="mt-0.5 h-4 w-4 shrink-0" aria-hidden="true" />
      <span>没有成功：{approval.result || "未知错误"}</span>
    </p>
  );
}

/**
 * The exact action the companion wants to take, shown before anything happens.
 * Only the buttons here can run it; the server executes the recorded arguments.
 */
export function ApprovalCard({
  approval,
  onDecide,
  bare = false,
  onLater,
}: {
  approval: ApprovalRecord;
  onDecide: (approval: ApprovalRecord, decision: "approve" | "reject", scope?: ApprovalScope) => Promise<void>;
  /** Inside a sheet: no card of its own. */
  bare?: boolean;
  /** Close without deciding (the sheet): the card stays in the chat. */
  onLater?: () => void;
}) {
  const action = approvalKind(approval);
  const browser = approval.context?.browser;
  // A press that may buy something reads as what it is, whatever kind of click it was held as.
  const copy = browser?.paying ? ACTIONS.payment : ACTIONS[action];
  const args = approval.arguments;
  const [busy, setBusy] = useState<"approve" | "reject" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [expanded, setExpanded] = useState(false);
  const standing = (approval.scopes ?? ["once"]).filter((option) => option !== "once");
  const subject = grantSubject(approval, action, args);
  const [scope, setScope] = useState<ApprovalScope>("once");
  const pending = approval.status === "pending";
  const destructive = DESTRUCTIVE.has(action);
  const body = text(args.body);
  // Offer "展开全文" only when the clamped body actually hides text (wrapping included).
  const bodyRef = useRef<HTMLParagraphElement>(null);
  const [clipped, setClipped] = useState(false);
  useLayoutEffect(() => {
    const el = bodyRef.current;
    if (el && !expanded) setClipped(el.scrollHeight > el.clientHeight + 1);
  }, [body, expanded]);

  const decide = async (decision: "approve" | "reject", chosen: ApprovalScope) => {
    if (busy) return;
    setBusy(decision);
    setScope(chosen);
    setError(null);
    try {
      await onDecide(approval, decision, chosen);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  };

  return (
    <div className={bare ? "" : "c-rise flex flex-col items-start"}>
      <section
        className={bare ? "w-full" : "c-card w-full max-w-[88%] rounded-[22px] p-4"}
        aria-label={`${copy.title}${pending ? "，等你确认" : ""}`}
      >
        <header className="flex items-center gap-2">
          <span className="c-primary grid h-8 w-8 place-items-center rounded-full" aria-hidden="true">
            <ActionIcon action={action} />
          </span>
          <span className="flex-1 text-[15px] font-semibold">{copy.title}</span>
          {pending && <span className="c-muted text-[12px]">等你确认</span>}
        </header>

        {browser?.summary && <p className="mt-3 text-[15px] leading-6">{browser.summary}</p>}

        <div className="mt-3 flex flex-col gap-1.5">
          <Field label="收件人" value={list(args.recipients)} />
          <Field label="抄送" value={list(args.cc)} />
          <Field label="密送" value={list(args.bcc)} />
          <Field label="主题" value={text(args.subject)} />
          {action === "forward_email" && <Field label="原邮件" value={`#${text(args.email_id)}`} />}
          {action === "delete_emails" && <Field label="数量" value={`${list(args.email_ids).split("、").filter(Boolean).length} 封`} />}
          {action === "email_command" && <Field label="协议" value={text(args.protocol).toUpperCase()} />}
          {action === "delete_event" && <Field label="日程" value={text(args.event_id)} />}
          {action === "browser_submit" && !browser && <Field label="点击" value={text(args.element)} />}
          {action === "browser_upload" && !browser && <Field label="文件" value={list(args.paths)} />}
          {action === "run_command" && <Field label="原因" value={text(args.reason)} />}
          {action === "goal_plan" && <Field label="目标" value={approval.context?.goal?.title ?? ""} />}
          {action === "goal_plan" && <Field label="原因" value={text(args.reason)} />}
          {action === "send_private" && <Field label="网站" value={approval.context?.egress?.host ?? ""} />}
          {list(args.attachments) && <Field label="附件" value={list(args.attachments)} />}
        </div>

        {action === "goal_plan" && (
          <PlanChange
            current={approval.context?.goal?.steps ?? []}
            proposed={Array.isArray(args.steps) ? args.steps.filter((step): step is string => typeof step === "string") : []}
          />
        )}

        {action === "send_private" && (
          <>
            <p className="c-muted mt-3 text-[13px] leading-5">这些内容来自你的邮件或日程，要发到这个网站。</p>
            {approval.context?.egress?.what && (
              <pre className="mt-2 max-h-40 overflow-auto whitespace-pre-wrap break-all rounded-xl p-3 text-[13px] leading-5" style={{ background: "var(--c-fill)" }}>
                {approval.context.egress.what}
              </pre>
            )}
          </>
        )}

        {action === "run_command" && text(args.command) && (
          <pre className="mt-3 max-h-40 overflow-auto whitespace-pre-wrap break-all rounded-xl p-3 text-[13px] leading-5" style={{ background: "var(--c-fill)" }}>
            {text(args.command)}
          </pre>
        )}

        {body && (
          <div className="mt-3 border-t pt-3 c-divider">
            <p
              ref={bodyRef}
              className={`whitespace-pre-wrap text-[15px] leading-relaxed ${expanded ? "" : "line-clamp-6"}`}
            >
              {body}
            </p>
            {clipped || expanded ? (
              <button
                type="button"
                onClick={() => setExpanded((value) => !value)}
                className="c-muted mt-1 min-h-11 text-[13px] font-medium"
                aria-expanded={expanded}
              >
                {expanded ? "收起" : "展开全文"}
              </button>
            ) : null}
          </div>
        )}

        <div className="mt-4">
          {pending ? (
            // As on nanoMuse: one stacked column — this once (the main button), the standing yeses, no.
            <div className="flex flex-col gap-2">
              <button
                type="button"
                disabled={busy !== null}
                onClick={() => decide("approve", "once")}
                className={`min-h-[46px] w-full rounded-full text-[15px] font-semibold transition active:scale-[0.98] disabled:opacity-60 ${
                  destructive ? "text-white" : "c-primary"
                }`}
                style={destructive ? { background: "var(--c-danger-fill)" } : undefined}
              >
                {busy === "approve" && scope === "once" ? `${copy.confirm}中…` : standing.length ? `${copy.confirm}，仅这次` : copy.confirm}
              </button>
              {standing.map((option) => (
                <button
                  key={option}
                  type="button"
                  disabled={busy !== null}
                  onClick={() => decide("approve", option)}
                  className="c-pressable min-h-11 w-full truncate rounded-full px-4 text-[15px] font-medium transition active:scale-[0.98] disabled:opacity-60"
                  style={{ background: "var(--c-fill)" }}
                >
                  {busy === "approve" && scope === option ? `${copy.confirm}中…` : standingLabel(option, subject)}
                </button>
              ))}
              <button
                type="button"
                disabled={busy !== null}
                onClick={() => decide("reject", "once")}
                className="c-pressable min-h-11 w-full rounded-full text-[15px] font-medium transition active:scale-[0.98] disabled:opacity-60"
                style={{ background: "var(--c-fill)" }}
              >
                {busy === "reject" ? "取消中…" : action === "goal_plan" ? "保持原样" : "不要"}
              </button>
              {onLater && (
                <button type="button" onClick={onLater} disabled={busy !== null} className="c-muted min-h-9 self-center text-[13px]">
                  稍后再说
                </button>
              )}
            </div>
          ) : (
            <StatusLine approval={approval} done={approval.scope && approval.scope !== "once" ? `${copy.done}，${SCOPE_LABELS[approval.scope]}不再问` : copy.done} />
          )}
          {error && (
            <p className="c-danger mt-2 text-[13px]" role="alert">
              {error}
            </p>
          )}
        </div>
      </section>
    </div>
  );
}
