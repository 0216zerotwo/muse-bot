import { fetchWithTimeout } from "@/lib/http";
import type { NanobotClient } from "@/lib/nanobot-client";
import type { McpPresetsPayload } from "@/lib/types";

import type { Price } from "./pricing";

/** Ways of talking; keys match ``TONES`` in ``nanobot/webui/companion_state.py``. */
export type CompanionTone = "warm" | "concise" | "playful" | "steady";

/** Server-persisted identity of the companion thread (``/api/webui/companion``). */
export interface CompanionState {
  name: string;
  avatar: string;
  /** Chat that companion mode always opens; ``null`` until first launch creates it. */
  chat_id: string | null;
  tone: CompanionTone;
  /** What it calls the user; ``null`` means no particular name. */
  user_name: string | null;
  /** The user's own words on how it should talk. */
  style_note: string | null;
  /** First-run setup finished (or skipped). */
  onboarded: boolean;
  /** The phone's IANA timezone, for the daily brief and quiet hours. */
  timezone: string | null;
  /** 外观, kept with the companion so every device (and a phone that lost its storage) has it. */
  theme_mode?: "system" | "light" | "dark";
  pet_size?: "small" | "medium" | "large";
  /** The newest 动态 post read (ms): the red dot counts only newer ones. */
  feed_seen_at?: number;
  /** Optional server runtime capabilities (backward-compatible when omitted). */
  capabilities?: {
    stt?: boolean;
    tts?: boolean;
    push?: boolean;
    browser?: boolean;
  };
  updated_at: string | null;
}

export type CompanionChanges = Partial<Omit<CompanionState, "updated_at">>;

/** This device's timezone, sent along whenever a time-of-day setting is saved. */
export function deviceTimezone(): string | undefined {
  return Intl.DateTimeFormat().resolvedOptions().timeZone || undefined;
}

/** Things it does on its own (``/api/webui/companion/routines``). */
export interface Routines {
  /** Local "HH:MM" of the daily brief, or ``null`` when off. */
  brief_time: string | null;
  /** A look every few hours during the day; writes only when it is worth it. */
  checkins: boolean;
}

export async function fetchRoutines(token: string): Promise<Routines> {
  const res = await fetchWithTimeout(
    "/api/webui/companion/routines",
    { headers: { Authorization: `Bearer ${token}` }, credentials: "same-origin" },
    20_000,
  );
  if (!res.ok) throw new Error(`routines failed: HTTP ${res.status}`);
  return (await res.json()) as Routines;
}

export function updateRoutines(client: NanobotClient, changes: Partial<Routines>): Promise<Routines> {
  return client.requestMutation<Routines>("companion.routines.update", { changes });
}

export async function fetchCompanionState(token: string): Promise<CompanionState> {
  const res = await fetchWithTimeout(
    "/api/webui/companion",
    { headers: { Authorization: `Bearer ${token}` }, credentials: "same-origin" },
    20_000,
  );
  if (!res.ok) throw new Error(`companion state failed: HTTP ${res.status}`);
  return (await res.json()) as CompanionState;
}

/** An action the companion asked to take in the user's name (``/api/webui/approvals``). */
export interface ApprovalRecord {
  id: string;
  session_key: string;
  call_id: string;
  tool_name: string;
  /** Exactly what will run if approved. */
  arguments: Record<string, unknown>;
  status: "pending" | "executed" | "failed" | "rejected";
  created_at_ms: number;
  decided_at_ms: number | null;
  /** Optional server-side approval timeout epoch ms (e.g. held exec commands). */
  expires_at_ms?: number | null;
  result: string | null;
  /** What sort of action it is (``APPROVAL_ACTIONS`` on the server); can depend on the arguments. */
  action?: string;
  /** How long a yes may last for this call (``once`` only when it has no target to bind to). */
  scopes?: ApprovalScope[];
  /** The scope the user chose when approving. */
  scope?: ApprovalScope;
  /** For a plan change: the goal as it is now; for send_private: the site and what would go there;
   * for a step in the browser: where it is, what it presses, and that in a sentence. */
  context?: { goal?: { title: string; steps: GoalStep[] }; egress?: { host: string; what: string }; browser?: BrowserStep };
}

export interface BrowserStep {
  host: string;
  /** The page's own name, when it is the page open now. */
  page: string;
  /** The button it presses. */
  target: string;
  /** It may buy or pay for something. */
  paying: boolean;
  summary: string;
}

export interface GoalStep {
  text: string;
  status: "pending" | "doing" | "done" | "blocked";
  note?: string;
}

/** A goal the user is working towards (``goal_add`` in chat), with steps and an optional check-in. */
export interface Goal {
  id: string;
  title: string;
  steps: GoalStep[];
  /** ``daily 08:00`` / ``weekdays 07:30`` / ``weekly mon 09:00`` / ``monthly 1 09:00``. */
  checkin: string | null;
  due: string | null;
  done: boolean;
  /** Paused: the plan stays, nothing is asked until it is resumed. */
  paused?: boolean;
  category?: GoalCategory | null;
  created_at: number;
  next_checkin_ms: number | null;
  /** Its work record, oldest first: steps, work handed to helpers and what came back, files made. */
  log?: GoalLogEntry[];
}

export interface GoalLogEntry {
  /** Epoch milliseconds. */
  at: number;
  kind: "step" | "helper" | "helper_done" | "file" | "goal";
  text: string;
  status?: GoalStep["status"];
  note?: string;
  helper?: string;
  emoji?: string;
}

export type GoalCategory =
  | "health" | "finance" | "career" | "learning" | "relationships" | "family" | "home" | "travel" | "creative" | "other";

export interface GoalChanges {
  done?: boolean;
  paused?: boolean;
  checkin?: string | null;
  due?: string | null;
  category?: GoalCategory | null;
  add_step?: string;
  step?: number;
  status?: GoalStep["status"];
}

export async function fetchGoals(token: string): Promise<Goal[]> {
  const res = await fetchWithTimeout(
    "/api/webui/companion/goals",
    { headers: { Authorization: `Bearer ${token}` }, credentials: "same-origin" },
    20_000,
  );
  if (!res.ok) throw new Error(`goals failed: HTTP ${res.status}`);
  return ((await res.json()) as { goals: Goal[] }).goals;
}

/** A new goal from the app (the companion makes most of them in chat with ``goal_add``). */
export async function addGoal(
  client: NanobotClient,
  goal: { title: string; steps: string[]; checkin: string | null; due: string | null; category: GoalCategory | null },
): Promise<Goal[]> {
  const body = await client.requestMutation<{ goals: Goal[] }>("companion.goals.change", { action: "add", changes: goal });
  return body.goals;
}

export async function changeGoal(
  client: NanobotClient,
  id: string,
  action: "update" | "remove" | "checkin",
  changes?: GoalChanges,
): Promise<Goal[]> {
  const body = await client.requestMutation<{ goals: Goal[] }>("companion.goals.change", { id, action, changes });
  return body.goals;
}

/** 自动触发: something the companion does when it happens (see the server's modules/triggers). */
export interface Trigger {
  id: string;
  kind: "webhook" | "page" | "email" | "calendar";
  name: string;
  instruction: string;
  url: string | null;
  every: number | null;
  paused: boolean;
  fired_at: number | null;
  created_at: number;
  /** Webhooks only: where other services POST. */
  hook_url?: string;
  /** Set up in a helper's chat: that helper's (it runs there); its id and name. */
  owner?: string | null;
  owner_name?: string;
}

