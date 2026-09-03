// Model Hub — TypeScript mirror of the FROZEN interface contracts
// (`avibe/docs/plans/model-hub-contracts/*`). Field names are exact
// (case included); the UI consumes these types and never edits the schemas.
//
// Contract versions are PER OBJECT, not per file. `_model_hub_success` stamps
// the envelope and nests the payload, so one response carries both numbers.
// Mirror the backend's own constant names so a bump is greppable across the
// boundary; never bump one object to cover another. When a frozen contract PR
// is staged ahead of this UI's merge base, optional members stay presence-gated
// and the PR records the server implementation and feature-flag activation
// edge. The client never synthesizes a fallback payload shape.

export const CONTRACT_VERSION = 7 as const;
export const AGENT_CHAIN_CONTRACT_VERSION = CONTRACT_VERSION;
export const PROBE_RESULT_CONTRACT_VERSION = CONTRACT_VERSION;

// ── source.schema.json ──────────────────────────────────────────────────
export type SourceKind = 'subscription' | 'api_key';
export const SOURCE_PROTOCOLS = [
  'anthropic',
  'openai_responses',
  'openai_chat',
] as const;
export type SourceProtocol = (typeof SOURCE_PROTOCOLS)[number];
export const SOURCE_DISPLAY_NAME_MAX_LENGTH = 64 as const;
export type SupplyChannel = 'native_cli' | 'hub';
/** v3 (§4.5): classified by whether the state heals itself. cooldown carries a
 *  `retry_at` and clears on its own; needs_action never recovers unattended;
 *  error is an unclassified failure and equally a blocker. */
export const SOURCE_STATUSES = ['active', 'standby', 'cooldown', 'needs_action', 'error'] as const;
export type SourceStatus = (typeof SOURCE_STATUSES)[number];
export type ModelOrigin = 'discovered' | 'manual';

/** Optional cause of a self-healing cooldown. A closed vocabulary, not a
 *  prefix: the key is rendered through i18n, never as raw upstream text. */
export const COOLDOWN_DETAIL_KEYS = [
  'models.source.cooldown.rate_limited',
  'models.source.cooldown.quota_exhausted',
  'models.source.cooldown.server_error',
] as const;
export type CooldownDetailKey = (typeof COOLDOWN_DETAIL_KEYS)[number];

/** Required on `needs_action`: the state's whole point is that the user must
 *  act, which is unrenderable without naming the action. */
export const NEEDS_ACTION_DETAIL_KEYS = [
  'models.source.needs_action.oauth_expired',
  'models.source.needs_action.balance_exhausted',
  'models.source.needs_action.credential_revoked',
  'models.source.needs_action.account_banned',
] as const;
export type NeedsActionDetailKey = (typeof NEEDS_ACTION_DETAIL_KEYS)[number];

export const ERROR_DETAIL_KEYS = ['models.source.error.unclassified'] as const;
export type ErrorDetailKey = (typeof ERROR_DETAIL_KEYS)[number];

/** The complete set `state.detail_key` (and `ProbeResult.error`) can hold. */
export type SourceDetailKey = CooldownDetailKey | NeedsActionDetailKey | ErrorDetailKey;

export type SourceState = {
  status: SourceStatus;
  /** ISO-8601; required on `cooldown`, null on every other status. Rendered as
   *  the retry ETA in the row's mono sub-line. */
  retry_at?: string | null;
  /** i18n key, never raw upstream error text. Optional on cooldown, required on
   *  needs_action and error, null on the two healthy statuses. */
  detail_key?: SourceDetailKey | null;
};

/**
 * The 額度 block of `source.schema.json`, mirrored so the contract stays typed.
 *
 * Nothing renders it: `cycle_used_pct` and `month_spend_cents` have one writer
 * each (`migration.py`, both `None`), so every value the UI could receive is
 * absent. The tab that used to promise 額度 reports metered tokens instead — see
 * `UsageTab.tsx`. Kept as a mirror rather than deleted because the contract file
 * still declares the fields; drop it when the contract does.
 */
export type SourceUsage = {
  cycle_used_pct?: number | null;
  month_spend_cents?: number | null;
  /** ISO 4217, e.g. USD / CNY. Absent means USD. */
  currency?: string | null;
  /** v3, subscription sources only: when the current cycle is projected to run
   *  out at the observed burn rate. null when unknown or not projectable. */
  projected_exhaust_at?: string | null;
};

