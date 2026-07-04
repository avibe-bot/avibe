import React, { createContext, useContext, useMemo, useRef } from 'react';
import { useTranslation } from 'react-i18next';
import { useToast } from './ToastContext';
import { apiFetch } from '../lib/apiFetch';

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

export type VaultSecret = {
  name: string;
  /** Flat tag list; skill association is a reserved `skill:<name>` tag (see lib/vaultTags). */
  tags: string[];
  kind: string;
  protection: string;
  signer_kind: string | null;
  /** Pinned public key for keypair secrets (non-secret); surfaced so the saved
   *  signing key's public key is recoverable after the create dialog closes. */
  signing_public_key?: { curve?: string; public_key: string } | null;
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
  ttl_seconds?: number;
  this_session_only?: boolean;
  agent_pubkey?: { public_key?: string; fingerprint?: string };
  deks?: Array<{ name: string; dek_blindbox: VaultBlindBox; approval: Record<string, unknown> }>;
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
};

export type ApiContextType = {
  getConfig: () => Promise<any>;
  getPlatformCatalog: () => Promise<any>;
  saveConfig: (payload: any) => Promise<any>;
  getSettings: (platform?: string) => Promise<any>;
  saveSettings: (payload: any, platform?: string) => Promise<any>;
  getUsers: (platform?: string) => Promise<any>;
  saveUsers: (payload: any, platform?: string) => Promise<any>;
  toggleAdmin: (userId: string, isAdmin: boolean, platform?: string) => Promise<any>;
  removeUser: (userId: string, platform?: string) => Promise<any>;
  getShowPages: () => Promise<any>;
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
  setShowPageVisibility: (sessionId: string, visibility: string) => Promise<any>;
  /** Create the session's Show Page if absent; resolves to `{ existed, ... }`. */
  ensureShowPage: (sessionId: string) => Promise<any>;
  rotateShowPageShare: (sessionId: string) => Promise<any>;
  /** Set a custom public link suffix (public pages only); rejects on a taken/invalid id. */
  setShowPageShareId: (sessionId: string, shareId: string) => Promise<any>;
  getBindCodes: () => Promise<any>;
  createBindCode: (type: string, expiresAt?: string) => Promise<any>;
  deleteBindCode: (code: string) => Promise<any>;
  getFirstBindCode: () => Promise<any>;
  detectCli: (binary: string) => Promise<any>;
  installAgent: (name: string) => Promise<InstallResult>;
  listDependencies: () => Promise<DependenciesResult>;
  installDependency: (dep: string) => Promise<InstallResult>;
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
  // drop a stale key without re-signing in. Codex also restarts its
  // persistent daemon so the cleared key takes effect on the next
  // request.
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
  doctor: () => Promise<any>;
  opencodeOptions: (cwd: string) => Promise<any>;
  opencodeSetupPermission: () => Promise<{ ok: boolean; message: string; config_path: string }>;
  opencodePermissionStatus: () => Promise<{ ok: boolean; permission_allowed: boolean; config_path: string }>;
  claudeAgents: (cwd?: string) => Promise<{ ok: boolean; agents?: { id: string; name: string; path: string; source?: string }[]; error?: string }>;
  claudeModels: () => Promise<{ ok: boolean; models?: string[]; reasoning_options?: Record<string, { value: string; label: string }[]>; model_labels?: Record<string, string>; error?: string }>;
  codexAgents: (cwd?: string) => Promise<{ ok: boolean; agents?: { id: string; name: string; path: string; source?: string; description?: string }[]; error?: string }>;
  codexModels: () => Promise<{ ok: boolean; models?: string[]; error?: string }>;
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
  getSession: (sessionId: string, params?: { cache?: boolean }) => Promise<WorkbenchSession>;
  getSessionBootstrap: (sessionId: string) => Promise<WorkbenchSessionBootstrap>;
  updateSession: (sessionId: string, payload: Partial<WorkbenchSessionUpdate>) => Promise<WorkbenchSession>;
  archiveSession: (sessionId: string) => Promise<WorkbenchSession>;
  /** Counts of resources permanently reclaimed when archiving this session
   *  (bound tasks/watches + active runs) — drives the irreversible-confirm dialog. */
  getArchivePreview: (sessionId: string) => Promise<{ tasks: number; watches: number; runs: number; queued: number }>;
  listSessionMessages: (sessionId: string, params?: { afterId?: string; beforeId?: string; aroundId?: string; limit?: number; tail?: boolean; cache?: boolean }) => Promise<{ messages: WorkbenchMessage[]; next_after_id: string | null; next_before_id?: string | null }>;
  // Full-text search over message content across all sessions. Backed by the
  // non-cached GET /api/search/messages (the query string varies per keystroke,
  // so caching would only bloat the read cache). Results group matches by
  // session, sessions ordered most-recent-match first.
  searchMessages: (q: string, opts?: { limit?: number }) => Promise<MessageSearchResult>;
  sendSessionMessage: (sessionId: string, payload: { text?: string; content?: Record<string, unknown>; metadata?: Record<string, unknown>; author_id?: string; author_name?: string }) => Promise<WorkbenchMessage>;
  markSessionRead: (sessionId: string, untilMessageId?: string) => Promise<{ updated: number; unread_counts: Record<string, number>; unread_by_session: Record<string, number> }>;
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
  getTurnState: (sessionId: string) => Promise<{ in_flight: boolean | null }>;
  getSessionDraft: (sessionId: string) => Promise<{ text: string }>;
  setSessionDraft: (sessionId: string, text: string) => Promise<{ ok: boolean }>;
  listInbox: (params?: { platform?: string; unreadOnly?: boolean; limit?: number; before?: string; cache?: boolean }) => Promise<InboxFeedResult>;
  connectWorkbenchEvents: (handlers: WorkbenchEventHandlers, options?: { reconnect?: boolean }) => () => void;
  listVibeAgents: (params?: { backend?: string; includeDisabled?: boolean }) => Promise<{ ok: boolean; agents: VibeAgentBrief[]; default_agent_name: string | null }>;
  getVibeAgent: (name: string) => Promise<{ ok: boolean; agent: VibeAgentFull; default_agent_name: string | null }>;
  createVibeAgent: (payload: VibeAgentCreatePayload) => Promise<{ ok: boolean; agent: VibeAgentFull }>;
  updateVibeAgent: (name: string, payload: VibeAgentUpdatePayload) => Promise<{ ok: boolean; agent: VibeAgentFull }>;
  setDefaultVibeAgent: (name: string) => Promise<{ ok: boolean; default_agent_name: string; agent: VibeAgentBrief }>;
  removeVibeAgent: (name: string) => Promise<{ ok: boolean; code?: string; message?: string; references?: Record<string, number>; removed_agent?: string }>;
  listVaultSecrets: () => Promise<{ ok: boolean; secrets: VaultSecret[] }>;
  getVaultVmk: () => Promise<VaultVmkResult>;
  getVaultPubkey: () => Promise<{ ok: boolean; public_key: string; fingerprint: string }>;
  getVaultAgentPubkey: () => Promise<{ ok: boolean; public_key: string; fingerprint: string }>;
  createVaultSecret: (payload: VaultCreatePayload, opts?: { handleError?: boolean }) => Promise<{ ok: boolean; secret?: VaultSecret; code?: string; message?: string }>;
  deleteVaultSecret: (name: string) => Promise<{ ok: boolean; removed?: boolean; code?: string; message?: string }>;
  getVaultProvisionRequest: (
    name: string,
    opts?: { handleError?: boolean },
  ) => Promise<{ ok: boolean; request: VaultRequest | null; ambiguous?: boolean }>;
  getVaultProvisionRequestById: (requestId: string, opts?: { handleError?: boolean }) => Promise<{ ok: boolean; request: VaultRequest | null }>;
  getVaultRequests: (params?: { status?: string; type?: string; limit?: number }, opts?: { handleError?: boolean }) => Promise<{ ok: boolean; requests: VaultRequest[] }>;
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
  listHarnessTasks: (params?: HarnessDefinitionsParams) => Promise<HarnessTasksResult>;
  setHarnessTaskEnabled: (taskId: string, enabled: boolean) => Promise<{ ok: boolean; task?: HarnessTask }>;
  deleteHarnessTask: (taskId: string) => Promise<{ ok: boolean; id?: string }>;
  listHarnessWatches: (params?: HarnessDefinitionsParams) => Promise<HarnessWatchesResult>;
  setHarnessWatchEnabled: (watchId: string, enabled: boolean) => Promise<{ ok: boolean; watch?: HarnessWatch }>;
  deleteHarnessWatch: (watchId: string) => Promise<{ ok: boolean; id?: string }>;
  listHarnessRuns: (params?: HarnessRunsParams) => Promise<HarnessRunsResult>;
  getHarnessRun: (runId: string) => Promise<{ ok: boolean; run: HarnessRun }>;
  getRunningAgents: () => Promise<RunningAgentsResult>;
  endRunningAgent: (payload: {
    backend?: string | null;
    state?: string | null;
    session_id?: string | null;
    composite_key?: string | null;
    base_session_id?: string | null;
    pid?: number | null;
  }) => Promise<{ ok: boolean; unreachable?: boolean; error?: string; action?: string }>;
  remoteAccessStatus: () => Promise<any>;
  pairVibeCloudRemoteAccess: (payload: { backend_url: string; pairing_key: string; device_name?: string }) => Promise<any>;
  startRemoteAccess: () => Promise<any>;
  stopRemoteAccess: () => Promise<any>;
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
};