export async function fetchTriggers(token: string): Promise<Trigger[]> {
  const res = await fetchWithTimeout(
    "/api/webui/companion/triggers",
    { headers: { Authorization: `Bearer ${token}` }, credentials: "same-origin" },
    20_000,
  );
  if (!res.ok) throw new Error(`triggers failed: HTTP ${res.status}`);
  return ((await res.json()) as { triggers: Trigger[] }).triggers;
}

export async function changeTrigger(
  client: NanobotClient,
  id: string,
  action: "remove" | "pause" | "resume",
): Promise<Trigger[]> {
  const body = await client.requestMutation<{ triggers: Trigger[] }>("companion.triggers.change", { id, action });
  return body.triggers;
}

export type ApprovalScope = "once" | "24h" | "always";

/** A standing yes: the action and target it covers, until it expires or is revoked. */
export interface ApprovalGrant {
  key: string;
  label: string;
  scope: Exclude<ApprovalScope, "once">;
  created_at_ms: number;
  expires_at_ms: number | null;
}

export async function fetchApprovalGrants(token: string): Promise<ApprovalGrant[]> {
  const res = await fetchWithTimeout(
    "/api/webui/approvals/grants",
    { headers: { Authorization: `Bearer ${token}` }, credentials: "same-origin" },
    20_000,
  );
  if (!res.ok) throw new Error(`grants failed: HTTP ${res.status}`);
  return ((await res.json()) as { grants: ApprovalGrant[] }).grants;
}

/** Revoke one standing yes, or all of them without a key. */
export async function revokeApprovalGrant(client: NanobotClient, key?: string): Promise<ApprovalGrant[]> {
  const body = await client.requestMutation<{ grants: ApprovalGrant[] }>("approval.grants.revoke", key ? { key } : {});
  return body.grants;
}

export async function fetchApprovals(token: string, sessionKey: string): Promise<ApprovalRecord[]> {
  const res = await fetchWithTimeout(
    `/api/webui/approvals?session_key=${encodeURIComponent(sessionKey)}`,
    { headers: { Authorization: `Bearer ${token}` }, credentials: "same-origin" },
    20_000,
  );
  if (!res.ok) throw new Error(`approvals failed: HTTP ${res.status}`);
  return ((await res.json()) as { approvals: ApprovalRecord[] }).approvals;
}

/** Global approvals across main, helper, browser_desk, and scheduled sessions. */
export async function fetchAllApprovals(token: string): Promise<ApprovalRecord[]> {
  const res = await fetchWithTimeout(
    "/api/webui/approvals?all=1",
    { headers: { Authorization: `Bearer ${token}` }, credentials: "same-origin" },
    20_000,
  );
  if (!res.ok) throw new Error(`approvals failed: HTTP ${res.status}`);
  return ((await res.json()) as { approvals: ApprovalRecord[] }).approvals;
}

/** Approving runs the recorded call on the server; the result comes back in the record. */
export function decideApproval(
  client: NanobotClient,
  id: string,
  decision: "approve" | "reject",
  scope: ApprovalScope = "once",
): Promise<ApprovalRecord> {
  return client.requestMutation<ApprovalRecord>("approval.decide", { id, decision, scope }, 60_000);
}

/** Mutations travel over the authenticated socket, like every WebUI write. */
export function updateCompanionState(
  client: NanobotClient,
  changes: CompanionChanges,
): Promise<CompanionState> {
  return client.requestMutation<CompanionState>("companion.update", { changes });
}

/** Remembered lines from USER.md and memory/MEMORY.md, grouped by heading. */
export interface MemoryItem {
  id: string;
  file: string;
  /** The exact source line; forgetting removes precisely this line. */
  line: string;
  text: string;
  /** Whether the user pinned this memory to protect it from automatic tidy/fade. */
  pinned?: boolean;
}

/** One change to memory: learned from the chats (added, corrected, dropped), or a tidy-up
 * (lines merged into one, a line dropped). Every one can be undone. */
export interface MemoryChange {
  id: string;
  /** Seconds since the epoch. */
  at: number;
  file: string;
  kind: "merge" | "drop" | "learn-add" | "learn-update" | "learn-remove" | "fade" | "forget";
  before: string[];
  after: string | null;
  why?: string;
  undone?: boolean;
}

export interface MemoryView {
  sections: { file: string; title: string; items: MemoryItem[] }[];
  changes?: MemoryChange[];
  /** When it last read the chats to update memory (seconds since the epoch). */
  learned_at?: number | null;
  /** After 整理一下: how many changes that pass made. */
  tidied?: { changed: number; running?: boolean };
  /** After 现在更新: how many changes it learned. */
  learned?: { learned: number; running?: boolean; error?: string };
}

export async function fetchMemory(token: string): Promise<MemoryView> {
  const res = await fetchWithTimeout(
    "/api/webui/companion/memory",
    { headers: { Authorization: `Bearer ${token}` }, credentials: "same-origin" },
    20_000,
  );
  if (!res.ok) throw new Error(`memory failed: HTTP ${res.status}`);
  return (await res.json()) as MemoryView;
}

export function forgetMemory(client: NanobotClient, item: MemoryItem): Promise<MemoryView> {
  return client.requestMutation<MemoryView>("companion.memory.forget", { file: item.file, line: item.line });
}

export function rememberMemory(client: NanobotClient, text: string): Promise<MemoryView> {
  return client.requestMutation<MemoryView>("companion.memory.remember", { text });
}

export function pinMemory(
  client: NanobotClient,
  item: Pick<MemoryItem, "file" | "line">,
  pinned?: boolean,
): Promise<MemoryView & { pinned?: boolean }> {
  return client.requestMutation<MemoryView & { pinned?: boolean }>("companion.memory.pin", {
    file: item.file,
    line: item.line,
    ...(pinned !== undefined ? { pinned } : {}),
  });
}

/** Tidy now (the model proposes, the server enforces the rules); can take a minute. */
export function tidyMemory(client: NanobotClient): Promise<MemoryView> {
  return client.requestMutation<MemoryView>("companion.memory.tidy", {}, 180_000);
}

/** Read the chats since the last pass and update memory now; can take a minute. */
export function learnMemory(client: NanobotClient): Promise<MemoryView> {
  return client.requestMutation<MemoryView>("companion.memory.learn", {}, 180_000);
}

export function undoMemoryChange(client: NanobotClient, id: string): Promise<MemoryView> {
  return client.requestMutation<MemoryView>("companion.memory.undo", { id });
}

/** Things the companion offers to do, generated from memory and recent chat. */
export interface Idea {
  title: string;
  reason: string;
  /** Sent as the user's message when they tap 去做. */
  prompt: string;
}

export interface IdeasPayload {
  generated_at: number;
  ideas: Idea[];
}

const IDEAS_TIMEOUT_MS = 90_000;

export async function fetchIdeas(token: string): Promise<IdeasPayload> {
  const res = await fetchWithTimeout(
    "/api/webui/companion/ideas",
    { headers: { Authorization: `Bearer ${token}` }, credentials: "same-origin" },
    IDEAS_TIMEOUT_MS,
  );
  if (!res.ok) {
    const detail = (await res.text()).trim();
    throw new Error(detail.startsWith("{") ? (JSON.parse(detail).error ?? detail) : detail || `HTTP ${res.status}`);
  }
  return (await res.json()) as IdeasPayload;
}