/**
 * Which rung of the provenance ladder produced `reasoning_efforts`.
 *
 * `upstream` (discovery carried a reasoning capability signal) and `catalog`
 * (the model id matches a builtin catalog entry) are auto-provided and
 * read-only. `user` is a list the user typed; `null` means no rung applies.
 *
 * Required here because `SuppliedModel` is the v7 API wire shape. The server
 * normalizes older persisted rows before serialization; compatibility with a
 * pre-v7 server belongs at the UI parsing boundary.
 */
export type ReasoningEffortsSource = 'upstream' | 'catalog' | 'user';

export type SuppliedModel = {
  /** Bare model id (no provider prefix). */
  id: string;
  display_name?: string | null;
  origin: ModelOrigin;
  reasoning_efforts: string[];
  reasoning_efforts_source: ReasoningEffortsSource | null;
  /** Persistent retirement tombstone; retired entries stay readable but are not callable. */
  retired?: boolean;
  discovered_at?: string | null;
};

export type Source = {
  id: string;
  /** Exact optional create correlation used only for lost-response recovery. */
  client_nonce?: string | null;
  /** Source creation time; ordinary audit/display metadata only. */
  created_at?: string | null;
  /** Latest successful full model discovery for this source. null means the
   *  source predates the field or has no known successful discovery. */
  last_discovered_at: string | null;
  kind: SourceKind;
  /** Standard vendor id (anthropic|openai|zhipuai|kimi|xai|…) or 'custom'. */
  vendor: string;
  display_name: string;
  protocol: SourceProtocol;
  /** api_key kind only. null = vendor official default. */
  base_url?: string | null;
  supply_channel: SupplyChannel;
  billing: 'monthly' | 'metered';
  state: SourceState;
  usage?: SourceUsage;
  /** Subscription identity for the row's mono sub-line (e.g. "me@gmail.com").
   *  Never secret material; may be null. */
  account_label?: string | null;
  /** api_key display mask, computed server-side once at provisioning
   *  (≤7-char prefix + "…" + last 4, e.g. "sk-ant-…8f2A"). Non-reversible;
   *  never secret material; may be null. */
  masked_credential?: string | null;
  models: SuppliedModel[];
  /** Opaque handle. Secret material NEVER appears here. */
  credential_ref?: string | null;
  /** Server-derived persisted Route-reference projection. */
  adopted_by?: AdoptedBy[];
};

// `priority.schema.json` does not exist in v3: there is no global source order,
// and no replacement for the removed `PUT /api/models/priority`. Order is a
// per-backend subset, carried by `AgentSupply.sources` below.

// ── agent-supply.schema.json ────────────────────────────────────────────
export type AgentBackend = 'claude' | 'codex' | 'opencode';
export type AgentMode = 'hub' | 'direct';
export type MenuKind = 'fixed' | 'open';

export type AgentMenu = {
  view: 'featured' | 'full';
  /** prefixed identifiers, e.g. zhipuai/glm-5.2. */
  checked: string[];
};

// ── backend-model.schema.json ───────────────────────────────────────────
/** How the row was first created. User edits stay authoritative afterwards,
 *  which is why this never gates editability — only `locked` does. */
export type BackendModelOrigin = 'builtin' | 'provider' | 'models_dev' | 'manual';
export const BACKEND_MODEL_INPUT_MODALITIES = ['text', 'image', 'audio', 'video', 'pdf'] as const;
export type BackendModelInputModality = (typeof BACKEND_MODEL_INPUT_MODALITIES)[number];
/** One member shorter than the input vocabulary: the schema declares no `pdf`
 *  output. Mirrored as two lists rather than one filtered at the call site, so a
 *  future divergence is a contract edit and not a UI condition. */
export const BACKEND_MODEL_OUTPUT_MODALITIES = ['text', 'image', 'audio', 'video'] as const;
export type BackendModelOutputModality = (typeof BACKEND_MODEL_OUTPUT_MODALITIES)[number];
export const BACKEND_MODEL_ID_MAX_LENGTH = 256 as const;
export const BACKEND_MODEL_EFFORT_MAX_LENGTH = 64 as const;

