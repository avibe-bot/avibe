// Model Hub — TypeScript mirror of the FROZEN interface contracts
// (`avibe/docs/plans/model-hub-contracts/*`). Field names are exact
// (case included); the UI consumes these types and never edits the schemas.
//
// Contract version pinned to the frozen v3. If the orchestrator bumps a
// contract, this file changes in lockstep — never ahead of it.

export const CONTRACT_VERSION = 3 as const;

// ── source.schema.json ──────────────────────────────────────────────────
export type SourceKind = 'subscription' | 'api_key';
export type SourceProtocol =
  | 'anthropic'
  | 'openai_responses'
  | 'openai_chat'
  | 'openai_compatible';
export type SupplyChannel = 'native_cli' | 'hub';
/** v3 (§4.5): classified by whether the state heals itself. cooldown carries a
 *  `retry_at` and clears on its own; needs_action never recovers unattended;
 *  error is an unclassified failure and equally a blocker. */
export type SourceStatus = 'active' | 'standby' | 'cooldown' | 'needs_action' | 'error';
export type ModelProvenance = 'discovered' | 'manual';

/** Optional cause of a self-healing cooldown. A closed vocabulary, not a
 *  prefix: the key is rendered through i18n, never as raw upstream text. */
export type CooldownDetailKey =
  | 'models.source.cooldown.network'
  | 'models.source.cooldown.timeout'
  | 'models.source.cooldown.rate_limited'
  | 'models.source.cooldown.quota_exhausted'
  | 'models.source.cooldown.server_error';

/** Required on `needs_action`: the state's whole point is that the user must
 *  act, which is unrenderable without naming the action. */
export type NeedsActionDetailKey =
  | 'models.source.needs_action.oauth_expired'
  | 'models.source.needs_action.balance_exhausted'
  | 'models.source.needs_action.credential_revoked'
  | 'models.source.needs_action.account_banned';

export type ErrorDetailKey = 'models.source.error.unclassified';

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

export type SourceUsage = {
  cycle_used_pct?: number | null;
  month_spend_cents?: number | null;
  /** ISO 4217, e.g. USD / CNY. Absent means USD (see formatSpend). */
  currency?: string | null;
  /** v3, subscription sources only: when the current cycle is projected to run
   *  out at the observed burn rate. null when unknown or not projectable. */
  projected_exhaust_at?: string | null;
};

export type SuppliedModel = {
  /** Bare model id (no provider prefix). */
  id: string;
  display_name?: string | null;
  provenance: ModelProvenance;
  discovered_at?: string | null;
};

export type Source = {
  id: string;
  /** v3, immutable once written: the `follow` policy recommends api_key sources
   *  by creation time ascending, so the rule needs a persisted stamp (the
   *  sources array itself is explicitly unordered). May be null on rows
   *  persisted before the field existed; `id` ascending is the tie-breaker. */
  created_at?: string | null;
  kind: SourceKind;
  /** Standard vendor id (anthropic|openai|zhipuai|kimi|xai|…) or 'custom'. */
  vendor: string;
  display_name: string;
  protocol: SourceProtocol;
  /** api_key kind only. null = vendor official default. */
  base_url?: string | null;
  supply_channel: SupplyChannel;
  /** Set iff a hub-held subscription the user explicitly consented to. */
  experimental_consent_at?: string | null;
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
};

// `priority.schema.json` does not exist in v3: there is no global source order,
// and no replacement for the removed `PUT /api/models/priority`. Order is a
// per-backend subset, carried by `AgentSupply.sources` below.

// ── agent-supply.schema.json ────────────────────────────────────────────
export type AgentBackend = 'claude' | 'codex' | 'opencode';
export type AgentMode = 'hub' | 'direct';
export type MenuKind = 'fixed' | 'open';

export type AgentCurrent = {
  model_id: string;
  source_id: string;
  channel: SupplyChannel;
};

export type AgentMapping = {
  /** real built-in model id, e.g. claude-opus-4-6. */
  builtin_id: string;
  target_model_id: string;
  enabled: boolean;
};

export type AgentMenu = {
  view: 'featured' | 'full';
  /** prefixed identifiers, e.g. zhipuai/glm-5.2. */
  checked: string[];
};

/** Whether the per-backend order is server-recommended or user-owned. `follow`
 *  recomputes on every read, so a newly eligible source joins automatically;
 *  `custom` is a frozen subset the server never reorders and never extends. */