export function refreshIdeas(client: NanobotClient): Promise<IdeasPayload> {
  return client.requestMutation<IdeasPayload>("companion.ideas.refresh", {}, IDEAS_TIMEOUT_MS);
}

/** Web Push: the server's application key and how many devices are subscribed. */
export interface PushConfig {
  public_key: string;
  devices: number;
}

export async function fetchPushConfig(token: string): Promise<PushConfig> {
  const res = await fetchWithTimeout(
    "/api/webui/push",
    { headers: { Authorization: `Bearer ${token}` }, credentials: "same-origin" },
    20_000,
  );
  if (!res.ok) throw new Error(`push config failed: HTTP ${res.status}`);
  return (await res.json()) as PushConfig;
}

export function subscribePush(client: NanobotClient, subscription: PushSubscriptionJSON): Promise<{ devices: number }> {
  return client.requestMutation("push.subscribe", { subscription });
}

export function unsubscribePush(client: NanobotClient, endpoint: string): Promise<{ devices: number }> {
  return client.requestMutation("push.unsubscribe", { endpoint });
}

export function sendTestPush(client: NanobotClient): Promise<{ sent: number }> {
  return client.requestMutation("push.test", {}, 30_000);
}

/* ---------- 模型: routing, fallbacks, usage ---------- */

export type ModelTier = "cheap" | "balanced" | "strong";

/** How the router sees one preset (``nanobot/webui/model_router.py``). */
export interface ModelInfo {
  description: string;
  tier: ModelTier;
  vision: boolean;
  /** ¥ per million tokens; ``null`` when not entered. */
  price_in: number | null;
  price_out: number | null;
}

export interface RouterSettings {
  enabled: boolean;
  /** Preset that does the routing; ``null`` = the cheapest listed model. */
  router: string | null;
  /** With routing off: the model the companion always uses. */
  fixed: string | null;
  models: Record<string, ModelInfo>;
}

export interface RoutingDecision {
  turn_id: string | null;
  at_ms: number;
  preset: string;
  rule: string;
  reason: string;
}

/** How hard a model thinks before answering; ``null`` keeps the provider's default. */
export type ReasoningEffort = "none" | "low" | "medium" | "high" | "xhigh" | "max" | "adaptive";

export interface ModelPreset {
  name: string;
  model: string;
  provider: string;
  /** The company in the catalog this model's service belongs to, when it is one. */
  vendor: string | null;
  reasoning_effort: ReasoningEffort | null;
  /** Longest single reply, in tokens. */
  max_tokens: number;
  temperature: number;
  context_window_tokens: number;
}

export interface ModelLimits {
  reasoning_effort?: ReasoningEffort | null;
  max_tokens?: number;
  temperature?: number;
}

export interface ModelsPayload {
  presets: ModelPreset[];
  /** Most tool steps one reply may take. */
  max_tool_iterations: number;
  /** The normal preset (``agents.defaults.model_preset``). */
  default: string | null;
  /** Tried in order when a model call fails. */
  fallbacks: string[];
  settings: RouterSettings;
  /** Last check per model; a model the provider refused is left out of routing. */
  /** ``latency_ms``: how long a one-token answer took (quick models get the small calls). */
  health: Record<string, { ok: boolean; error: string | null; checked_at: number; latency_ms?: number | null }>;
  decisions: RoutingDecision[];
  media: Record<MediaKind, { active: string | null; models: MediaModel[] }>;
  /** After adding: the new model's name. */
  added?: string;
}

export interface ModelsChanges extends Partial<Omit<RouterSettings, "models">> {
  /** The everyday chat model. */
  default?: string;
  models?: Record<string, ModelInfo | null>;
  fallbacks?: string[];
  /** Per-model limits, saved on the model preset itself. */
  presets?: Record<string, ModelLimits>;
  max_tool_iterations?: number;
  /** Which drawing / video model is in use (null: none). */
  media_active?: Partial<Record<MediaKind, string | null>>;
}

/** 对话 (chat), 图像 (drawing) and 视频 models are listed and added apart. */
export type ModelKind = "text" | MediaKind;
export type MediaKind = "image" | "video";

export interface MediaModel {
  name: string;
  model: string;
  vendor: string | null;
  /** The Hermes backend that runs it: openai (any OpenAI-compatible images API), openrouter, xai. */
  backend: string;
  /** From 检测 (the company's platform); absent until checked. */
  price?: Price | null;
}

/** A company 添加模型 offers: paste its key, pick a model. */
export interface ModelVendor {
  id: string;
  name: string;
  kinds: ModelKind[];
  /** Where to get an API key. */
  console: string;
  logo_url: string | null;
  color: string | null;
  note: string;
  /** Its key is saved already: the key field may stay empty. For a subscription: it is signed in. */
  has_key: boolean;
  /** 用订阅账号登录 / 国内 / 海外 / 聚合平台 */
  group: "sub" | "cn" | "global" | "hub";
  /** A subscription signs in instead of taking a key: "chatgpt" | "grok". */
  login: string | null;
  /** This machine is signed in already (the Codex CLI's ~/.codex/auth.json): import it. */
  local_login?: boolean;
}

/** A subscription sign-in in progress: enter ``user_code`` on ``url``; the server waits for it. */
export interface LoginState {
  id: string;
  kind: string;
  status: "waiting" | "done" | "failed";
  error: string | null;
  user_code: string;
  url: string;
  expires_in: number;
}

export function startLogin(client: NanobotClient, login: string): Promise<LoginState> {
  return client.requestMutation<LoginState>("companion.oauth.start", { login }, 30_000);
}

export function importLogin(client: NanobotClient, login: string): Promise<{ ok: boolean }> {
  return client.requestMutation<{ ok: boolean }>("companion.oauth.import", { login }, 30_000);
}

export function loginStatus(client: NanobotClient, id: string): Promise<LoginState> {
  return client.requestMutation<LoginState>("companion.oauth.status", { id });
}

/** One of a company's models, as the open registry models.dev describes it. */
export interface VendorModel {
  id: string;
  name: string;
  note: string;
  vision: boolean;
  reasoning: boolean;
  context: number | null;
  /** 元 per million tokens. */
  price_in: number | null;
  price_out: number | null;
  recommended: boolean;
  /** Any kind's price, in its own style (per token, second, clip, picture). */
  price?: Price | null;
  /** Found on the company's platform at the last 检测. */
  checked?: boolean;
  /** A drawing/video model the platform no longer listed at the last 检测. */
  missing?: boolean;
}

/** A company's models of one kind, and when its platform was last read (检测). */
export interface VendorModels {
  models: VendorModel[];
  checked: { at: number; sources: string[] } | null;
  can_check: boolean;
}

export async function fetchModelCatalog(token: string): Promise<ModelVendor[]> {
  const res = await fetchWithTimeout(
    "/api/webui/companion/model-catalog",
    { headers: { Authorization: `Bearer ${token}` }, credentials: "same-origin" },
    20_000,
  );
  if (!res.ok) throw new Error(`model catalog failed: HTTP ${res.status}`);
  return ((await res.json()) as { vendors: ModelVendor[] }).vendors;
}