/** One model a backend Agent exposes: backend menu metadata, never upstream
 *  inventory and never a Route. The catalog deliberately holds no Source id,
 *  upstream model id, priority or fallback — those stay with the Route. */
export type BackendModel = {
  /** The exact identifier the backend Agent emits. OpenCode uses provider/model. */
  id: string;
  display_name: string | null;
  origin: BackendModelOrigin;
  /** The provider/model identity chosen on models.dev, null for a manual row. */
  models_dev_id: string | null;
  context_window: number | null;
  max_output_tokens: number | null;
  input_modalities: BackendModelInputModality[];
  output_modalities: BackendModelOutputModality[];
  /** Tri-state. `null` is not `false`: the server omits the capability from the
   *  backend projection entirely, leaving the backend's own default in force.
   *  Every shipped builtin row starts here, so the editor must never state a
   *  value the user did not. */
  supports_tools: boolean | null;
  supports_reasoning: boolean | null;
  /** Sent verbatim upstream. `[]` means the backend omits the effort parameter,
   *  which is a decision — not an absence the UI may fill in. */
  reasoning_efforts: string[];
  /** Server-derived: visible, but not editable, removable or reorderable. */
  locked: boolean;
  /** Server-derived: false only for a backend-owned selector such as Claude
   *  Code's `Default`, which never names a Route key. */
  routeable: boolean;
};

/** `baseline` is the last full list this caller observed; `models` is its
 *  desired replacement. The server applies the difference to the latest saved
 *  list, so a concurrent editor's unrelated row survives. */
export type BackendModelsPut = {
  baseline: BackendModel[];
  models: BackendModel[];
};

/** One normalized models.dev candidate. Metadata only: choosing it fills the
 *  editor's fields and persists nothing until the catalog itself is saved. */
export type ModelsDevMatch = {
  provider_id: string;
  provider_name: string;
  model_id: string;
  models_dev_id: string;
  display_name: string | null;
  context_window: number | null;
  max_output_tokens: number | null;
  input_modalities: BackendModelInputModality[];
  output_modalities: BackendModelOutputModality[];
  /** Null when models.dev does not state the capability, which a fill carries
   *  through rather than resolving to `false` on the model's behalf. */
  supports_tools: boolean | null;
  supports_reasoning: boolean | null;
  reasoning_efforts: string[];
};

/** Why a source cannot serve this backend at all. A closed vocabulary — a new
 *  cause ships its enum member and its locale copy in the same change. */
export type EligibilityReasonKey =
  'models.eligibility.subscription_wrong_client';

/** Why a source that MAY serve this backend still cannot be launched on this
 *  machine. Independent of eligibility and of the source's own health: the
 *  credential is fine, the CLI process is not there. A `hub` source is always
 *  null — nothing local to run. */
export type ProcessAvailabilityReason = 'native_cli_unavailable';

/** Server-computed per (source, backend). The UI renders this and never
 *  re-derives eligibility from source kind/vendor. */
export type SourceEligibility = {
  source_id: string;
  eligible: boolean;
  /** Required (and non-null) when `eligible` is false; null when it is true. */
  reason_key?: EligibilityReasonKey | null;
  /** Whether this source can serve the CURRENT selection — true/false while
   *  `selected_model_id` is non-null, null when no selection exists. Membership
   *  in the model chain, which is a narrower question than being in `order`. */
  in_current_model_chain?: boolean | null;
  /** Non-null when the source is enabled and healthy and still cannot run here. */
  process_availability_reason?: ProcessAvailabilityReason | null;
};

export type AgentSources = {
  /** Enabled source ids for THIS backend, position 0 = tried first. A subset:
   *  ids absent from it are not enabled here, not merely lower-priority. */
  order: string[];
  /** Every source the hub holds, eligible or not — the drawer's 不适用 section
   *  reads its `reason_key` from here. */
  eligibility?: SourceEligibility[] | null;
};

export type RouteHop = {
  source_id: string;
  model_id: string;
};

export type RouteHopRef = RouteHop & {
  backend: AgentBackend;
  menu_model: string;
  /** One-based position in the Route before the guarded mutation. */
  position: number;
};

export type AgentRoute = {
  hops: RouteHop[];
};

/** Agent-level supply rollup (§4.5). `waiting` = every enabled source is
 *  cooling and one will come back; `interrupted` = nothing can serve the
 *  selection without the user acting. */