export type SourcePolicy = 'follow' | 'custom';

/** Why a source cannot serve this backend at all. A closed vocabulary — a new
 *  cause ships its enum member and its locale copy in the same change. */
export type EligibilityReasonKey =
  | 'models.eligibility.subscription_wrong_client'
  | 'models.eligibility.opencode_api_key_only'
  | 'models.eligibility.consent_required';

/** Server-computed per (source, backend). The UI renders this and never
 *  re-derives eligibility from source kind/vendor. */
export type SourceEligibility = {
  source_id: string;
  eligible: boolean;
  /** Required (and non-null) when `eligible` is false; null when it is true. */
  reason_key?: EligibilityReasonKey | null;
};

export type AgentSources = {
  policy: SourcePolicy;
  /** Enabled source ids for THIS backend, position 0 = tried first. A subset:
   *  ids absent from it are not enabled here, not merely lower-priority. */
  order: string[];
  /** Every source the hub holds, eligible or not — the drawer's 不适用 section
   *  reads its `reason_key` from here. */
  eligibility?: SourceEligibility[] | null;
};

/** Agent-level supply rollup (§4.5). `waiting` = every enabled source is
 *  cooling and one will come back; `interrupted` = nothing can serve the
 *  selection without the user acting. Both pin `current` to null, so a stale
 *  使用中 can never render. */
export type SupplyStatus = 'ok' | 'degraded' | 'waiting' | 'interrupted';

/** AC-9's per-Agent read projection: the ENABLED named Agents on this backend,
 *  each with the model it effectively runs (explicit selection or the backend
 *  default) and its own rollup. Empty for direct-mode backends. */
export type NamedAgentSupply = {
  name: string;
  effective_model_id: string | null;
  /** null exactly when `effective_model_id` is null — no model, no capability
   *  to report. */
  supply_status: SupplyStatus | null;
};

/** How many sources can currently serve a selectable model. `chain_length: 0`
 *  is the honest 「ticked but nothing supplies it」 state. */
export type ModelSupply = {
  model_id: string;
  chain_length: number;
};

export type AgentSupply = {
  backend: AgentBackend;
  mode: AgentMode;
  menu_kind: MenuKind;
  /** The named Agent whose selection `selected_model_id` came from, or null when
   *  the backend default supplies it. null whenever `selected_model_id` is. */
  selected_by_agent?: string | null;
  /** The model the next turn would ask for. null in direct mode and when no
   *  selection resolves — an honest null, never a guessed default. */
  selected_model_id?: string | null;
  /** What the next turn would actually use (composite pill). null in exactly
   *  three cases: mode=direct, supply_status=waiting, supply_status=interrupted. */
  current?: AgentCurrent | null;
  /** Per-backend enabled subset + order + policy. null when mode=direct. */
  sources?: AgentSources | null;
  /** Rollup over `sources.order` for the current selection. null in direct mode
   *  and whenever `selected_model_id` is null. */
  supply_status?: SupplyStatus | null;
  /** Supply depth per selectable model. null when mode=direct. */
  model_supply?: ModelSupply[] | null;
  /** AC-9 attribution source. Always present; every entry's `supply_status` is
   *  null in direct mode. */
  named_agents?: NamedAgentSupply[];
  mappings?: AgentMapping[];
  menu?: AgentMenu | null;
  /** v1.2 read-only projection: fixed-menu backends only — the backend's real
   *  built-in model ids (from vibe/backend_model_catalog.py). null for open-menu
   *  backends. The mapping drawer renders these; the UI never hardcodes menus. */
  builtin_models?: string[] | null;
  /** v1.2 read-only projection: opencode only — server mirror of
   *  STANDARD_OPENCODE_VENDOR_IDS, so the UI never hand-mirrors vendor prefixes.
   *  null otherwise. */
  standard_vendors?: string[] | null;
};