export async function fetchVendorModels(client: NanobotClient, vendor: string, kind: ModelKind): Promise<VendorModels> {
  return client.requestMutation<VendorModels>("companion.models.vendor", { vendor, kind }, 30_000);
}

/** 检测: the company's platform read now for its current model IDs and prices (up to two minutes). */
export async function checkVendorModels(client: NanobotClient, vendor: string, kind: ModelKind): Promise<VendorModels> {
  return client.requestMutation<VendorModels>("companion.models.check", { vendor, kind }, 150_000);
}

export async function fetchModels(token: string): Promise<ModelsPayload> {
  const res = await fetchWithTimeout(
    "/api/webui/companion/models",
    { headers: { Authorization: `Bearer ${token}` }, credentials: "same-origin" },
    20_000,
  );
  if (!res.ok) throw new Error(`models failed: HTTP ${res.status}`);
  return (await res.json()) as ModelsPayload;
}

export function updateModels(client: NanobotClient, changes: ModelsChanges): Promise<ModelsPayload> {
  return client.requestMutation<ModelsPayload>("companion.models.update", { changes });
}

/** One tiny request per model (all when ``names`` is omitted) to see which ones the provider serves. */
export function checkModels(client: NanobotClient, names?: string[]): Promise<ModelsPayload> {
  return client.requestMutation<ModelsPayload>("companion.models.check", names ? { names } : {}, 120_000);
}

export interface UsageModelRow {
  provider: string;
  model: string;
  preset: string | null;
  calls: number;
  input_tokens: number;
  output_tokens: number;
  /** The part of the input that was a cache hit (billed at about a tenth). */
  cached_tokens?: number;
  /** Estimated ¥ from the prices entered; ``null`` without prices. */
  cost: number | null;
  /** On a subscription (会员账号登录): the plan pays, nothing per token. */
  subscription?: boolean;
}

export interface UsagePeriod {
  by_model: UsageModelRow[];
  /** Tokens per source: user (chat), cron (background), router, dream (memory), system, api. */
  by_source: Record<string, number>;
  /** All input, and the part of it that was a cache hit. */
  input_tokens?: number;
  cached_tokens?: number;
  /** Text and pictures/clips together; null when nothing used had a price. */
  cost: number | null;
  media?: UsageMediaRow[];
}

export interface UsageDailyBucket {
  date: string;
  input_tokens: number;
  cached_tokens: number;
  output_tokens: number;
  cost: number | null;
  media_cost: number;
  requests: number;
}

export interface UsagePayload {
  today: UsagePeriod;
  week: UsagePeriod;
  /** Last 14 days in ascending YYYY-MM-DD order (optional for older servers). */
  daily?: UsageDailyBucket[];
  /** 7-day prompt cache hit rate in [0, 1]. */
  cache_hit_rate?: number;
  /** Estimated 7-day CNY cost saved via prompt cache hits. */
  cache_saved_cost?: number;
}

/** Pictures or clips made with one model in a period. */
export interface UsageMediaRow {
  name: string;
  kind: MediaKind;
  model: string;
  count: number;
  seconds: number;
  /** ¥ from its price; null when it has none (检测 in 模型 fills it in). */
  cost: number | null;
  /** On a subscription (会员账号登录): the plan pays, nothing per picture or clip. */
  subscription?: boolean;
}

export async function fetchUsage(token: string): Promise<UsagePayload> {
  const res = await fetchWithTimeout(
    "/api/webui/companion/usage",
    { headers: { Authorization: `Bearer ${token}` }, credentials: "same-origin" },
    20_000,
  );
  if (!res.ok) throw new Error(`usage failed: HTTP ${res.status}`);
  return (await res.json()) as UsagePayload;
}

export type CreationKind = "image" | "video" | "document" | "audio";

/** One file in 创意: something the companion made or sent, filed by kind. */
export interface Creation {
  /** Path under 创意, e.g. "图片/2026-10-18 海边的猫.png". */
  id: string;
  kind: CreationKind;
  name: string;
  title: string;
  /** generated (its own image tool), chat (sent in the chat), link (downloaded), workspace (written there), canvas (saved from Artifact canvas). */
  origin: "generated" | "chat" | "link" | "workspace" | "canvas";
  prompt: string | null;
  size: number;
  created_at: string;
  url: string | null;
}

export interface CreationsPayload {
  items: Creation[];
  counts: Record<CreationKind, number>;
}

export async function fetchCreations(token: string): Promise<CreationsPayload> {
  const res = await fetchWithTimeout(
    "/api/webui/companion/creations",
    { headers: { Authorization: `Bearer ${token}` }, credentials: "same-origin" },
    20_000,
  );
  if (!res.ok) throw new Error(`creations failed: HTTP ${res.status}`);
  return (await res.json()) as CreationsPayload;
}

/** Take a file off the 创意 list; it stays on the bot's disk. */
export function hideCreation(client: NanobotClient, id: string): Promise<CreationsPayload> {
  return client.requestMutation<CreationsPayload>("companion.creations.hide", { id });
}

export type ArtifactSaveFormat =
  | "html"
  | "htm"
  | "svg"
  | "md"
  | "markdown"
  | "mermaid"
  | "mmd"
  | "txt"
  | "text"
  | "json"
  | "csv"
  | "py"
  | "python"
  | "js"
  | "javascript"
  | "ts"
  | "typescript"
  | "tsx"
  | "css"
  | "sh"
  | "bash"
  | "yaml"
  | "yml";

export interface SaveCreationArtifactInput {
  /** Existing creation id to update in place (triggers version backup on server). */
  id?: string;
  title: string;
  content: string;
  format?: ArtifactSaveFormat;
  prompt?: string;
}

export interface SaveCreationArtifactPayload extends CreationsPayload {
  saved_id: string;
  updated?: boolean;
}

/** Save or update a canvas/code artifact in 创意 and return the updated list + saved_id. */
export function saveCreationArtifact(
  client: NanobotClient,
  input: SaveCreationArtifactInput,
): Promise<SaveCreationArtifactPayload> {
  return client.requestMutation<SaveCreationArtifactPayload>("companion.creations.save", {
    ...(input.id ? { id: input.id } : {}),
    title: input.title,
    content: input.content,
    format: input.format ?? "md",
    ...(input.prompt ? { prompt: input.prompt } : {}),
  });
}

/** A picture or an imported Codex pet the user added as the companion's look. */
export interface CustomAvatar {
  id: string;
  kind: "image" | "pet";
  name: string;
  created_at: string;
  /** For pictures. */
  image_url: string | null;
  /** For pets: the Codex atlas and, when present, the errands atlas. */
  sheet_url: string | null;
  actions_url: string | null;
  /** A look drawn from a description: shown whole, with a busy and a happy picture. */
  character?: boolean;
  working_url?: string | null;
  happy_url?: string | null;
  /** Its motion clips (looping animated pictures) by state: idle, thinking, working, waiting. */
  motions?: Partial<Record<"idle" | "thinking" | "working" | "waiting", string>>;
  /** The clips are still being made (the video model is at work). */
  motion_pending?: boolean;
}

export interface AvatarsPayload {
  items: CustomAvatar[];
}

export async function fetchAvatars(token: string): Promise<AvatarsPayload> {
  const res = await fetchWithTimeout(
    "/api/webui/companion/avatars",
    { headers: { Authorization: `Bearer ${token}` }, credentials: "same-origin" },
    20_000,
  );
  if (!res.ok) throw new Error(`avatars failed: HTTP ${res.status}`);
  return (await res.json()) as AvatarsPayload;
}

