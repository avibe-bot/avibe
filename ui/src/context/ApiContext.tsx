import React, { createContext, useContext, useEffect, useMemo, useRef } from 'react';
import { useTranslation } from 'react-i18next';
import { useToast } from './ToastContext';
import type { AgentSupply } from '../components/settings/models/types';
import { apiFetch, recoverRemoteAuthFromSessionProbe } from '../lib/apiFetch';
import { isAuthorizationSensitiveReadPath } from '../lib/authorizationCache';
import type { TurnActivityGroupWire } from '../lib/agentActivity';
import type { AgentGraphParams, AgentGraphResult, AgentGraphVisibility } from '../lib/agentGraph';
import { onPageReactivated, type PageReactivationListener } from '../lib/pageActivity';
import type { ShowPagePayload } from '../lib/showPageLinks';
import { visibilityActivityEvents } from '../lib/sessionVisibilityEvents';
import { normalizeSessionInfo, type InstanceCapabilities, type SessionInfo } from '../lib/sessionInfo';
import type { VaultSessionPolicy } from '../lib/vaultSandboxPolicy';
import {
  classifyShowPageAccessProbe,
  type ShowPageAccess,
  type ShowPageAccessProbe,
  type ShowAccessApplyRequest,
  type ShowAccessApplyResult,
  type ShowAccessSettingsResult,
} from '../lib/showPageAccess';
import {
  WorkbenchEventReconnectLoop,
  WORKBENCH_EVENT_HEARTBEAT_FALLBACK_MS,
  declaredWorkbenchHeartbeatInterval,
  isWorkbenchHeartbeatFresh,
  parseWorkbenchHeartbeatInterval,
  streamCoveredGap,
  workbenchEventStaleAfterMs,
  type WorkbenchControllerLegState,
  type WorkbenchEventConnectionState,
} from '../lib/workbenchEventConnection';
import type { DockDoc } from './dockDoc';
import { archivedConflictSessionId, selectApiErrorFields } from './apiErrorParse';
import {
  SessionDraftPersistence,
  type SessionDraftSaveResult,
  type SessionDraftServerState,
  type SessionDraftWrite,
} from '../lib/sessionDraftPersistence';
import { getExistingWebPushSubscription, getWebPushDeviceId } from '../lib/webPush';
import { reportRemoteAuthorizationState, type RemoteAuthorizationState } from '../lib/remoteAuth';
import {
  configMutationsToPayload,
  type ConfigMutation,
} from '../lib/configMutations';

export type { InstanceCapabilities, SessionInfo };
export type { ShowPageAccess };
export type { ConfigMutation };

// The workbench Dock API response shape ({ ok, dock }); the Dock document type
// itself lives with the DockProvider that owns reconciliation.
export type DockResponse = { ok: boolean; dock: DockDoc };

// Global workbench toggles persisted server-side in state_meta. Currently just
// the background-work banner switch (spec req 2); read by ChatPage (to gate the
// banner) and the Harness page (the toggle card).
export type WorkbenchPrefs = { ok?: boolean; background_work_banner_enabled: boolean };

// One backend's *global* instructions file, surfaced by the Global Prompts
// editor. ``backend`` is an agent backend id (claude / opencode / codex).
export type GlobalPromptFile = {
  backend: string;
  path: string;
  filename: string;
  content: string;
  exists: boolean;
  /** True when the file exists but couldn't be decoded as UTF-8; the editor
   *  then warns and refuses to overwrite it with an empty draft. */
  read_error: boolean;
};

/** Receive addresses derived from a keypair secret's secp256k1 public key. */
export type SigningAddresses = {
  eth?: string;
  btc_legacy?: string;
  btc_segwit?: string;
  btc_taproot?: string;
};

export type VaultSecret = {
  name: string;
  /** Flat tag list; skill association is a reserved `skill:<name>` tag (see lib/vaultTags). */
  tags: string[];
  kind: string;
  protection: string;
  signer_kind: string | null;
  /** Receive addresses derived from a keypair secret's public key. Agents and the UI
   *  identify a signing key by address; the raw public key is never surfaced here. */
  signing_addresses?: SigningAddresses | null;
  source: string;
  description?: string | null;
  policy: Record<string, unknown>;
  last_used_at: string | null;
  use_count: number;
  created_at: string;
  updated_at: string;
};

export type VaultAuditEvent = {
  id: string;
  ts: string;
  event: string;
  secret_name: string | null;
  request_id: string | null;
  grant_id: string | null;
};

export type VaultRequest = {
  id: string;
  request_type: string;
  secret_name: string | null;
  requester: unknown;
  delivery: unknown;
  status: string;
  message_id: string | null;
  created_at: string;
  decided_at: string | null;
  expires_at: string | null;
  card?: Record<string, unknown> | null;
  session?: VaultRequestSession | null;
};

export type VaultRequestSession = {
  id: string;
  title: string | null;
  label: string | null;
  platform: string | null;
  scope_kind: string | null;
  is_workbench: boolean;
};

export type VaultRequestSpec = {
  kind?: 'static';
  protection?: 'standard' | 'protected';
  description?: string;
  /** May already include `skill:<name>` tags; `links.skills` is a bare-name convenience mirror. */
  tags?: string[];
  policy?: {
    allowed_hosts?: string[];
    auth?: { type?: 'bearer' | 'header' | 'query'; name?: string };
  };
  links?: { skills?: string[] };
};

/**
 * How a grant's protected set was selected. Env selectors are explicit secret/env
 * names (`OPENAI_API_KEY`, `DB_URL=PROD_DB_URL`); tag selectors group by tag, with
 * skill selectors carried as `skill:<name>` tags.
 */
export type VaultSourceSelector = { env?: string[]; tags?: string[] };

/**
 * A grant is a first-class, time-limited authorization for avault to use a fixed set
 * of protected secrets (design: docs/plans/vaults-grant-delivery-refactor.md §6). It
 * is keyed by `id` (the grant_id); `member_snapshot` is the frozen protected set and
 * `source_selector` records how it was chosen. Tag edits never mutate an active grant.
 */
export type VaultGrant = {
  id: string;
  source_selector: VaultSourceSelector;
  session_id: string | null;
  purpose: string;
  status: string;
  request_id: string | null;
  created_at: string;
  expires_at: string;
  revoked_at: string | null;
  member_snapshot: string[];
  member_count: number;
  runtime_member_count: number;
  delivery_ready?: boolean;
  delivery_status?: string;
  one_shot?: boolean;
};

type VaultBlindBox = {
  scheme: string;
  enc: string;
  ct: string;
};

/**
 * Browser-relayed protected access fulfillment. The browser releases each protected
 * DEK as an opaque HPKE blind box addressed to the resident avault agent and submits
 * ONLY `{name, dek_blindbox, approval}` per secret — never a raw DEK or plaintext.
 */
export type VaultAccessFulfillmentPayload = {
  grant_id?: string;
  session_id?: string | null;
  /** Approver-chosen agent access duration (protocol v2 §7.1); supersedes the old fixed ttl. */
  grant_duration?: VaultGrantDuration;
  this_session_only?: boolean;
  agent_pubkey?: { public_key?: string; fingerprint?: string };
  deks?: Array<{ name: string; dek_blindbox: VaultBlindBox; approval: Record<string, unknown> }>;
};

export type VaultSandboxRootMetadataResult = {
  ok: boolean;
  root_metadata: {
    daemon: {
      verificationKeys: Array<{ alg: 'ed25519'; keyId: string; publicKey: string }>;
    };
  };
  code?: string;
  message?: string;
};

/** Approver-chosen agent access duration (protocol v2 §7.1). `'one-time'` = a single delivery. */
export type VaultGrantDuration = 'one-time' | number;

/**
 * The ed25519-signed operation context the daemon issues and the sandbox renders verbatim
 * (protocol v2 §6.2). Everything a human authorizes — the covered secrets, session, command,
 * egress, source, and agent access duration — travels inside the signed `display` block so a
 * compromised parent can't rewrite the consent story.
 */
export type VaultSignedOperationContext = {
  v: 2;
  purpose: 'agent-deliver' | 'sign' | 'reveal';
  requestId: string;
  grantId?: string;
  display: {
    secrets: Array<{ name: string; kind: 'static' | 'keypair' }>;
    sessionLabel?: string;
    command?: string;
    egress?: string;
    source?: { env?: string[]; tags?: string[]; skills?: string[] };
    grantTtlSeconds?: number;
  };
  agent?: { publicKey: { public_key: string; fingerprint?: string }; fingerprint: string };
  expiresAt: string;
  signature: { alg: 'ed25519'; keyId: string; value: string };
};

/**
 * One `POST /api/vault/agent-bindings:batch` call returns per-secret signed contexts sharing one
 * display block (protocol v2 §7.1) — the parent brokers all members to the sandbox in a single
 * `approveRelease`, then submits the resulting blind boxes with the matching `approval` handles.
 */
export type VaultAgentBindingsBatchResult = {
  ok: boolean;
  request_id?: string;
  grant_id?: string;
  grant_duration?: VaultGrantDuration;
  ttl_seconds?: number;
  agent?: { publicKey: { public_key: string; fingerprint?: string }; fingerprint: string };
  agent_pubkey: { public_key: string; fingerprint: string };
  items: Array<{
    name: string;
    context: VaultSignedOperationContext;
    approval: { nonce: string; expires_at_unix: number };
  }>;
  code?: string;
  message?: string;
};

/** Persisted vault session settings (daemon side). Mirrors `GET/PATCH /api/vault/settings`. */
export type VaultSettings = {
  unlock_window_seconds: number;
  strict_approvals: boolean;
  last_grant_ttl: VaultGrantDuration;
};

export type VaultSettingsResult = {
  ok: boolean;
  settings: VaultSettings;
  policy: VaultSessionPolicy;
  code?: string;
  message?: string;
};

/**
 * `POST /api/vault/secrets/<name>/reveal-context`. The daemon signs a `reveal` context naming the
 * secret; the sandbox renders it and displays the plaintext in-frame. The protected record
 * `envelope` is relayed alongside so the sandbox can open it (the parent only holds ciphertext).
 */
export type VaultRevealContextResult = {
  ok: boolean;
  context?: VaultSignedOperationContext;
  envelope?: VaultSealedEnvelope;
  code?: string;
  message?: string;
};

type VaultSealedEnvelope = {
  ciphertext: string;
  nonce: string;
  wrap_meta: string | Record<string, unknown>;
};

export type VaultVmkResult = {
  ok: boolean;
  exists: boolean;
  wrap_meta: string | null;
};

export type VaultWebAuthnCredentialDescriptor = {
  type: 'public-key';
  id: string;
  factor_id?: string;
  transports?: AuthenticatorTransport[];
};

export type VaultWebAuthnAssertionOptions = {
  challenge: string;
  rpId: string;
  userVerification: UserVerificationRequirement;
  allowCredentials: VaultWebAuthnCredentialDescriptor[];
};

export type VaultWebAuthnRegistrationOptions = {
  ok: boolean;
  challenge_id: string;
  expires_at: string;
  rp_id: string;
  origin: string;
  requires_existing_factor?: boolean;
  webauthn: {
    rp: { name: string; id: string };
    user: { id: string; name: string; displayName: string };
    challenge: string;
    pubKeyCredParams: PublicKeyCredentialParameters[];
    authenticatorSelection: AuthenticatorSelectionCriteria;
    extensions?: AuthenticationExtensionsClientInputs;
  };
  authorization?: {
    challenge_id: string;
    expires_at: string;
    webauthn: VaultWebAuthnAssertionOptions;
  };
  code?: string;
  message?: string;
};

export type VaultWebAuthnSerializedCredential = {
  id: string;
  rawId: string;
  type: string;
  response: Record<string, unknown>;
};

export type VaultWebAuthnAuthz = {
  kind: 'webauthn';
  challenge_id: string;
  factor_id: string;
  assertion: VaultWebAuthnSerializedCredential;
};

export type VaultWebAuthnRegistrationPayload = {
  challenge_id: string;
  credential: VaultWebAuthnSerializedCredential;
  label?: string;
  authz?: VaultWebAuthnAuthz;
};

export type VaultCreatePayload = {
  name: string;
  protection?: 'standard' | 'protected';
  blind_box?: VaultBlindBox;
  sealed?: VaultSealedEnvelope;
  envelope?: VaultSealedEnvelope;
  description?: string;
  /** Flat tag list; skill association is folded in as `skill:<name>` tags (see lib/vaultTags). */
  tags?: string[];
  kind?: string;
  signer_kind?: string | null;
  policy?: Record<string, unknown>;
  public_meta?: Record<string, unknown>;
  /** Bare skill names. Sent alongside the folded `skill:<name>` tags so skill scopes work on
   *  the pre-refactor backend (which populates vault_links from links.skills); `links.skills`
   *  is also part of the final request spec (design §5). */
  links?: { skills?: string[] };
  provision_request_id?: string;
  /** Set on the first protected secret so the daemon atomically guards single VMK init. */
  establishing_vmk?: boolean;
  /** First protected-vault setup registers the delete-authz public key in the same transaction. */
  authz_factor_registration?: VaultWebAuthnRegistrationPayload;
};

/**
 * Value-free metadata edit (`PATCH /api/vault/secrets/<name>`). At least one field must be
 * present. `description: null`/blank clears it; `tags: []` clears all tags; `policy` replaces
 * the visible fetch policy (allowed_hosts + auth) while the backend preserves internal keys
 * such as `always_ask`. Name / kind / protection / value are never editable through this path.
 */
export type VaultMetadataUpdatePayload = {
  description?: string | null;
  tags?: string[];
  policy?: Record<string, unknown>;
};

export type TunnelQualitySnapshot = {
  schema_version: 1 | 2;
  state: 'healthy' | 'degraded' | 'recovering' | 'unknown';
  grade: 'good' | 'fair' | 'poor' | 'critical' | 'unknown';
  sampled_at: string;
  protocol: 'quic' | 'http2' | 'unknown';
  transport?: {
    configured: 'auto' | 'quic' | 'http2';
    effective: 'quic' | 'http2' | 'unknown';
  };
  connector_count: number;
  ha_connections: number;
  rtt_ms: { min: number; median: number; max: number } | null;
  baseline_median_rtt_ms: number | null;
  edge_locations: string[];
  window_seconds: number;
  request_errors_per_minute: number;
  packet_loss_per_minute: number;
  request_path?: {
    source: 'synthetic_local';
    status: 'healthy' | 'degraded' | 'unavailable' | 'insufficient';
    confidence: 'low' | 'medium' | 'high';
    window_seconds: number;
    sample_count: number;
    success_count: number;
    latency_ms: { p50: number; p95: number; p99: number; max: number } | null;
    failure_rate: number;
    slow_request_rate: {
      over_500_ms: number;
      over_1000_ms: number;
      over_2000_ms: number;
    };
    baseline_p95_ms: number | null;
  } | null;
  recovery: {
    state: 'idle' | 'evaluating' | 'draining' | 'cooldown';
    last_attempt_at: string | null;
    last_trigger: 'availability' | 'latency' | 'tail_latency' | 'errors' | 'manual' | null;
    last_result: 'improved' | 'no_improvement' | 'failed' | null;
    previous_median_rtt_ms: number | null;
    result_median_rtt_ms: number | null;
    previous_protocol?: 'quic' | 'http2' | null;
    result_protocol?: 'quic' | 'http2' | null;
    previous_p95_ms?: number | null;
    result_p95_ms?: number | null;
    previous_p99_ms?: number | null;
    result_p99_ms?: number | null;
    next_attempt_at: string | null;
    attempt_count_window: number;
  };
};

export type CloudflareEdgeLocation = {
  colo: string;
  location?: string;
  country?: string;
};

export type TunnelNetworkPath = {
  schema_version: 1;
  provider: 'Cloudflare';
  asn: 13335;
  sampled_at: string;
  locations_pending: boolean;
  client_access: 'local' | 'remote';
  client_ingress: CloudflareEdgeLocation | null;
  connector: {
    locations: Array<CloudflareEdgeLocation & { id: string }>;
    edge_ips: string[];
  };
  route: {
    assessment: 'same_metro' | 'same_country' | 'cross_country' | 'unknown';
  };
};

export type RemoteAccessStatus = {
  ok: boolean;
  enabled: boolean;
  paired: boolean;
  running: boolean;
  public_url?: string;
  pid_state?: string;
  transport_protocol?: 'auto' | 'quic' | 'http2';
  settings?: RemoteAccessSettings;
  tunnel_quality?: TunnelQualitySnapshot;
  network_path?: TunnelNetworkPath;
  error?: string;
  optimization_started?: boolean;
};

export type RemoteAccessSettings = {
  transport_protocol: 'auto' | 'quic' | 'http2';
  auto_recovery: boolean;
  optimization_profile: 'stable' | 'balanced' | 'low_latency';
  edge_ip_version: 'auto' | '4' | '6';
  edge_bind_address: string;
};

export type TunnelNetworkInterface = {
  id: string;
  name: string;
  address: string;
  ip_version: '4' | '6';
};

export type TunnelConnectivityDiagnostics = {
  ok: boolean;
  sampled_at: string;
  effective_protocol: 'quic' | 'http2' | 'unknown';
  dns: { status: 'available' | 'unavailable' | 'unknown' };
  quic: { status: 'available' | 'unavailable' | 'unknown'; source: string };
  http2: { status: 'available' | 'unavailable' | 'unknown'; source: string };
  cloudflared_version?: string | null;
  error?: string;
};