export type SupplyStatus = 'ok' | 'degraded' | 'waiting' | 'interrupted';

/** AC-9's per-Agent read projection: the ENABLED named Agents on this backend,
 *  each with its explicitly configured model and its own rollup. Empty for
 *  direct-mode backends. */
export type NamedAgentSupply = {
  name: string;
  effective_model_id: string | null;
  /** null exactly when `effective_model_id` is null — no model, no capability
   *  to report. */
  supply_status: SupplyStatus | null;
  /** A configuration gap is distinct from a configured Route whose Sources
   *  are unavailable. */
  route_reason?: 'route_unconfigured' | null;
};

/** How many sources can currently serve a selectable model. `chain_length: 0`
 *  is the honest 「ticked but nothing supplies it」 state. */
export type ModelSupply = {
  model_id: string;
  chain_length: number;
  has_runnable_hop: boolean;
};

export type AgentSupply = {
  backend: AgentBackend;
  /** Server-authoritative CLI installation fact for this backend. */
  cli_present: boolean;
  mode: AgentMode;
  menu_kind: MenuKind;
  /** The named Agent whose explicit selection `selected_model_id` came from.
   *  null whenever `selected_model_id` is null. */
  selected_by_agent?: string | null;
  /** The model the next turn would ask for. null in direct mode and when no
   *  selection resolves — an honest null, never a guessed default. */
  selected_model_id?: string | null;
  /** TRUE iff `selected_model_id` originates from the Agent's explicit
   *  configuration. FALSE covers a resolver-picked value. */
  selected_model_explicit?: boolean;
  /** Per-backend enabled subset + order. null when mode=direct. */
  sources?: AgentSources | null;
  /** Exact configured Route for each menu model. Runtime reads this verbatim. */
  routes?: Record<string, AgentRoute> | null;
  /** Rollup over `sources.order` for the current selection. null in direct mode
   *  and whenever `selected_model_id` is null. This is not a backend-wide
   *  rollup; group summaries derive from `named_agents`. */
  supply_status?: SupplyStatus | null;
  /** Supply depth per selectable model. null when mode=direct. */
  model_supply?: ModelSupply[] | null;
  /** AC-9 attribution source. Always present; every entry's `supply_status` is
   *  null in direct mode. */
  named_agents?: NamedAgentSupply[];
  /** The one editable model catalog for this backend. Its ORDER is the Agent
   *  menu order. Presence-gated: a server that predates backend model catalogs
   *  omits it, and the UI then falls back to `builtin_models`/`menu` for the
   *  rolling-upgrade window rather than synthesizing a payload shape. */
  catalog_models?: BackendModel[] | null;
  menu?: AgentMenu | null;
  /** v1.2 read-only projection: fixed-menu backends only — the backend's real
   *  built-in model ids (from vibe/backend_model_catalog.py). null for open-menu
   *  backends. The route editor renders these; the UI never hardcodes menus. */
  builtin_models?: string[] | null;
  /** v1.2 read-only projection: opencode only — server mirror of
   *  STANDARD_OPENCODE_VENDOR_IDS, so the UI never hand-mirrors vendor prefixes.
   *  null otherwise. */
  standard_vendors?: string[] | null;
};

// ── migration-scan.schema.json ──────────────────────────────────────────
export type MigrationKind = 'api_key' | 'oauth_native' | 'opencode_provider';
/** Native OAuth remains native; controlled_import is reserved and not
 *  applicable in v3. Keys and base URLs may be imported. */
export type MigrationAction = 'import' | 'controlled_import' | 'keep_native' | 'reauth';

export type MigrationItem = {
  id: string;
  backend: AgentBackend;
  kind: MigrationKind;
  /** e.g. "sk-…dd3c + 自定义 Base URL"; never full secrets. */
  masked_detail: string;
  proposed_action: MigrationAction;
  selected: boolean;
  /** i18n key for the row's secondary line. */
  notes_key?: string | null;
};

export type MigrationScan = { items: MigrationItem[] };

// ── resolution-event.schema.json ────────────────────────────────────────
export type ResolutionEventKind =
  | 'switch'
  | 'cooldown'
  | 'recover'
  | 'skip'
  | 'channel_switch'
  /** v3: a source stopped in a way that will never heal unattended. */
  | 'needs_action'
  /** v3: the chain emptied — both endpoints are null by contract. */
  | 'supply_interrupted';