/* ---------- adding models and services (the app's own console) ---------- */

export interface NewModel {
  kind: ModelKind;
  /** A company from the catalog, or "custom" for any OpenAI-compatible service (then base_url). */
  vendor: string;
  /** Only for "custom": the OpenAI-compatible address, e.g. https://api.example.com/v1 */
  base_url?: string;
  /** Left empty to reuse the key saved for this company / address. */
  api_key: string;
  model: string;
  /** Optional; the model's own name otherwise. */
  name?: string;
}

/** Saves the model and checks right away whether the service answers. */
export function addModel(client: NanobotClient, values: NewModel): Promise<ModelsPayload> {
  return client.requestMutation<ModelsPayload>("companion.models.add", { ...values }, 60_000);
}

export function removeModel(client: NanobotClient, name: string): Promise<ModelsPayload> {
  return client.requestMutation<ModelsPayload>("companion.models.remove", { name });
}

export type NewService =
  | { kind: "url"; name: string; url: string; token: string }
  | { kind: "command"; name: string; command: string; env: string };

export function addService(client: NanobotClient, values: NewService): Promise<McpPresetsPayload> {
  return client.requestMutation<McpPresetsPayload>("settings.mcp.custom", { ...values }, 180_000);
}

/* ---------- SOUL.md and MEMORY.md, edited as documents ---------- */

async function getJson<T>(token: string, path: string): Promise<T> {
  const res = await fetchWithTimeout(path, { headers: { Authorization: `Bearer ${token}` }, credentials: "same-origin" }, 20_000);
  if (!res.ok) throw new Error(`${path}: HTTP ${res.status}`);
  return (await res.json()) as T;
}

/** Who the companion is, in its own words (the user's part of SOUL.md). */
export function fetchSoul(token: string): Promise<{ text: string }> {
  return getJson(token, "/api/webui/companion/soul");
}

export function saveSoul(client: NanobotClient, text: string): Promise<{ text: string }> {
  return client.requestMutation<{ text: string }>("companion.soul.update", { text });
}

/** Everything it remembers, as one document: 关于你 and 我记下的, one entry per paragraph. */
export function fetchMemoryDoc(token: string): Promise<{ text: string }> {
  return getJson(token, "/api/webui/companion/memory-doc");
}

export function saveMemoryDoc(client: NanobotClient, text: string): Promise<{ text: string }> {
  return client.requestMutation<{ text: string }>("companion.memory.doc", { text });
}

/* ---------- The browser it works in ---------- */

export interface ComputerStatus {
  running: boolean;
  since: number | null;
  page: { title: string; url: string } | null;
  /** Who has the browser: the user after 接手, or "handoff" while the companion waits on the user. */
  hold: "user" | "handoff" | null;
  /** The companion asked the user to do something in the browser (a login, a captcha). */
  handoff: { id: string; reason: string; at: number } | null;
  last_action: { verb: string; detail: string; at: number } | null;
}

export function fetchComputer(token: string): Promise<ComputerStatus> {
  return getJson(token, "/api/webui/companion/computer");
}

/* ---------- 动态 ---------- */

/** One post the companion wrote for the feed (see the server's modules/feed). */
export interface FeedPost {
  id: string;
  /** Written at, ms. */
  ts: number;
  title: string;
  /** A short read in markdown; key words link to their sources. */
  body: string;
  area: string;
  /** A picture of the story (https), or null. */
  image: string | null;
  /** A follow-up the user might ask, or "". */
  prompt: string;
}

export interface FeedPayload {
  /** The sentence that steers every post. */
  instructions: string;
  /** The daily routine: on, at ``time`` (HH:MM, local). */
  daily: boolean;
  time: string;
  generated_at: number | null;
  posts: FeedPost[];
  /** Posts with a ❤ (kept on the server: the next run writes more like them). */
  liked?: string[];
}

export async function fetchFeed(token: string): Promise<FeedPayload> {
  const res = await fetchWithTimeout(
    "/api/webui/companion/feed",
    { headers: { Authorization: `Bearer ${token}` }, credentials: "same-origin" },
    20_000,
  );
  if (!res.ok) throw new Error(`feed failed: HTTP ${res.status}`);
  return (await res.json()) as FeedPayload;
}

export function changeFeed(
  client: NanobotClient,
  action: "update" | "write_now" | "delete" | "like" | "discussed",
  extra: { changes?: Partial<Pick<FeedPayload, "instructions" | "daily" | "time">>; id?: string; on?: boolean } = {},
): Promise<FeedPayload> {
  return client.requestMutation<FeedPayload>("companion.feed.change", { action, ...extra });
}

/** A side chat (旁聊): rename it or delete it. */
export function changeChat(client: NanobotClient, id: string, action: "rename" | "delete", title?: string): Promise<unknown> {
  return client.requestMutation("companion.chats.change", { id, action, title });
}

export interface SetChatModelPayload {
  sessions?: unknown[];
  chat_id: string;
  model_preset: string | null;
  reasoning_effort: string | null;
}

/** Override or reset (null) the model preset and reasoning effort for one chat (main or side). */
export function setChatModel(
  client: NanobotClient,
  chatId: string,
  options: { model_preset?: string | null; reasoning_effort?: ReasoningEffort | null },
): Promise<SetChatModelPayload> {
  const payload: Record<string, unknown> = { id: chatId, action: "set_model" };
  if ("model_preset" in options) payload.model_preset = options.model_preset ?? null;
  if ("reasoning_effort" in options) payload.reasoning_effort = options.reasoning_effort ?? null;
  return client.requestMutation<SetChatModelPayload>("companion.chats.change", payload);
}

/** Pin or unpin a side chat so it stays at the top of the sidebar. */
export function pinChat(
  client: NanobotClient,
  chatId: string,
  pinned?: boolean,
): Promise<{ sessions?: unknown[] }> {
  return client.requestMutation<{ sessions?: unknown[] }>("companion.chats.change", {
    id: chatId,
    action: "pin",
    ...(pinned !== undefined ? { pinned } : {}),
  });
}

/** Archive or unarchive a side chat. */
export function archiveChat(
  client: NanobotClient,
  chatId: string,
  archived?: boolean,
): Promise<{ sessions?: unknown[] }> {
  return client.requestMutation<{ sessions?: unknown[] }>("companion.chats.change", {
    id: chatId,
    action: "archive",
    ...(archived !== undefined ? { archived } : {}),
  });
}

/** Remove all empty, unpinned, inactive side chats in one step. */
export function pruneEmptyChats(
  client: NanobotClient,
): Promise<{ sessions?: unknown[]; removed_count: number }> {
  return client.requestMutation<{ sessions?: unknown[]; removed_count: number }>("companion.chats.change", {
    action: "prune_empty",
  });
}

export interface ChatContextUsage {
  chat_id?: string;
  estimated_tokens: number;
  context_window: number;
  usage_ratio: number;
  tier1_threshold: number;
  tier2_threshold: number;
  has_summary_checkpoint: boolean;
  model: string;
  model_preset: string | null;
  reasoning_effort: string | null;
}