export type ApiContextType = {
  getConfig: () => Promise<any>;
  getPlatformCatalog: () => Promise<any>;
  mutateConfig: (mutations: readonly ConfigMutation[]) => Promise<any>;
  waitForAgentActivityConfigMutations: () => Promise<void>;
  onConfigChanged: (handler: (config: unknown) => void) => () => void;
  getSettings: (platform?: string) => Promise<any>;
  saveSettings: (payload: any, platform?: string) => Promise<any>;
  saveThreadSettings: (platform: string, channelId: string, threadId: string, settings: any) => Promise<any>;
  deleteThreadSettings: (platform: string, channelId: string, threadId: string) => Promise<any>;
  getUsers: (platform?: string) => Promise<any>;
  saveUsers: (payload: any, platform?: string) => Promise<any>;
  toggleAdmin: (userId: string, isAdmin: boolean, platform?: string) => Promise<any>;
  removeUser: (userId: string, platform?: string) => Promise<any>;
  getShowPages: () => Promise<any>;
  getShowPageAccess: (sessionId: string) => Promise<ShowPageAccess>;
  probeShowPageAccess: (sessionId: string) => Promise<ShowPageAccessProbe>;
  getShowAccessSettings: (sessionId: string) => Promise<ShowAccessSettingsResult>;
  applyShowAccess: (
    sessionId: string,
    payload: ShowAccessApplyRequest,
  ) => Promise<ShowAccessApplyResult>;
  getWebPushStatus: (payload?: WebPushStatusPayload) => Promise<WebPushStatus>;
  getWebPushVapidPublicKey: () => Promise<{ ok: boolean; public_key: string }>;
  subscribeWebPush: (
    subscription: PushSubscriptionJSON,
    deviceLabel?: string,
    deviceId?: string,
    previousEndpoints?: string[],
  ) => Promise<WebPushSubscriptionResult>;
  unsubscribeWebPush: (endpoint: string) => Promise<{ ok: boolean; disabled: boolean }>;
  sendWebPushTest: (payload?: { title?: string; body?: string; url?: string; endpoint?: string }) => Promise<WebPushTestResult>;
  setShowPageAvailability: (sessionId: string, offline: boolean) => Promise<any>;
  /** Read the session's Show Page without creating it; rejects with
   *  `show_page_not_found` — silently, as an expected answer — when there is none.
   *  Everything that only DISPLAYS the page uses this — `ensureShowPage` is reserved
   *  for the one caller that owns the first-creation prompt. */
  getShowPage: (sessionId: string) => Promise<ShowPagePayload>;
  /** Create the session's Show Page if absent; resolves to `{ existed, ... }`. Callers
   *  MUST honor `existed === false` by sending the visualize prompt: that edge is
   *  reported once, so a caller that ignores it silently consumes it. To only read the
   *  page, use `getShowPage`. */
  ensureShowPage: (sessionId: string) => Promise<any>;
  /** Upload an image as the page's workspace-root favicon (multipart); resolves to the
   *  refreshed page payload carrying the fresh `icon_version` (§7.1j). */
  uploadShowPageIcon: (sessionId: string, file: File) => Promise<any>;
  /** The workbench Dock document (resident-tile order + pinned Show Pages). */
  getDock: () => Promise<DockResponse>;
  /** Pin a session's Show Page to the Dock as an app (idempotent). */
  pinDockShowPage: (sessionId: string) => Promise<DockResponse>;
  /** Unpin a session's Show Page from the Dock (idempotent; leaves the page). */
  unpinDockShowPage: (sessionId: string) => Promise<DockResponse>;
  /** Persist a new resident-tile (docked-subset) order. ``known`` is the client's
   *  baseline id set (built-ins ∪ pins) for optimistic concurrency: a stale
   *  reorder is rejected server-side and re-synced without a toast. Returns the
   *  payload; check ``ok``. */
  setDockOrder: (order: string[], known?: string[]) => Promise<DockResponse>;
  /** Global workbench toggles (state_meta-backed): banner on/off. */
  getWorkbenchPrefs: () => Promise<WorkbenchPrefs>;
  /** Persist the background-work banner global toggle; resolves to updated prefs. */
  setBackgroundWorkBannerEnabled: (enabled: boolean) => Promise<WorkbenchPrefs>;
  getBindCodes: () => Promise<any>;
  createBindCode: (type: string, expiresAt?: string) => Promise<any>;
  deleteBindCode: (code: string) => Promise<any>;
  getFirstBindCode: () => Promise<any>;
  detectCli: (binary: string) => Promise<any>;
  installAgent: (name: string) => Promise<InstallResult>;
  listDependencies: () => Promise<DependenciesResult>;
  installDependency: (dep: string) => Promise<InstallResult>;
  getMemorySettings: () => Promise<MemorySettingsResult>;
  saveMemorySettings: (patch: MemorySettingsPatch) => Promise<MemorySettingsResult>;
  getMemoryProcessingRecord: () => Promise<MemoryProcessingRecordResult>;
  getMemoryProcessingRecordEntries: (project: string, cursor?: string | null, limit?: number) => Promise<MemoryProcessingRecordListResult>;
  getMemoryProcessingRecordEntry: (project: string, memcellId: string) => Promise<MemoryProcessingRecordDetailResult>;
  getMemoryStatus: () => Promise<MemoryStatusResult>;
  getMemoryFailures: () => Promise<MemoryFailureLogResult>;
  getMemoryMaintenance: () => Promise<MemoryMaintenanceResult>;
  getMemoryProfile: () => Promise<MemoryItemsResult>;
  searchMemory: (query: string, limit?: number, project?: string) => Promise<MemoryRecallResult>;
  listMemoryEpisodes: (
    project: string,
    options?: {
      page?: number;
      cursor?: string | null;
      limit?: number;
      origin?: MemoryOrigin;
    },
  ) => Promise<MemoryListResult>;
  listMemoryProjects: () => Promise<{ status: 'ok'; projects: Array<{ id: string; kind: 'default' | 'named' | 'all' }> } | { status: 'failed'; error?: string }>;
  deleteMemoryData: (confirmLoss: true) => Promise<MemoryDataOperationResult>;
  wakeMemory: () => Promise<MemoryWakeResult>;
  repairMemory: (confirmLoss: true) => Promise<MemoryDataOperationResult>;
  getBackendRuntime: (name: string) => Promise<BackendRuntimeInfo>;
  restartBackend: (name: string) => Promise<BackendRestartResult>;
  getCodexAuth: () => Promise<CodexAuthState>;
  saveCodexAuth: (payload: CodexAuthPayload) => Promise<CodexAuthSaveResult>;
  getClaudeAuth: () => Promise<ClaudeAuthState>;
  saveClaudeAuth: (payload: ClaudeAuthPayload) => Promise<ClaudeAuthSaveResult>;
  startOAuthWeb: (backend: 'claude' | 'codex', forceReset?: boolean) => Promise<OAuthWebStartResult>;
  startOAuthWebForOpencodeProvider: (
    providerId: string,
    forceReset?: boolean,
  ) => Promise<OAuthWebStartResult>;
  getOAuthWebStatus: (
    backend: 'claude' | 'codex' | 'opencode',
    flowId: string,
  ) => Promise<OAuthWebStatus>;
  submitOAuthWebCode: (
    backend: 'claude' | 'codex' | 'opencode',
    flowId: string,
    code: string,
  ) => Promise<OAuthWebMutationResult>;
  cancelOAuthWeb: (
    backend: 'claude' | 'codex' | 'opencode',
    flowId: string,
  ) => Promise<OAuthWebMutationResult>;
  removeBackendAuth: (backend: 'claude' | 'codex') => Promise<OAuthWebMutationResult>;
  removeClaudeOAuthCredentials: () => Promise<OAuthWebMutationResult>;
  // Selectively clear just the stored API key — leave OAuth credentials
  // intact. Symmetric to OpenCode's per-provider DELETE: lets the user
  // drop a stale key without re-signing in. The backend runtime is
  // refreshed so cached sessions observe the change on the next request.
  removeBackendApiKey: (backend: 'claude' | 'codex') => Promise<OAuthWebMutationResult>;
  testBackendAuth: (
    backend: 'claude' | 'codex',
    options?: { model?: string },
  ) => Promise<BackendAuthTestResult>;
  testOpencodeProvider: (
    providerId: string,
    options?: { model?: string },
  ) => Promise<BackendAuthTestResult>;
  getOpencodeProviders: () => Promise<OpencodeProviderListResult>;
  readOpencodeOptionsForModelPicker: () => Promise<OpencodeOptionsResult>;
  saveOpencodeCustomProvider: (
    payload: OpencodeCustomProviderPayload,
  ) => Promise<OpencodeMutationResult>;
  deleteOpencodeCustomProvider: (providerId: string) => Promise<OpencodeMutationResult>;
  setOpencodeProviderAuth: (
    providerId: string,
    apiKey: string,
    baseUrl?: string,
  ) => Promise<OpencodeMutationResult>;
  deleteOpencodeProviderAuth: (providerId: string) => Promise<OpencodeMutationResult>;
  setOpencodeDefaultProvider: (providerId: string) => Promise<OpencodeMutationResult>;
  saveOpencodeProviderModel: (
    providerId: string,
    payload: { model_id: string; reasoning_efforts?: string[] },
  ) => Promise<OpencodeMutationResult>;
  deleteOpencodeProviderModel: (
    providerId: string,
    modelId: string,
  ) => Promise<OpencodeMutationResult>;
  slackAuthTest: (botToken?: string, proxyUrl?: string) => Promise<any>;
  slackChannels: (botToken?: string, browseAll?: boolean, force?: boolean, includeNotReturned?: boolean) => Promise<any>;
  slackManifest: () => Promise<{ ok: boolean; manifest?: string; manifest_compact?: string; error?: string }>;
  discordAuthTest: (botToken?: string, proxyUrl?: string) => Promise<any>;
  discordGuilds: (botToken?: string) => Promise<any>;
  discordChannels: (botToken: string | undefined, guildId: string, force?: boolean, includeNotReturned?: boolean) => Promise<any>;
  telegramAuthTest: (botToken?: string, proxyUrl?: string) => Promise<any>;
  telegramChats: (includePrivate?: boolean, includeNotReturned?: boolean) => Promise<any>;
  larkAuthTest: (appId: string, appSecret?: string, domain?: string, proxyUrl?: string) => Promise<any>;
  larkChats: (appId: string, appSecret?: string, domain?: string, force?: boolean, includeNotReturned?: boolean) => Promise<any>;
  deleteChannel: (platform: string, id: string, scopeType?: string) => Promise<any>;
  larkTempWsStart: (appId: string, appSecret?: string, domain?: string) => Promise<any>;
  larkTempWsStop: () => Promise<any>;
  wechatStartLogin: () => Promise<any>;
  wechatPollLogin: (sessionKey: string, verifyCode?: string) => Promise<any>;
  doctor: (options?: { deep?: boolean }) => Promise<any>;
  opencodeOptions: (cwd: string) => Promise<any>;
  opencodeSetupPermission: () => Promise<{ ok: boolean; message: string; config_path: string }>;
  opencodePermissionStatus: () => Promise<{ ok: boolean; permission_allowed: boolean; config_path: string }>;
  claudeAgents: (cwd?: string) => Promise<{ ok: boolean; agents?: { id: string; name: string; path: string; source?: string }[]; error?: string }>;
  claudeModels: () => Promise<{ ok: boolean; models?: string[]; reasoning_options?: Record<string, { value: string; label: string }[]>; model_labels?: Record<string, string>; catalog_refresh_pending?: boolean; error?: string }>;
  codexAgents: (cwd?: string) => Promise<{ ok: boolean; agents?: { id: string; name: string; path: string; source?: string; description?: string }[]; error?: string }>;
  codexModels: () => Promise<{ ok: boolean; models?: string[]; reasoning_options?: Record<string, { value: string; label: string }[]>; model_labels?: Record<string, string>; catalog_refresh_pending?: boolean; error?: string }>;
  /** Picker-safe persisted catalog. Null tells pickers to use the native fallback. */
  readModelHubAgentCatalogForModelPicker: (
    backend: string,
  ) => Promise<Pick<AgentSupply, 'backend' | 'mode' | 'catalog_models'> | null>;
  getLogs: (lines?: number, source?: string) => Promise<{ logs: LogEntry[]; total: number; source: string; sources: LogSource[] }>;
  getVersion: () => Promise<VersionInfo>;
  doUpgrade: () => Promise<UpgradeResult>;
  browseDirectory: (path: string, showHidden?: boolean) => Promise<{ ok: boolean; path?: string; parent?: string | null; dirs?: { name: string; path: string }[]; error?: string }>;
  browseFavorites: () => Promise<{ ok: boolean; system?: string; favorites?: { key: string; path: string }[]; error?: string }>;
  browseMkdir: (path: string) => Promise<{ path: string }>;
  listProjects: (includeArchived?: boolean, options?: { cache?: boolean }) => Promise<{ projects: WorkbenchProject[] }>;
  getWorkbenchProjectsBootstrap: (params?: {
    includeArchived?: boolean;
    projectIds?: string[];
    status?: 'active' | 'archived' | 'all';
    limit?: number;
    cache?: boolean;
  }) => Promise<WorkbenchProjectsBootstrap>;
  createProject: (payload: { folder_path: string; display_name?: string }) => Promise<WorkbenchProject>;
  // Default-Agent fields accept null to CLEAR the project default (back to the
  // global default); omit a field to leave it untouched.
  updateProject: (
    projectId: string,
    payload: {
      display_name?: string;
      folder_path?: string;
      agent_backend?: string | null;
      agent_id?: string | null;
      expected_agent_id?: string | null;
      agent_name?: string | null;
      agent_variant?: string | null;
      model?: string | null;
      reasoning_effort?: string | null;
    },
  ) => Promise<WorkbenchProject>;
  archiveProject: (projectId: string) => Promise<WorkbenchProject>;
  getProjectAgentsMd: (projectId: string) => Promise<{
    content: string;
    source: 'agents' | 'claude' | 'none';
    symlinked: boolean;
    claude_is_regular_file: boolean;
  }>;
  saveProjectAgentsMd: (
    projectId: string,
    payload: { content: string; symlink: boolean },
  ) => Promise<{ ok: boolean; symlinked: boolean; claude_is_regular_file: boolean; migrated: boolean; symlink_error: string | null }>;
  getGlobalPrompts: () => Promise<{ backends: GlobalPromptFile[] }>;
  saveGlobalPrompts: (
    payload: { content: string; backends: string[] },
  ) => Promise<{ ok: boolean; backends: GlobalPromptFile[] }>;
  listSessions: (params?: { projectId?: string; status?: 'active' | 'archived' | 'all'; limit?: number; beforeId?: string; q?: string; cache?: boolean }) => Promise<{ sessions: WorkbenchSession[]; next_before_id: string | null }>;
  createSession: (payload: WorkbenchSessionCreate) => Promise<WorkbenchSession>;
  forkSession: (sessionId: string) => Promise<WorkbenchSession>;
  getSession: (sessionId: string, params?: { cache?: boolean; handleError?: boolean }) => Promise<WorkbenchSession>;
  getSessionResult: (sessionId: string) => Promise<WorkbenchSessionReadResult>;
  getSessionBootstrap: (sessionId: string) => Promise<WorkbenchSessionBootstrap>;
  updateSession: (sessionId: string, payload: Partial<WorkbenchSessionUpdate>) => Promise<WorkbenchSession>;
  archiveSession: (sessionId: string) => Promise<WorkbenchSession>;
  /** Apply the terminal archived state for raw request paths that intentionally
   *  bypass the shared JSON error handler. */
  convergeSessionArchived: (sessionId: string) => void;
  /** Subscribe to "the server just refused a write because that session is
   *  archived". Fires for EVERY request whose error body carries
   *  ``session_archived``, whatever the verb — the messages POST, the sessions
   *  PATCH, fork, and every Show Page mutation — so a surface holding a stale
   *  pre-archive row converges once, here, instead of each call site
   *  re-implementing it (and the next verb re-introducing the same defect).
   *  Returns an unsubscribe. */
  onSessionArchived: (handler: (sessionId: string) => void) => () => void;
  /** Counts of resources permanently reclaimed when archiving this session
   *  (bound tasks/watches + active runs) — drives the irreversible-confirm dialog. */
  getArchivePreview: (sessionId: string) => Promise<{ tasks: number; watches: number; runs: number; queued: number }>;
  listSessionMessages: (sessionId: string, params?: { afterId?: string; beforeId?: string; aroundId?: string; aroundNativeId?: string; aroundNativePlatform?: string; aroundTurnId?: string; aroundRunId?: string; limit?: number; tail?: boolean; cache?: boolean }) => Promise<{ messages: WorkbenchMessage[]; next_after_id: string | null; next_before_id?: string | null; anchor_id?: string | null }>;
  // Chat Activity panel (GET /api/sessions/<id>/activity): summary of turn groups
  // for chips, and one group's rows for lazy expand. Only used when the
  // ``ui.show_agent_activity`` toggle is on (see lib/agentActivity).
  getSessionActivity: (sessionId: string) => Promise<{ groups: TurnActivityGroupWire[] }>;
  getSessionActivityGroup: (sessionId: string, groupId: string) => Promise<TurnActivityGroupWire>;
  // Full-text search over message content across all sessions. Backed by the
  // non-cached GET /api/search/messages (the query string varies per keystroke,
  // so caching would only bloat the read cache). Results group matches by
  // session, sessions ordered most-recent-match first. ``includeArchived`` opts
  // archived sessions in (they stay excluded by default); archived groups are
  // flagged and open read-only.
  searchMessages: (
    q: string,
    opts?: { limit?: number; includeArchived?: boolean },
  ) => Promise<MessageSearchResult>;
  sendSessionMessage: (sessionId: string, payload: { text?: string; content?: Record<string, unknown>; metadata?: Record<string, unknown>; author_id?: string; author_name?: string }) => Promise<WorkbenchMessage>;
  markSessionRead: (sessionId: string, untilMessageId?: string, opts?: { handleError?: boolean }) => Promise<{ updated: number; unread_counts: Record<string, number>; unread_by_session?: Record<string, number> }>;
  cancelSession: (
    sessionId: string,
  ) => Promise<{
    ok: boolean;
    status?: string;
    code?: string;
    detail?: string;
    recovered_agent_status?: boolean;
  }>;
  // Send-while-busy queue (messages sent while a turn runs) + per-session draft.
  listSessionQueue: (sessionId: string, options?: { cache?: boolean }) => Promise<{ queued: WorkbenchMessage[] }>;
  removeQueuedMessage: (sessionId: string, messageId: string) => Promise<{ removed: boolean }>;
  sendQueuedNow: (sessionId: string, messageId: string) => Promise<{ ok: boolean; status?: string; code?: string; detail?: string }>;
  getTurnState: (sessionId: string, options?: { handleError?: boolean }) => Promise<SessionRuntimeState>;
  getCachedSessionDraft: (sessionId: string) => string | null;
  cacheSessionDraft: (sessionId: string, text: string) => void;
  getSessionDraft: (sessionId: string) => Promise<{ text: string }>;
  setSessionDraft: (sessionId: string, text: string) => Promise<{ ok: boolean }>;
  reconcileSessionDraftAfterSend: (
    sessionId: string,
    draft: { text: string; updated_at: string | null },
  ) => Promise<void>;
  recoverSessionDraftAfterRejectedSend: (sessionId: string) => Promise<void>;
  listInbox: (params?: { platform?: string; unreadOnly?: boolean; limit?: number; before?: string; onlySession?: string; cache?: boolean; handleError?: boolean }) => Promise<InboxFeedResult>;
  connectWorkbenchEvents: (handlers: WorkbenchEventHandlers) => () => void;
  listVibeAgents: (params?: {
    backend?: string;
    includeDisabled?: boolean;
    includeArchived?: boolean;
    cache?: boolean;
  }) => Promise<{ ok: boolean; agents: VibeAgentBrief[]; default_agent_name: string | null }>;
  getVibeAgentOnboarding: () => Promise<VibeAgentOnboardingResult>;
  onboardVibeAgents: () => Promise<VibeAgentOnboardingResult>;
  getVibeAgent: (
    name: string,
    params?: { cache?: boolean; handleError?: boolean; expectedCodes?: readonly string[] },
  ) => Promise<{ ok: boolean; agent: VibeAgentFull; default_agent_name: string | null }>;
  createVibeAgent: (payload: VibeAgentCreatePayload) => Promise<{ ok: boolean; agent: VibeAgentFull }>;
  updateVibeAgent: (name: string, payload: VibeAgentUpdatePayload) => Promise<{ ok: boolean; agent: VibeAgentFull }>;
  setDefaultVibeAgent: (name: string) => Promise<{ ok: boolean; default_agent_name: string; agent: VibeAgentBrief }>;
  removeVibeAgent: (name: string) => Promise<{ ok: boolean; code?: string; message?: string; references?: Record<string, number>; removed_agent?: string; archived_agent?: VibeAgentBrief; default_agent_name?: string | null }>;
  listVaultSecrets: () => Promise<{ ok: boolean; secrets: VaultSecret[] }>;
  getVaultVmk: () => Promise<VaultVmkResult>;
  getVaultPubkey: () => Promise<{ ok: boolean; public_key: string; fingerprint: string }>;
  getVaultAgentPubkey: () => Promise<{ ok: boolean; public_key: string; fingerprint: string }>;
  getVaultSandboxRootMetadata: () => Promise<VaultSandboxRootMetadataResult>;
  /** One batch of signed agent-delivery contexts for every protected member of a request (§7.1). */
  createVaultAgentBindingsBatch: (payload: {
    request_id: string;
    grant_duration?: VaultGrantDuration;
  }) => Promise<VaultAgentBindingsBatchResult>;
  getVaultSettings: () => Promise<VaultSettingsResult>;
  saveVaultSettings: (payload: Partial<VaultSettings>) => Promise<VaultSettingsResult>;
  createVaultRevealContext: (
    name: string,
    payload?: { session_label?: string },
  ) => Promise<VaultRevealContextResult>;
  deriveSigningAddresses: (publicKey: string) => Promise<{ ok: boolean; addresses?: SigningAddresses; code?: string; message?: string }>;
  createVaultAuthzWebAuthnOptions: () => Promise<VaultWebAuthnRegistrationOptions>;
  registerVaultAuthzWebAuthnFactor: (
    payload: VaultWebAuthnRegistrationPayload,
  ) => Promise<{ ok: boolean; factor?: Record<string, unknown>; code?: string; message?: string }>;
  createVaultSecret: (payload: VaultCreatePayload, opts?: { handleError?: boolean }) => Promise<{ ok: boolean; secret?: VaultSecret; code?: string; message?: string }>;
  updateVaultSecret: (name: string, payload: VaultMetadataUpdatePayload, opts?: { handleError?: boolean }) => Promise<{ ok: boolean; secret?: VaultSecret; code?: string; message?: string }>;
  deleteVaultSecret: (name: string) => Promise<{ ok: boolean; removed?: boolean; code?: string; message?: string }>;
  getVaultProvisionRequest: (
    name: string,
    opts?: { handleError?: boolean },
  ) => Promise<{ ok: boolean; request: VaultRequest | null; ambiguous?: boolean }>;
  getVaultProvisionRequestById: (requestId: string, opts?: { handleError?: boolean }) => Promise<{ ok: boolean; request: VaultRequest | null }>;
  getVaultRequests: (params?: { status?: string; type?: string; limit?: number; session?: string }, opts?: { handleError?: boolean }) => Promise<{ ok: boolean; requests: VaultRequest[] }>;
  denyVaultRequest: (requestId: string) => Promise<{ ok: boolean; request?: VaultRequest; code?: string; message?: string }>;
  fulfillVaultAccessRequest: (requestId: string, payload: VaultAccessFulfillmentPayload) => Promise<{ ok: boolean; request_id?: string; grant?: VaultGrant; result?: { type: string; grant?: VaultGrant }; code?: string; message?: string }>;
  getVaultGrants: (params?: { status?: string; sessionId?: string }, opts?: { handleError?: boolean }) => Promise<{ ok: boolean; grants: VaultGrant[] }>;
  createVaultGrant: (payload: Record<string, unknown>) => Promise<{ ok: boolean; grant: VaultGrant; code?: string; message?: string }>;
  revokeVaultGrant: (grantId: string) => Promise<{ ok: boolean; grant?: VaultGrant; code?: string; message?: string }>;
  signVaultDigest: (payload: Record<string, unknown>) => Promise<{ ok: boolean; signature?: Record<string, unknown>; request?: VaultRequest; code?: string; message?: string }>;
  pinVaultPubkey: (payload: Record<string, unknown>) => Promise<{ ok: boolean; secret?: VaultSecret; code?: string; message?: string }>;
  getVaultAudit: (params?: { secret?: string; limit?: number }) => Promise<{ ok: boolean; events: VaultAuditEvent[] }>;
  importVibeAgents: (payload: { from?: 'claude' | 'codex' | 'opencode'; name?: string; all?: boolean; file?: string; backend?: string }) => Promise<{ ok: boolean; imported?: any[]; skipped?: any[]; error?: string; code?: string; message?: string }>;
  // Agent Skills — thin shells over the askill CLI (see /api/skills*).
  listSkills: (params?: { scope?: SkillScope | 'all'; projectId?: string; backends?: string[] }) => Promise<SkillsListResult>;
  previewSkillSource: (source: string, params?: { projectId?: string }) => Promise<SkillsPreviewResult>;
  addSkill: (payload: { source: string; scope: SkillScope; projectId?: string; backends?: string[]; all?: boolean; skill?: string; copy?: boolean }) => Promise<SkillsMutationResult>;
  removeSkill: (name: string, params?: { scope?: SkillScope; projectId?: string; backends?: string[] }) => Promise<SkillsMutationResult>;
  findSkills: (query: string) => Promise<SkillsFindResult>;
  uploadSkillZip: (file: File, params?: { projectId?: string }) => Promise<SkillsUploadResult>;
  checkSkills: (params?: { scope?: SkillScope; projectId?: string }) => Promise<SkillsCheckResult>;
  updateSkill: (name: string, params?: { scope?: SkillScope; projectId?: string }) => Promise<SkillsMutationResult>;
  getHarnessCounts: () => Promise<HarnessCountsResult>;
  getHarnessBootstrap: (params?: HarnessBootstrapParams) => Promise<HarnessBootstrapResult>;
  // ``opts.handleError: false`` suppresses the global error toast (and bypasses
  // the read cache) for best-effort background polls — e.g. the open trigger
  // detail panel's 4s refresh, which must not toast on every tick when an
  // endpoint is down. Matches the getSession/vault handleError idiom.
  listHarnessTasks: (params?: HarnessDefinitionsParams, opts?: { handleError?: boolean }) => Promise<HarnessTasksResult>;
  setHarnessTaskEnabled: (taskId: string, enabled: boolean) => Promise<{ ok: boolean; task?: HarnessTask }>;
  deleteHarnessTask: (taskId: string) => Promise<{ ok: boolean; id?: string }>;
  listHarnessWatches: (params?: HarnessDefinitionsParams, opts?: { handleError?: boolean }) => Promise<HarnessWatchesResult>;
  setHarnessWatchEnabled: (watchId: string, enabled: boolean) => Promise<{ ok: boolean; watch?: HarnessWatch }>;
  deleteHarnessWatch: (watchId: string) => Promise<{ ok: boolean; id?: string }>;
  listHarnessRuns: (params?: HarnessRunsParams, opts?: { handleError?: boolean }) => Promise<HarnessRunsResult>;
  getHarnessRun: (runId: string) => Promise<{ ok: boolean; run: HarnessRun }>;
  getRunningAgents: () => Promise<RunningAgentsResult>;
  // Agents · 运行图 graph payload (contract §3). Realtime — refetched off SSE,
  // so it bypasses the read cache. ``live_unreachable`` is set when the
  // controller is down and the graph fell back to DB-only (history).
  getAgentsGraph: (params?: AgentGraphParams) => Promise<AgentGraphResult & { live_unreachable?: boolean }>;
  // Foreground/background toggle from the graph detail panel (contract §2,
  // M1-owned PATCH). Returns the updated session payload.
  setSessionVisibility: (sessionId: string, visibility: AgentGraphVisibility) => Promise<WorkbenchSession>;
  endRunningAgent: (payload: {
    backend?: string | null;
    state?: string | null;
    session_id?: string | null;
    composite_key?: string | null;
    base_session_id?: string | null;
    pid?: number | null;
  }) => Promise<{ ok: boolean; unreachable?: boolean; error?: string; action?: string }>;
  remoteAccessStatus: () => Promise<RemoteAccessStatus>;
  pairVibeCloudRemoteAccess: (payload: { backend_url: string; pairing_key: string; device_name?: string }) => Promise<any>;
  startRemoteAccess: () => Promise<RemoteAccessStatus>;
  stopRemoteAccess: () => Promise<RemoteAccessStatus>;
  optimizeRemoteAccessRoute: () => Promise<RemoteAccessStatus>;
  getRemoteAccessNetworkInterfaces: () => Promise<{ ok: boolean; interfaces: TunnelNetworkInterface[] }>;
  saveRemoteAccessSettings: (settings: RemoteAccessSettings) => Promise<RemoteAccessStatus>;
  diagnoseRemoteAccess: () => Promise<TunnelConnectivityDiagnostics>;
  getAuthSession: () => Promise<SessionInfo>;
  signOut: () => Promise<{ ok: boolean }>;
};