export type ResolutionReason =
  | 'quota_exhausted'
  | 'rate_limited'
  | 'server_error'
  | 'network'
  | 'recovery'
  | 'manual'
  // Non-self-healing causes mirror the source-state detail vocabulary.
  | 'credential_expired'
  | 'credential_revoked'
  | 'balance_exhausted'
  | 'account_banned'
  | 'permission_denied'
  | 'unclassified_error'
  | 'no_enabled_source'
  | 'no_eligible_source'
  | 'route_unconfigured'
  | 'source_missing'
  | 'model_unsupported';
export type BillingNote = null | 'entered_metered' | 'left_metered';
/** v3: `action_required` on needs_action / supply_interrupted, `info` on the six
 *  traffic kinds. */
export type EventSeverity = 'info' | 'action_required';

export type ResolutionEvent = {
  id: string;
  ts: string;
  agent: AgentBackend | 'system';
  kind: ResolutionEventKind;
  /** v3: nullable — a supply interruption is about a backend, not a model. */
  model_id: string | null;
  /** v3: canonical `src_*` id or null. The feed derives 「已删除」 at render time
   *  from this failing to resolve against live sources. */
  from_source?: string | null;
  to_source?: string | null;
  reason: ResolutionReason;
  /** drives the gold dot in the 最近切换 list. */
  billing_note?: BillingNote;
  severity?: EventSeverity | null;
  human_zh: string;
  human_en: string;
};

// ── agent-chain.schema.json ─────────────────────────────────────────────
export const CHAIN_HEALTHS = ['healthy', 'cooldown', 'backoff', 'needs_action', 'error'] as const;
export type ChainHealth = (typeof CHAIN_HEALTHS)[number];

/** Closed runtime-unavailability vocabulary shared by chain and probe results. */
export const CHAIN_UNAVAILABLE_REASONS = [
  'native_cli_unavailable',
  'models.source.backoff.connection_failed',
  'source_missing',
  'model_unsupported',
  ...NEEDS_ACTION_DETAIL_KEYS,
  ...ERROR_DETAIL_KEYS,
] as const;
export type ChainUnavailableReason = (typeof CHAIN_UNAVAILABLE_REASONS)[number];

export type AgentChainLink = {
  source_id: string;
  model_id: string;
  /** v4: the source's serving channel, mirroring `Source.supply_channel`. `hub`
   *  is definitionally process-available; `native_cli` is additionally gated by
   *  whether this process can launch the sanctioned CLI under its own login. */
  channel: SupplyChannel;
  health: ChainHealth;
  /** Health permits the turn AND this process can serve the channel. Always
   *  false for needs_action / error, and whenever `reason` is non-null; true for
   *  healthy, and for a cooldown whose `retry_at` has already passed. */
  runnable: boolean;
  /** v4: process availability, orthogonal to the source-global `health`.
   *  Non-null exactly when this process cannot launch the native_cli source — at
   *  ANY health — and it forces `runnable: false`. Hub is always null. */
  reason: ChainUnavailableReason | null;
  /** Non-null only on `cooldown`. Retained once the stamp has passed, so a
   *  non-null `retry_at` does not imply `runnable: false`. */
  retry_at: string | null;
};

/** GET /api/models/agents/<backend>/chain?model=<id> — hub mode only. Direct
 *  mode answers with the `direct_mode` error instead of an empty chain, because
 *  `chain: []` would be a false Hub-starvation alarm about a backend whose
 *  native CLI runs the model fine (AC-7). */
export type AgentChain = {
  contract_version: typeof AGENT_CHAIN_CONTRACT_VERSION;
  backend: AgentBackend;
  model_id: string;
  current: RouteHop | null;
  chain: AgentChainLink[];
  /** v4 pins this to the array it summarises: `ok` iff some member is runnable,
   *  the two blocked values iff none is. */
  supply_state: 'ok' | 'waiting' | 'interrupted';
};

// ── probe-result.schema.json ────────────────────────────────────────────
/** v4 widened this beyond `state.detail_key`: the native_cli branch reports
 *  process unavailability, which no source-state key can express. */
export type ProbeErrorKey =
  | SourceDetailKey
  | 'models.source.backoff.connection_failed'
  | 'models.probe.native_cli_unavailable';