/** Fetch active context window occupancy for one websocket chat session. */
export async function fetchChatContext(
  token: string,
  chatId: string,
  signal?: AbortSignal,
): Promise<ChatContextUsage> {
  const key = chatId.startsWith("websocket:") ? chatId : `websocket:${chatId}`;
  const res = await fetchWithTimeout(
    `/api/sessions/${encodeURIComponent(key)}/context`,
    {
      headers: { Authorization: `Bearer ${token}` },
      credentials: "same-origin",
      signal,
    },
    15_000,
  );
  if (!res.ok) throw new Error(`context failed: HTTP ${res.status}`);
  return (await res.json()) as ChatContextUsage;
}

/** One message out of a chat (long-press → 删除): the user's, or a whole reply. */
export function deleteMessage(client: NanobotClient, chatId: string, turnId: string, role: "user" | "reply"): Promise<unknown> {
  return client.requestMutation("companion.chats.change", { id: chatId, action: "delete_message", turn_id: turnId, role });
}

/** 重新生成: the newest answer again (the server takes the old one out and asks again). */
export function regenerateReply(client: NanobotClient, chatId: string, turnId: string): Promise<unknown> {
  return client.requestMutation("companion.chats.change", { id: chatId, action: "regenerate", turn_id: turnId });
}

/** Edit a user message in place, drop later turns, and resubmit for a fresh streaming reply. */
export function editAndResubmitMessage(
  client: NanobotClient,
  chatId: string,
  turnId: string,
  content: string,
): Promise<{ chat_id?: string; turn_id?: string }> {
  return client.requestMutation<{ chat_id?: string; turn_id?: string }>("companion.chats.change", {
    id: chatId,
    action: "edit_message",
    turn_id: turnId,
    content,
  });
}

/** Fork a new side chat from the main chat or any side chat up to ``turnId``. */
export function forkSideChat(
  client: NanobotClient,
  chatId: string,
  turnId?: string | null,
  title?: string,
): Promise<{ chat_id: string; key?: string }> {
  const payload: Record<string, unknown> = { id: chatId, action: "fork" };
  if (turnId) payload.turn_id = turnId;
  if (title) payload.title = title;
  return client.requestMutation<{ chat_id: string; key?: string }>("companion.chats.change", payload);
}

export type SearchScope = "all" | "main" | "sides" | "helpers";

export interface ChatSearchHit {
  chat_id: string;
  key?: string;
  chat_title: string;
  title?: string;
  main?: boolean;
  helper?: boolean | string;
  role: "user" | "assistant";
  text: string;
  snippet?: string;
  turn_id: string | null;
  at_ms: number;
  created_at_ms?: number;
  matches?: Array<[number, number]>;
}

export interface ChatSearchOptions {
  scope?: SearchScope;
  chatId?: string;
  role?: "user" | "assistant";
  limit?: number;
  signal?: AbortSignal;
}

/** 查找聊天记录: messages in any chat that say the words, newest first. */
export async function searchChats(
  token: string,
  query: string,
  options: ChatSearchOptions = {},
): Promise<ChatSearchHit[]> {
  const extra: string[] = [];
  if (options.scope) extra.push(`scope=${encodeURIComponent(options.scope)}`);
  if (options.chatId) extra.push(`chat_id=${encodeURIComponent(options.chatId)}`);
  if (options.role) extra.push(`role=${encodeURIComponent(options.role)}`);
  if (typeof options.limit === "number") extra.push(`limit=${encodeURIComponent(String(options.limit))}`);
  const qs = `q=${encodeURIComponent(query)}${extra.length ? `&${extra.join("&")}` : ""}`;
  const res = await fetchWithTimeout(
    `/api/webui/companion/search?${qs}`,
    {
      headers: { Authorization: `Bearer ${token}` },
      credentials: "same-origin",
      signal: options.signal,
    },
    20_000,
  );
  if (!res.ok) throw new Error(`search failed: HTTP ${res.status}`);
  return ((await res.json()) as { hits?: ChatSearchHit[] }).hits ?? [];
}

export interface FollowupsResponse {
  chat_id: string;
  turn_id: string;
  suggestions: string[];
}

/** Fetch suggested contextual follow-up prompts for a completed reply turn. */
export async function fetchFollowups(
  token: string,
  chatId: string,
  turnId?: string | null,
  signal?: AbortSignal,
): Promise<FollowupsResponse> {
  const key = chatId.startsWith("websocket:") ? chatId : `websocket:${chatId}`;
  const query = turnId ? `?turn_id=${encodeURIComponent(turnId)}` : "";
  const res = await fetchWithTimeout(
    `/api/sessions/${encodeURIComponent(key)}/followups${query}`,
    {
      headers: { Authorization: `Bearer ${token}` },
      credentials: "same-origin",
      signal,
    },
    15_000,
  );
  if (!res.ok) throw new Error(`followups failed: HTTP ${res.status}`);
  const data = (await res.json()) as Partial<FollowupsResponse>;
  return {
    chat_id: data.chat_id ?? chatId,
    turn_id: data.turn_id ?? turnId ?? "",
    suggestions: Array.isArray(data.suggestions) ? data.suggestions : [],
  };
}

export interface TtsSynthesisResult {
  ok: boolean;
  url?: string;
  cached?: boolean;
  provider?: string;
  voice?: string;
  error?: string;
}

/** Request cached neural speech synthesis audio from the companion server. */
export async function synthesizeTts(
  token: string,
  text: string,
  options: { voice?: string; speed?: number; provider?: string; signal?: AbortSignal } = {},
): Promise<TtsSynthesisResult> {
  const body: Record<string, unknown> = { text };
  if (options.voice) body.voice = options.voice;
  if (typeof options.speed === "number") body.speed = options.speed;
  if (options.provider) body.provider = options.provider;
  const res = await fetchWithTimeout(
    "/api/webui/companion/tts",
    {
      method: "POST",
      headers: {
        Authorization: `Bearer ${token}`,
        "Content-Type": "application/json",
      },
      credentials: "same-origin",
      body: JSON.stringify(body),
      signal: options.signal,
    },
    30_000,
  );
  let data: Partial<TtsSynthesisResult>;
  try {
    data = (await res.json()) as Partial<TtsSynthesisResult>;
  } catch {
    data = {};
  }
  if (!res.ok) {
    return { ok: false, error: data.error ?? `TTS failed: HTTP ${res.status}` };
  }
  return {
    ok: Boolean(data.ok && data.url),
    url: data.url,
    cached: data.cached,
    provider: data.provider,
    voice: data.voice,
    error: data.error,
  };
}

/* ---------- 帮手: helpers the companion calls on ---------- */

export interface HelperWork {
  turn_id: string;
  /** Epoch milliseconds. */
  at: number;
  /** Which chat handed it the work (主要聊天, a side chat's name, or 你). */
  from: string;
  task: string;
  answer: string;
  done: boolean;
}

export interface Helper {
  id: string;
  name: string;
  emoji: string;
  /** A photo for its face (a data URL), shown instead of the emoji; "" for none. */
  avatar: string;
  title: string;
  instructions: string;
  chat_id: string;
  notes: string[];
  /** A model from the 模型 page, or "" for the main assistant's. */
  model: string;
  /** One of the six defaults ("" for one the user made). */
  preset: string;
  /** A default one still as it came. */
  is_default: boolean;
  /** Just made and not set up yet: it asks the user what to do in its chat. */
  setup: boolean;
  working: boolean;
  work: HelperWork[];
  /** What it has learned of the user's ways in its line of work; it follows them by default. */
  habits: HelperHabit[];
  /** The newest message in its chat, for the list. */
  last: LastMessage | null;
  created_at: number;
}