// Workbench project — a scope row with platform='avibe' / scope_type='project'.
// ``folder_path`` mirrors ``scope_settings.workdir`` and is what Agent runs
// pick up as their cwd.
// A project's default Agent route (backend + agent + model + effort), stored on
// the project and inherited by new sessions created under it. ``null`` (or an
// absent field) means "no project default" → fall back to the global default.
export type ProjectDefaultAgent = {
  agent_backend: string | null;
  agent_id: string | null;
  agent_name: string | null;
  agent_variant: string | null;
  model: string | null;
  reasoning_effort: string | null;
};

export type WorkbenchProject = {
  id: string;
  scope_id: string;
  display_name: string;
  folder_path: string;
  created_at: string;
  last_active_at: string | null;
  archived: boolean;
  default_agent?: ProjectDefaultAgent | null;
  metadata?: Record<string, unknown>;
  capabilities: {
    can_chat: boolean;
    has_folder: boolean;
  };
};

export type ProjectSessionsPage = {
  sessions: WorkbenchSession[];
  next_before_id: string | null;
};

export type WorkbenchProjectsBootstrap = {
  projects: WorkbenchProject[];
  sessions: Record<string, ProjectSessionsPage | undefined>;
};

// Workbench session — a row in ``agent_sessions`` created via /api/sessions.
// ``project_id`` is the short ``proj_<hex>`` suffix of ``scope_id``.
export type WorkbenchSession = {
  id: string;
  scope_id: string | null;
  project_id: string | null;
  title: string | null;
  agent_id: string | null;
  agent_name: string | null;
  agent_backend: string | null;
  agent_variant: string | null;
  model: string | null;
  reasoning_effort: string | null;
  status: string;
  /** Storage projection, not a user preference: ``foreground`` = an ordinary chat,
   *  ``background`` = hidden and undelivered, ``system`` = a row the RUNTIME owns
   *  (kept out of session lists, still an Inbox destination — today the
   *  workspace-notifications session, which accepts no turn, see
   *  ``sessionReadOnlyReason``). Optional because payloads cached by an older client
   *  predate the field; the server always sends it. */
  visibility?: 'foreground' | 'background' | 'system';
  pinned: boolean;
  /** Live agent-runtime status driving the sidebar dot: idle (gray) /
   *  running (green) / failed (red). Distinct from the lifecycle ``status``. */
  agent_status: 'idle' | 'running' | 'failed';
  workdir: string | null;
  native_session_id: string | null;
  created_at: string;
  updated_at: string;
  last_active_at: string | null;
  metadata: Record<string, unknown>;
};

export type WorkbenchSessionReadResult = {
  status: number;
  session: WorkbenchSession | null;
};

export type WorkbenchSessionCreate = {
  project_id: string;
  // Optional: when omitted the server resolves the current default Agent.
  agent_backend?: string;
  agent_id?: string;
  agent_name?: string;
  agent_variant?: string;
  model?: string;
  reasoning_effort?: string;
  title?: string;
  metadata?: Record<string, unknown>;
};

export type WorkbenchSessionUpdate = {
  title: string | null;
  agent_id: string | null;
  agent_name: string | null;
  // Session execution snapshot, not a scope/default route selector.
  agent_backend: string;
  agent_variant: string;
  model: string | null;
  reasoning_effort: string | null;
  pinned: boolean;
};

// One Vibe Agent row from ``/agents`` (brief view used in list rendering).
// ``source`` distinguishes system-builtin agents from user-created ones —
// system agents lock the ``backend`` field and refuse delete, but their
// model / effort / system_prompt / enabled state are still editable.
export type VibeAgentBrief = {
  id: string;
  name: string;
  display_name: string;
  description: string | null;
  backend: string;
  model: string | null;
  reasoning_effort: string | null;
  enabled: boolean;
  archived: boolean;
  archived_at: string | null;
  source: string;
  updated_at: string;
};

export type VibeAgentFull = VibeAgentBrief & {
  system_prompt: string | null;
  metadata: Record<string, unknown>;
  created_at: string;
};

export type VibeAgentOnboardingItem = {
  id: string;
  name: string;
  backend: string;
  source: string;
  enabled: boolean;
  status: 'not_onboarded' | 'private' | 'published' | 'managed_elsewhere';
  access_level: 'private' | 'scope' | 'public' | null;
  group_ids: string[];
  policy_revision: number | null;
  applied_acl_revision: number | null;
};

export type VibeAgentOnboardingResult = {
  ok: boolean;
  available: boolean;
  organization_id: string | null;
  console_url?: string;
  agents: VibeAgentOnboardingItem[];
  counts: {
    total: number;
    system: number;
    custom: number;
    not_onboarded: number;
    private: number;
    published: number;
    conflicts: number;
  };
  created?: number;
  unchanged?: number;
  conflicts?: number;
  sync?: { ok?: boolean; error?: string };
};

export type VibeAgentCreatePayload = {
  name: string;
  backend: string;
  description?: string | null;
  model?: string | null;
  reasoning_effort?: string | null;
  system_prompt?: string | null;
  metadata?: Record<string, unknown>;
  enabled?: boolean;
};

// Agent Skills (askill CLI). The backend returns the askill --json envelope,
// optionally enriched; logical failures come back as { ok: false, error }
// with HTTP 200 (callers branch on `ok`, like the agents endpoints).
export type SkillScope = 'global' | 'project';
export type AskillAgentRef = { id: string; name: string };
export type SkillsErrorBody = { code: string; message: string; details?: unknown };
export type SkillBrief = {
  name: string;
  scope: SkillScope;
  path: string;
  agents: AskillAgentRef[];
  description?: string | null;
  version?: string | null;
  // Enriched natively by `list --json` (askill v0.1.13+).
  tags?: string[];
  sourceType?: string | null;
  sourceUrl?: string | null;
  installSource?: string | null;
  installedAt?: string | null;
  updatedAt?: string | null;
};
export type SkillsListResult = {
  ok: boolean;
  error?: SkillsErrorBody;
  filters?: { scope: string; agents: AskillAgentRef[] };
  summary?: { global: number; project: number };
  skills?: SkillBrief[];
  /** Set when the selected project has no folder configured: the backend
   *  returned global skills only (project-scoped skills aren't possible). */
  project_no_folder?: boolean;
};
export type SkillAiBreakdown = { key: string; label: string; score: number };
export type SkillSearchItem = {
  id: string | number;
  name: string;
  description: string;
  owner: string;
  repo: string | null;
  tags: string[];
  stars: number | null;
  aiScore: number | null;
  aiBreakdown: SkillAiBreakdown[];
  updatedAt: string | null;
  installSource: string;
  url: string | null;
};
export type SkillsFindResult = {
  ok: boolean;
  error?: SkillsErrorBody;
  query?: string;
  count?: number;
  skills?: SkillSearchItem[];
};
export type SkillDiscovered = { name: string; description: string; path?: string | null };
export type SkillsPreviewResult = {
  ok: boolean;
  error?: SkillsErrorBody;
  action?: string;
  source?: Record<string, unknown>;
  skills?: SkillDiscovered[];
};
export type SkillsMutationResult = { ok: boolean; error?: SkillsErrorBody; [key: string]: unknown };
// Result of uploading a .zip: the server unpacks it and previews the skills
// inside; `dir` is the server-side path to install from via addSkill.
export type SkillsUploadResult = {
  ok: boolean;
  error?: SkillsErrorBody;
  dir?: string;
  skills?: SkillDiscovered[];
};
export type SkillCheckStatus = 'update_available' | 'up_to_date' | 'uncheckable';
export type SkillCheckItem = {
  name: string;
  scope: SkillScope;
  status: SkillCheckStatus;
  localVersion?: string | null;
  remoteVersion?: string | null;
  reason?: string | null;
};
export type SkillsCheckResult = {
  ok: boolean;
  error?: SkillsErrorBody;
  summary?: { total: number; updateAvailable: number; upToDate: number; uncheckable: number };
  skills?: SkillCheckItem[];
};

export type VibeAgentUpdatePayload = {
  name?: string;
  description?: string | null;
  model?: string | null;
  reasoning_effort?: string | null;
  system_prompt?: string | null;
  metadata?: Record<string, unknown>;
  enabled?: boolean;
};

// Events streamed by ``GET /api/events`` — the broker JSON-encodes each
// payload as ``{type, data}`` (older servers may include ``ts``).
// ``connectWorkbenchEvents`` parses and dispatches to type-specific handlers;
// subscribers can also catch any event via ``onAny`` for logging/analytics.
export type WorkbenchEventEnvelope<T = unknown> = {
  type: string;
  data: T;
  ts?: number;
};

export type WorkbenchEventHandlers = {
  /**
   * A stream is live and reaching this consumer, and any gap before now is
   * over. Every gap ends here, on either of the two legs a stream is carried
   * over: the browser socket breaking (an error, a page that came back to a
   * stream that could not prove it survived, a heartbeat that stopped arriving)
   * is recovered by reconnecting, and this fires once the new subscription
   * exists; the UI server's controller bridge dropping and coming back is
   * recovered in place, and this fires when it does. Nothing replays either
   * gap, so this is also the one place to re-read whatever this consumer keeps
   * live off the stream — including the window between its own first read and
   * this subscription.
   *
   * Consumers must not re-derive when a gap happened. Neither the reactivation
   * edge nor `onEventBridgeStatus` is that signal: a returning page whose stream
   * never broke has missed nothing, and a bridge report is a level rather than
   * an edge. Both verdicts are made in one place, and a consumer recomputing
   * them will drift from it.
   *
   * It carries no payload, deliberately. Which leg came back, and whether any
   * handshake stands behind this edge at all -- a page returning onto a stream
   * that could not prove it survived says to catch up now, rather than wait for
   * a replacement several backoff windows away -- are distinctions a catch-up
   * cannot branch on without silently skipping the gaps it does not recognise.
   * So there is nothing here to branch on. `onEventBridgeStatus` stays the level
   * a bridge indicator renders from, and is not a second catch-up trigger: every
   * bridge recovery arrives here too, so refetching from both would charge each
   * one twice.
   */
  onConnected?: () => void;
  onConnectionState?: (state: WorkbenchEventConnectionState) => void;
  onEventBridgeStatus?: (data: { connected: boolean }) => void;
  onAuthorizationChanged?: (data: {
    project_ids?: string[];
    resource_kinds?: string[];
    instance_authorization_revision?: number;
  }) => void;
  onMessageNew?: (data: WorkbenchMessage) => void;
  // ``visibility`` (contract A6): the backend carries the session's current
  // foreground/background on visibility/scope changes so the Inbox can drop /
  // restore the card live. Absent on pre-M1 backends ⇒ consumers no-op.
  onSessionActivity?: (data: {
    session_id: string;
    scope_id: string | null;
    event: string;
    title?: string | null;
    visibility?: 'foreground' | 'background';
    pinned?: boolean;
    // Client-synthesized marker (never on a real backend event): a foreground
    // restore, so the projects tree grows its window to bring the row back.
    restored?: boolean;
  }) => void;
  onInboxUnreadChanged?: (data: {
    session_id?: string;
    scope_id?: string | null;
    delta?: number;
    unread_counts: Record<string, number>;
    unread_by_session?: Record<string, number>;
  }) => void;
  // A session's inbox card changed — new agent reply, or the user replied.
  // Carries the recomputed per-session row so consumers upsert + re-sort in
  // place without a refetch (the realtime "bump to top" signal).
  onInboxSessionUpdated?: (data: InboxSession) => void;
  // Session-level turn lifecycle (the controller is the authority): a turn for
  // this session started / settled. Drives the Chat working indicator + Stop
  // button without the browser having to infer turn end from message rows.
  onTurnStart?: (data: { session_id: string }) => void;
  onTurnEnd?: (data: { session_id: string }) => void;
  // A session's live agent-runtime status changed (idle/running/failed) — the
  // sidebar dot recolors from this without a refetch. Same controller→browser
  // bus as turn.start/turn.end; published only when the value actually moves.
  onSessionStatus?: (data: { session_id: string; agent_status: 'idle' | 'running' | 'failed' }) => void;
  // The send-while-busy queue for a session changed (enqueue / flush / remove).
  onQueueUpdated?: (data: { session_id: string }) => void;
  onRunsUpdated?: (data: {
    run_id: string;
    status: HarnessRunStatus;
    run_type?: string;
    session_id?: string;
    definition_id?: string;
    updated_at?: string;
    cancel_requested?: boolean;
  }) => void;
  onVaultsUpdated?: (data: {
    scope: string;
    request_id?: string;
    request_status?: string;
    grant_id?: string;
    grant_status?: string;
    secret_name?: string;
  }) => void;
  onRemoteAccessQuality?: (data: TunnelQualitySnapshot) => void;
  onAny?: (event: WorkbenchEventEnvelope) => void;
  onError?: (err: Event) => void;
};

// One row from the platform-agnostic ``messages`` table.
export type WorkbenchMessage = {
  id: string;
  scope_id: string | null;
  session_id: string | null;
  platform: string;
  author: 'user' | 'agent' | 'system' | string;
  // First-class message type: 'user' | 'harness' | 'assistant' | 'tool_call' |
  // 'notify' | 'result'. Distinct from the coarse author — the chat renders
  // 'notify' as a terminal status marker, and the inbox previews 'result' only.
  type: 'user' | 'harness' | 'assistant' | 'tool_call' | 'notify' | 'result' | string;
  // Origin of the message, distinct from the coarse ``author`` role: a
  // harness-triggered prompt uses author/type='harness'. Drives the transcript's
  // "Scheduled task" / "Watch" provenance tag.
  source: 'user' | 'agent' | 'harness' | string | null;
  author_id: string | null;
  author_name: string | null;
  // Read-side provenance for an agent-callback ("自动触发") harness message (A9a):
  // the session that triggered the run, resolved from the run's source_actor.
  // Present only on agent_run harness messages; enables the source-session chip
  // + /chat/<source_session_id> deep-link.
  source_session_id?: string | null;
  source_session_title?: string | null;
  source_session_agent_name?: string | null;
  native_message_id: string | null;
  parent_native_message_id: string | null;
  // Server-owned read projection. Durable Message rows never carry this field.
  projection?: 'claimed_delivery' | null;
  text: string;
  content: Record<string, unknown>;
  metadata: Record<string, unknown>;
  created_at: string;
  updated_at: string;
  delivered_at: string | null;
  read_at: string | null;
};

// One highlighted message-content hit from GET /api/search/messages, split by
// the server so the UI never has to locate the match: ``prefix`` + ``match`` +
// ``suffix`` reconstruct a window of the message text, with ``match`` the part
// to highlight (empty when the snippet is just leading context).
export type MessageSnippet = {
  prefix: string;
  match: string;
  suffix: string;
};

// A single matching message within a session group. The row chip derives its
// role from author/type/source so harness prompts remain distinct from both
// human input and agent output.
export type MessageSearchMatch = {
  id: string;
  author: string;
  source: string | null;
  type: 'user' | 'harness' | 'result' | string;
  created_at: string;
  snippet: MessageSnippet;
};

// Matches grouped by their session, with enough session/project context to
// render a group header and (in P3/P4) navigate into the chat at the match.
export type MessageSearchSession = {
  session_id: string;
  title: string | null;
  project_id: string | null;
  project_name: string | null;
  // True when the session is archived (only possible with includeArchived) —
  // the group is marked in the results and the chat opens read-only.
  archived: boolean;
  matches: MessageSearchMatch[];
};

export type MessageSearchResult = {
  sessions: MessageSearchSession[];
  total: number;
  session_count: number;
};

// Union item for the background-work banner. Backend activities (from the
// process-local SessionActivityRegistry) and live-derived harness items
// (watches / scheduled tasks / delegated agent runs) share this shape. The
// legacy fields stay for backward compatibility; `item_kind` / `label` /
// `since` / `schedule_type` are the unified fields the banner renders and
// routes on. `item_kind` is optional so a pre-union payload degrades to a
// backend activity.
export type SessionActivityItemKind = 'backend_activity' | 'watch' | 'task' | 'agent_run';

export type SessionActivityState = {
  id: string;
  backend: string;
  runtime_key: string;
  session_id: string | null;
  kind: string;
  status: string;
  description: string | null;
  started_at: string;
  updated_at: string;
  item_kind?: SessionActivityItemKind;
  label?: string | null;
  since?: string;
  schedule_type?: 'at' | 'cron' | null;
};

export type SessionRuntimeState = {
  in_flight: boolean | null;
  foreground: 'idle' | 'running' | 'unknown';
  native_turn_started: boolean;
  pending_input_count: number;
  background_activities: SessionActivityState[];
  pending_activity_output_count: number;
  connection: 'connected' | 'reconnecting' | 'disconnected' | 'unknown';
  backend?: string;
  recovered_agent_status?: boolean;
};

export type WorkbenchSessionBootstrap = {
  session: WorkbenchSession;
  capabilities: { can_chat: boolean };
  agents: VibeAgentBrief[];
  default_agent_name: string | null;
  config: any | null;
  messages: WorkbenchMessage[];
  next_after_id: string | null;
  next_before_id?: string | null;
  queued: WorkbenchMessage[];
  draft: { text: string; updated_at: string | null };
  turn_state: SessionRuntimeState;
};

function sessionDraftServerState(payload: unknown): SessionDraftServerState {
  const draft = payload && typeof payload === 'object'
    ? payload as { text?: unknown; updated_at?: unknown }
    : {};
  return {
    text: typeof draft.text === 'string' ? draft.text : '',
    updatedAt: typeof draft.updated_at === 'string' ? draft.updated_at : null,
  };
}

const SESSION_DRAFT_WRITE_TIMEOUT_MS = 12_000;
const SESSION_DRAFT_RECONCILE_TIMEOUT_MS = 5_000;

// One row of the per-session ("Slack-like") inbox feed from ``GET /api/inbox``.
// Aggregated per session at query time: ``preview_text`` is the session's latest
// agent ``result`` (aligned with the avibe chat, which only shows results),
// ``last_activity_at`` is the most recent message of *any* author (the sort
// key), and ``replied`` is true when the session is awaiting the agent — the
// user's latest message is newer than the agent's latest reply, so it stays set
// for the whole agent turn (even mid-stream) and clears only once the agent
// replies.
export type InboxSession = {
  session_id: string;
  scope_id: string | null;
  project_id: string | null;
  project_name: string | null;
  title: string | null;
  last_activity_at: string;
  last_message_author: string | null;
  replied: boolean;
  preview_text: string;
  preview_at: string | null;
  unread_count: number;
  unread: boolean;
};

export type InboxFeedResult = {
  sessions: InboxSession[];
  next_cursor: string | null;
  unread_by_session: Record<string, number>;
  unread_total: number;
  unread_sessions: number;
};

// =============================================================================
// Harness (scheduled tasks / watches / runs)
// =============================================================================

// Server-resolved view of a task/watch's bound session, for the cards. A
// workbench session carries a title; an IM session resolves to its platform +
// channel display name.
//
// ``session_is_workbench`` chooses the icon and which label to show.
// ``session_openable`` — and only it — decides whether the label is a link:
// ``/chat/<id>`` opens IM-bound sessions too, so linking on "is workbench" hid
// working destinations behind a bare id. One predicate answers it for every
// surface (``storage/agent_session_rows.py::session_openable_in_chat``).
export type HarnessSessionSummary = {
  session_title: string | null;
  session_platform: string | null;
  session_scope_kind: string | null;
  session_label: string | null;
  session_is_workbench: boolean;
  session_openable: boolean;
};

// What a task/watch is *doing*, derived server-side from columns that already
// exist. ``enabled`` is a switch and was being read as a state, which made a
// one-shot that finished on its own indistinguishable from one the user paused.
// ``lifecycle_detail`` is set only on ``finished`` rows and says how they ended.
export type HarnessLifecycleState = 'running' | 'waiting' | 'paused' | 'finished';
export type HarnessLifecycleDetail = 'normal' | 'timeout' | 'error' | 'missed' | 'canceled';
export type HarnessDefinitionHealth = 'failing' | 'degraded' | 'healthy' | 'unknown';

// The fields every task/watch row reads to describe its state.
export type HarnessDefinitionState = {
  lifecycle_state: HarnessLifecycleState | null;
  lifecycle_detail: HarnessLifecycleDetail | null;
  // When the scheduler will fire this next; null when nothing is promised.
  next_run_at: string | null;
  // When the current wait began — set only while ``waiting``, so a paused row's
  // last start reads as history rather than a wait anyone is still in.
  waiting_since: string | null;
  // When the run that makes this row ``running`` actually started. Null while
  // that run is still queued, and null in every other state — a duration for
  // "how long has this been running" must come from the run that is running,
  // not from whenever the row last did anything.
  running_since: string | null;
  retired_at?: string | null;
  lifecycle_finished_at?: string | null;
  // Derived from this definition's own settled run outcomes, never from
  // ``last_run_at``/``last_error``: those are overwritten on every fire, so one
  // success used to erase days of failure and a daily-failing cron rendered
  // identically to a daily-succeeding one.
  //
  // ``failing`` = the newest verdict failed. ``degraded`` = the newest succeeded
  // but a failure is still inside the window — a success downgrades, it does not
  // clear. ``unknown`` = health could not be computed, which must not read as a
  // clean bill of health.
  health: HarnessDefinitionHealth | null;
  // How many verdicts back the failure run reaches, and how many failures are in
  // the window at all. Both age out on their own; neither is acknowledgment state.
  consecutive_failures: number;
  recent_failures: number;
  // Watches separate waiter health above from the Agent Runs created by events.
  // Tasks omit these because their execution is already the definition health.
  processing_health?: HarnessDefinitionHealth | null;
  processing_consecutive_failures?: number;
  processing_recent_failures?: number;
};

export type HarnessTask = HarnessSessionSummary & HarnessDefinitionState & {
  id: string;
  name: string | null;
  agent_name: string | null;
  session_policy: string | null;
  session_id: string | null;
  session_key: string;
  prompt: string;
  message: string;
  message_payload: Record<string, unknown> | null;
  schedule_type: string;
  cron: string | null;
  run_at: string | null;
  timezone: string;
  post_to: string | null;
  deliver_key: string | null;
  enabled: boolean;
  created_at: string | null;
  updated_at: string | null;
  last_run_at: string | null;
  last_run_id: string | null;
  last_error: string | null;
  resume_blocked?: {
    code: string;
    owner_session_id: string;
  } | null;
  // Command tasks: a scheduled definition that runs a subprocess instead of
  // prompting an Agent. Non-null ``shell_command`` OR a non-empty ``command``
  // argv is what makes a row one (see ``taskIsCommand``); its ``prompt`` is
  // empty and — when ``metadata.on_failure`` is ``"none"`` — it has no session
  // at all, so nothing here may be assumed present.
  //
  // ``/api/harness/tasks`` serves the raw store row, so these are exactly the
  // keys ``_scheduled_task_from_row`` writes: ``metadata`` already decoded, not
  // ``metadata_json``.
  shell_command?: string | null;
  command?: unknown[] | null;
  timeout_seconds?: number | null;
  last_exit_code?: number | null;
  metadata?: Record<string, unknown> | null;
  // Where a command task's subprocess runs. Null is not "nowhere": a definition
  // bound to a Session follows that Session's workdir, read live at fire time
  // (``_bound_session_workdir``), so the pane names the source rather than
  // printing a blank.
  cwd?: string | null;
};