/** POST /api/models/agents/<backend>/probe — hub mode only, same reason as the
 *  chain route: there is no `src_*` identity to report in direct mode. */
export type ProbeResult = {
  contract_version: typeof PROBE_RESULT_CONTRACT_VERSION;
  backend: AgentBackend;
  /** v4: which channel's truth this result reports. The two halves are not
   *  comparable — see `reachable` and `latency_ms`. */
  channel: SupplyChannel;
  /** Channel-scoped usability right now. hub: the upstream request succeeded.
   *  native_cli: process READINESS only — this process can launch the CLI under
   *  its own login — which is never completion evidence. */
  reachable: boolean;
  source_id: string;
  model_id: string;
  /** Hub round trip of the minimal upstream request; null there means the attempt
   *  never completed. ALWAYS null for native_cli, in both readiness outcomes,
   *  because nothing upstream is timed — a local number would impersonate
   *  completion evidence. */
  latency_ms: number | null;
  /** Closed vocabulary, null on every reachable result. hub: the ten
   *  `state.detail_key` values. native_cli: only the unavailability key. */
  error: ProbeErrorKey | null;
};

/** api.md — the exact Route hops materialized by Add Source. */
export type AddedTo = {
  backend: AgentBackend;
  menu_model: string;
  source_id: string;
  model_id: string;
  /** One-based position in the persisted Route chain. */
  position: number;
};

/** api.md — stable Source-card projection of persisted Route references. */
export type AdoptedBy = {
  backend: AgentBackend;
  menu_model: string;
};

// ── oauth-flow.schema.json ──────────────────────────────────────────────
export type OAuthFlowState =
  | 'starting'
  | 'awaiting_action'
  | 'verifying'
  | 'success'
  | 'failed'
  | 'cancelled';
/** What the UI must collect back from the user. */
export type OAuthExpects = 'none' | 'paste_code' | 'paste_callback_url';

export type OAuthPresentation = {
  auth_url?: string | null;
  device_code?: string | null;
  expects: OAuthExpects;
  /** i18n key for the step-2 helper line. */
  instructions_key?: string | null;
};

export type OAuthFlow = {
  flow_id: string;
  /** Exact client correlation echoed by nonce-backed OAuth starts. */
  client_nonce?: string | null;
  /**
   * What the flow is FOR, and therefore what its terminal success response
   * carries: `create` → `{flow, source, adopted_by}`, `reauth` →
   * `{flow, source, recovered, interrupted_pairs}`.
   *
   * Read from the payload, never from which button the client pressed — a poll
   * that lands after a page reload has no memory of the button. Optional here
   * because the schema keeps it optional for payloads that predate it; a flow
   * without it is a `create` flow, which is the only kind this UI starts.
   */
  intent?: 'create' | 'reauth';
  /** Pending Source this flow binds to (deterministic association; hub-channel
   *  flows always set it). The server derives the created source's id from it. */
  source_id?: string | null;
  vendor: string;
  channel: SupplyChannel;
  state: OAuthFlowState;
  presentation: OAuthPresentation;
  /** i18n key; raw upstream errors never surface. */
  error_key?: string | null;
  expires_at?: string | null;
};

// ── runtime-dependency.schema.json ──────────────────────────────────────
export type RuntimeHealth = 'ok' | 'degraded' | 'down' | 'not_started' | 'not_installed' | 'installing';

type RuntimeManifestAsset = {
  platform: 'darwin-arm64' | 'darwin-x64' | 'linux-amd64' | 'linux-arm64';
  url: string;
  size_bytes: number;
  sha256: string;
};

export type RuntimeManifest = {
  name: 'cliproxyapi';
  resolution: 'unresolved';
  assets: [];
} | {
  name: 'cliproxyapi';
  resolution: 'resolved' | 'unsupported';
  version: string;
  source_sha: string;
  assets: RuntimeManifestAsset[];
};

export type RuntimeDependency = {
  contract_version: typeof CONTRACT_VERSION;
  /** Persisted user intent. Older runtime payloads omit it. */
  enabled?: boolean;
  /** Server host platform, never the browser platform. */
  host_platform?: string;
  manifest: RuntimeManifest;
  status: {
    installed_version?: string | null;
    verified: boolean;
    listening?: { host: '127.0.0.1'; port: number } | null;
    health: RuntimeHealth;
    last_check?: string | null;
    error_key?: 'settings.models.install.fail.detail' | null;
  };
};