export interface HelperHabit {
  id: string;
  text: string;
  /** "you": typed on its profile; "chat": it heard the user say so; "learned": noticed over time. */
  by: "you" | "chat" | "learned";
  at: number;
}

export interface LastMessage {
  text: string;
  at: number;
  /** "我", the speaker's name in a group, or "" for the helper itself. */
  who: string;
}

export interface HelperTemplate {
  name: string;
  emoji: string;
  title: string;
  instructions: string;
  /** A few things to ask it, offered in its empty chat. */
  starters?: string[];
}

/** 群聊: a group chat of helpers. */
export interface HelperGroup {
  id: string;
  name: string;
  /** Helper ids. */
  members: string[];
  chat_id: string;
  created_at: number;
  last?: LastMessage | null;
}

export interface HelpersPayload {
  helpers: Helper[];
  templates: HelperTemplate[];
  max: number;
  groups: HelperGroup[];
  /** Most helpers in one group. */
  max_group: number;
  /** After making a helper or a group: the chat to open. */
  opened?: string;
}

export async function fetchHelpers(token: string): Promise<HelpersPayload> {
  const res = await fetchWithTimeout(
    "/api/webui/companion/helpers",
    { headers: { Authorization: `Bearer ${token}` }, credentials: "same-origin" },
    20_000,
  );
  if (!res.ok) throw new Error(`helpers failed: HTTP ${res.status}`);
  return (await res.json()) as HelpersPayload;
}

export type HelperFields = Partial<Pick<Helper, "name" | "emoji" | "avatar" | "title" | "instructions" | "notes" | "model">>;

export function changeHelper(
  client: NanobotClient,
  action: "create" | "blank" | "update" | "reset" | "delete",
  extra: { id?: string; idea?: string } & HelperFields = {},
): Promise<HelpersPayload> {
  return client.requestMutation<HelpersPayload>("companion.helpers.change", { action, ...extra });
}

/** 让 AI 画一个: the image model draws its face (takes a minute or so); it is put on right away. */
export function drawHelperAvatar(client: NanobotClient, id: string, idea: string): Promise<HelpersPayload> {
  return client.requestMutation<HelpersPayload>("companion.helpers.change", { action: "avatar", id, idea }, 240_000);
}

/** A helper's settings as text to send someone; they paste it into 新建帮手 → 导入. */
export function helperShareText(helper: Pick<Helper, "name" | "emoji" | "title" | "instructions">): string {
  return JSON.stringify({ helper: 1, name: helper.name, emoji: helper.emoji, title: helper.title, instructions: helper.instructions }, null, 1);
}

/** Settings pasted from 分享: the fields to start a new helper from, or null when it isn't one. */
export function parseHelperShare(text: string): HelperFields | null {
  const start = text.indexOf("{");
  const end = text.lastIndexOf("}");
  if (start < 0 || end <= start) return null;
  try {
    const value = JSON.parse(text.slice(start, end + 1)) as Record<string, unknown>;
    if (typeof value.name !== "string" || !value.name.trim()) return null;
    const str = (key: string, max: number) => (typeof value[key] === "string" ? (value[key] as string).trim().slice(0, max) : "");
    return { name: str("name", 12), emoji: str("emoji", 4) || "🤖", title: str("title", 16), instructions: str("instructions", 2000) };
  } catch {
    return null;
  }
}

/** 它摸清的你的习惯: one the user tells it on its profile, or drops. */
export function changeHabit(client: NanobotClient, helperId: string, change: { text: string } | { habit: string }): Promise<HelpersPayload> {
  return client.requestMutation<HelpersPayload>("companion.helpers.change", {
    action: "text" in change ? "habit_add" : "habit_remove",
    id: helperId,
    ...change,
  });
}

export function changeGroup(
  client: NanobotClient,
  action: "create" | "update" | "delete",
  extra: { id?: string; name?: string; members?: string[] } = {},
): Promise<HelpersPayload> {
  return client.requestMutation<HelpersPayload>("companion.groups.change", { action, ...extra });
}

/* ---------- 权限: what it may do on its own ---------- */

export type PermissionLevel = "deny" | "ask" | "allow";

export interface PermissionKind {
  key: string;
  label: string;
  detail: string;
  level: PermissionLevel;
  /** The levels this kind offers (paying can't be 允许). */
  levels: PermissionLevel[];
}

export async function fetchPermissions(token: string): Promise<{ kinds: PermissionKind[] }> {
  const res = await fetchWithTimeout(
    "/api/webui/companion/permissions",
    { headers: { Authorization: `Bearer ${token}` }, credentials: "same-origin" },
    20_000,
  );
  if (!res.ok) throw new Error(`permissions failed: HTTP ${res.status}`);
  return (await res.json()) as { kinds: PermissionKind[] };
}

export function setPermission(client: NanobotClient, kind: string, level: PermissionLevel): Promise<{ kinds: PermissionKind[] }> {
  return client.requestMutation("companion.permissions.set", { kind, level });
}

/* ---------- 安全凭证: site logins and API keys, typed in by the user only ---------- */

export interface VaultLogin {
  id: string;
  label: string;
  origin: string;
  identifier: string;
  created_at: string;
}

export interface VaultKey {
  env: string;
  label: string;
  created_at: number;
  /** The variable holds a value now. */
  set: boolean;
}

export interface VaultPayload {
  logins: VaultLogin[];
  keys: VaultKey[];
}

export async function fetchVault(token: string): Promise<VaultPayload> {
  const res = await fetchWithTimeout(
    "/api/webui/companion/vault",
    { headers: { Authorization: `Bearer ${token}` }, credentials: "same-origin" },
    20_000,
  );
  if (!res.ok) throw new Error(`vault failed: HTTP ${res.status}`);
  return (await res.json()) as VaultPayload;
}

/** The password goes to the server only, over the app's own connection; it is never shown again. */
export function saveLogin(client: NanobotClient, values: { label: string; origin: string; identifier: string; password: string }): Promise<VaultPayload> {
  return client.requestMutation<VaultPayload>("companion.vault.change", { action: "add_login", ...values });
}

export function saveKey(client: NanobotClient, values: { label: string; env: string; value: string }): Promise<VaultPayload> {
  return client.requestMutation<VaultPayload>("companion.vault.change", { action: "add_key", ...values });
}

export function removeCredential(client: NanobotClient, kind: "login" | "key", id: string): Promise<VaultPayload> {
  return client.requestMutation<VaultPayload>("companion.vault.change", { action: "remove", kind, id });
}

/* ---------- 技能: what it knows how to do step by step ---------- */

export interface SkillInfo {
  name: string;
  description: string;
  category: string;
  /** Epoch milliseconds. */
  updated_at: number;
}

export interface SkillsPayload {
  /** Ones it wrote itself or the user taught it, newest first. */
  learned: SkillInfo[];
  /** The ones that come with it (read-only here). */
  bundled: SkillInfo[];
}

export async function fetchSkills(token: string): Promise<SkillsPayload> {
  const res = await fetchWithTimeout(
    "/api/webui/companion/skills",
    { headers: { Authorization: `Bearer ${token}` }, credentials: "same-origin" },
    20_000,
  );
  if (!res.ok) throw new Error(`skills failed: HTTP ${res.status}`);
  return (await res.json()) as SkillsPayload;
}