// ── migration-scan.schema.json ──────────────────────────────────────────
export type MigrationKind = 'api_key' | 'oauth_native' | 'opencode_provider';
/** Option 1 (spec v1.1): Claude oauth_native → keep_native (sanctioned as-is);
 *  Codex auth.json → controlled_import behind the consent-gated flag, else
 *  keep_native; keys / base URLs → import. */
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
  | 'mapping_applied'
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
  | 'mapping'
  // v3 — the five non-self-healing causes, a bijection with the needs_action /
  // error detail keys above.
  | 'credential_expired'
  | 'credential_revoked'
  | 'balance_exhausted'
  | 'account_banned'
  | 'unclassified_error'
  // v3 — supply exhaustion causes.
  | 'no_enabled_source'
  | 'no_eligible_source'
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
export type ChainHealth = 'healthy' | 'cooldown' | 'needs_action' | 'error';

export type AgentChainLink = {
  source_id: string;
  via_mapping: boolean;
  /** The id actually sent upstream. Non-null whenever `via_mapping` is true. */
  resolved_model_id: string | null;
  health: ChainHealth;
  /** false for cooldown / needs_action / error; true only for healthy. */
  runnable: boolean;
  /** Non-null only on `cooldown`. */
  retry_at: string | null;
};

/** GET /api/models/agents/<backend>/chain?model=<id> — hub mode only. Direct
 *  mode answers with the `direct_mode` error instead of an empty chain, because
 *  `chain: []` would be a false Hub-starvation alarm about a backend whose
 *  native CLI runs the model fine (AC-7). */
export type AgentChain = {
  contract_version: typeof CONTRACT_VERSION;
  backend: AgentBackend;
  model_id: string;
  chain: AgentChainLink[];
  supply_state: 'ok' | 'waiting' | 'interrupted';
};

// ── probe-result.schema.json ────────────────────────────────────────────
/** POST /api/models/agents/<backend>/probe — hub mode only, same reason as the
 *  chain route: there is no `src_*` identity to report in direct mode. */
export type ProbeResult = {
  contract_version: typeof CONTRACT_VERSION;
  backend: AgentBackend;
  reachable: boolean;
  source_id: string;
  model_id: string;
  latency_ms: number | null;
  via_mapping: boolean;
  /** Mirrors the `state.detail_key` vocabulary; null iff reachable. */
  error: SourceDetailKey | null;
};

/** api.md — returned by the source-creation routes: which backends adopted the
 *  new source into their order, and where. `position` is ONE-based. A `custom`
 *  backend is absent (the UI hints 「有新来源未启用」 instead). */
export type AdoptedBy = {
  backend: AgentBackend;
  policy: SourcePolicy;
  position: number;
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
export type RuntimeHealth = 'ok' | 'degraded' | 'down' | 'not_installed';

export type RuntimeDependency = {
  manifest: {
    name: 'cliproxyapi';
    version: string;
    source_sha: string;
    assets: Array<{
      platform: 'darwin-arm64' | 'darwin-x64' | 'linux-amd64' | 'linux-arm64';
      url: string;
      size_bytes: number;
      sha256: string;
    }>;
  };
  status: {
    installed_version?: string | null;
    verified: boolean;
    listening?: { host: '127.0.0.1'; port: number } | null;
    health: RuntimeHealth;
    last_check?: string | null;
  };
};

// ── API envelope + request shapes (api.md) ──────────────────────────────
export type ApiOk<T> = { ok: true; contract_version: typeof CONTRACT_VERSION } & T;
export type ApiErr = {
  ok: false;
  contract_version: typeof CONTRACT_VERSION;
  error: string;
  detail?: string;
};

/** POST /api/models/sources — api_key create validates + discovers models. */
export type ApiKeySourceCreate = {
  kind: 'api_key';
  vendor: string;
  base_url?: string | null;
  key: string;
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
  experimental_consent?: boolean;
};

/** PATCH /api/models/sources/<id> — display_name and/or base_url only
 *  (contract: never accepts credential material). */
export type SourcePatch = {
  display_name?: string;
  base_url?: string | null;
};

/** PUT /api/models/agents/<backend>/sources — a TOTAL body: `follow` hands the
 *  order back to the server, `custom` freezes exactly the ids sent. The route
 *  rejects unknown keys, so `contract_version` is deliberately NOT part of it. */
export type AgentSourcesPut = { policy: 'follow' } | { policy: 'custom'; order: string[] };

/** POST /api/models/custom-models — appends a manual-provenance model entry to
 *  a source's supply list (frame 08). */
export type CustomModelCreate = {
  source_id: string;
  model_id: string;
  display_name?: string | null;
};

/** POST /api/models/migration/apply response. */
export type MigrationApplyResult = {
  applied: number;
  sources: Source[];
};