// ── usage-summary.schema.json ───────────────────────────────────────────
/**
 * Metered token usage over a trailing local-day window. A REPORT ONLY: the
 * schema forbids any consumer feeding it back into resolution, admission, or
 * cooldown, and the UI honours that by never reading it outside the usage tab.
 *
 * FIELD names are the schema's exactly, as everywhere in this file. The three
 * per-dimension TYPE names are `UsageBy*` rather than the schema's `SourceUsage`
 * / `ModelUsage` / `DayUsage`: `SourceUsage` is already taken above by
 * source.schema.json's cycle-quota-and-spend projection, which is a different
 * concept on a different document, and shadowing it would make「用量」ambiguous
 * in exactly the file that exists to remove ambiguity.
 */
export type UsageCounters = {
  /** Self-measured upstream calls that reached the model. One turn contributes
   *  more than one when it failed over. Always available. */
  requests: number;
  /** Metered calls whose upstream response carried a token report. Never
   *  greater than `requests`; a shortfall means MISSING REPORTS, not zero
   *  usage, so no view may present the difference as unused capacity. */
  token_reports: number;
  /** Vendor-reported input tokens composed per protocol, cache included. */
  input_tokens: number;
  /** Subset of `input_tokens` served from cache. */
  cached_input_tokens: number;
  output_tokens: number;
};

export type UsageByModel = UsageCounters & {
  /** Ledger key, which for a long identifier is a head plus a digest rather
   *  than the identifier itself — a string nobody typed. Display `label`,
   *  never this. `usageProjection.modelIdentity` is the only reader. */
  model_id: string;
  /** The model identity this row was metered under, joined from current Source
   *  config; null once the model is gone. */
  label: string | null;
};

export type UsageBySource = UsageCounters & {
  source_id: string;
  /** Joined from current Source config; null once the Source is gone. */
  label: string | null;
  /** When this Source last had a call metered, served or billed-and-failed. */
  last_metered_at: string | null;
  /** Never empty. A model's identity is the (source, model) pair, so this
   *  nesting is the contract's own answer to a flat model map. */
  models: UsageByModel[];
};

export type UsageByDay = UsageCounters & { day: string };

export type UsageSummary = {
  /** The window the server actually served, after clamping to retention. Views
   *  render THIS, never the number they asked for. */
  window_days: number;
  /** First local day of the window, present even when it carries no turn. */
  from_day: string;
  /** Last local day of the window, which is the host's today. */
  to_day: string;
  totals: UsageCounters;
  /** One entry per Source with at least one metered turn, busiest first. */
  sources: UsageBySource[];
  /** One entry per local day carrying a metered turn, oldest first — a trend
   *  series, so a day with no turn is ABSENT rather than reported as zero. */
  days: UsageByDay[];
};

/** `window_days` bounds from the schema. The offered options live in
 *  `usageProjection` and are gated against these. */
export const USAGE_WINDOW_MIN_DAYS = 1 as const;
export const USAGE_WINDOW_MAX_DAYS = 62 as const;
export const USAGE_DEFAULT_WINDOW_DAYS = 30 as const;

// ── API envelope + request shapes (api.md) ──────────────────────────────
export type ApiOk<T> = { ok: true; contract_version: typeof CONTRACT_VERSION } & T;
export type ApiErr = {
  ok: false;
  contract_version: typeof CONTRACT_VERSION;
  error: string;
  detail?: string;
  /** Present on `source_last_supplier`: the (backend, model) pairs the refused
   *  write would have left with no source, and the named Agents that run them. */
  would_interrupt?: SupplyGap[];
};

/** api.md supply guard — one (backend, model) pair the write would strand, plus
 *  the named Agents affected. `agents` is what the confirm copy names, because
 *  「删除后 pm 将没有可用来源」 is actionable where a bare pair is not. */
export type SupplyGap = {
  backend: AgentBackend;
  model_id: string;
  agents: string[];
};

// ── observation-result.schema.json ─────────────────────────────────────
export type ObservationOutcome =
  | 'observed'
  | 'ambiguous'
  | 'unreachable'
  | 'authentication_failed'
  | 'adapter_error'
  | 'timeout';

export type ObservationAuthentication = 'authenticated' | 'rejected' | 'unknown';
export type ObservationDiscovery = 'succeeded' | 'failed' | 'not_attempted';