// One Vibe Agent row from ``/agents`` (brief view used in list rendering).
// ``source`` distinguishes system-builtin agents from user-created ones —
// system agents lock the ``backend`` field and refuse delete, but their
// model / effort / system_prompt / enabled state are still editable.
export type VibeAgentBrief = {
  id: string;
  name: string;
  description: string | null;
  backend: string;
  model: string | null;
  reasoning_effort: string | null;
  enabled: boolean;
  source: string;
  updated_at: string;
};

export type VibeAgentFull = VibeAgentBrief & {
  system_prompt: string | null;
  metadata: Record<string, unknown>;
  created_at: string;
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
  onConnected?: (data: { sub_id: number }) => void;
  onMessageNew?: (data: WorkbenchMessage) => void;
  onSessionActivity?: (data: { session_id: string; scope_id: string | null; event: string; title?: string | null }) => void;
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
  // First-class message type: 'user' | 'assistant' | 'tool_call' | 'notify' |
  // 'result'. Distinct from the coarse author — the chat renders 'notify' as a
  // terminal status marker, and the inbox previews 'result' only.
  type: 'user' | 'assistant' | 'tool_call' | 'notify' | 'result' | string;
  // Origin of the message, distinct from the coarse ``author`` role: a
  // harness-triggered prompt is author='user' but source='harness'. Drives the
  // transcript's "Scheduled task" / "Watch" provenance tag.
  source: 'user' | 'agent' | 'harness' | string | null;
  author_id: string | null;
  author_name: string | null;
  native_message_id: string | null;
  parent_native_message_id: string | null;
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

// A single matching message within a session group. ``type`` is the coarse
// chat role the row chip renders ('user' → YOU, otherwise AGENT); ``source``
// carries provenance (harness/user/agent) like WorkbenchMessage.
export type MessageSearchMatch = {
  id: string;
  author: string;
  source: string | null;
  type: 'user' | 'result' | string;
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
  matches: MessageSearchMatch[];
};

export type MessageSearchResult = {
  sessions: MessageSearchSession[];
  total: number;
  session_count: number;
};

export type WorkbenchSessionBootstrap = {
  session: WorkbenchSession;
  agents: VibeAgentBrief[];
  default_agent_name: string | null;
  config: any | null;
  messages: WorkbenchMessage[];
  next_after_id: string | null;
  next_before_id?: string | null;
  queued: WorkbenchMessage[];
  draft: { text: string };
  turn_state: { in_flight: boolean | null };
};

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
// workbench session carries a title and is linkable to its chat; an IM session
// resolves to its platform + channel display name and is not linkable.
export type HarnessSessionSummary = {
  session_title: string | null;
  session_platform: string | null;
  session_scope_kind: string | null;
  session_label: string | null;
  session_is_workbench: boolean;
};

export type HarnessTask = HarnessSessionSummary & {
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
  next_run_at: string | null;
};

export type HarnessWatchRuntime = {
  running: boolean;
  pid?: number | null;
  started_at?: string | null;
  updated_at?: string | null;
};

export type HarnessWatch = HarnessSessionSummary & {
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
  last_event_at: string | null;
  last_error: string | null;
  last_exit_code: number | null;
  runtime: HarnessWatchRuntime;
};

export type HarnessRunStatus = 'queued' | 'running' | 'succeeded' | 'failed' | 'canceled' | (string & {});

export type HarnessDefinitionStatus = 'all' | 'enabled' | 'disabled';

export type HarnessDefinitionCounts = {
  all: number;
  enabled: number;
  disabled: number;
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

export type HarnessRun = {
  id: string;
  request_type: string | null;
  run_type: string | null;
  status: HarnessRunStatus;
  definition_id: string | null;
  task_id: string | null;
  source_kind: string | null;
  source_actor: string | null;
  parent_run_id: string | null;
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
};

export type HarnessCountsResult = {
  tasks: HarnessDefinitionCounts;
  watches: HarnessDefinitionCounts;
  runs: HarnessRunCounts;
};

export type HarnessBootstrapParams = {
  tab?: 'tasks' | 'watches' | 'runs';
  status?: HarnessDefinitionStatus | HarnessRunStatus;
  query?: string;
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

export type SessionInfo =
  | { remote: false }
  | { remote: true; authenticated: false }
  // sub is the stable OIDC subject; prefer it over email for per-account scoping (email can
  // be absent or shared across subjects).
  | { remote: true; authenticated: true; email: string; sub?: string };

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
  status?: 'running' | 'succeeded' | 'failed';
};

export type DependencyItem = {
  id: string;
  kind: 'tool' | 'runtime' | 'node';
  required: boolean;
  installed: boolean;
  version: string | null;
  status: 'ready' | 'missing' | 'upgrade_required';
};

export type DependenciesResult = { ok: boolean; deps: DependencyItem[] };

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
  settings_env_base_url: string | null;
  settings_conflict: boolean;
  message?: string;
};

export type ClaudeAuthPayload = {
  auth_mode: ClaudeAuthMode;
  api_key?: string | null;
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

export type WebPushStatus = {
  ok: boolean;
  configured: boolean;
  public_key: string;
  subscription_count: number;
  current_subscription_enabled?: boolean;
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
  const eventSourceRef = useRef<EventSource | null>(null);
  const eventHandlersRef = useRef(new Set<WorkbenchEventHandlers>());
  const eventConnectionRef = useRef<{ sub_id: number } | null>(null);

  const handleApiError = async (res: Response, path: string) => {
    let errorMessage = `Request failed: ${path} (${res.status})`;
    let errorCode: string | null = null;
    
    try {
      const data = await res.json();
      // Accept the legacy ``error`` shape (string code or ``{ code, message }``) AND the
      // top-level ``{ code, message }`` shape newer routes use (e.g. /api/vault/*), so callers
      // always get a real ``ApiError.code`` to branch on instead of a generic status string.
      const rawErr = data.error ?? (data.code ? { code: data.code, message: data.message } : undefined);
      if (rawErr) {
        // Localize by code, falling back to the server-provided message so we never render a
        // key like ``errors.[object Object]``.
        const code = typeof rawErr === 'string' ? rawErr : rawErr?.code;
        const fallback =
          typeof rawErr === 'string' ? rawErr : rawErr?.message ?? rawErr?.code ?? errorMessage;
        errorCode = typeof code === 'string' ? code : null;
        errorMessage = errorCode ? t(`errors.${errorCode}`, { defaultValue: fallback }) : fallback;
      }
    } catch {
      // Response is not JSON, use status text
      errorMessage = `${path}: ${res.statusText || 'Unknown error'} (${res.status})`;
    }

    // Log error details to console
    console.error(`[API Error] ${path}`, {
      status: res.status,
      statusText: res.statusText,
      error: errorMessage,
    });

    // Show toast to user
    showToast(errorMessage, 'error');

    throw new ApiError(errorMessage, res.status, errorCode);
  };

  const getJson = async (path: string, { handleError = true }: { handleError?: boolean } = {}) => {
    const res = await apiFetch(path);
    if (!res.ok && handleError) {
      await handleApiError(res, path);
    }
    return res.json();
  };

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

  const parseWorkbenchEnvelope = <T,>(raw: string): WorkbenchEventEnvelope<T> | null => {
    try {
      return JSON.parse(raw) as WorkbenchEventEnvelope<T>;
    } catch (err) {
      console.error('[workbench-events] parse failed', err, raw);
      return null;
    }
  };

  const closeWorkbenchEventSource = () => {
    eventSourceRef.current?.close();
    eventSourceRef.current = null;
    eventConnectionRef.current = null;
  };

  const ensureWorkbenchEventSource = (options?: { reconnect?: boolean }) => {
    if (options?.reconnect) {
      closeWorkbenchEventSource();
    }
    if (eventSourceRef.current) return;

    const source = new EventSource('/api/events');
    eventSourceRef.current = source;
    source.addEventListener('connected', (e: MessageEvent) => {
      try {
        eventConnectionRef.current = JSON.parse(e.data) as { sub_id: number };
      } catch (err) {
        console.error('[workbench-events] connected parse failed', err, e.data);
        eventConnectionRef.current = null;
      }
      if (eventConnectionRef.current) {
        const connected = eventConnectionRef.current;
        dispatchToWorkbenchHandlers((handlers) => handlers.onConnected?.(connected));
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
    source.onerror = (err) => {
      dispatchToWorkbenchHandlers((handlers) => handlers.onError?.(err));
    };
  };

  const requestJson = async (
    path: string,
    init: RequestInit,
    errorPath = path,
    { clearCache = true, handleError = true }: { clearCache?: boolean; handleError?: boolean } = {},
  ) => {
    const res = await apiFetch(path, init);
    if (!res.ok && handleError) {
      await handleApiError(res, errorPath);
    }
    const payloadJson = await res.json().catch(() => ({}));
    if (res.ok && clearCache) {
      clearReadCache();
    }
    return { res, payloadJson };
  };

  const postJson = async (path: string, payload: any, opts?: { handleError?: boolean }) => {
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

  const patchJson = async (path: string, payload: any) => {
    const { payloadJson } = await requestJson(path, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
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
    saveConfig: (payload) => postJson('/api/config', payload),
    getSettings: (platform) => getJson(platform ? `/api/settings?platform=${encodeURIComponent(platform)}` : '/api/settings'),
    saveSettings: (payload, platform) => postJson('/api/settings', platform ? { ...payload, platform } : payload),
    getUsers: (platform) => getJson(platform ? `/api/users?platform=${encodeURIComponent(platform)}` : '/api/users'),
    saveUsers: (payload, platform) => postJson('/api/users', platform ? { ...payload, platform } : payload),
    toggleAdmin: (userId, isAdmin, platform) => postJson(`/api/users/${encodeURIComponent(userId)}/admin`, platform ? { is_admin: isAdmin, platform } : { is_admin: isAdmin }),
    removeUser: (userId, platform) =>
      deleteJson(platform ? `/api/users/${encodeURIComponent(userId)}?platform=${encodeURIComponent(platform)}` : `/api/users/${encodeURIComponent(userId)}`),
    getShowPages: () => getJson('/api/show-pages'),
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
    setShowPageVisibility: (sessionId, visibility) => postJson(`/api/show-pages/${encodeURIComponent(sessionId)}/visibility`, { visibility }),
    ensureShowPage: (sessionId) => postJson(`/api/show-pages/${encodeURIComponent(sessionId)}/ensure`, {}),
    rotateShowPageShare: (sessionId) => postJson(`/api/show-pages/${encodeURIComponent(sessionId)}/rotate-share`, {}),
    setShowPageShareId: (sessionId, shareId) => postJson(`/api/show-pages/${encodeURIComponent(sessionId)}/share-id`, { share_id: shareId }),
    getBindCodes: () => getJson('/api/bind-codes'),
    createBindCode: (type, expiresAt) => postJson('/api/bind-codes', { type, expires_at: expiresAt }),
    deleteBindCode: (code) => deleteJson(`/api/bind-codes/${encodeURIComponent(code)}`),
    getFirstBindCode: () => getJson('/api/setup/first-bind-code'),
    detectCli: (binary) => getJson(`/api/cli/detect?binary=${encodeURIComponent(binary)}`),
    installAgent: (name) => startAndPollAgentInstall(name),
    listDependencies: () => getJson('/api/dependencies'),
    installDependency: (dep) => startAndPollDependencyInstall(dep),
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
    doctor: () => postJson('/api/doctor', {}),
    opencodeOptions: (cwd) => postJson('/api/opencode/options', { cwd }),
    opencodeSetupPermission: () => postJson('/api/opencode/setup-permission', {}),
    opencodePermissionStatus: () => getJson('/api/opencode/permission-status'),
    claudeAgents: (cwd) => cwd ? getJson(`/api/claude/agents?cwd=${encodeURIComponent(cwd)}`) : getJson('/api/claude/agents'),
    claudeModels: () => getJson('/api/claude/models'),
    codexAgents: (cwd) => cwd ? getJson(`/api/codex/agents?cwd=${encodeURIComponent(cwd)}`) : getJson('/api/codex/agents'),
    codexModels: () => getJson('/api/codex/models'),
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
        ? getJson(`/api/sessions/${encodeURIComponent(sessionId)}`)
        : getCachedJson(`/api/sessions/${encodeURIComponent(sessionId)}`),
    getSessionBootstrap: (sessionId) =>
      getJson(`/api/sessions/${encodeURIComponent(sessionId)}/bootstrap`),
    updateSession: async (sessionId, payload) => {
      const { payloadJson } = await requestJson(`/api/sessions/${encodeURIComponent(sessionId)}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      }, `PATCH /api/sessions/${sessionId}`);
      return payloadJson;
    },
    archiveSession: (sessionId) => deleteJson(`/api/sessions/${encodeURIComponent(sessionId)}`),
    getArchivePreview: (sessionId) =>
      getJson(`/api/sessions/${encodeURIComponent(sessionId)}/archive-preview`),
    listSessionMessages: (sessionId, params) => {
      const search = new URLSearchParams();
      if (params?.afterId) search.set('after_id', params.afterId);
      if (params?.beforeId) search.set('before_id', params.beforeId);
      if (params?.aroundId) search.set('around_id', params.aroundId);
      if (params?.limit) search.set('limit', String(params.limit));
      if (params?.tail) search.set('tail', '1');
      const qs = search.toString();
      const base = `/api/sessions/${encodeURIComponent(sessionId)}/messages`;
      const path = qs ? `${base}?${qs}` : base;
      return params?.cache === false ? getJson(path) : getCachedJson(path);
    },
    searchMessages: (q, opts) => {
      const search = new URLSearchParams();
      search.set('q', q);
      if (opts?.limit) search.set('limit', String(opts.limit));
      return getJson(`/api/search/messages?${search.toString()}`);
    },
    sendSessionMessage: (sessionId, payload) =>
      postJson(`/api/sessions/${encodeURIComponent(sessionId)}/messages`, payload),
    markSessionRead: (sessionId, untilMessageId) =>
      postJson(
        `/api/sessions/${encodeURIComponent(sessionId)}/mark-read`,
        untilMessageId ? { until_message_id: untilMessageId } : {},
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
    getTurnState: async (sessionId) => {
      const path = `/api/sessions/${encodeURIComponent(sessionId)}/turn-state`;
      const res = await apiFetch(path);
      if (res.status === 504) {
        readCacheRef.current.delete(path);
        return { in_flight: null };
      }
      if (!res.ok) {
        await handleApiError(res, path);
      }
      return res.json();
    },
    getSessionDraft: (sessionId) => getCachedJson(`/api/sessions/${encodeURIComponent(sessionId)}/draft`),
    setSessionDraft: async (sessionId, text) => {
      const { res, payloadJson } = await requestJson(`/api/sessions/${encodeURIComponent(sessionId)}/draft`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text }),
      }, `/api/sessions/${sessionId}/draft`, { handleError: false });
      return res.ok ? payloadJson : { ok: false };
    },
    listInbox: (params) => {
      const search = new URLSearchParams();
      if (params?.platform) search.set('platform', params.platform);
      if (params?.unreadOnly) search.set('unread_only', '1');
      if (params?.limit) search.set('limit', String(params.limit));
      if (params?.before) search.set('before', params.before);
      const qs = search.toString();
      const path = qs ? `/api/inbox?${qs}` : '/api/inbox';
      return params?.cache === false ? getJson(path) : getCachedJson(path);
    },
    listVibeAgents: (params) => {
      const search = new URLSearchParams();
      if (params?.backend) search.set('backend', params.backend);
      if (params?.includeDisabled) search.set('include_disabled', '1');
      const qs = search.toString();
      return getCachedJson(qs ? `/api/agents?${qs}` : '/api/agents', 5_000);
    },
    getVibeAgent: (name) => getCachedJson(`/api/agents/${encodeURIComponent(name)}`, 5_000),
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
    createVaultSecret: (payload, opts) => postJson('/api/vault/secrets', payload, opts),
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
      if (params?.query) search.set('query', params.query);
      if (params?.page) search.set('page', String(params.page));
      if (params?.limit) search.set('limit', String(params.limit));
      const qs = search.toString();
      return getCachedJson(qs ? `/api/harness/bootstrap?${qs}` : '/api/harness/bootstrap');
    },
    listHarnessTasks: (params) => {
      const search = new URLSearchParams();
      if (params?.status) search.set('status', params.status);
      if (params?.query) search.set('query', params.query);
      if (params?.page) search.set('page', String(params.page));
      if (params?.limit) search.set('limit', String(params.limit));
      const qs = search.toString();
      return getCachedJson(qs ? `/api/harness/tasks?${qs}` : '/api/harness/tasks');
    },
    setHarnessTaskEnabled: (taskId, enabled) =>
      patchJson(`/api/harness/tasks/${encodeURIComponent(taskId)}`, { enabled }),
    deleteHarnessTask: (taskId) => deleteJson(`/api/harness/tasks/${encodeURIComponent(taskId)}`),
    listHarnessWatches: (params) => {
      const search = new URLSearchParams();
      if (params?.status) search.set('status', params.status);
      if (params?.query) search.set('query', params.query);
      if (params?.page) search.set('page', String(params.page));
      if (params?.limit) search.set('limit', String(params.limit));
      const qs = search.toString();
      return getCachedJson(qs ? `/api/harness/watches?${qs}` : '/api/harness/watches');
    },
    setHarnessWatchEnabled: (watchId, enabled) =>
      patchJson(`/api/harness/watches/${encodeURIComponent(watchId)}`, { enabled }),
    deleteHarnessWatch: (watchId) => deleteJson(`/api/harness/watches/${encodeURIComponent(watchId)}`),
    listHarnessRuns: (params) => {
      const search = new URLSearchParams();
      if (params?.status) search.set('status', params.status);
      if (params?.runType) search.set('run_type', params.runType);
      if (params?.agentName) search.set('agent_name', params.agentName);
      if (params?.definitionId) search.set('definition_id', params.definitionId);
      if (params?.query) search.set('query', params.query);
      if (params?.page) search.set('page', String(params.page));
      if (params?.limit) search.set('limit', String(params.limit));
      const qs = search.toString();
      return getCachedJson(qs ? `/api/harness/runs?${qs}` : '/api/harness/runs');
    },
    getHarnessRun: (runId) => getCachedJson(`/api/harness/runs/${encodeURIComponent(runId)}`),
    connectWorkbenchEvents: (handlers, options) => {
      eventHandlersRef.current.add(handlers);
      ensureWorkbenchEventSource(options);
      if (eventConnectionRef.current) {
        queueMicrotask(() => {
          if (eventHandlersRef.current.has(handlers) && eventConnectionRef.current) {
            handlers.onConnected?.(eventConnectionRef.current);
          }
        });
      }
      return () => {
        eventHandlersRef.current.delete(handlers);
        if (eventHandlersRef.current.size === 0) {
          closeWorkbenchEventSource();
        }
      };
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
    getAuthSession: () => getJson('/api/session'),
    signOut: () => postJson('/auth/logout', {}),
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }), [showToast, t]);

  return <ApiContext.Provider value={value}>{children}</ApiContext.Provider>;
};
