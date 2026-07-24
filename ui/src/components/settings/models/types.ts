// Model Hub — TypeScript mirror of the FROZEN interface contracts
// (`avibe/docs/plans/model-hub-contracts/*`). Field names are exact
// (case included); the UI consumes these types and never edits the schemas.
//
// Contract version pinned to the frozen v1. If the orchestrator bumps a
// contract, this file changes in lockstep — never ahead of it.

export const CONTRACT_VERSION = 1 as const;

// ── source.schema.json ──────────────────────────────────────────────────
export type SourceKind = 'subscription' | 'api_key';
export type SourceProtocol =
  | 'anthropic'
  | 'openai_responses'
  | 'openai_chat'
  | 'openai_compatible';
export type SupplyChannel = 'native_cli' | 'hub';
export type SourceStatus = 'active' | 'standby' | 'cooldown' | 'error';
export type ModelProvenance = 'discovered' | 'manual';

export type SourceState = {
  status: SourceStatus;
  /** ISO-8601; the cooldown retry ETA rendered in the row's mono sub-line. */
  retry_at?: string | null;
  /** i18n key, never raw upstream error text. */
  detail_key?: string | null;
};

export type SourceUsage = {
  cycle_used_pct?: number | null;
  month_spend_cents?: number | null;
  /** ISO 4217, e.g. CNY / USD. */
  currency?: string | null;
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

// ── priority.schema.json ────────────────────────────────────────────────
export type Priority = {
  contract_version: typeof CONTRACT_VERSION;
  /** source ids, position 0 = spend first; every non-deleted source once. */
  order: string[];
};

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

export type AgentSupply = {
  backend: AgentBackend;
  mode: AgentMode;
  menu_kind: MenuKind;
  /** What the next turn would use (composite pill). null when mode=direct. */
  current?: AgentCurrent | null;
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
  | 'channel_switch';
export type ResolutionReason =
  | 'quota_exhausted'
  | 'rate_limited'
  | 'server_error'
  | 'network'
  | 'recovery'
  | 'manual'
  | 'mapping';
export type BillingNote = null | 'entered_metered' | 'left_metered';

export type ResolutionEvent = {
  id: string;
  ts: string;
  agent: AgentBackend | 'system';
  kind: ResolutionEventKind;
  model_id: string;
  from_source?: string | null;
  to_source?: string | null;
  reason: ResolutionReason;
  /** drives the gold dot in the 最近切换 list. */
  billing_note?: BillingNote;
  human_zh: string;
  human_en: string;
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
      platform: 'darwin-arm64' | 'linux-amd64';
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

/** POST /api/models/sources — finalize a completed subscription OAuth flow into
 *  a persisted Source. `oauth_flow_ref` is the flow id; the server derives the
 *  source id from the flow binding and rejects credential/state fields, so the
 *  UI never sends them. `experimental_consent` is sent only for the consent-
 *  gated hub-held channel. */
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