export type SourceObservation = {
  contract_version: typeof CONTRACT_VERSION;
  outcome: ObservationOutcome;
  reachable: boolean | null;
  authenticated: ObservationAuthentication;
  protocol: SourceProtocol | null;
  discovery: ObservationDiscovery;
  models: string[];
};

/** POST /api/models/sources/observe — never persists a Source. */
export type ApiKeySourceObservation = {
  vendor: string;
  base_url?: string | null;
  key: string;
  /** Omission auto-detects; a value restricts observation to this protocol. */
  protocol?: SourceProtocol;
};

/** POST /api/models/sources — api_key create observes again before persisting. */
export type ApiKeySourceCreate = {
  kind: 'api_key';
  vendor: string;
  display_name?: string;
  base_url?: string | null;
  key: string;
  /** Stable across a create retry; persisted only when the Source commits. */
  client_nonce?: string;
  /** Omission auto-detects; a value is persisted only after response proof. */
  protocol?: SourceProtocol;
  /** Explicit consent for a repeated, protocol-proven inventory failure. */
  accept_unavailable_inventory?: boolean;
};

/**
 * POST /api/models/sources with an `oauth_flow_ref` — the explicit-finalize arm
 * of the create route.
 *
 * NOT used by this UI, on purpose. A terminal `create` flow is already
 * materialized by the status/submit response that reports its success, and the
 * flow binding is consumed with it: posting here afterwards is rejected as
 * `flow_not_found`. The shape stays because the route does; the client's job is
 * to consume the terminal result, not to re-create from it.
 */
export type OAuthSourceCreate = {
  kind: 'subscription';
  vendor: string;
  oauth_flow_ref: string;
  supply_channel: SupplyChannel;
  display_name?: string;
};

/** PATCH /api/models/sources/<id> — display_name and/or base_url only
 *  (contract: never accepts credential material). */
export type SourcePatch = {
  display_name?: string;
  base_url?: string | null;
  force?: boolean;
  would_remove_hops?: RouteHopRef[];
  would_interrupt?: SupplyGap[];
};

/** PUT /api/models/agents/<backend>/sources — replaces the complete order. */
export type AgentSourcesPut = { order: string[] };

/** PUT /api/models/agents/<backend>/chain?model=<id> — replaces exact hops. */
export type AgentChainPut = {
  hops: RouteHop[];
  force?: boolean;
  would_remove_hops?: RouteHopRef[];
  would_interrupt?: SupplyGap[];
};

export type AgentChainMutation = {
  chain: AgentChain;
  removed_hops: RouteHopRef[];
  interrupted: SupplyGap[];
};

/**
 * PUT /api/models/sources/<id>/credential — hub-channel api_key sources only.
 * Also a TOTAL body that rejects unknown keys (`contract_version` included), so
 * `force` is omitted rather than sent false on the unguarded first attempt.
 */
export type CredentialReplace = {
  key: string;
  force?: boolean;
  would_remove_hops?: RouteHopRef[];
  would_interrupt?: SupplyGap[];
};

/**
 * POST /api/models/sources/<id>/reauth. The acknowledgement is server-enforced
 * and unconditional for native sources — pre-login, before anything is
 * destroyed — so the client always confirms and always sends it. Same
 * closed-body rule: nothing else may appear.
 */
export type ReauthRequest = { acknowledge_irreversible: true };

/**
 * The shared tail of both repair routes (api.md "recovery symmetry"), and of a
 * terminal `reauth` OAuth flow.
 *
 * `recovered` is the server's own judgement that the prior state was
 * needs_action/error — the client never re-derives it, and it is also the reason
 * a repair is exempt from the supply guard: `interrupted_pairs` on a recovering
 * write is a report of what is still stranded, not a refusal.
 */
export type SourceRepaired = {
  source: Source;
  recovered: boolean;
  interrupted_pairs: SupplyGap[];
};

/** POST /api/models/sources/<source_id>/models — appends a user-authored model
 *  entry to a Source inventory (frame 08). */
export type CustomModelCreate = {
  model_id: string;
  display_name?: string | null;
  reasoning_efforts: string[];
};

/** POST /api/models/migration/apply response. */
export type MigrationApplyResult = {
  applied: number;
  sources: Source[];
};