export type HarnessWatchRuntime = {
  running: boolean;
  pid?: number | null;
  started_at?: string | null;
  updated_at?: string | null;
};

export type HarnessWatch = HarnessSessionSummary & HarnessDefinitionState & {
  id: string;
  name: string | null;
  agent_name: string | null;
  session_policy: string | null;
  session_id: string | null;
  session_key: string;
  command: unknown[];
  shell_command: string | null;
  prefix: string | null;
  message: string | null;
  message_payload: Record<string, unknown> | null;
  cwd: string | null;
  mode: string;
  timeout_seconds: number;
  lifetime_timeout_seconds: number;
  retry_exit_codes: number[];
  retry_delay_seconds: number;
  post_to: string | null;
  deliver_key: string | null;
  enabled: boolean;
  created_at: string | null;
  updated_at: string | null;
  last_started_at: string | null;
  last_finished_at: string | null;
  retired_at: string | null;
  last_event_at: string | null;
  last_error: string | null;
  last_exit_code: number | null;
  // Decoded server-side metadata includes durable Watch admission facts such as
  // the circuit-breaker incident; it is troubleshooting evidence, not a new UI
  // lifecycle state.
  metadata: Record<string, unknown> | null;
  runtime: HarnessWatchRuntime;
  // Whether the waiter process is alive. ``null`` means we have never seen a
  // heartbeat for it, which is not the same as having seen it exit — the row
  // must not report a dead waiter on the strength of never having looked.
  process_alive: boolean | null;
};

export type HarnessRunStatus = 'queued' | 'running' | 'succeeded' | 'failed' | 'canceled' | (string & {});

// Everything the list endpoint accepts. The UI offers four of these as chips
// (see ``harnessLifecycle.ts``); the per-state values stay valid so a deep link
// can name one exactly.
export type HarnessDefinitionStatus =
  | 'all'
  | 'active'
  | 'running'
  | 'waiting'
  | 'paused'
  | 'finished';

// Counts are per *state*, one bucket per lifecycle value plus the total, so a
// chip spanning two states sums them client-side rather than asking the server
// for a bucket named after a chip.
export type HarnessDefinitionCounts = {
  total: number;
  running: number;
  waiting: number;
  paused: number;
  finished: number;
  [key: string]: number;
};

export type HarnessRunCounts = {
  all: number;
  queued: number;
  running: number;
  succeeded: number;
  failed: number;
  canceled: number;
  [key: string]: number;
};

// A run carries the same resolved session summary as a task/watch, so the same
// DetailSession component renders all three. ``callback_session`` is nested
// rather than prefix-flattened for exactly that reason.
export type HarnessRun = HarnessSessionSummary & {
  id: string;
  request_type: string | null;
  run_type: string | null;
  status: HarnessRunStatus;
  definition_id: string | null;
  // The task/watch this run came from, named. Present even when soft-deleted —
  // a run outlives its definition — with ``definition_deleted`` telling the UI
  // to show the name without a link.
  definition_name: string | null;
  definition_kind: 'task' | 'watch' | null;
  definition_deleted: boolean;
  callback_session: HarnessSessionSummary | null;
  task_id: string | null;
  source_kind: string | null;
  // Polymorphic: a session id when ``source_kind === 'agent'``, otherwise a
  // parent run id, a vault request handle, or a human's name. Render it raw
  // only when ``source_session`` is null — that is the resolved form, and it is
  // non-null exactly when the actor names a session.
  source_actor: string | null;
  // ``source_actor`` narrowed to the session case, so the UI never has to
  // re-derive "is this string an id?" from ``source_kind``.
  source_session_id: string | null;
  source_session: HarnessSessionSummary | null;
  parent_run_id: string | null;
  // Callback (report-back) lineage — serialized by the backend run row but
  // previously unrendered; the run detail surfaces these (Part B).
  callback_session_id: string | null;
  callback_run_id: string | null;
  callback_status: string | null;
  callback_error: string | null;
  agent_name: string | null;
  agent_id: string | null;
  agent_backend: string | null;
  model: string | null;
  reasoning_effort: string | null;
  session_policy: string | null;
  session_key: string | null;
  session_id: string | null;
  post_to: string | null;
  deliver_key: string | null;
  prompt: string | null;
  message: string | null;
  message_payload: Record<string, unknown> | null;
  result_text: string | null;
  result_payload: Record<string, unknown> | null;
  message_ids: string[];
  cancel_requested: boolean;
  cancel_requested_at: string | null;
  pid: number | null;
  exit_code: number | null;
  error: string | null;
  stdout: string | null;
  stderr: string | null;
  created_at: string | null;
  started_at: string | null;
  completed_at: string | null;
  updated_at: string | null;
  metadata: Record<string, unknown>;
  ok: boolean | null;
};

export type HarnessRunsParams = {
  status?: HarnessRunStatus;
  runType?: string;
  // Comma-serialized server-side exclusion. ``run_type`` is an equality match,
  // so "everything except watcher heartbeats" needs its own param — and an
  // exclusion keeps a future run type visible by default instead of dropping it.
  excludeRunType?: string[];
  agentName?: string;
  definitionId?: string;
  query?: string;
  page?: number;
  limit?: number;
};

export type HarnessDefinitionsParams = {
  status?: HarnessDefinitionStatus;
  query?: string;
  page?: number;
  limit?: number;
};

export type HarnessPageResultBase<TCounts> = {
  counts: TCounts;
  total: number;
  page: number;
  limit: number;
  has_more: boolean;
};

export type HarnessTasksResult = HarnessPageResultBase<HarnessDefinitionCounts> & {
  tasks: HarnessTask[];
};

export type HarnessWatchesResult = HarnessPageResultBase<HarnessDefinitionCounts> & {
  watches: HarnessWatch[];
};

export type HarnessRunsResult = HarnessPageResultBase<HarnessRunCounts> & {
  runs: HarnessRun[];
  // Every run_type present in the ledger, so the selector can offer a type the
  // UI has no built-in name for. Optional: an older server omits it.
  run_types?: string[];
};

export type HarnessCountsResult = {
  tasks: HarnessDefinitionCounts;
  watches: HarnessDefinitionCounts;
  runs: HarnessRunCounts;
};

export type HarnessBootstrapParams = {
  tab?: 'tasks' | 'watches' | 'runs';
  status?: HarnessDefinitionStatus | HarnessRunStatus;
  /** Runs tab only — mirrors the dedicated /api/harness/runs filters so the
   * first paint already reflects the active filter instead of flashing an
   * unfiltered page. */
  run_type?: string;
  exclude_run_type?: string[];
  query?: string;
  /** Scope tasks/watches to a bound session (background-work banner deep-link). */
  session_id?: string;
  page?: number;
  limit?: number;
};

export type HarnessBootstrapResult = {
  counts: HarnessCountsResult;
  tab: 'tasks' | 'watches' | 'runs';
  page: HarnessTasksResult | HarnessWatchesResult | HarnessRunsResult;
};

// =============================================================================
// Running agents (live process view)
// =============================================================================

export type RunningAgentState = 'active' | 'idle' | 'orphan';

export type RunningAgent = {
  backend: string;
  state: RunningAgentState;
  base_session_id: string | null;
  composite_key: string | null;
  workdir: string | null;
  pid: number | null;
  pid_shared: boolean;
  native_session_id: string | null;
  model: string | null;
  elapsed_seconds: number | null;
  session_id: string | null;
  title: string | null;
  platform: string | null;
  scope_type: string | null;
  scope_display_name: string | null;
  trigger_source: 'human' | 'agent' | 'scheduled' | 'watch' | 'webhook' | 'callback' | null;
  agent_name: string | null;
  openable_in_chat: boolean;
};

export type RunningAgentCounts = {
  total: number;
  active: number;
  idle: number;
  orphan: number;
  by_backend: Record<string, number>;
};

export type RunningAgentsResult =
  | { ok: true; agents: RunningAgent[]; counts: RunningAgentCounts; unreachable?: false }
  | { ok: false; unreachable: true; agents: RunningAgent[]; counts: Partial<RunningAgentCounts> };

export type LogEntry = {
  timestamp: string;
  level: string;
  logger: string;
  message: string;
  source: string;
};

export type LogSource = {
  key: string;
  filename: string;
  path: string;
  exists: boolean;
  total: number;
  logs?: LogEntry[];
};

export type VersionInfo = {
  current: string;
  latest: string | null;
  has_update: boolean;
  error: string | null;
  build?: {
    kind: 'package' | 'source';
    revision?: string;
    dirty?: boolean;
  };
};

export type UpgradeResult = {
  ok: boolean;
  message: string;
  output: string | null;
  restarting: boolean;
};

export type InstallResult = {
  ok: boolean;
  message: string;
  output: string | null;
  path?: string | null;
  job_id?: string;
  status?: 'running' | 'succeeded' | 'failed' | 'rejected';
  reason?: string | null;
  action_class?: 'operator_only';
  download_error?: DependencyDownloadError | null;
};

export type DependencyDownloadError = {
  kind: 'http' | 'dns' | 'tls' | 'timeout' | 'network' | 'permission' | 'disk' | 'io' | 'unknown';
  message: string;
  url?: string | null;
  host?: string | null;
  http_status?: number;
  retryable: boolean;
  attempts: number;
};

export type DependencyItem = {
  id: string;
  kind: 'tool' | 'runtime' | 'node';
  required: boolean | null;
  installed: boolean | null;
  version: string | null;
  status: 'ready' | 'not_required' | 'missing' | 'upgrade_required' | 'unsupported' | 'error';
  readiness?: 'ready' | 'not_required' | 'not_ready' | 'memory_requirement_unreadable';
  action_class?: 'none' | 'repairable' | 'operator_only';
  reason?: string | null;
  release_state?: 'published' | 'unavailable' | null;
  download_error?: DependencyDownloadError | null;
  inspection_error?: { kind: string; message: string } | null;
};

export type DependenciesResult = { ok: boolean; deps: DependencyItem[] };

// Current Memory contract: docs/MEMORY.md.
// Keys are write-only: GET never returns a usable `api_key`, only `has_api_key`.
export type MemoryRerankProvider = 'deepinfra' | 'vllm' | 'dashscope';

export type MemoryEndpointConfig = {
  base_url: string | null;
  model: string | null;
  // Write-only: the settings GET never returns a usable key, only `has_api_key`.
  // Typed as `null` so no caller can read a saved key back off the response.
  api_key: null;
  has_api_key: boolean;
  provider?: MemoryRerankProvider | null;
};

export type MemoryProcessingConfig = {
  llm: MemoryEndpointConfig;
  embedding: MemoryEndpointConfig;
  rerank?: MemoryEndpointConfig;
  multimodal?: MemoryEndpointConfig;
};

export type MemorySettings = {
  status: 'ok';
  enabled: boolean;
  mode: 'organization' | 'platform' | 'custom';
  cloud_available?: boolean;
  managed?: boolean;
  transition_notice_pending?: boolean;
  capability_paused?: boolean;
  im_attachment_capture_available?: boolean;
  processing: MemoryProcessingConfig;
};

// Omitting a field keeps its current value; an explicit `api_key: null` clears it.
// Required keys can clear only while Memory is disabled; optional endpoints can
// be removed while Memory stays enabled.
export type MemoryEndpointPatch = {
  base_url?: string | null;
  model?: string | null;
  api_key?: string | null;
  provider?: MemoryRerankProvider | null;
};

export type MemorySettingsPatch = {
  enabled?: boolean;
  mode?: 'platform' | 'custom';
  acknowledge_transition?: true;
  processing?: {
    llm?: MemoryEndpointPatch;
    embedding?: MemoryEndpointPatch;
    rerank?: MemoryEndpointPatch;
    multimodal?: MemoryEndpointPatch;
  };
  confirm_loss?: boolean;
};

export type MemoryFailureDiagnostic = {
  side?: 'embedding' | 'llm' | 'rerank' | 'multimodal';
  http_status?: number | null;
  provider_error_code?: string | null;
  message?: string;
};
export type MemoryFailure = {
  status: 'failed';
  error: string;
  diagnostic?: MemoryFailureDiagnostic;
};

export type MemorySettingsResult =
  | (MemorySettings & { runtime?: { ok?: boolean; [key: string]: unknown } })
  | MemoryFailure;

export type MemoryStatus = {
  status: 'ok';
  state: 'disabled' | 'starting' | 'running' | 'degraded' | 'needs_repair';
  reason: string | null;
  source: {
    status: 'available' | 'stale' | 'unknown' | 'unavailable';
    observed_at: string | null;
    reason: string | null;
  };
  health: null | {
    status: string;
    version: string | null;
    capabilities: Record<string, unknown>;
    disabled_features: string[];
  };
  attachment_capture?: {
    status: 'ready' | 'not_configured' | 'unavailable';
  };
};

// A dependency-missing failure from the internal handler omits `status` and
// only carries `error`; normalize both shapes at the call site.
export type MemoryStatusResult = MemoryStatus | MemoryFailure | { error: string };

export type MemoryFailureLogEntry = {
  id: string;
  kind: string;
  state: string;
  operation: string;
  occurred_at: string;
  error_code: string | null;
  attempts: number;
  generation: number;
  request_id: string | null;
};

export type MemoryFailureLog = {
  status: 'ok';
  items: MemoryFailureLogEntry[];
};

export type MemoryFailureLogResult =
  | MemoryFailureLog
  | MemoryFailure
  | { error: string };

export type MemoryMaintenance = {
  status: 'ok';
  data_exists: boolean;
  can_delete_data: boolean;
};

export type MemoryMaintenanceResult = MemoryMaintenance | MemoryFailure | { error: string };

export type MemoryProcessingRecordSummary = {
  status: 'ok';
  runtime: {
    source: MemoryStatus['source'];
    health: MemoryStatus['health'];
  };
  sources: MemoryProcessingRecordSources;
  anomalies: {
    source: MemoryProcessingSourceStatus;
    items: MemoryFailureLogEntry[];
  };
  maintenance: {
    source: MemoryProcessingSourceStatus;
    data_exists: boolean;
    can_delete_data: boolean;
  };
};

export type MemoryProcessingRecordResult =
  | MemoryProcessingRecordSummary
  | MemoryFailure
  | { error: string };

export type MemoryItemKind = 'profile' | 'episode' | 'fact';

export type MemoryProfileExplicitInfo = {
  description: string;
  category: string | null;
  evidence: string | null;
};

export type MemoryProfileTrait = {
  description: string;
  trait: string | null;
  basis: string | null;
  evidence: string | null;
};

export type MemoryProfile = {
  summary: string | null;
  explicit_info: MemoryProfileExplicitInfo[];
  implicit_traits: MemoryProfileTrait[];
  updated_at: string | null;
};

export type MemorySearchWarning = 'memory_search_partial' | 'memory_search_truncated';

export type MemoryItem = {
  kind: MemoryItemKind;
  text: string;
  date: string | null;
  profile?: MemoryProfile;
  project?: string;
  origin?: 'user' | 'agent' | 'both';
};

export type MemoryItemsResult =
  | { status: 'ok'; items: MemoryItem[]; warnings: string[]; profile_warning?: 'empty' | null }
  | MemoryFailure;

export type MemoryRecallResult =
  | {
      status: 'ok';
      items: MemoryItem[];
      warnings: MemorySearchWarning[];
      requested_mode: 'auto' | 'keyword' | 'vector' | 'hybrid' | 'agentic';
      effective_mode: 'keyword' | 'vector' | 'hybrid' | 'agentic';
      source: 'everos';
      current_session_overlay: boolean;
      watermark_ms: number | null;
      freshness: 'unknown';
    }
  | MemoryFailure;

export type MemoryListWarning = 'memory_list_partial' | 'memory_list_truncated';
export type MemoryOrigin = 'user' | 'agent';

export type MemoryListItem = {
  id: string;
  kind: 'episode';
  subject: string;
  summary: string;
  body: string;
  timestamp: string;
  project: string;
  origin?: MemoryOrigin;
};

export type MemoryListResult =
  | {
      status: 'ok';
      items: MemoryListItem[];
      count: number;
      total_count: number | null;
      warnings: MemoryListWarning[];
      page?: number;
      page_size?: number;
      next_cursor?: string | null;
    }
  | MemoryFailure;

export type MemoryProcessingSourceStatus = {
  status: 'available' | 'partial' | 'stale' | 'unknown' | 'unavailable';
  observed_at: string | null;
  reason?: string | null;
};

export type MemoryProcessingRecordSources = {
  memcells: MemoryProcessingSourceStatus;
  runs: MemoryProcessingSourceStatus;
  semantic: MemoryProcessingSourceStatus;
};

export type MemoryProcessingRecordEntry = {
  memcell_id: string;
  project_id: string;
  session_id: string;
  owner_id: string;
  timestamp_ms: number;
  preview: string;
  payload: { status: 'available' | 'partial' | 'unavailable'; reason: string | null; item_count: number };
  runs: { status: 'available' | 'partial' | 'unavailable'; reason: string | null; total: number; statuses: Record<string, number> };
};

export type MemoryProcessingRecordListResult =
  | {
      status: 'ok';
      entries: MemoryProcessingRecordEntry[];
      next_cursor: string | null;
      sections: MemoryProcessingRecordSources;
    }
  | MemoryFailure;

export type MemoryProcessingPayloadItem = {
  id: string;
  timestamp_ms: number;
  sender_id: string;
  content: Array<{ type: 'text'; text: string; omitted_bytes: number }>;
};

export type MemoryProcessingRun = {
  run_id: string;
  strategy: string;
  attempt: number;
  status: string;
  started_at: string | null;
  finished_at: string | null;
  error: string | null;
  event_topic: string;
};

export type MemoryProcessingSemanticItem = {
  kind: 'episode' | 'fact';
  entry_id: string;
  timestamp: string | null;
  content: string;
  subject?: string | null;
  summary?: string | null;
};

export type MemoryProcessingRecordDetailResult =
  | {
      status: 'ok';
      entry: Pick<MemoryProcessingRecordEntry, 'memcell_id' | 'project_id' | 'session_id' | 'owner_id' | 'timestamp_ms'>;
      payload: { status: 'available' | 'partial' | 'unavailable'; reason?: string | null; items: MemoryProcessingPayloadItem[]; omitted_count?: number };
      runs: { status: 'available' | 'partial' | 'unavailable'; reason?: string | null; items: MemoryProcessingRun[]; omitted_count?: number };
      semantic: { status: 'available' | 'partial' | 'unavailable'; reason?: string | null; items: MemoryProcessingSemanticItem[]; omitted_count?: number };
      current_state: {
        status: 'available' | 'partial' | 'unavailable';
        reason?: string | null;
        label?: 'current_unattributed';
        profile?: { status: 'present' | 'missing'; updated_at_ms: number | null };
        indexing?: { status: string; reason?: string; items?: Array<{ md_path: string; status: string; updated_at: string | null; error: string | null }> };
      };
    }
  | MemoryFailure;

export type MemoryWakeResult =
  | { ok: true; state: 'running' }
  | { ok: false; state?: MemoryStatus['state']; error?: string };

export type MemoryDataOperationResult = {
  ok: boolean;
  operation: 'repair' | 'delete_data';
  state?: MemoryStatus['state'];
  result?: 'completed' | 'unchanged' | 'partial' | 'deleted_readiness_failed' | 'failed';
  error?: string;
  data_deleted?: boolean;
  data_remaining?: boolean;
  roots?: Array<{
    path: string;
    existed: boolean;
    deleted: boolean;
    error?: string;
  }>;
};

export type BackendRuntimeInfo = {
  ok: boolean;
  name?: string;
  enabled?: boolean;
  cli_path?: string;
  resolved_path?: string | null;
  installed?: boolean;
  current_version?: string | null;
  latest_version?: string | null;
  has_update?: boolean;
  supports_restart?: boolean;
  process_status?: 'running' | 'stopped' | 'unknown';
  error?: string;
};

export type BackendRestartResult = {
  ok: boolean;
  message: string;
};

export type CodexAuthMode = 'oauth' | 'api_key';

// Mirrors Codex CLI's ``cli_auth_credentials_store`` setting. ``auto`` is
// Codex's documented default and is treated as keyring-preferred — when
// the live store is not ``file`` the on-disk ``auth.json`` may not be
// the source of truth, so the UI must not interpret ``has_api_key=false``
// as "no key configured" in that case.
export type CodexCredentialsStore = 'file' | 'keyring' | 'auto' | (string & {});

export type ActiveAuthMode = 'oauth' | 'api_key' | 'none';

// Identity decoded from the ChatGPT JWT inside ``~/.codex/auth.json``.
// All fields are best-effort — the OAuth bundle may carry partial
// claims, in which case the panel renders only what's present.
export type CodexChatGptAccount = {
  email: string | null;
  name: string | null;
  plan_type: string | null;
  organizations: Array<{
    id: string | null;
    title: string | null;
    role: string | null;
    is_default: boolean;
  }> | null;
};

export type CodexAuthState = {
  ok: boolean;
  auth_mode: CodexAuthMode;
  // What the running Codex CLI is actually using at launch — separate
  // from ``auth_mode`` which is the user's saved intent. Lets the UI
  // surface "Currently active: …" so the two-radio choice is no longer
  // ambiguous about which mode is live.
  active_auth_mode: ActiveAuthMode;
  has_api_key: boolean;
  api_key_length: number;
  // Server-masked preview (e.g. ``sk-proj-•••••••••H8mN``). Used to
  // pre-fill the API Key input so the page reflects the saved state
  // instead of looking empty. Plaintext keys never leave the server.
  api_key_masked: string | null;
  base_url: string | null;
  has_chatgpt_tokens: boolean;
  chatgpt_account?: CodexChatGptAccount | null;
  credentials_store: CodexCredentialsStore;
  file_store_active: boolean;
  // True when Codex is in keyring-preferred mode and disk shows no
  // key/tokens — the live auth may live in the OS keychain (we cannot
  // portably read it). UI must not claim "no key configured" in that
  // case; it should prompt the user to choose a mode (saving will pin
  // file storage so subsequent reads work).
  auth_mode_uncertain?: boolean;
  message?: string;
};

export type CodexAuthPayload = {
  auth_mode: CodexAuthMode;
  api_key?: string | null;
  base_url?: string | null;
};

// Non-fatal warning the server attached to a config-mutation response.
// Used today for "we cleared a custom relay pointer because OAuth tokens
// won't validate against your custom base_url"; new codes can be added
// without touching the type.
export type BackendNotice = {
  code: string;
  provider_id?: string;
  base_url?: string;
  detail?: string;
};