export function readSkill(client: NanobotClient, name: string): Promise<{ name: string; content: string; bundled: boolean }> {
  return client.requestMutation("companion.skills.change", { action: "read", name });
}

export function saveSkill(client: NanobotClient, name: string, content: string, isNew = false): Promise<SkillsPayload> {
  return client.requestMutation<SkillsPayload>("companion.skills.change", { action: "save", name, content, new: isNew });
}

export function deleteSkill(client: NanobotClient, name: string): Promise<SkillsPayload> {
  return client.requestMutation<SkillsPayload>("companion.skills.change", { action: "delete", name });
}

/** One step the user did in the browser while teaching it (教它做). */
export interface TaughtStep {
  do: "open" | "click" | "type" | "key" | "back";
  url?: string;
  target?: string;
  text?: string;
  key?: string;
}

/** Turn recorded steps into a skill draft (name + SKILL.md) to review; can take a minute. */
export function draftSkill(client: NanobotClient, goal: string, steps: TaughtStep[]): Promise<{ name: string; content: string }> {
  return client.requestMutation("companion.skills.change", { action: "draft", goal, steps }, 180_000);
}

/* ---------- 个人知识库: local SQLite FTS5 document vault ---------- */

export interface KnowledgeDocument {
  id: string;
  title: string;
  filename: string;
  collection: string;
  mime: string;
  size_bytes: number;
  char_count: number;
  chunk_count: number;
  content_sha256: string;
  origin: string;
  /** Unix epoch seconds (float). */
  created_at: number;
  /** Unix epoch seconds (float). */
  updated_at: number;
  deduplicated?: boolean;
}

export interface KnowledgeSearchHit {
  doc_id: string;
  title: string;
  filename: string;
  collection: string;
  chunk_index: number;
  heading: string;
  snippet: string;
  content: string;
  score: number;
  /** Character index ranges `[start, end]` inside `snippet` for highlighting. */
  matches: [number, number][];
}

export interface KnowledgeStats {
  doc_count: number;
  chunk_count: number;
  total_bytes: number;
}

export interface KnowledgePayload {
  documents: KnowledgeDocument[];
  collections: string[];
  collection_counts?: Record<string, number>;
  stats: KnowledgeStats;
  hits?: KnowledgeSearchHit[];
}

async function readApiError(res: Response, fallback: string): Promise<string> {
  const detail = (await res.text()).trim();
  if (detail.startsWith("{")) {
    try {
      const parsed = JSON.parse(detail) as { error?: unknown; detail?: unknown };
      if (typeof parsed.error === "string" && parsed.error.trim()) return parsed.error.trim();
      if (typeof parsed.detail === "string" && parsed.detail.trim()) return parsed.detail.trim();
    } catch {
      // Fall through to raw text / fallback.
    }
  }
  return detail || `${fallback}: HTTP ${res.status}`;
}

export async function fetchKnowledge(
  token: string,
  options: { query?: string; collection?: string; signal?: AbortSignal } = {},
): Promise<KnowledgePayload> {
  const params = new URLSearchParams();
  if (options.query?.trim()) params.set("q", options.query.trim());
  if (options.collection?.trim()) params.set("collection", options.collection.trim());
  const qs = params.toString();
  const res = await fetchWithTimeout(
    qs ? `/api/webui/companion/knowledge?${qs}` : "/api/webui/companion/knowledge",
    {
      headers: { Authorization: `Bearer ${token}` },
      credentials: "same-origin",
      signal: options.signal,
    },
    20_000,
  );
  if (!res.ok) throw new Error(await readApiError(res, "knowledge failed"));
  return (await res.json()) as KnowledgePayload;
}

export async function uploadKnowledgeFile(
  token: string,
  file: File,
  options: { collection?: string; title?: string; signal?: AbortSignal } = {},
): Promise<KnowledgePayload & { added: KnowledgeDocument }> {
  const form = new FormData();
  form.append("file", file, file.name);
  form.append("filename", file.name);
  if (options.collection?.trim()) form.append("collection", options.collection.trim());
  if (options.title?.trim()) form.append("title", options.title.trim());

  const res = await fetchWithTimeout(
    "/api/webui/companion/knowledge/upload",
    {
      method: "POST",
      headers: { Authorization: `Bearer ${token}` },
      credentials: "same-origin",
      body: form,
      signal: options.signal,
    },
    60_000,
  );
  if (!res.ok) throw new Error(await readApiError(res, "knowledge upload failed"));
  return (await res.json()) as KnowledgePayload & { added: KnowledgeDocument };
}

export function updateKnowledgeDocument(
  client: NanobotClient,
  id: string,
  changes: { title?: string; collection?: string },
  listCollection?: string,
): Promise<KnowledgePayload & { document: KnowledgeDocument }> {
  return client.requestMutation<KnowledgePayload & { document: KnowledgeDocument }>("companion.knowledge.change", {
    action: "update",
    id,
    ...(changes.title !== undefined ? { title: changes.title } : {}),
    ...(changes.collection !== undefined ? { collection: changes.collection } : {}),
    ...(listCollection ? { list_collection: listCollection } : {}),
  });
}

export function deleteKnowledgeDocument(
  client: NanobotClient,
  id: string,
  listCollection?: string,
): Promise<KnowledgePayload> {
  return client.requestMutation<KnowledgePayload>("companion.knowledge.change", {
    action: "delete",
    id,
    ...(listCollection ? { list_collection: listCollection } : {}),
  });
}

export interface KnowledgeChunk {
  chunk_index: number;
  heading: string;
  content: string;
}

export interface KnowledgeDocumentDetail {
  document: KnowledgeDocument;
  chunks: KnowledgeChunk[];
}

export function readKnowledgeDocument(
  client: NanobotClient,
  id: string,
): Promise<KnowledgeDocumentDetail> {
  return client.requestMutation<KnowledgeDocumentDetail>("companion.knowledge.change", {
    action: "read",
    id,
  });
}

export function importCreationToKnowledge(
  client: NanobotClient,
  creationId: string,
  options: { collection?: string; listCollection?: string; replaceExisting?: boolean } = {},
): Promise<KnowledgePayload & { added: KnowledgeDocument }> {
  return client.requestMutation<KnowledgePayload & { added: KnowledgeDocument }>("companion.knowledge.change", {
    action: "import_creation",
    creation_id: creationId,
    ...(options.collection?.trim() ? { collection: options.collection.trim() } : {}),
    ...(options.listCollection?.trim() ? { list_collection: options.listCollection.trim() } : {}),
    ...(options.replaceExisting !== undefined ? { replace_existing: options.replaceExisting } : {}),
  });
}

export function searchKnowledge(
  client: NanobotClient,
  query: string,
  options: { collection?: string; limit?: number } = {},
): Promise<{ query: string; collection: string; hits: KnowledgeSearchHit[] }> {
  return client.requestMutation<{ query: string; collection: string; hits: KnowledgeSearchHit[] }>(
    "companion.knowledge.change",
    {
      action: "search",
      query,
      ...(options.collection?.trim() ? { collection: options.collection.trim() } : {}),
      ...(options.limit !== undefined ? { limit: options.limit } : {}),
    },
  );
}