export type CodexAuthSaveResult = CodexAuthState & {
  restart?: BackendRestartResult;
  notices?: BackendNotice[];
};

export type ClaudeAuthMode = 'oauth' | 'api_key';
export type ClaudeCredentialType = 'api_key' | 'auth_token';

// Claude Code reads ``~/.claude/settings.json`` at launch and its ``env``
// block wins over inherited process env. avibe therefore writes
// API-key auth into that file directly; ``v2config`` only appears for
// legacy installs that have not yet been migrated by the next save.
export type ClaudeApiKeySource = 'v2config' | 'settings_json' | null;

export type ClaudeAuthState = {
  ok: boolean;
  auth_mode: ClaudeAuthMode;
  // Live source the CLI is actually inheriting at launch (api_key when
  // V2Config injects ``ANTHROPIC_API_KEY`` and strips OAuth env vars,
  // oauth when Claude Code reports or stores a usable first-party login).
  active_auth_mode: ActiveAuthMode;
  has_api_key: boolean;
  api_key_length: number;
  api_key_masked: string | null;
  api_key_source?: ClaudeApiKeySource;
  // Raw Claude Code account-token signal. This may remain true while Avibe
  // is actively using API-key mode, so UI "signed in" indicators should use
  // active_auth_mode instead.
  has_oauth_credentials: boolean;
  base_url: string | null;
  settings_path: string | null;
  settings_exists: boolean;
  settings_env_has_key: boolean;
  settings_env_key_length: number;
  settings_env_key_var: 'ANTHROPIC_API_KEY' | 'ANTHROPIC_AUTH_TOKEN' | null;
  credential_type: ClaudeCredentialType | null;
  settings_env_base_url: string | null;
  settings_conflict: boolean;
  message?: string;
};

export type ClaudeAuthPayload = {
  auth_mode: ClaudeAuthMode;
  api_key?: string | null;
  credential_type?: ClaudeCredentialType;
  base_url?: string | null;
};

export type ClaudeAuthSaveResult = ClaudeAuthState & {
  restart?: BackendRestartResult;
  partial?: boolean;
  warning?: string;
  detail?: string;
};

// One entry in the OpenCode provider grid. The full catalog is built
// dynamically on the server by merging ``/provider`` + ``/provider/auth``
// + ``/config/providers`` — there is **no** hard-coded list in the UI.
// ``local`` is inferred from the absence of network auth methods (Ollama,
// LM Studio); the page renders its own "Local" badge for those rows.
export type OAuthWebState =
  | 'starting'
  | 'awaiting_code'
  | 'verifying'
  | 'success'
  | 'failed'
  | 'cancelled';

export type OAuthWebStartResult = {
  ok: boolean;
  flow_id?: string;
  backend?: 'claude' | 'codex';
  state?: OAuthWebState;
  url?: string | null;
  device_code?: string | null;
  awaiting_code?: boolean;
  error?: string;
  detail?: string;
};

export type OAuthWebStatus = {
  ok: boolean;
  flow_id?: string;
  backend?: 'claude' | 'codex';
  state?: OAuthWebState;
  url?: string | null;
  device_code?: string | null;
  awaiting_code?: boolean;
  error?: string | null;
};

export type OAuthWebMutationResult = {
  ok: boolean;
  error?: string;
  detail?: string;
  notices?: BackendNotice[];
  restart?: BackendRestartResult;
  // ``partial: true`` rides on ``ok: true`` when the V2Config side of
  // the operation succeeded but the CLI subprocess (``codex logout`` /
  // ``claude auth logout``) reported a non-zero exit. The caller should
  // show a warning rather than a green success — credentials may still
  // be on disk. Pairs with ``warning`` (machine-readable code) and
  // ``detail`` (human-readable excerpt).
  partial?: boolean;
  warning?: string;
};

export type BackendAuthTestResult = {
  ok: boolean;
  duration_ms?: number;
  excerpt?: string;
  exit_code?: number;
  error?: string;
  detail?: string;
};

export type OpencodeProvider = {
  id: string;
  name: string;
  description: string;
  configured: boolean;
  // ``configured`` means usable (including keyless local custom providers).
  // ``has_auth`` means there is an auth.json or legacy opencode.json key entry
  // that the UI can safely offer to remove.
  has_auth?: boolean;
  oauth_available: boolean;
  local: boolean;
  custom?: boolean;
  adapter?: 'openai-compatible' | 'anthropic-compatible' | string | null;
  models: string[];
  model_entries?: {
    id: string;
    user_managed: boolean;
    reasoning_efforts?: string[];
  }[];
  default_model: string | null;
  // Optional ``baseURL`` override persisted in opencode.json. Surfaced so
  // the Settings page can pre-populate the Base URL input with the last
  // saved value instead of starting empty on every reload.
  base_url?: string | null;
  // Server-masked preview of the api-type credential stored in
  // ``~/.local/share/opencode/auth.json`` (e.g. ``sk-proj-•••H8mN``).
  // ``null``/missing when the provider uses OAuth or hasn't been
  // configured yet. Mirrors Claude / Codex's ``api_key_masked`` so the
  // user can see at a glance which providers have a stored key without
  // having to expand each card.
  api_key_masked?: string | null;
  // ``api`` / ``oauth`` / null — the auth type currently stored for the
  // provider. OpenCode's ``auth.json`` only carries ONE entry per
  // provider at a time, so this is also the type that will be used at
  // launch. Lets the UI badge dual-mode providers (e.g. openai) with
  // which source is live, instead of leaving the user guessing.
  active_auth_type?: 'api' | 'oauth' | string | null;
};

export type OpencodeCustomProviderPayload = {
  provider_id: string;
  name: string;
  adapter: 'openai-compatible' | 'anthropic-compatible';
  base_url: string;
  api_key?: string;
};

export type OpencodeProviderListResult = {
  ok: boolean;
  message?: string;
  providers?: OpencodeProvider[];
  default_provider?: string;
  // True when ``opencode.json`` has ``permission: "allow"`` — the
  // setting that lets OpenCode skip the interactive tool-call approval
  // prompt avibe can't reply to. The Settings page hides the
  // "Allow tool calls" affordance when this is already true.
  permission_allowed?: boolean;
};

export type OpencodeOptionsResult = {
  ok: boolean;
  data?: {
    models?: { providers?: unknown[] };
    reasoning_options?: Record<string, { value: string; label: string }[]>;
    [key: string]: unknown;
  };
};

export type OpencodeMutationResult = {
  ok: boolean;
  message?: string;
  default_provider?: string;
  provider_id?: string;
  model_id?: string;
  catalog_refresh?: {
    ok: boolean;
    message?: string;
    catalog?: OpencodeProviderListResult | null;
  };
};

export type WebPushNormalDeliveryOwner = {
  policy?: string;
  disposition?: string | null;
  reason?: string;
};

export type WebPushNormalDeliveryRecent = {
  at?: string;
  message_id?: string | null;
  session_id?: string | null;
  owners?: Record<string, WebPushNormalDeliveryOwner>;
  disposition?: string | null;
};

/** Normal-path authorization evaluation shared by the test/status surface. */
export type WebPushNormalDelivery = {
  user_key?: string;
  policy?: string;
  authorized?: boolean | null;
  disposition?: string | null;
  reason?: string;
  revision_state?: string;
  recent_deliveries?: WebPushNormalDeliveryRecent[];
};

export type WebPushStatus = {
  ok: boolean;
  configured: boolean;
  public_key: string;
  subscription_count: number;
  current_subscription_enabled?: boolean;
  normal_delivery?: WebPushNormalDelivery;
};

export type WebPushStatusPayload = {
  endpoint?: string;
  subscription?: PushSubscriptionJSON;
  device_id?: string;
  device_label?: string;
  previous_endpoints?: string[];
};

export type WebPushSubscriptionResult = {
  ok: boolean;
  subscription: {
    id: string;
    user_key: string;
    endpoint: string;
    enabled: boolean;
    device_id?: string | null;
    user_agent?: string | null;
    device_label?: string | null;
  };
};

export type WebPushTestResult = {
  ok: boolean;
  sent?: number;
  failed?: number;
  error?: string;
  normal_delivery?: WebPushNormalDelivery;
};

// Error thrown by the JSON helpers below when a request fails. Carries the
// HTTP status and the server's machine-readable ``error`` code (when the body
// includes one) so callers can branch on *why* a request failed instead of
// only seeing a human string. The AuthGuard relies on this to tell a policy
// block (e.g. ``remote_access_host_mismatch``) apart from an unconfigured
// instance.
export class ApiError extends Error {
  readonly status: number;
  readonly code: string | null;
  constructor(message: string, status: number, code: string | null) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.code = code;
  }
}

const ApiContext = createContext<ApiContextType | undefined>(undefined);
const CONFIG_CACHE_TTL_MS = 30_000;

export const useApi = () => {
  const context = useContext(ApiContext);
  if (!context) {
    throw new Error('useApi must be used within ApiProvider');
  }
  return context;
};

export const ApiProvider: React.FC<{ children: React.ReactNode }> = ({ children }) => {
  const { showToast } = useToast();
  const { t } = useTranslation();
  const readCacheRef = useRef(new Map<string, { expiresAt: number; promise: Promise<any> }>());
  const configChangedHandlersRef = useRef(new Set<(config: unknown) => void>());
  // Agent Activity is global across chat routes. Keep its writes ordered for the
  // provider lifetime without making unrelated runtime-backed config saves block chat.
  const agentActivityConfigMutationTailRef = useRef<Promise<unknown>>(Promise.resolve());
  const eventSourceRef = useRef<EventSource | null>(null);
  const eventHandlersRef = useRef(new Set<WorkbenchEventHandlers>());
  const eventConnectionRef = useRef<{ sub_id: number; source?: 'browser' | 'controller' } | null>(null);
  // The controller leg of this stream, which is the second thing that can break:
  // the browser socket stays open and heartbeating while the UI server loses
  // `/internal/events`, and `vibe/inbox_bridge.py` resumes the live feed without
  // replaying what the controller published in between. `unknown` is not a third
  // kind of outage -- it is a stream that has not heard from the leg yet, which
  // is what keeps its first "connected" report from reading as a recovery.
  const eventControllerLegRef = useRef<WorkbenchControllerLegState>('unknown');
  // When the active stream last proved it was alive, and the cadence the server
  // said it would prove it at. Null whenever there is no stream to speak for --
  // including a stream that has connected but not yet been heard from, because a
  // heartbeat is the only thing that proves continuity and nothing else may
  // stand in for one. A server too old to send them therefore never reads as
  // proven, which is the pre-heartbeat behavior this optimization replaces.
  const eventHeartbeatAtRef = useRef<number | null>(null);
  const eventHeartbeatIntervalRef = useRef(WORKBENCH_EVENT_HEARTBEAT_FALLBACK_MS);
  // When the deadline the current stream is being held to started running, or
  // null when this server never promised a cadence and so cannot be held to one.
  // Deliberately separate from the stamp above: a promise is what makes a
  // deadline enforceable, and only a heartbeat is proof the stream is carrying
  // events. Collapsing them either lets a handshake vouch for a stream or leaves
  // a stream that dies before its first heartbeat with no deadline at all.
  const eventHeartbeatClockAtRef = useRef<number | null>(null);
  // Fires when the next heartbeat is overdue. A heartbeat is a continuous clock,
  // so whoever trusts it has to keep watching it: sampling only at the moment a
  // page returns would leave a stream that dies one second later unquestioned
  // until the next return, which may never come while the tab stays open.
  const eventHeartbeatWatchdogRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const eventConnectionStateRef = useRef<WorkbenchEventConnectionState>('reconnecting');
  const eventReconnectLoopRef = useRef<WorkbenchEventReconnectLoop | null>(null);
  const resumeWorkbenchEventsRef = useRef<PageReactivationListener>(() => {});
  const syncSessionDraftsRef = useRef<() => void>(() => {});
  const stopWorkbenchEventsRef = useRef<() => void>(() => {});
  const sessionArchivedHandlersRef = useRef(new Set<(sessionId: string) => void>());
  const sessionDraftPersistence = useMemo(() => new SessionDraftPersistence(), []);

  const convergeSessionArchived = (sessionId: string) => {
    sessionDraftPersistence.clearSession(sessionId);
    for (const handler of Array.from(sessionArchivedHandlersRef.current)) {
      try {
        handler(sessionId);
      } catch (err) {
        console.error('[API] session-archived subscriber failed', err);
      }
    }
  };

  const handleApiError = async (
    res: Response,
    path: string,
    { expectedCodes }: { expectedCodes?: readonly string[] } = {},
  ) => {
    let errorMessage = `Request failed: ${path} (${res.status})`;
    let errorCode: string | null = null;

    try {
      const data = await res.json();
      const parsed = selectApiErrorFields(data, errorMessage);
      if (parsed) {
        // Localize by code, falling back to the server-provided message so we never render a
        // key like ``errors.[object Object]``.
        errorCode = parsed.code;
        errorMessage = parsed.code
          ? t(`errors.${parsed.code}`, { defaultValue: parsed.fallback })
          : parsed.fallback;
      }
    } catch {
      // Response is not JSON, use status text
      errorMessage = `${path}: ${res.statusText || 'Unknown error'} (${res.status})`;
    }

    // A code the caller declared EXPECTED is an answer, not a failure: it still
    // rejects, so no caller can mistake an error body for data, but it is neither
    // announced to the user nor logged as an error. Declared per call site, applied
    // here, so "expected" cannot mean two different things in two helpers.
    if (!(errorCode !== null && expectedCodes?.includes(errorCode))) {
      // Log error details to console
      console.error(`[API Error] ${path}`, {
        status: res.status,
        statusText: res.statusText,
        error: errorMessage,
      });

      // Show toast to user
      showToast(errorMessage, 'error');
    }

    // Archive is TERMINAL, so this particular refusal is not a failure to retry
    // but a state change the client missed (a backgrounded/offline tab can drop
    // the archive SSE). Announce it once, from the one place every JSON helper
    // funnels its errors through, so any subscriber converges no matter WHICH
    // session-scoped write tripped it. Best-effort and non-throwing: a subscriber
    // must never change whether/what this handler throws.
    const archivedSessionId = archivedConflictSessionId(errorCode, path);
    if (archivedSessionId) {
      convergeSessionArchived(archivedSessionId);
    }

    throw new ApiError(errorMessage, res.status, errorCode);
  };

  const onSessionArchived = (handler: (sessionId: string) => void) => {
    sessionArchivedHandlersRef.current.add(handler);
    return () => {
      sessionArchivedHandlersRef.current.delete(handler);
    };
  };

  const onConfigChanged = (handler: (config: unknown) => void) => {
    configChangedHandlersRef.current.add(handler);
    return () => {
      configChangedHandlersRef.current.delete(handler);
    };
  };

  const convergeConfig = (config: unknown) => {
    for (const handler of Array.from(configChangedHandlersRef.current)) {
      try {
        handler(config);
      } catch (err) {
        console.error('[API] config-changed subscriber failed', err);
      }
    }
  };

  const getJson = async (
    path: string,
    { handleError = true, expectedCodes }: { handleError?: boolean; expectedCodes?: readonly string[] } = {},
  ) => {
    const res = await apiFetch(path);
    if (!res.ok && handleError) {
      await handleApiError(res, path, { expectedCodes });
    }
    return res.json();
  };

  // Absence is data for a GET of ONE session's Show Page, never an incident: the share
  // panel opens on sessions that have no page yet and renders that as an empty link.
  // The property is owned here instead of declared per call site because the panel fires
  // several of these reads at once — one of them forgetting is a toast the user sees for
  // the normal case, and the reader cannot tell which of the parallel reads produced it.
  // Mutations stay off this path deliberately: pinning or re-skinning a page that does
  // not exist IS a fault worth announcing. So is the POST-shaped access-settings read,
  // which only mounts once an access read has already proven the page exists, so an
  // absent page there means it vanished mid-session rather than never existed.
  const readShowPageJson = (path: string) => getJson(path, { expectedCodes: ['show_page_not_found'] });

  const getCachedJson = (path: string, ttlMs = 1500, opts?: { handleError?: boolean }) => {
    // Best-effort callers (handleError: false) bypass the shared read cache so a
    // silently-failing request can't hand its suppressed-error promise to a
    // toast-enabled caller hitting the same path.
    if (opts?.handleError === false) {
      return getJson(path, opts);
    }
    const now = Date.now();
    const cached = readCacheRef.current.get(path);
    if (cached && cached.expiresAt > now) {
      return cached.promise;
    }

    const promise = getJson(path).catch((err) => {
      readCacheRef.current.delete(path);
      throw err;
    });
    readCacheRef.current.set(path, { expiresAt: now + ttlMs, promise });
    return promise;
  };

  const clearReadCache = () => {
    readCacheRef.current.clear();
  };

  const clearReadCacheMatching = (predicate: (path: string) => boolean) => {
    for (const path of readCacheRef.current.keys()) {
      if (predicate(path)) {
        readCacheRef.current.delete(path);
      }
    }
  };

  const clearSessionReadCache = (sessionId: string) => {
    const encoded = encodeURIComponent(sessionId);
    const sessionPrefix = `/api/sessions/${encoded}`;
    clearReadCacheMatching((path) =>
      path === sessionPrefix ||
      path.startsWith(`${sessionPrefix}/`) ||
      path.startsWith('/api/sessions?') ||
      path === '/api/sessions' ||
      path.startsWith('/api/workbench/projects-bootstrap') ||
      path.startsWith('/api/inbox?') ||
      path === '/api/inbox',
    );
  };

  const dispatchToWorkbenchHandlers = (dispatch: (handlers: WorkbenchEventHandlers) => void) => {
    for (const handlers of Array.from(eventHandlersRef.current)) {
      dispatch(handlers);
    }
  };

  // Feed a locally-synthesized session.activity event through the SAME handler
  // set + read-cache invalidation the SSE 'session.activity' listener uses, so a
  // client-originated change (e.g. a visibility PATCH) reconciles every workbench
  // cache via its own reducer even when the SSE stream is down. Idempotent with a
  // later real SSE event carrying the same change.
  const emitLocalSessionActivity = (data: Parameters<NonNullable<WorkbenchEventHandlers['onSessionActivity']>>[0]) => {
    if (data.session_id) clearSessionReadCache(data.session_id);
    dispatchToWorkbenchHandlers((handlers) => handlers.onSessionActivity?.(data));
  };

  const setWorkbenchEventConnectionState = (state: WorkbenchEventConnectionState) => {
    if (eventConnectionStateRef.current === state) return;
    eventConnectionStateRef.current = state;
    dispatchToWorkbenchHandlers((handlers) => handlers.onConnectionState?.(state));
  };

  const parseWorkbenchEnvelope = <T,>(raw: string): WorkbenchEventEnvelope<T> | null => {
    try {
      return JSON.parse(raw) as WorkbenchEventEnvelope<T>;
    } catch (err) {
      console.error('[workbench-events] parse failed', err, raw);
      return null;
    }
  };

  const clearWorkbenchHeartbeatWatchdog = () => {
    if (eventHeartbeatWatchdogRef.current === null) return;
    clearTimeout(eventHeartbeatWatchdogRef.current);
    eventHeartbeatWatchdogRef.current = null;
  };

  const closeActiveWorkbenchEventSource = () => {
    const source = eventSourceRef.current;
    eventSourceRef.current = null;
    source?.close();
    eventConnectionRef.current = null;
    eventControllerLegRef.current = 'unknown';
    // The stamp and the cadence both speak for one socket only; a later stream
    // must earn its own, including the right to be watched on a timer.
    eventHeartbeatAtRef.current = null;
    eventHeartbeatClockAtRef.current = null;
    clearWorkbenchHeartbeatWatchdog();
  };

  /**
   * The stream's own handshake, or null while events cannot reach handlers. A
   * controller-sourced stream is itself the controller leg, so it is down when
   * that leg reports down.
   */
  const workbenchEventHandshake = () => {
    const connection = eventConnectionRef.current;
    if (!connection) return null;
    if (connection.source === 'controller' && eventControllerLegRef.current !== 'connected') return null;
    return connection;
  };

  /**
   * Reconnecting this socket would close no gap, because it is carrying
   * everything it can carry right now.
   *
   * Scoped to this socket on purpose: a stream reaches the browser over two legs
   * that fail independently, and each one is judged by whoever can see it. This
   * is the browser leg. Reopening it cannot repair the controller leg behind the
   * UI server -- the replacement would inherit the same severed bridge -- so the
   * controller leg is not a term here; it announces its own recovery instead, in
   * the `workbench.events.bridge.status` listener below.
   *
   * That makes this the answer to one question only: whether recycling this
   * socket would close anything. It is not a verdict on the stream, and asking
   * it as one is how a controller-leg outage came to read as a gap-free resume.
   * `streamCoveredGap` is where every leg is accounted for.
   *
   * All three terms are needed for this leg. `readyState` is the browser's own
   * transport verdict, which it revises on network changes and HTTP/2 ping
   * timeouts. The handshake says a stream that is open can also reach handlers.
   * Neither survives suspension: a backgrounded tab can have its socket dropped
   * and be resumed with the connection still reported `OPEN` and no `error` ever
   * delivered, so only a recent heartbeat distinguishes a quiet stream from that
   * zombie.
   */
  const isWorkbenchBrowserLegLive = () =>
    eventSourceRef.current?.readyState === EventSource.OPEN &&
    workbenchEventHandshake() !== null &&
    isWorkbenchHeartbeatFresh(
      eventHeartbeatAtRef.current,
      eventHeartbeatIntervalRef.current,
      Date.now(),
    );

  /**
   * This stream is over: drop it, say so, and let the loop schedule the next
   * attempt. One owner for the transition, because a stream that dies silently
   * has to end up in exactly the same state as one that reports `error` --
   * consumers recover through the reconnect's `connected`, so a path that
   * skipped any of these steps would strand them.
   */
  const failWorkbenchEventStream = () => {
    closeActiveWorkbenchEventSource();
    setWorkbenchEventConnectionState('reconnecting');
    dispatchToWorkbenchHandlers((handlers) => handlers.onEventBridgeStatus?.({ connected: false }));
    getWorkbenchEventReconnectLoop().failed();
  };

  /**
   * Watch for the heartbeat the current stream owes us. A stream that stops
   * proving itself is dead whether or not the browser ever says so, and the
   * only way a consumer learns that is if someone is still looking.
   */
  const armWorkbenchHeartbeatWatchdog = () => {
    clearWorkbenchHeartbeatWatchdog();
    // Only a stream whose server promised a cadence owes a heartbeat. A deadline
    // is a claim about a cadence, so a server that never declared one cannot be
    // held to it: against an older server -- a rollback under a tab that stayed
    // open -- this would otherwise close a healthy stream every stale window
    // forever, charging every consumer a catch-up each round. A modern server
    // makes that promise in its handshake, which is what puts a stream that dies
    // before its first heartbeat on a deadline too.
    const clockAt = eventHeartbeatClockAtRef.current;
    if (clockAt === null) return;
    const staleAt = clockAt + workbenchEventStaleAfterMs(eventHeartbeatIntervalRef.current);
    eventHeartbeatWatchdogRef.current = setTimeout(() => {
      eventHeartbeatWatchdogRef.current = null;
      // A hidden page cannot hold a stream open anyway, and its timers are
      // throttled or frozen: the reactivation edge is the honest check there.
      if (document.visibilityState !== 'visible') return;
      // A heartbeat may have landed since this timer was set, in which case the
      // stream is proving itself and only the deadline moved.
      if (isWorkbenchBrowserLegLive()) {
        armWorkbenchHeartbeatWatchdog();
        return;
      }
      failWorkbenchEventStream();
    }, Math.max(0, staleAt - Date.now()));
  };

  /**
   * Start this stream's clock from its server's promise. Called from the
   * handshake, so a stream that opens and never sends a heartbeat still has a
   * deadline to miss -- the case a stamp-only clock cannot express, because
   * there is nothing to stamp.
   *
   * Sticky per stream: only a frame that carries a cadence is allowed to speak
   * for one, so a controller-relayed handshake (no cadence of its own) never
   * clears a clock the UI server's own handshake started.
   */
  const declareWorkbenchHeartbeatCadence = (intervalMs: number) => {
    eventHeartbeatIntervalRef.current = intervalMs;
    eventHeartbeatClockAtRef.current = Date.now();
    armWorkbenchHeartbeatWatchdog();
  };

  /**
   * Record proof of life and keep watching for the next one. Stamping and
   * watching are one action on purpose: a stamp nobody watches expires
   * unnoticed, and a watchdog armed off a stale stamp fires against the wrong
   * deadline.
   *
   * A heartbeat also restarts the clock, and does so even from a server that
   * never declared a cadence: whatever the deadline was measured from, the
   * newest proof is now the honest starting point.
   */
  const stampWorkbenchHeartbeat = (intervalMs?: number) => {
    const now = Date.now();
    eventHeartbeatAtRef.current = now;
    eventHeartbeatClockAtRef.current = now;
    if (intervalMs !== undefined) eventHeartbeatIntervalRef.current = intervalMs;
    armWorkbenchHeartbeatWatchdog();
  };

  function reconnectWorkbenchEventSource(): void {
    if (eventHandlersRef.current.size === 0) return;
    closeActiveWorkbenchEventSource();
    openWorkbenchEventSource();
  }

  function getWorkbenchEventReconnectLoop(): WorkbenchEventReconnectLoop {
    if (!eventReconnectLoopRef.current) {
      eventReconnectLoopRef.current = new WorkbenchEventReconnectLoop({
        reconnect: reconnectWorkbenchEventSource,
        isVisible: () => document.visibilityState === 'visible',
        isBrowserLegLive: isWorkbenchBrowserLegLive,
      });
    }
    return eventReconnectLoopRef.current;
  }

  function openWorkbenchEventSource(): void {
    if (
      eventSourceRef.current ||
      eventHandlersRef.current.size === 0 ||
      document.visibilityState !== 'visible'
    ) return;

    let source: EventSource;
    try {
      source = new EventSource('/api/events');
    } catch (err) {
      setWorkbenchEventConnectionState('reconnecting');
      getWorkbenchEventReconnectLoop().failed();
      const event = err instanceof Event ? err : new Event('error');
      dispatchToWorkbenchHandlers((handlers) => handlers.onError?.(event));
      return;
    }

    eventSourceRef.current = source;
    setWorkbenchEventConnectionState('reconnecting');
    getWorkbenchEventReconnectLoop().attemptStarted();
    source.addEventListener('connected', (e: MessageEvent) => {
      if (eventSourceRef.current !== source) return;
      // Deliberately no stamp here. This frame proves the stream is alive now,
      // which is not the question: the question is whether it will still be
      // proving that in a minute, and only a heartbeat answers it. Seeding from
      // the handshake would hand a stream up to one stale window of unearned
      // trust -- enough for a suspended tab to return, be believed, and skip the
      // catch-up for a gap it did have.
      //
      // What it may do is start the clock: the frame's `interval_ms` is the
      // server declaring the cadence it owes, which is a promise, not proof.
      // That is what puts a stream that opens and then goes silent on a
      // deadline, while a server too old to declare one is still never
      // watchdogged and still never believed without a heartbeat.
      try {
        const parsed = JSON.parse(e.data) as {
          sub_id?: number;
          interval_ms?: unknown;
          type?: string;
          data?: unknown;
        };
        const sourceKind = typeof parsed.sub_id === 'number' ? 'browser' : 'controller';
        eventConnectionRef.current = {
          sub_id: typeof parsed.sub_id === 'number' ? parsed.sub_id : -1,
          source: sourceKind,
        };
        const declaredIntervalMs = declaredWorkbenchHeartbeatInterval(parsed.interval_ms);
        if (declaredIntervalMs !== undefined) declareWorkbenchHeartbeatCadence(declaredIntervalMs);
        if (sourceKind === 'controller') {
          eventControllerLegRef.current = 'connected';
          setWorkbenchEventConnectionState('connected');
          dispatchToWorkbenchHandlers((handlers) => handlers.onEventBridgeStatus?.({ connected: true }));
        }
      } catch (err) {
        console.error('[workbench-events] connected parse failed', err, e.data);
        eventConnectionRef.current = null;
      }
      if (eventConnectionRef.current) {
        getWorkbenchEventReconnectLoop().streamOpened();
        dispatchToWorkbenchHandlers((handlers) => handlers.onConnected?.());
      }
    });
    // The server's proof of life. It carries no news, so nothing is dispatched
    // to handlers -- the arrival itself is the whole payload, and the declared
    // cadence lets the staleness window be sized by the side that sets it.
    source.addEventListener('heartbeat', (e: MessageEvent) => {
      if (eventSourceRef.current !== source) return;
      let intervalMs: number | undefined;
      try {
        const payload = JSON.parse(e.data) as { interval_ms?: unknown };
        intervalMs = parseWorkbenchHeartbeatInterval(payload.interval_ms);
      } catch {
        // The frame arrived, which is what matters; keep the current cadence.
      }
      // The only place a stream is ever stamped, which is what lets a null stamp
      // mean "nothing has proved this stream yet" even on a stream already
      // running against a declared deadline.
      stampWorkbenchHeartbeat(intervalMs);
    });
    source.addEventListener('authorization.changed', (e: MessageEvent) => {
      const envelope = parseWorkbenchEnvelope<{
        project_ids?: string[];
        resource_kinds?: string[];
        instance_authorization_revision?: number;
      }>(e.data);
      if (!envelope) return;
      clearReadCacheMatching(isAuthorizationSensitiveReadPath);
      dispatchToWorkbenchHandlers((handlers) => {
        handlers.onAny?.(envelope);
        handlers.onAuthorizationChanged?.(envelope.data);
      });
    });
    source.addEventListener('remote.authorization', (e: MessageEvent) => {
      try {
        const payload = JSON.parse(e.data) as { state?: RemoteAuthorizationState };
        if (payload.state) reportRemoteAuthorizationState(payload.state);
      } catch (err) {
        console.error('[workbench-events] remote authorization parse failed', err, e.data);
      }
    });
    source.addEventListener('message.new', (e: MessageEvent) => {
      const envelope = parseWorkbenchEnvelope<WorkbenchMessage>(e.data);
      if (!envelope) return;
      if (envelope.data.session_id) {
        clearSessionReadCache(envelope.data.session_id);
      } else {
        clearReadCacheMatching((path) => path.startsWith('/api/inbox') || path.startsWith('/api/sessions'));
      }
      dispatchToWorkbenchHandlers((handlers) => {
        handlers.onAny?.(envelope);
        handlers.onMessageNew?.(envelope.data);
      });
    });
    source.addEventListener('session.activity', (e: MessageEvent) => {
      const envelope = parseWorkbenchEnvelope<any>(e.data);
      if (!envelope) return;
      if (envelope.data.session_id) {
        clearSessionReadCache(envelope.data.session_id);
        if (envelope.data.event === 'archived') {
          sessionDraftPersistence.clearSession(envelope.data.session_id);
        }
      } else {
        clearReadCacheMatching((path) => path.startsWith('/api/inbox') || path.startsWith('/api/sessions'));
      }
      dispatchToWorkbenchHandlers((handlers) => {
        handlers.onAny?.(envelope);
        handlers.onSessionActivity?.(envelope.data);
      });
    });
    source.addEventListener('inbox.unread.changed', (e: MessageEvent) => {
      const envelope = parseWorkbenchEnvelope<any>(e.data);
      if (!envelope) return;
      clearReadCacheMatching((path) => path.startsWith('/api/inbox') || path.startsWith('/api/sessions'));
      dispatchToWorkbenchHandlers((handlers) => {
        handlers.onAny?.(envelope);
        handlers.onInboxUnreadChanged?.(envelope.data);
      });
    });
    source.addEventListener('inbox.session.updated', (e: MessageEvent) => {
      const envelope = parseWorkbenchEnvelope<InboxSession>(e.data);
      if (!envelope) return;
      clearSessionReadCache(envelope.data.session_id);
      dispatchToWorkbenchHandlers((handlers) => {
        handlers.onAny?.(envelope);
        handlers.onInboxSessionUpdated?.(envelope.data);
      });
    });
    source.addEventListener('turn.start', (e: MessageEvent) => {
      const envelope = parseWorkbenchEnvelope<{ session_id: string }>(e.data);
      if (!envelope) return;
      clearSessionReadCache(envelope.data.session_id);
      dispatchToWorkbenchHandlers((handlers) => {
        handlers.onAny?.(envelope);
        handlers.onTurnStart?.(envelope.data);
      });
    });
    source.addEventListener('turn.end', (e: MessageEvent) => {
      const envelope = parseWorkbenchEnvelope<{ session_id: string }>(e.data);
      if (!envelope) return;
      clearSessionReadCache(envelope.data.session_id);
      dispatchToWorkbenchHandlers((handlers) => {
        handlers.onAny?.(envelope);
        handlers.onTurnEnd?.(envelope.data);
      });
    });
    source.addEventListener('session.status', (e: MessageEvent) => {
      const envelope = parseWorkbenchEnvelope<{
        session_id: string;
        agent_status: 'idle' | 'running' | 'failed';
      }>(e.data);
      if (!envelope) return;
      clearSessionReadCache(envelope.data.session_id);
      dispatchToWorkbenchHandlers((handlers) => {
        handlers.onAny?.(envelope);
        handlers.onSessionStatus?.(envelope.data);
      });
    });
    source.addEventListener('queue.updated', (e: MessageEvent) => {
      const envelope = parseWorkbenchEnvelope<{ session_id: string }>(e.data);
      if (!envelope) return;
      clearSessionReadCache(envelope.data.session_id);
      dispatchToWorkbenchHandlers((handlers) => {
        handlers.onAny?.(envelope);
        handlers.onQueueUpdated?.(envelope.data);
      });
    });
    source.addEventListener('runs.updated', (e: MessageEvent) => {
      const envelope = parseWorkbenchEnvelope<{
        run_id: string;
        status: HarnessRunStatus;
        run_type?: string;
        session_id?: string;
        definition_id?: string;
        updated_at?: string;
        cancel_requested?: boolean;
      }>(e.data);
      if (!envelope) return;
      clearReadCacheMatching((path) => path.startsWith('/api/harness'));
      dispatchToWorkbenchHandlers((handlers) => {
        handlers.onAny?.(envelope);
        handlers.onRunsUpdated?.(envelope.data);
      });
    });
    source.addEventListener('vaults.updated', (e: MessageEvent) => {
      const envelope = parseWorkbenchEnvelope<{
        scope: string;
        request_id?: string;
        request_status?: string;
        grant_id?: string;
        grant_status?: string;
        secret_name?: string;
      }>(e.data);
      if (!envelope) return;
      clearReadCacheMatching((path) => path.startsWith('/api/vault/'));
      dispatchToWorkbenchHandlers((handlers) => {
        handlers.onAny?.(envelope);
        handlers.onVaultsUpdated?.(envelope.data);
      });
    });
    source.addEventListener('remote_access.quality.changed', (e: MessageEvent) => {
      const envelope = parseWorkbenchEnvelope<TunnelQualitySnapshot>(e.data);
      if (!envelope) return;
      dispatchToWorkbenchHandlers((handlers) => {
        handlers.onAny?.(envelope);
        handlers.onRemoteAccessQuality?.(envelope.data);
      });
    });
    source.addEventListener('workbench.events.bridge.status', (e: MessageEvent) => {
      if (eventSourceRef.current !== source) return;
      const envelope = parseWorkbenchEnvelope<{ connected: boolean }>(e.data);
      if (!envelope) return;
      getWorkbenchEventReconnectLoop().streamOpened();
      const previousLeg = eventControllerLegRef.current;
      eventControllerLegRef.current = envelope.data.connected ? 'connected' : 'disconnected';
      setWorkbenchEventConnectionState(envelope.data.connected ? 'connected' : 'reconnecting');
      dispatchToWorkbenchHandlers((handlers) => {
        handlers.onAny?.(envelope);
        handlers.onEventBridgeStatus?.(envelope.data);
      });
      // A leg that was down and is now up is a gap that just ended. The browser
      // socket stayed open across it, so no reconnect will announce this one --
      // but the meaning is identical, so it arrives through the same signal, and
      // consumers need no second concept to handle it. Only from `disconnected`:
      // a new stream's opening report is the leg's state, not a recovery, and
      // treating it as one would charge every connect a duplicate catch-up.
      if (previousLeg === 'disconnected' && envelope.data.connected) {
        dispatchToWorkbenchHandlers((handlers) => handlers.onConnected?.());
      }
    });
    source.onerror = (err) => {
      if (eventSourceRef.current !== source) return;
      failWorkbenchEventStream();
      // EventSource does not expose a failed response's status or JSON body.
      // Probe through apiFetch, then inspect the successful /api/session form;
      // both 401s and the 200 refresh payload enter the shared login recovery.
      void apiFetch('/api/session', { cache: 'no-store' })
        .then(recoverRemoteAuthFromSessionProbe)
        .catch(() => undefined);
      dispatchToWorkbenchHandlers((handlers) => handlers.onError?.(err));
    };
  }

  const ensureWorkbenchEventSource = () => {
    getWorkbenchEventReconnectLoop();
    openWorkbenchEventSource();
  };

  const stopWorkbenchEventSource = () => {
    closeActiveWorkbenchEventSource();
    eventReconnectLoopRef.current?.stop();
    eventReconnectLoopRef.current = null;
    setWorkbenchEventConnectionState('reconnecting');
  };
  stopWorkbenchEventsRef.current = stopWorkbenchEventSource;

  /**
   * The page or the network came back. Catch the transport up; the stream then
   * tells consumers whether anything was missed, by reconnecting or not.
   *
   * One owner, deliberately. Every consumer used to subscribe to the raw
   * reactivation edge and refetch unconditionally, which is why returning to a
   * tab cost a burst of reads that a healthy stream had already delivered. Now
   * only a stream that cannot prove it survived the gap costs a catch-up, and
   * consumers hear about it through the same `connected` signal an ordinary
   * mid-session reconnect already used.
   *
   * `awaySince` is when the gap being recovered from opened, or null for one
   * nothing can date -- a network return, or a page back from an away period
   * the sampler never saw begin.
   */
  const wakeWorkbenchEvents: PageReactivationListener = (awaySince) => {
    if (eventHandlersRef.current.size === 0 || document.visibilityState !== 'visible') return;
    // Read before waking, because waking is what changes the answer.
    //
    // Two questions, asked separately because they have different subjects. The
    // transport one -- is this socket worth keeping -- is about the browser leg,
    // and is the reconnect loop's. This one is about the whole path across an
    // interval, so it is asked of every leg, by the one function that knows what
    // the legs are; the browser leg's own verdict goes in as a term rather than
    // standing in for the answer.
    const survivedTheGap = streamCoveredGap({
      browserLegLive: isWorkbenchBrowserLegLive(),
      lastHeartbeatAt: eventHeartbeatAtRef.current,
      controllerLeg: eventControllerLegRef.current,
      awaySince,
      intervalMs: eventHeartbeatIntervalRef.current,
      now: Date.now(),
    });
    // The indicator belongs to whoever opens a stream: openWorkbenchEventSource
    // marks it reconnecting on every attempt. Announcing it here instead made a
    // wake that keeps a live stream flash "reconnecting" over a healthy one.
    getWorkbenchEventReconnectLoop().wake();
    // Timers are not trustworthy across a hidden period -- throttled, frozen,
    // or fired while hidden and declined -- so re-establish the watch on a
    // stream that was kept rather than reasoning about which of those happened.
    // Idempotent: it re-arms off the surviving clock, and declines for a stream
    // that was recycled or never declared a cadence.
    armWorkbenchHeartbeatWatchdog();
    // This edge owns its own catch-up. Deferring it to the replacement stream's
    // handshake would make recovery depend on the thing being recovered: a
    // server that is down, an offline network, or a backoff wait leaves the
    // reconnect pending for as long as it takes, and until then a returning page
    // shows whatever it had before it was hidden with nothing on the way. Paying
    // it here is what the unconditional refetch did before this change, so a
    // reactivation onto a broken stream costs no more than it used to; the
    // replacement's later `connected` is a second catch-up in exactly the case
    // that already paid for two.
    if (!survivedTheGap) {
      dispatchToWorkbenchHandlers((handlers) => handlers.onConnected?.());
    }
  };
  resumeWorkbenchEventsRef.current = wakeWorkbenchEvents;

  useEffect(() => {
    const wakeIfVisible: PageReactivationListener = (awaySince) => {
      if (document.visibilityState !== 'visible') return;
      resumeWorkbenchEventsRef.current(awaySince);
      syncSessionDraftsRef.current();
    };
    // Regaining the network is its own gap, independent of the page coming back,
    // and an undated one: nothing here watched the connection drop, so no
    // heartbeat can be placed inside it.
    const wakeFromNetwork = () => wakeIfVisible(null);
    const stopReactivation = onPageReactivated(wakeIfVisible);
    window.addEventListener('online', wakeFromNetwork);
    if (document.visibilityState === 'visible') syncSessionDraftsRef.current();
    return () => {
      stopReactivation();
      window.removeEventListener('online', wakeFromNetwork);
      stopWorkbenchEventsRef.current();
    };
  }, []);

  const requestJson = async (
    path: string,
    init: RequestInit,
    errorPath = path,
    {
      clearCache = true,
      handleError = true,
      expectedCodes,
    }: {
      clearCache?: boolean;
      handleError?: boolean;
      expectedCodes?: readonly string[];
    } = {},
  ) => {
    const res = await apiFetch(path, init);
    if (!res.ok && handleError) {
      await handleApiError(res, errorPath, { expectedCodes });
    }
    const payloadJson = await res.json().catch(() => ({}));
    if (res.ok && clearCache) {
      clearReadCache();
    }
    return { res, payloadJson };
  };

  const readSessionDraftServer = async (
    sessionId: string,
    timeoutMs = SESSION_DRAFT_RECONCILE_TIMEOUT_MS,
  ): Promise<SessionDraftServerState | null> => {
    const controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), timeoutMs);
    try {
      const res = await apiFetch(
        `/api/sessions/${encodeURIComponent(sessionId)}/draft`,
        { signal: controller.signal },
      );
      if (!res.ok) return null;
      return sessionDraftServerState(await res.json().catch(() => null));
    } catch {
      return null;
    } finally {
      window.clearTimeout(timeout);
    }
  };

  const writeSessionDraft = async (
    sessionId: string,
    draft: SessionDraftWrite,
  ): Promise<SessionDraftSaveResult> => {
    const controller = new AbortController();
    let timedOut = false;
    const timeout = window.setTimeout(() => {
      timedOut = true;
      controller.abort();
    }, SESSION_DRAFT_WRITE_TIMEOUT_MS);
    try {
      const { res, payloadJson } = await requestJson(
        `/api/sessions/${encodeURIComponent(sessionId)}/draft`,
        {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            text: draft.text,
            expected_updated_at: draft.expectedUpdatedAt,
          }),
          signal: controller.signal,
        },
        `/api/sessions/${sessionId}/draft`,
        { handleError: false },
      );
      const hasServerDraft = payloadJson?.draft && typeof payloadJson.draft === 'object';
      const server = sessionDraftServerState(payloadJson?.draft);
      if (res.status === 409 && payloadJson?.code === 'draft_conflict') {
        return { ok: false, conflict: true, server };
      }
      return res.ok
        ? { ok: true, ...(hasServerDraft ? { server } : {}) }
        : { ok: false };
    } catch (error) {
      if (!timedOut) throw error;
      // The request may have committed before its response stalled. Reconcile
      // once before releasing queued successors so they inherit the actual
      // cloud revision instead of manufacturing a conflict from uncertainty.
      const server = await readSessionDraftServer(sessionId);
      if (!server) return { ok: false };
      if (server.text === draft.text) return { ok: true, server };
      return server.updatedAt !== draft.expectedUpdatedAt
        ? { ok: false, conflict: true, server }
        : {
            ok: false,
            server,
            // The abort only stops waiting for the response; the synchronous
            // server transaction may still commit. If the queued successor
            // conflicts specifically with this text, rebase and retry once.
            retryConflictIfServerText: draft.text,
          };
    } finally {
      window.clearTimeout(timeout);
    }
  };
  const rebaseAndRetrySessionDraft = async (
    sessionId: string,
    server: SessionDraftServerState,
  ): Promise<void> => {
    sessionDraftPersistence.rebase(sessionId, server);
    await sessionDraftPersistence.retry(
      sessionId,
      (draft) => writeSessionDraft(sessionId, draft),
    );
  };
  syncSessionDraftsRef.current = () => {
    void sessionDraftPersistence.retryAll(writeSessionDraft);
  };

  const postJson = async (
    path: string,
    payload: any,
    opts?: { handleError?: boolean; expectedCodes?: readonly string[] },
  ) => {
    const { payloadJson } = await requestJson(
      path,
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      },
      path,
      opts,
    );
    return payloadJson;
  };

  // DELETE wrapper that routes 4xx/5xx through ``handleApiError`` so the
  // global toast and console-error surface stay consistent with
  // ``getJson``/``postJson``. New mutating helpers should route through
  // requestJson/postJson/patchJson/deleteJson so successful mutations always
  // invalidate reusable GET promises.
  const deleteJson = async (path: string, payload?: any) => {
    const { payloadJson } = await requestJson(path, {
      method: 'DELETE',
      ...(payload === undefined
        ? {}
        : {
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload),
          }),
    });
    return payloadJson;
  };

  const patchJson = async (path: string, payload: any, opts?: { handleError?: boolean }) => {
    const { payloadJson } = await requestJson(
      path,
      {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      },
      path,
      opts,
    );
    return payloadJson;
  };

  const sleep = (ms: number) => new Promise((resolve) => window.setTimeout(resolve, ms));

  const startAndPollDependencyInstall = async (dep: string): Promise<InstallResult> => {
    const started = await postJson(`/api/dependencies/${encodeURIComponent(dep)}/install`, {});
    const jobId = typeof started?.job_id === 'string' ? started.job_id : null;
    if (!jobId) return started;

    const deadline = Date.now() + 310_000;
    let last = started;
    while (Date.now() < deadline) {
      await sleep(1000);
      last = await getJson(`/api/dependencies/${encodeURIComponent(dep)}/install/${encodeURIComponent(jobId)}`);
      if (last?.status === 'succeeded' || last?.status === 'failed') {
        return last;
      }
    }
    return { ...last, ok: false, status: 'failed', message: t('settings.dependencies.installFailed') };
  };

  const startAndPollAgentInstall = async (name: string): Promise<InstallResult> => {
    const started = await postJson(`/api/agent/${encodeURIComponent(name)}/install`, {});
    const jobId = typeof started?.job_id === 'string' ? started.job_id : null;
    if (!jobId) return started;

    const deadline = Date.now() + 310_000;
    let last = started;
    while (Date.now() < deadline) {
      await sleep(1000);
      last = await getJson(
        `/api/agent/${encodeURIComponent(name)}/install/${encodeURIComponent(jobId)}`,
      );
      if (last?.status === 'succeeded' || last?.status === 'failed') {
        return last;
      }
    }
    return {
      ...last,
      ok: false,
      status: 'failed',
      message: last?.message || t('backendLifecycle.upgradeFailed'),
    };
  };

  // ``useMemo`` is load-bearing here, not a perf tweak. Without it,
  // ``ApiProvider`` produces a fresh ``value`` object on every render
  // — including the renders triggered by ToastProvider's state
  // updates above us in the tree. Each new ``value`` flips the
  // identity of ``api`` for every ``useApi()`` consumer, so any
  // ``useEffect(..., [api])`` re-runs on every toast.
  //
  // Concrete failure that this fix addresses (reported on iOS Safari
  // for PR #282): clicking "Copy" on the Codex device-code block
  // calls ``showToast('copied')`` → ToastProvider re-renders →
  // ApiProvider re-renders → ``value`` identity changes →
  // SettingsCodexProviderPage's mount effect re-runs → calls
  // ``getCodexAuth()`` → reads the disk state (still ``apikey``
  // because the OAuth flow hasn't completed) → ``setAuthMode("api_
  // key")`` → the segmented radio flips back to API Key mid-login.
  // Defensive patches at the event boundary (preventDefault,
  // disabled buttons, setter guards) didn't help because the click
  // wasn't the trigger — the cascading re-render was.
  //
  // ``[showToast, t]`` are intentional deps: ``showToast`` is stable
  // (``useCallback`` in ToastContext) so it never invalidates by
  // itself; ``t`` only changes on locale switch — recomputing then
  // is correct (cached error messages would otherwise stay in the
  // old language).
  const value: ApiContextType = useMemo(() => ({
    getConfig: () => getCachedJson('/api/config', CONFIG_CACHE_TTL_MS),
    getPlatformCatalog: () => getJson('/api/platforms'),
    mutateConfig: (mutations) => {
      const save = async () => {
        const config = await postJson('/api/config', configMutationsToPayload(mutations));
        convergeConfig(config);
        return config;
      };
      const touchesAgentActivity = mutations.some((mutation) => (
        mutation.kind === 'set'
        && mutation.path.length <= 2
        && mutation.path.every((part, index) => part === ['ui', 'show_agent_activity'][index])
      ));
      if (!touchesAgentActivity) return save();
      const mutation = agentActivityConfigMutationTailRef.current
        .catch(() => undefined)
        .then(save);
      agentActivityConfigMutationTailRef.current = mutation;
      return mutation;
    },
    waitForAgentActivityConfigMutations: async () => {
      // Include writes appended while the current tail is settling; return only
      // when the provider's Agent Activity queue is actually idle.
      while (true) {
        const pending = agentActivityConfigMutationTailRef.current;
        await pending.catch(() => undefined);
        if (pending === agentActivityConfigMutationTailRef.current) return;
      }
    },
    onConfigChanged,
    getSettings: (platform) => getJson(platform ? `/api/settings?platform=${encodeURIComponent(platform)}` : '/api/settings'),
    saveSettings: (payload, platform) => postJson('/api/settings', platform ? { ...payload, platform } : payload),
    saveThreadSettings: (platform, channelId, threadId, settings) => postJson('/api/settings/thread', {
      platform,
      channel_id: channelId,
      thread_id: threadId,
      settings,
    }),
    deleteThreadSettings: (platform, channelId, threadId) => deleteJson(
      `/api/settings/thread?platform=${encodeURIComponent(platform)}&channel_id=${encodeURIComponent(channelId)}&thread_id=${encodeURIComponent(threadId)}`,
    ),
    getUsers: (platform) => getJson(platform ? `/api/users?platform=${encodeURIComponent(platform)}` : '/api/users'),
    saveUsers: (payload, platform) => postJson('/api/users', platform ? { ...payload, platform } : payload),
    toggleAdmin: (userId, isAdmin, platform) => postJson(`/api/users/${encodeURIComponent(userId)}/admin`, platform ? { is_admin: isAdmin, platform } : { is_admin: isAdmin }),
    removeUser: (userId, platform) =>
      deleteJson(platform ? `/api/users/${encodeURIComponent(userId)}?platform=${encodeURIComponent(platform)}` : `/api/users/${encodeURIComponent(userId)}`),
    getShowPages: () => getJson('/api/show-pages'),
    getShowPageAccess: (sessionId) => readShowPageJson(`/api/show-pages/${encodeURIComponent(sessionId)}/access`),
    probeShowPageAccess: async (sessionId) => {
      try {
        const response = await apiFetch(`/api/show-pages/${encodeURIComponent(sessionId)}/access`);
        let payload: unknown;
        try {
          payload = await response.json();
        } catch {
          return { status: 'error', access: null };
        }
        return classifyShowPageAccessProbe(response.status, payload);
      } catch {
        return { status: 'error', access: null };
      }
    },
    getShowAccessSettings: (sessionId) => postJson(
      `/api/show-pages/${encodeURIComponent(sessionId)}/access-settings/read`,
      { page_id: sessionId },
    ),
    applyShowAccess: (sessionId, payload) => postJson(
      `/api/show-pages/${encodeURIComponent(sessionId)}/access-settings/apply`,
      { page_id: sessionId, ...payload },
    ),
    getWebPushStatus: (payload) =>
      payload ? postJson('/api/web-push/status', payload) : getJson('/api/web-push/status'),
    getWebPushVapidPublicKey: () => getJson('/api/web-push/vapid-public-key'),
    subscribeWebPush: (subscription, deviceLabel, deviceId, previousEndpoints) =>
      postJson('/api/web-push/subscriptions', {
        subscription,
        device_label: deviceLabel,
        device_id: deviceId,
        previous_endpoints: previousEndpoints,
      }),
    unsubscribeWebPush: (endpoint) => deleteJson('/api/web-push/subscriptions', { endpoint }),
    sendWebPushTest: (payload) => postJson('/api/web-push/test', payload ?? {}),
    setShowPageAvailability: (sessionId, offline) => postJson(
      `/api/show-pages/${encodeURIComponent(sessionId)}/availability`,
      { offline },
    ),
    getShowPage: (sessionId) => readShowPageJson(`/api/show-pages/${encodeURIComponent(sessionId)}`),
    ensureShowPage: (sessionId) => postJson(`/api/show-pages/${encodeURIComponent(sessionId)}/ensure`, {}),
    uploadShowPageIcon: async (sessionId, file) => {
      // Multipart POST: the server names the on-disk file, so we send only the bytes
      // and a filename hint. `requestJson` adds CSRF + surfaces errors like everything
      // else; letting the browser set the multipart Content-Type/boundary (no JSON).
      const form = new FormData();
      form.append('file', file, file.name);
      const { payloadJson } = await requestJson(
        `/api/show-pages/${encodeURIComponent(sessionId)}/icon`,
        { method: 'POST', body: form },
      );
      return payloadJson;
    },
    getDock: () => getJson('/api/dock'),
    pinDockShowPage: (sessionId) => postJson('/api/dock/pins', { session_id: sessionId }),
    unpinDockShowPage: (sessionId) => deleteJson(`/api/dock/pins/${encodeURIComponent(sessionId)}`),
    setDockOrder: async (order, known) => {
      // Suppress the global error toast: a stale-reorder rejection (the server
      // rejects an order whose ``known`` baseline no longer matches its installed
      // set) is handled by the optimistic re-sync in DockContext, not a
      // user-facing error. ``known`` is the client's current id set (built-ins ∪
      // pins), sent so the server can reject a stale tab that would otherwise
      // silently undock a pin another tab installed. The caller inspects ``ok``.
      const { payloadJson } = await requestJson(
        '/api/dock/order',
        {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(known ? { order, known } : { order }),
        },
        'PUT /api/dock/order',
        { handleError: false },
      );
      return payloadJson;
    },
    getWorkbenchPrefs: () => getCachedJson('/api/workbench/prefs', 5_000),
    setBackgroundWorkBannerEnabled: async (enabled) => {
      // Default handleError:true — a non-2xx (auth/CSRF/server) throws so the
      // caller reverts its optimistic switch instead of diverging from the
      // persisted value.
      const { payloadJson } = await requestJson(
        '/api/workbench/prefs',
        {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ background_work_banner_enabled: enabled }),
        },
        'PUT /api/workbench/prefs',
      );
      return payloadJson;
    },
    getBindCodes: () => getJson('/api/bind-codes'),
    createBindCode: (type, expiresAt) => postJson('/api/bind-codes', { type, expires_at: expiresAt }),
    deleteBindCode: (code) => deleteJson(`/api/bind-codes/${encodeURIComponent(code)}`),
    getFirstBindCode: () => getJson('/api/setup/first-bind-code'),
    detectCli: (binary) => getJson(`/api/cli/detect?binary=${encodeURIComponent(binary)}`),
    installAgent: (name) => startAndPollAgentInstall(name),
    listDependencies: () => getJson('/api/dependencies'),
    installDependency: (dep) => startAndPollDependencyInstall(dep),
    // handleError: false — every route returns closed `{status:'failed',error}` bodies (never a
    // thrown ApiError/toast) so the Memory page can render its own inline state per code.
    getMemorySettings: () => getJson('/api/memory/settings', { handleError: false }),
    saveMemorySettings: (patch) => patchJson('/api/memory/settings', patch, { handleError: false }),
    getMemoryProcessingRecord: () => getJson('/api/memory/processing-record', { handleError: false }),
    getMemoryProcessingRecordEntries: (project, cursor = null, limit = 20) => {
      const query = new URLSearchParams({ limit: String(limit), project });
      if (cursor) query.set('cursor', cursor);
      return getJson(`/api/memory/processing-record/entries?${query.toString()}`, { handleError: false });
    },
    getMemoryProcessingRecordEntry: (project, memcellId) => {
      const query = new URLSearchParams({ memcell_id: memcellId, project });
      return getJson(`/api/memory/processing-record/entry?${query.toString()}`, { handleError: false });
    },
    getMemoryStatus: () => getJson('/api/memory/status', { handleError: false }),
    getMemoryFailures: () => getJson('/api/memory/failures', { handleError: false }),
    getMemoryMaintenance: () => getJson('/api/memory/maintenance', { handleError: false }),
    getMemoryProfile: () => getJson('/api/memory/profile', { handleError: false }),
    searchMemory: (query, limit = 20, project) => postJson('/api/memory/search', {
      query,
      policy: {
        mode: 'hybrid',
        max_results: limit,
        include_profile: true,
        include_current_session: false,
      },
      ...(project ? { project } : {}),
    }, { handleError: false }),
    listMemoryEpisodes: (project, options = {}) => {
      const limit = options.limit ?? 20;
      return postJson('/api/memory/list', {
        project,
        limit,
        ...(options.origin ? { origin: options.origin } : {}),
        ...(project === 'all'
          ? (options.cursor ? { cursor: options.cursor } : {})
          : { page: options.page ?? 1 }),
      }, { handleError: false });
    },
    listMemoryProjects: () => getJson('/api/memory/projects', { handleError: false }),
    deleteMemoryData: (confirmLoss) => postJson('/api/memory/delete-data', { confirm_loss: confirmLoss }, { handleError: false }),
    wakeMemory: () => postJson('/api/memory/runtime/wake', {}, { handleError: false }),
    repairMemory: (confirmLoss) => postJson('/api/memory/repair', { confirm_loss: confirmLoss }, { handleError: false }),
    getBackendRuntime: (name) => getJson(`/api/backend/${encodeURIComponent(name)}/runtime`),
    restartBackend: (name) => postJson(`/api/backend/${encodeURIComponent(name)}/restart`, {}),
    getCodexAuth: () => getJson('/api/backend/codex/auth'),
    saveCodexAuth: (payload) => postJson('/api/backend/codex/auth', payload),
    getClaudeAuth: () => getJson('/api/backend/claude/auth'),
    saveClaudeAuth: (payload) => postJson('/api/backend/claude/auth', payload),
    startOAuthWeb: (backend, forceReset = true) =>
      postJson(`/api/backend/${encodeURIComponent(backend)}/auth/oauth/start`, {
        force_reset: forceReset,
      }),
    startOAuthWebForOpencodeProvider: (providerId, forceReset = true) =>
      postJson(
        `/api/backend/opencode/provider/${encodeURIComponent(providerId)}/auth/oauth/start`,
        { force_reset: forceReset },
      ),
    getOAuthWebStatus: (backend, flowId) =>
      getJson(
        `/api/backend/${encodeURIComponent(backend)}/auth/oauth/status/${encodeURIComponent(flowId)}`,
      ),
    submitOAuthWebCode: (backend, flowId, code) =>
      postJson(`/api/backend/${encodeURIComponent(backend)}/auth/oauth/submit-code`, {
        flow_id: flowId,
        code,
      }),
    cancelOAuthWeb: (backend, flowId) =>
      postJson(`/api/backend/${encodeURIComponent(backend)}/auth/oauth/cancel`, {
        flow_id: flowId,
      }),
    removeBackendAuth: (backend) =>
      postJson(`/api/backend/${encodeURIComponent(backend)}/auth/oauth/remove`, {}),
    removeClaudeOAuthCredentials: () =>
      postJson('/api/backend/claude/auth/oauth/credentials/remove', {}),
    removeBackendApiKey: (backend) =>
      postJson(`/api/backend/${encodeURIComponent(backend)}/auth/api-key/remove`, {}),
    testBackendAuth: (backend, options) =>
      postJson(`/api/backend/${encodeURIComponent(backend)}/auth/test`, {
        ...(options?.model ? { model: options.model } : {}),
      }),
    testOpencodeProvider: (providerId, options) =>
      postJson(`/api/backend/opencode/provider/${encodeURIComponent(providerId)}/test`, {
        ...(options?.model ? { model: options.model } : {}),
      }),
    getOpencodeProviders: () => getJson('/api/backend/opencode/providers'),
    // Model pickers absorb the expected Owner-only refusal and retain typed-value
    // fallback. Keep that policy on this dedicated reader so direct options calls
    // still report access failures and no picker can forget the suppression.
    readOpencodeOptionsForModelPicker: () =>
      postJson('/api/opencode/options', { cwd: '~' }, {
        expectedCodes: ['instance_access_forbidden'],
      }),
    saveOpencodeCustomProvider: (payload) =>
      postJson('/api/backend/opencode/custom-provider', payload),
    deleteOpencodeCustomProvider: (providerId) =>
      deleteJson(`/api/backend/opencode/custom-provider/${encodeURIComponent(providerId)}`),
    setOpencodeProviderAuth: (providerId, apiKey, baseUrl) =>
      // Forward ``base_url`` only when the caller passed something
      // (including an explicit empty string for "clear"); omitting it
      // entirely tells the server to leave the stored value untouched,
      // which is the right default for callers that don't care about
      // the base-URL override.
      postJson(`/api/backend/opencode/provider/${encodeURIComponent(providerId)}/auth`, {
        api_key: apiKey,
        ...(baseUrl !== undefined ? { base_url: baseUrl } : {}),
      }),
    deleteOpencodeProviderAuth: (providerId) =>
      deleteJson(`/api/backend/opencode/provider/${encodeURIComponent(providerId)}/auth`),
    setOpencodeDefaultProvider: (providerId) =>
      postJson('/api/backend/opencode/default-provider', { provider_id: providerId }),
    saveOpencodeProviderModel: (providerId, payload) =>
      postJson(`/api/backend/opencode/provider/${encodeURIComponent(providerId)}/models`, payload),
    deleteOpencodeProviderModel: (providerId, modelId) =>
      deleteJson(
        `/api/backend/opencode/provider/${encodeURIComponent(providerId)}/models/${encodeURIComponent(modelId)}`,
      ),
    slackAuthTest: (botToken, proxyUrl) => postJson('/api/slack/auth_test', { bot_token: botToken, proxy_url: proxyUrl || undefined }),
    slackChannels: (botToken, browseAll, force, includeNotReturned) => postJson('/api/slack/channels', { bot_token: botToken, browse_all: browseAll || false, force: force || false, include_not_returned: includeNotReturned || false }),
    slackManifest: () => getJson('/api/slack/manifest'),
    discordAuthTest: (botToken, proxyUrl) => postJson('/api/discord/auth_test', { bot_token: botToken, proxy_url: proxyUrl || undefined }),
    discordGuilds: (botToken) => postJson('/api/discord/guilds', { bot_token: botToken }),
    discordChannels: (botToken, guildId, force, includeNotReturned) => postJson('/api/discord/channels', { bot_token: botToken, guild_id: guildId, force: force || false, include_not_returned: includeNotReturned || false }),
    telegramAuthTest: (botToken, proxyUrl) => postJson('/api/telegram/auth_test', { bot_token: botToken, proxy_url: proxyUrl || undefined }),
    telegramChats: (includePrivate, includeNotReturned) => postJson('/api/telegram/chats', { include_private: includePrivate || false, include_not_returned: includeNotReturned || false }),
    larkAuthTest: (appId, appSecret, domain, proxyUrl) => postJson('/api/lark/auth_test', { app_id: appId, app_secret: appSecret, domain: domain || 'feishu', proxy_url: proxyUrl || undefined }),
    larkChats: (appId, appSecret, domain, force, includeNotReturned) => postJson('/api/lark/chats', { app_id: appId, app_secret: appSecret, domain: domain || 'feishu', force: force || false, include_not_returned: includeNotReturned || false }),
    deleteChannel: (platform, id, scopeType) => postJson('/api/channels/delete', { platform, id, scope_type: scopeType || 'channel' }),
    larkTempWsStart: (appId, appSecret, domain) => postJson('/api/lark/temp_ws/start', { app_id: appId, app_secret: appSecret, domain: domain || 'feishu' }),
    larkTempWsStop: () => postJson('/api/lark/temp_ws/stop', {}),
    wechatStartLogin: () => postJson('/api/wechat/qr_login/start', {}),
    wechatPollLogin: (sessionKey, verifyCode) => postJson('/api/wechat/qr_login/poll', { session_key: sessionKey, verify_code: verifyCode || undefined }),
    doctor: (options = {}) => postJson('/api/doctor', options.deep ? { deep: true } : {}),
    opencodeOptions: (cwd) => postJson('/api/opencode/options', { cwd }),
    opencodeSetupPermission: () => postJson('/api/opencode/setup-permission', {}),
    opencodePermissionStatus: () => getJson('/api/opencode/permission-status'),
    claudeAgents: (cwd) => cwd ? getJson(`/api/claude/agents?cwd=${encodeURIComponent(cwd)}`) : getJson('/api/claude/agents'),
    claudeModels: () => getJson('/api/claude/models'),
    codexAgents: (cwd) => cwd ? getJson(`/api/codex/agents?cwd=${encodeURIComponent(cwd)}`) : getJson('/api/codex/agents'),
    codexModels: () => getJson('/api/codex/models'),
    // Direct mode, rolling upgrades, and a temporarily unreadable Hub catalog
    // all keep the existing native picker behavior without surfacing a toast.
    readModelHubAgentCatalogForModelPicker: (backend) =>
      getJson(`/api/models/agents/${encodeURIComponent(backend)}/models`, { handleError: false })
        .then((payload) => {
          const agent = payload?.ok === false ? null : payload?.agent;
          return agent && typeof agent === 'object'
            ? (agent as Pick<AgentSupply, 'backend' | 'mode' | 'catalog_models'>)
            : null;
        })
        .catch(() => null),
    getLogs: (lines = 500, source) => postJson('/api/logs', source ? { lines, source } : { lines }),
    getVersion: () => getCachedJson('/api/version', 10_000),
    doUpgrade: () => postJson('/api/upgrade', {}),
    browseDirectory: (path, showHidden) => postJson('/api/browse', { path, show_hidden: showHidden || false }),
    browseFavorites: () => getJson('/api/browse/favorites'),
    browseMkdir: (path) => postJson('/api/browse/mkdir', { path }),
    listProjects: (includeArchived, options) => {
      const path = `/api/projects${includeArchived ? '?include_archived=1' : ''}`;
      return options?.cache === false ? getJson(path) : getCachedJson(path);
    },
    getWorkbenchProjectsBootstrap: (params) => {
      const search = new URLSearchParams();
      if (params?.includeArchived) search.set('include_archived', '1');
      if (params?.status) search.set('status', params.status);
      if (params?.limit) search.set('limit', String(params.limit));
      for (const projectId of params?.projectIds ?? []) {
        search.append('project_id', projectId);
      }
      const qs = search.toString();
      const path = qs ? `/api/workbench/projects-bootstrap?${qs}` : '/api/workbench/projects-bootstrap';
      return params?.cache === false ? getJson(path) : getCachedJson(path);
    },
    createProject: (payload) => postJson('/api/projects', payload),
    updateProject: async (projectId, payload) => {
      const { payloadJson } = await requestJson(`/api/projects/${encodeURIComponent(projectId)}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      }, `PATCH /api/projects/${projectId}`);
      return payloadJson;
    },
    archiveProject: (projectId) => deleteJson(`/api/projects/${encodeURIComponent(projectId)}`),
    getProjectAgentsMd: (projectId) =>
      getJson(`/api/projects/${encodeURIComponent(projectId)}/agents-md`),
    saveProjectAgentsMd: async (projectId, payload) => {
      const { payloadJson } = await requestJson(`/api/projects/${encodeURIComponent(projectId)}/agents-md`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      }, `PUT /api/projects/${projectId}/agents-md`);
      return payloadJson;
    },
    getGlobalPrompts: () => getJson('/api/global-prompts'),
    saveGlobalPrompts: async (payload) => {
      const { payloadJson } = await requestJson('/api/global-prompts', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      return payloadJson;
    },
    listSessions: (params) => {
      const search = new URLSearchParams();
      if (params?.projectId) search.set('project_id', params.projectId);
      if (params?.status) search.set('status', params.status);
      if (params?.limit) search.set('limit', String(params.limit));
      if (params?.beforeId) search.set('before_id', params.beforeId);
      if (params?.q) search.set('q', params.q);
      const qs = search.toString();
      const path = qs ? `/api/sessions?${qs}` : '/api/sessions';
      return params?.cache === false ? getJson(path) : getCachedJson(path);
    },
    createSession: (payload) => postJson('/api/sessions', payload),
    forkSession: (sessionId) =>
      postJson(`/api/sessions/${encodeURIComponent(sessionId)}/fork`, {}),
    getSession: (sessionId, params) =>
      params?.cache === false
        ? getJson(`/api/sessions/${encodeURIComponent(sessionId)}`, { handleError: params?.handleError })
        : getCachedJson(`/api/sessions/${encodeURIComponent(sessionId)}`, undefined, { handleError: params?.handleError }),
    getSessionResult: async (sessionId) => {
      const res = await apiFetch(`/api/sessions/${encodeURIComponent(sessionId)}`);
      const payload = await res.json().catch(() => null);
      return {
        status: res.status,
        session: res.ok && payload && typeof payload.id === 'string' ? payload : null,
      };
    },
    getSessionBootstrap: async (sessionId) => {
      const read = sessionDraftPersistence.beginRead(sessionId);
      try {
        const payload = await getJson(`/api/sessions/${encodeURIComponent(sessionId)}/bootstrap`);
        if (payload?.session?.status === 'archived') {
          sessionDraftPersistence.clearSession(sessionId);
          return payload;
        }
        const server = sessionDraftServerState(payload?.draft);
        const text = sessionDraftPersistence.reconcileRead(sessionId, read, server);
        void sessionDraftPersistence.retry(
          sessionId,
          (draft) => writeSessionDraft(sessionId, draft),
        );
        return text === server.text
          ? payload
          : { ...payload, draft: { ...(payload.draft ?? {}), text } };
      } catch (error) {
        sessionDraftPersistence.releaseRead(sessionId, read);
        throw error;
      }
    },
    updateSession: async (sessionId, payload) => {
      const { payloadJson } = await requestJson(`/api/sessions/${encodeURIComponent(sessionId)}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      }, `PATCH /api/sessions/${sessionId}`);
      return payloadJson;
    },
    archiveSession: async (sessionId) => {
      const payload = await deleteJson(`/api/sessions/${encodeURIComponent(sessionId)}`);
      sessionDraftPersistence.clearSession(sessionId);
      return payload;
    },
    convergeSessionArchived,
    onSessionArchived,
    getArchivePreview: (sessionId) =>
      getJson(`/api/sessions/${encodeURIComponent(sessionId)}/archive-preview`),
    listSessionMessages: (sessionId, params) => {
      const search = new URLSearchParams();
      if (params?.afterId) search.set('after_id', params.afterId);
      if (params?.beforeId) search.set('before_id', params.beforeId);
      if (params?.aroundId) search.set('around_id', params.aroundId);
      if (params?.aroundNativeId) search.set('around_native_id', params.aroundNativeId);
      if (params?.aroundNativePlatform) search.set('around_native_platform', params.aroundNativePlatform);
      if (params?.aroundTurnId) search.set('around_turn_id', params.aroundTurnId);
      if (params?.aroundRunId) search.set('around_run_id', params.aroundRunId);
      if (params?.limit) search.set('limit', String(params.limit));
      if (params?.tail) search.set('tail', '1');
      const qs = search.toString();
      const base = `/api/sessions/${encodeURIComponent(sessionId)}/messages`;
      const path = qs ? `${base}?${qs}` : base;
      return params?.cache === false ? getJson(path) : getCachedJson(path);
    },
    getSessionActivity: (sessionId) =>
      // Uncached: this is also the gap-recovery resync path, so it must reflect
      // durable state (not a stale read served after a missed message.new).
      getJson(`/api/sessions/${encodeURIComponent(sessionId)}/activity`),
    getSessionActivityGroup: (sessionId, groupId) =>
      getJson(
        `/api/sessions/${encodeURIComponent(sessionId)}/activity?group_id=${encodeURIComponent(groupId)}`,
      ),
    searchMessages: (q, opts) => {
      const search = new URLSearchParams();
      search.set('q', q);
      if (opts?.limit) search.set('limit', String(opts.limit));
      if (opts?.includeArchived) search.set('include_archived', '1');
      return getJson(`/api/search/messages?${search.toString()}`);
    },
    sendSessionMessage: (sessionId, payload) =>
      postJson(`/api/sessions/${encodeURIComponent(sessionId)}/messages`, payload),
    markSessionRead: (sessionId, untilMessageId, opts) =>
      postJson(
        `/api/sessions/${encodeURIComponent(sessionId)}/mark-read`,
        untilMessageId ? { until_message_id: untilMessageId } : {},
        opts,
      ),
    cancelSession: async (sessionId) => {
      const { res, payloadJson } = await requestJson(`/api/sessions/${encodeURIComponent(sessionId)}/cancel`, {
        method: 'POST',
      }, `/api/sessions/${sessionId}/cancel`, { handleError: false });
      // 503 + 404 are surfaced to the caller as plain payloads so the
      // UI can render a sensible "nothing to stop" / "socket down"
      // state without throwing.
      return { ok: res.ok, ...payloadJson };
    },
    listSessionQueue: (sessionId, options) => {
      const path = `/api/sessions/${encodeURIComponent(sessionId)}/queue`;
      return options?.cache === false ? getJson(path) : getCachedJson(path);
    },
    removeQueuedMessage: (sessionId, messageId) =>
      deleteJson(`/api/sessions/${encodeURIComponent(sessionId)}/queue/${encodeURIComponent(messageId)}`),
    sendQueuedNow: async (sessionId, messageId) => {
      const { res, payloadJson } = await requestJson(
        `/api/sessions/${encodeURIComponent(sessionId)}/queue/${encodeURIComponent(messageId)}/send-now`,
        { method: 'POST' },
        `/api/sessions/${sessionId}/queue/${messageId}/send-now`,
        { handleError: false },
      );
      return { ok: res.ok, ...payloadJson };
    },
    getTurnState: async (sessionId, options) => {
      const path = `/api/sessions/${encodeURIComponent(sessionId)}/turn-state`;
      const res = await apiFetch(path);
      if (res.status === 504) {
        readCacheRef.current.delete(path);
        return {
          in_flight: null,
          foreground: 'unknown',
          native_turn_started: false,
          pending_input_count: 0,
          background_activities: [],
          pending_activity_output_count: 0,
          connection: 'unknown',
        };
      }
      if (!res.ok) {
        if (options?.handleError === false) {
          throw new ApiError(`Request failed: ${path} (${res.status})`, res.status, null);
        }
        await handleApiError(res, path);
      }
      return res.json();
    },
    getCachedSessionDraft: (sessionId) => sessionDraftPersistence.peek(sessionId),
    cacheSessionDraft: (sessionId, text) => sessionDraftPersistence.cache(sessionId, text),
    getSessionDraft: async (sessionId) => {
      const read = sessionDraftPersistence.beginRead(sessionId);
      try {
        const payload = await getJson(`/api/sessions/${encodeURIComponent(sessionId)}/draft`);
        const server = sessionDraftServerState(payload);
        const text = sessionDraftPersistence.reconcileRead(sessionId, read, server);
        void sessionDraftPersistence.retry(
          sessionId,
          (draft) => writeSessionDraft(sessionId, draft),
        );
        return { text };
      } catch (error) {
        sessionDraftPersistence.releaseRead(sessionId, read);
        throw error;
      }
    },
    setSessionDraft: (sessionId, text) => sessionDraftPersistence.save(
      sessionId,
      text,
      (draft) => writeSessionDraft(sessionId, draft),
    ),
    reconcileSessionDraftAfterSend: (sessionId, draft) => (
      rebaseAndRetrySessionDraft(sessionId, sessionDraftServerState(draft))
    ),
    recoverSessionDraftAfterRejectedSend: async (sessionId) => {
      // The message reservation clears the cloud draft before dispatch. A
      // rejected/unknown dispatch therefore needs an authoritative revision
      // before the composer's optimistic text restoration is replayed.
      sessionDraftPersistence.markRejectedSend(sessionId);
      const server = await readSessionDraftServer(sessionId);
      if (!server) return;
      await rebaseAndRetrySessionDraft(sessionId, server);
    },
    listInbox: (params) => {
      const search = new URLSearchParams();
      if (params?.platform) search.set('platform', params.platform);
      if (params?.unreadOnly) search.set('unread_only', '1');
      if (params?.limit) search.set('limit', String(params.limit));
      if (params?.before) search.set('before', params.before);
      if (params?.onlySession) search.set('session', params.onlySession);
      const qs = search.toString();
      const path = qs ? `/api/inbox?${qs}` : '/api/inbox';
      const options = { handleError: params?.handleError };
      return params?.cache === false ? getJson(path, options) : getCachedJson(path, 1500, options);
    },
    listVibeAgents: (params) => {
      const search = new URLSearchParams();
      if (params?.backend) search.set('backend', params.backend);
      if (params?.includeDisabled) search.set('include_disabled', '1');
      if (params?.includeArchived) search.set('include_archived', '1');
      const qs = search.toString();
      const path = qs ? `/api/agents?${qs}` : '/api/agents';
      return params?.cache === false ? getJson(path) : getCachedJson(path, 5_000);
    },
    getVibeAgentOnboarding: () => getJson('/api/agent-onboarding'),
    onboardVibeAgents: () => postJson('/api/agent-onboarding', {}),
    getVibeAgent: (name, params) => {
      const path = `/api/agents/${encodeURIComponent(name)}`;
      return params?.cache === false
        ? getJson(path, { handleError: params.handleError, expectedCodes: params.expectedCodes })
        : getCachedJson(path, 5_000, { handleError: params?.handleError });
    },
    createVibeAgent: (payload) => postJson('/api/agents', payload),
    updateVibeAgent: async (name, payload) => {
      const { payloadJson } = await requestJson(`/api/agents/${encodeURIComponent(name)}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      }, `PATCH /api/agents/${name}`);
      return payloadJson;
    },
    setDefaultVibeAgent: (name) => postJson('/api/agents/default', { name }),
    removeVibeAgent: (name) => deleteJson(`/api/agents/${encodeURIComponent(name)}`),
    getVaultVmk: () => getCachedJson('/api/vault/vmk', 1500, { handleError: false }),
    listVaultSecrets: () => getCachedJson('/api/vault/secrets', 1500),
    getVaultPubkey: () => getCachedJson('/api/vault/pubkey', 1500),
    getVaultAgentPubkey: () => getCachedJson('/api/vault/agent/pubkey', 1500),
    getVaultSandboxRootMetadata: () => getCachedJson('/api/vault/sandbox/root-metadata', 1500, { handleError: false }),
    createVaultAgentBindingsBatch: (payload) =>
      postJson('/api/vault/agent-bindings:batch', payload, { handleError: false }),
    getVaultSettings: () => getJson('/api/vault/settings', { handleError: false }),
    saveVaultSettings: (payload) => patchJson('/api/vault/settings', payload, { handleError: false }),
    createVaultRevealContext: (name, payload) =>
      postJson(`/api/vault/secrets/${encodeURIComponent(name)}/reveal-context`, payload ?? {}, { handleError: false }),
    deriveSigningAddresses: (publicKey) =>
      postJson('/api/vault/signing-addresses', { public_key: publicKey }, { handleError: false }),
    createVaultAuthzWebAuthnOptions: () => postJson('/api/vault/authz/factors/webauthn/options', {}),
    registerVaultAuthzWebAuthnFactor: (payload) => postJson('/api/vault/authz/factors/webauthn', payload),
    createVaultSecret: (payload, opts) => postJson('/api/vault/secrets', payload, opts),
    updateVaultSecret: (name, payload, opts) => patchJson(`/api/vault/secrets/${encodeURIComponent(name)}`, payload, opts),
    deleteVaultSecret: (name) => deleteJson(`/api/vault/secrets/${encodeURIComponent(name)}`),
    getVaultProvisionRequest: (name, opts) =>
      getCachedJson(`/api/vault/provision-requests/${encodeURIComponent(name)}`, 1500, opts),
    getVaultProvisionRequestById: (requestId, opts) =>
      getCachedJson(`/api/vault/provision-requests/by-id/${encodeURIComponent(requestId)}`, 1500, opts),
    getVaultRequests: (params, opts) => {
      const search = new URLSearchParams();
      if (params?.status) search.set('status', params.status);
      if (params?.type) search.set('type', params.type);
      if (params?.limit) search.set('limit', String(params.limit));
      if (params?.session) search.set('session', params.session);
      const qs = search.toString();
      return getCachedJson(qs ? `/api/vault/requests?${qs}` : '/api/vault/requests', 1500, opts);
    },
    denyVaultRequest: (requestId) => postJson(`/api/vault/requests/${encodeURIComponent(requestId)}/deny`, {}),
    fulfillVaultAccessRequest: (requestId, payload) =>
      postJson(`/api/vault/requests/${encodeURIComponent(requestId)}/fulfill-access`, payload),
    getVaultGrants: (params, opts) => {
      const search = new URLSearchParams();
      if (params?.status) search.set('status', params.status);
      if (params?.sessionId) search.set('session_id', params.sessionId);
      const qs = search.toString();
      return getCachedJson(qs ? `/api/vault/grants?${qs}` : '/api/vault/grants', 1500, opts);
    },
    createVaultGrant: (payload) => postJson('/api/vault/grants', payload),
    revokeVaultGrant: (grantId) => deleteJson(`/api/vault/grants/${encodeURIComponent(grantId)}`),
    signVaultDigest: (payload) => postJson('/api/vault/sign', payload),
    pinVaultPubkey: (payload) => postJson('/api/vault/pubkey-pin', payload),
    getVaultAudit: (params) => {
      const search = new URLSearchParams();
      if (params?.secret) search.set('secret', params.secret);
      if (params?.limit) search.set('limit', String(params.limit));
      const qs = search.toString();
      return getCachedJson(qs ? `/api/vault/audit?${qs}` : '/api/vault/audit', 1500);
    },
    importVibeAgents: (payload) => postJson('/api/agents/import', payload),
    listSkills: (params) => {
      const search = new URLSearchParams();
      if (params?.scope) search.set('scope', params.scope);
      if (params?.projectId) search.set('project_id', params.projectId);
      if (params?.backends?.length) search.set('backends', params.backends.join(','));
      const qs = search.toString();
      return getCachedJson(qs ? `/api/skills?${qs}` : '/api/skills', 5_000);
    },
    previewSkillSource: (source, params) =>
      postJson('/api/skills/preview', { source, project_id: params?.projectId }),
    addSkill: (payload) =>
      postJson('/api/skills', {
        source: payload.source,
        scope: payload.scope,
        project_id: payload.projectId,
        backends: payload.backends,
        all: payload.all,
        skill: payload.skill,
        copy: payload.copy,
      }),
    removeSkill: (name, params) => {
      const search = new URLSearchParams();
      if (params?.scope) search.set('scope', params.scope);
      if (params?.projectId) search.set('project_id', params.projectId);
      if (params?.backends?.length) search.set('backends', params.backends.join(','));
      const qs = search.toString();
      return deleteJson(qs ? `/api/skills/${encodeURIComponent(name)}?${qs}` : `/api/skills/${encodeURIComponent(name)}`);
    },
    findSkills: (query) => getJson(`/api/skills/find?q=${encodeURIComponent(query)}`),
    uploadSkillZip: async (file, params) => {
      // Read the file client-side and send it as base64 JSON so the upload
      // rides the same /api route + auth as everything else (no multipart).
      const dataUrl = await new Promise<string>((resolve, reject) => {
        const reader = new FileReader();
        reader.onload = () => resolve(String(reader.result));
        reader.onerror = () => reject(reader.error);
        reader.readAsDataURL(file);
      });
      const base64 = dataUrl.split(',')[1] ?? '';
      return postJson('/api/skills/upload', {
        filename: file.name,
        content_base64: base64,
        project_id: params?.projectId,
      });
    },
    checkSkills: (params) => {
      const search = new URLSearchParams();
      if (params?.scope) search.set('scope', params.scope);
      if (params?.projectId) search.set('project_id', params.projectId);
      const qs = search.toString();
      return getCachedJson(qs ? `/api/skills/check?${qs}` : '/api/skills/check', 5_000);
    },
    updateSkill: (name, params) =>
      postJson('/api/skills/update', { name, scope: params?.scope, project_id: params?.projectId }),
    getHarnessCounts: () => getCachedJson('/api/harness/counts'),
    getHarnessBootstrap: (params) => {
      const search = new URLSearchParams();
      if (params?.tab) search.set('tab', params.tab);
      if (params?.status) search.set('status', params.status);
      if (params?.run_type) search.set('run_type', params.run_type);
      if (params?.exclude_run_type?.length) search.set('exclude_run_type', params.exclude_run_type.join(','));
      if (params?.query) search.set('query', params.query);
      if (params?.session_id) search.set('session_id', params.session_id);
      if (params?.page) search.set('page', String(params.page));
      if (params?.limit) search.set('limit', String(params.limit));
      const qs = search.toString();
      return getCachedJson(qs ? `/api/harness/bootstrap?${qs}` : '/api/harness/bootstrap');
    },
    listHarnessTasks: (params, opts) => {
      const search = new URLSearchParams();
      if (params?.status) search.set('status', params.status);
      if (params?.query) search.set('query', params.query);
      if (params?.page) search.set('page', String(params.page));
      if (params?.limit) search.set('limit', String(params.limit));
      const qs = search.toString();
      return getCachedJson(qs ? `/api/harness/tasks?${qs}` : '/api/harness/tasks', undefined, opts);
    },
    setHarnessTaskEnabled: (taskId, enabled) =>
      patchJson(`/api/harness/tasks/${encodeURIComponent(taskId)}`, { enabled }),
    deleteHarnessTask: (taskId) => deleteJson(`/api/harness/tasks/${encodeURIComponent(taskId)}`),
    listHarnessWatches: (params, opts) => {
      const search = new URLSearchParams();
      if (params?.status) search.set('status', params.status);
      if (params?.query) search.set('query', params.query);
      if (params?.page) search.set('page', String(params.page));
      if (params?.limit) search.set('limit', String(params.limit));
      const qs = search.toString();
      return getCachedJson(qs ? `/api/harness/watches?${qs}` : '/api/harness/watches', undefined, opts);
    },
    setHarnessWatchEnabled: (watchId, enabled) =>
      patchJson(`/api/harness/watches/${encodeURIComponent(watchId)}`, { enabled }),
    deleteHarnessWatch: (watchId) => deleteJson(`/api/harness/watches/${encodeURIComponent(watchId)}`),
    listHarnessRuns: (params, opts) => {
      const search = new URLSearchParams();
      if (params?.status) search.set('status', params.status);
      if (params?.runType) search.set('run_type', params.runType);
      if (params?.excludeRunType?.length) search.set('exclude_run_type', params.excludeRunType.join(','));
      if (params?.agentName) search.set('agent_name', params.agentName);
      if (params?.definitionId) search.set('definition_id', params.definitionId);
      if (params?.query) search.set('query', params.query);
      if (params?.page) search.set('page', String(params.page));
      if (params?.limit) search.set('limit', String(params.limit));
      const qs = search.toString();
      return getCachedJson(qs ? `/api/harness/runs?${qs}` : '/api/harness/runs', undefined, opts);
    },
    getHarnessRun: (runId) => getCachedJson(`/api/harness/runs/${encodeURIComponent(runId)}`),
    connectWorkbenchEvents: (handlers) => {
      eventHandlersRef.current.add(handlers);
      ensureWorkbenchEventSource();
      queueMicrotask(() => {
        if (eventHandlersRef.current.has(handlers)) {
          handlers.onConnectionState?.(eventConnectionStateRef.current);
        }
      });
      if (workbenchEventHandshake()) {
        queueMicrotask(() => {
          if (eventHandlersRef.current.has(handlers) && workbenchEventHandshake()) {
            handlers.onConnected?.();
          }
        });
      }
      if (eventControllerLegRef.current === 'connected') {
        queueMicrotask(() => {
          if (eventHandlersRef.current.has(handlers)) {
            handlers.onEventBridgeStatus?.({ connected: true });
          }
        });
      }
      return () => {
        eventHandlersRef.current.delete(handlers);
        if (eventHandlersRef.current.size === 0) {
          stopWorkbenchEventSource();
        }
      };
    },
    getAgentsGraph: (params) => {
      const search = new URLSearchParams();
      if (params?.window) search.set('window', params.window);
      if (params?.project) search.set('project', params.project);
      if (params?.includeEnded === false) search.set('include_ended', '0');
      if (params?.includeBackground === false) search.set('include_background', '0');
      const qs = search.toString();
      return getJson(qs ? `/api/agents-graph?${qs}` : '/api/agents-graph');
    },
    setSessionVisibility: async (sessionId, visibility) => {
      const session = (await patchJson(`/api/sessions/${encodeURIComponent(sessionId)}`, {
        visibility,
      })) as WorkbenchSession;
      // Single chokepoint: replay the committed PATCH as the same session.activity
      // event sequence the backend emits, through the existing workbench-event
      // pipeline, so the projects tree AND the inbox reconcile via their own
      // reducers even when the SSE stream is down (remote/mobile). Any caller
      // (sidebar hide, graph toggle) inherits this; a real SSE event arriving
      // later is an idempotent no-op.
      for (const event of visibilityActivityEvents({
        sessionId,
        scopeId: session.scope_id,
        title: session.title,
        visibility,
      })) {
        emitLocalSessionActivity(event);
      }
      return session;
    },
    getRunningAgents: async () => {
      const res = await apiFetch('/api/running-agents');
      // 503/504 means controller is down; surface as unreachable instead of throwing.
      if (res.status === 503 || res.status === 504) {
        return { ok: false as const, unreachable: true as const, agents: [], counts: {} };
      }
      if (!res.ok) {
        await handleApiError(res, '/api/running-agents');
      }
      return res.json();
    },
    endRunningAgent: async (payload) => {
      const res = await apiFetch('/api/running-agents/end', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      if (res.status === 503) {
        return { ok: false, unreachable: true };
      }
      // 409 (couldn't end) returns a body with ok:false + error; surface it.
      return res.json().catch(() => ({ ok: res.ok }));
    },
    remoteAccessStatus: () => getJson('/api/remote-access/status'),
    pairVibeCloudRemoteAccess: (payload) => postJson('/api/remote-access/vibe-cloud/pair', payload),
    startRemoteAccess: () => postJson('/api/remote-access/start', {}),
    stopRemoteAccess: () => postJson('/api/remote-access/stop', {}),
    optimizeRemoteAccessRoute: () => postJson('/api/remote-access/optimize-route', {}),
    getRemoteAccessNetworkInterfaces: () => getJson('/api/remote-access/network-interfaces'),
    saveRemoteAccessSettings: (settings) => postJson('/api/remote-access/settings', settings),
    diagnoseRemoteAccess: () => postJson('/api/remote-access/diagnostics', {}),
    getAuthSession: () => getJson('/api/session').then(normalizeSessionInfo),
    signOut: async () => {
      let endpoint: string | undefined;
      try {
        endpoint = (await getExistingWebPushSubscription())?.endpoint;
      } catch {
        // Logout remains authoritative when Push APIs are unavailable.
      }
      return postJson('/auth/logout', {
        device_id: getWebPushDeviceId(),
        ...(endpoint ? { endpoint } : {}),
      });
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }), [sessionDraftPersistence, showToast, t]);

  return <ApiContext.Provider value={value}>{children}</ApiContext.Provider>;
};
