// Model Hub API client. Presents ONE typed surface to the UI; internally it
// either serves in-memory fixtures (mock mode, default while L2 is unmerged) or
// calls the frozen `/api/models/*` REST endpoints (live mode). Components never
// branch on the mode — flip `MODELS_API_MODE` in featureFlags.ts to switch.
//
// Methods unwrap the frozen envelope ({ok:true, …} | {ok:false, error}) and
// throw an Error carrying the machine code on failure, so callers work with
// plain domain objects.
import { apiFetch } from '@/lib/apiFetch';
import { MODELS_API_MODE } from './featureFlags';
import {
  buildMockAgents,
  buildMockEvents,
  buildMockMigration,
  buildMockPriority,
  buildMockRuntime,
  buildMockSources,
  mockDiscoveredCount,
} from './mockData';
import type {
  AgentBackend,
  AgentMapping,
  AgentMenu,
  AgentMode,
  AgentSupply,
  ApiKeySourceCreate,
  CustomModelCreate,
  MigrationApplyResult,
  MigrationScan,
  OAuthFlow,
  OAuthSourceCreate,
  Priority,
  ResolutionEvent,
  RuntimeDependency,
  Source,
  SourcePatch,
  SupplyChannel,
} from './types';
import { CONTRACT_VERSION } from './types';

export type ModelsApi = {
  listSources(): Promise<Source[]>;
  createApiKeySource(draft: ApiKeySourceCreate): Promise<Source>;
  /** Finalize a completed subscription OAuth flow into a persisted Source. */
  createOAuthSource(draft: OAuthSourceCreate): Promise<Source>;
  /** Rename / re-point a source (display_name, base_url). */
  patchSource(id: string, patch: SourcePatch): Promise<Source>;
  /** Re-run discovery on a hub source; resolves with the discovered count. */
  testSource(id: string): Promise<number>;
  /** Delete a source. `force` overrides the only-supplier guard. */
  deleteSource(id: string, force?: boolean): Promise<void>;
  putPriority(order: string[]): Promise<Priority>;
  listAgents(): Promise<AgentSupply[]>;
  setAgentMode(backend: AgentBackend, mode: AgentMode): Promise<AgentSupply>;
  putMappings(backend: AgentBackend, mappings: AgentMapping[]): Promise<AgentSupply>;
  putMenu(menu: AgentMenu): Promise<AgentSupply>;
  addCustomModel(draft: CustomModelCreate): Promise<Source>;
  deleteCustomModel(sourceId: string, modelId: string): Promise<Source>;
  scanMigration(): Promise<MigrationScan>;
  applyMigration(itemIds: string[]): Promise<MigrationApplyResult>;
  listEvents(limit?: number): Promise<ResolutionEvent[]>;
  getRuntimeStatus(): Promise<RuntimeDependency>;
  startOAuth(vendor: string, channel: SupplyChannel): Promise<OAuthFlow>;
  getOAuthStatus(flowId: string): Promise<OAuthFlow>;
  submitOAuth(flowId: string, value: string): Promise<OAuthFlow>;
  cancelOAuth(flowId: string): Promise<void>;
};

const isLive = () => MODELS_API_MODE === 'live';

// ── Live client ─────────────────────────────────────────────────────────
class ApiCallError extends Error {
  code: string;
  detail?: string;
  constructor(code: string, detail?: string) {
    super(detail || code);
    this.name = 'ApiCallError';
    this.code = code;
    this.detail = detail;
  }
}

async function call<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await apiFetch(path, init);
  let payload: any = null;
  try {
    payload = await res.json();
  } catch {
    throw new ApiCallError('bad_response', `Non-JSON response from ${path}`);
  }
  if (!res.ok || payload?.ok === false) {
    throw new ApiCallError(payload?.error || `http_${res.status}`, payload?.detail);
  }
  return payload as T;
}

const jsonInit = (method: string, body?: unknown): RequestInit => ({
  method,
  headers: { 'Content-Type': 'application/json' },
  body: body === undefined ? undefined : JSON.stringify(body),
});

const liveApi: ModelsApi = {
  listSources: () => call<{ sources: Source[] }>('/api/models/sources').then((r) => r.sources),
  createApiKeySource: (draft) => call<{ source?: Source } & Source>('/api/models/sources', jsonInit('POST', draft)).then((r) => (r.source ?? r) as Source),
  createOAuthSource: (draft) => call<{ source?: Source } & Source>('/api/models/sources', jsonInit('POST', draft)).then((r) => (r.source ?? r) as Source),
  patchSource: (id, patch) => call<{ source?: Source } & Source>(`/api/models/sources/${encodeURIComponent(id)}`, jsonInit('PATCH', patch)).then((r) => (r.source ?? r) as Source),
  testSource: (id) => call<{ discovered: number }>(`/api/models/sources/${encodeURIComponent(id)}/test`, jsonInit('POST')).then((r) => r.discovered),
  deleteSource: (id, force) => call(`/api/models/sources/${encodeURIComponent(id)}${force ? '?force=1' : ''}`, jsonInit('DELETE')).then(() => undefined),
  putPriority: (order) => call<{ priority?: Priority } & Priority>('/api/models/priority', jsonInit('PUT', { contract_version: CONTRACT_VERSION, order })).then((r) => (r.priority ?? r) as Priority),
  listAgents: () => call<{ agents: AgentSupply[] }>('/api/models/agents').then((r) => r.agents),
  setAgentMode: (backend, mode) => call<{ agent?: AgentSupply } & AgentSupply>(`/api/models/agents/${backend}/mode`, jsonInit('PATCH', { mode })).then((r) => (r.agent ?? r) as AgentSupply),
  putMappings: (backend, mappings) => call<{ agent?: AgentSupply } & AgentSupply>(`/api/models/agents/${backend}/mappings`, jsonInit('PUT', { mappings })).then((r) => (r.agent ?? r) as AgentSupply),
  putMenu: (menu) => call<{ agent?: AgentSupply } & AgentSupply>('/api/models/agents/opencode/menu', jsonInit('PUT', { menu })).then((r) => (r.agent ?? r) as AgentSupply),
  addCustomModel: (draft) => call<{ source?: Source } & Source>('/api/models/custom-models', jsonInit('POST', draft)).then((r) => (r.source ?? r) as Source),
  deleteCustomModel: (sourceId, modelId) => call<{ source?: Source } & Source>('/api/models/custom-models', jsonInit('DELETE', { source_id: sourceId, model_id: modelId })).then((r) => (r.source ?? r) as Source),
  scanMigration: () => call<{ scan?: MigrationScan } & MigrationScan>('/api/models/migration/scan', jsonInit('POST')).then((r) => (r.scan ?? r) as MigrationScan),
  applyMigration: (itemIds) => call<MigrationApplyResult>('/api/models/migration/apply', jsonInit('POST', { item_ids: itemIds })),
  listEvents: (limit = 20) => call<{ events: ResolutionEvent[] }>(`/api/models/events?limit=${limit}`).then((r) => r.events),
  getRuntimeStatus: () => call<{ runtime?: RuntimeDependency } & RuntimeDependency>('/api/models/runtime/status').then((r) => (r.runtime ?? r) as RuntimeDependency),
  startOAuth: (vendor, channel) => call<{ flow?: OAuthFlow } & OAuthFlow>('/api/models/oauth/start', jsonInit('POST', { vendor, channel })).then((r) => (r.flow ?? r) as OAuthFlow),
  getOAuthStatus: (flowId) => call<{ flow?: OAuthFlow } & OAuthFlow>(`/api/models/oauth/status/${encodeURIComponent(flowId)}`).then((r) => (r.flow ?? r) as OAuthFlow),
  submitOAuth: (flowId, value) => call<{ flow?: OAuthFlow } & OAuthFlow>('/api/models/oauth/submit', jsonInit('POST', { flow_id: flowId, value })).then((r) => (r.flow ?? r) as OAuthFlow),
  cancelOAuth: (flowId) => call('/api/models/oauth/cancel', jsonInit('POST', { flow_id: flowId })).then(() => undefined),
};

// ── Mock client ─────────────────────────────────────────────────────────
// A single mutable store so reorder / add / mode-switch stick across calls
// within a session, giving a realistic demo without a backend.
type MockFlow = { flow: OAuthFlow; polls: number; submitted: boolean };

const rid = (prefix: string) => `${prefix}_${Math.random().toString(36).slice(2, 10)}`;
const delay = <T>(value: T, ms = 260): Promise<T> => new Promise((r) => setTimeout(() => r(value), ms));

class MockStore {
  sources = buildMockSources();
  priority = buildMockPriority();
  agents = buildMockAgents();
  events = buildMockEvents();
  runtime = buildMockRuntime();
  flows = new Map<string, MockFlow>();

  private ordered(): Source[] {
    const byId = new Map(this.sources.map((s) => [s.id, s]));
    const ranked = this.priority.order.map((id) => byId.get(id)).filter((s): s is Source => Boolean(s));
    const extras = this.sources.filter((s) => !this.priority.order.includes(s.id));
    return [...ranked, ...extras];
  }

  listSources() {
    return delay(structuredClone(this.ordered()));
  }

  createApiKeySource(draft: ApiKeySourceCreate) {
    const count = mockDiscoveredCount(draft.vendor);
    const source: Source = {
      id: rid('src'),
      kind: 'api_key',
      vendor: draft.vendor,
      display_name: draft.vendor === 'custom' ? hostLabel(draft.base_url) : vendorLabel(draft.vendor),
      protocol: draft.vendor === 'anthropic' ? 'anthropic' : 'openai_compatible',
      base_url: draft.base_url ?? null,
      supply_channel: 'hub',
      billing: 'metered',
      state: { status: 'standby', retry_at: null, detail_key: null },
      usage: { cycle_used_pct: null, month_spend_cents: 0, currency: 'CNY' },
      account_label: null,
      // Simulates L2 computing the display mask once at provisioning.
      masked_credential: maskKey(draft.key),
      models: Array.from({ length: count }, (_, i) => ({
        id: `${draft.vendor}-model-${i + 1}`,
        display_name: null,
        provenance: 'discovered' as const,
        discovered_at: new Date().toISOString(),
      })),
      credential_ref: rid('cred'),
    };
    this.sources.push(source);
    this.priority.order.push(source.id);
    return delay(structuredClone(source), 900); // simulate probe latency
  }

  deleteSource(id: string, force = false) {
    // Mirror the server's only-supplier guard (mode_switch_blocked): a source
    // currently bound as some agent's supply can't be dropped without force.
    if (!force && this.agents.some((a) => a.current?.source_id === id)) {
      throw new ApiCallError('mode_switch_blocked');
    }
    this.sources = this.sources.filter((s) => s.id !== id);
    this.priority.order = this.priority.order.filter((x) => x !== id);
    for (const a of this.agents) if (a.current?.source_id === id) a.current = null;
    return delay(undefined);
  }

  putPriority(order: string[]) {
    // Server echoes the authoritative full order (every non-deleted source once).
    const known = new Set(this.sources.map((s) => s.id));
    const cleaned = order.filter((id) => known.has(id));
    const missing = this.sources.map((s) => s.id).filter((id) => !cleaned.includes(id));
    this.priority = { contract_version: CONTRACT_VERSION, order: [...cleaned, ...missing] };
    return delay(structuredClone(this.priority));
  }

  listAgents() {
    return delay(structuredClone(this.agents));
  }

  setAgentMode(backend: AgentBackend, mode: AgentMode) {
    const agent = this.agents.find((a) => a.backend === backend);
    if (!agent) throw new ApiCallError('source_not_found');
    agent.mode = mode;
    if (mode === 'direct') {
      agent.current = null;
    } else if (!agent.current) {
      const top = this.ordered().find((s) => s.state.status !== 'error');
      agent.current = top
        ? { model_id: top.models[0]?.id ?? 'unknown', source_id: top.id, channel: top.supply_channel }
        : null;
    }
    return delay(structuredClone(agent));
  }

  putMappings(backend: AgentBackend, mappings: AgentMapping[]) {
    const agent = this.agents.find((a) => a.backend === backend);
    if (!agent) throw new ApiCallError('source_not_found');
    agent.mappings = mappings;
    return delay(structuredClone(agent));
  }

  putMenu(menu: AgentMenu) {
    const agent = this.agents.find((a) => a.backend === 'opencode');
    if (!agent) throw new ApiCallError('source_not_found');
    agent.menu = menu;
    return delay(structuredClone(agent));
  }

  addCustomModel(draft: CustomModelCreate) {
    const source = this.sources.find((s) => s.id === draft.source_id);
    if (!source) throw new ApiCallError('source_not_found');
    const existing = source.models.find((m) => m.id === draft.model_id);
    if (existing) {
      existing.display_name = draft.display_name ?? existing.display_name;
      existing.provenance = 'manual';
    } else {
      source.models.push({
        id: draft.model_id,
        display_name: draft.display_name ?? null,
        provenance: 'manual',
        discovered_at: null,
      });
    }
    return delay(structuredClone(source), 400);
  }

  deleteCustomModel(sourceId: string, modelId: string) {
    const source = this.sources.find((s) => s.id === sourceId);
    if (!source) throw new ApiCallError('source_not_found');
    source.models = source.models.filter((m) => !(m.id === modelId && m.provenance === 'manual'));
    return delay(structuredClone(source));
  }

  scanMigration() {
    // Read-only: recompute the fixture each call so re-scans stay idempotent.
    return delay(buildMockMigration(), 500);
  }

  applyMigration(itemIds: string[]) {
    const scan = buildMockMigration();
    // reauth needs the interactive OAuth flow, so it is never bulk-applied here.
    const chosen = scan.items.filter((i) => itemIds.includes(i.id) && i.proposed_action !== 'reauth');
    // Copy-only: each selected native config materializes a new source; the
    // (simulated) originals are never touched. import lands on the hub channel;
    // keep_native registers a sanctioned native_cli source. (controlled_import
    // is deferred per the 2026-07-23 L6 finding, so it's never emitted here.)
    for (const item of chosen) {
      const isKey = item.kind === 'api_key' || item.kind === 'opencode_provider';
      const channel: SupplyChannel = item.proposed_action === 'keep_native' ? 'native_cli' : 'hub';
      this.sources.push({
        id: rid('src'),
        kind: isKey ? 'api_key' : 'subscription',
        vendor: item.backend === 'opencode' ? 'zhipuai' : item.backend === 'codex' ? 'openai' : 'anthropic',
        display_name: item.masked_detail.split(' · ')[0] || 'Imported',
        protocol: item.backend === 'codex' ? 'openai_responses' : 'anthropic',
        base_url: null,
        supply_channel: channel,
        // No hub-held subscription is created by migration, so never consented.
        experimental_consent_at: null,
        billing: isKey ? 'metered' : 'monthly',
        state: { status: 'standby', retry_at: null, detail_key: null },
        usage: isKey ? { cycle_used_pct: null, month_spend_cents: 0, currency: 'CNY' } : { cycle_used_pct: 0, month_spend_cents: null, currency: null },
        account_label: channel === 'native_cli' ? 'me@gmail.com' : null,
        masked_credential: isKey ? 'sk-…dd3c' : null,
        models: [{ id: item.backend === 'opencode' ? 'glm-5.2' : item.backend === 'codex' ? 'gpt-5.6' : 'claude-opus-4-6', display_name: null, provenance: 'discovered', discovered_at: new Date().toISOString() }],
        credential_ref: channel === 'hub' ? rid('cred') : null,
      });
    }
    // Enable hub on the backends that received a hub-channel import.
    for (const backend of new Set(chosen.filter((i) => i.proposed_action !== 'keep_native').map((i) => i.backend))) {
      const agent = this.agents.find((a) => a.backend === backend);
      if (agent) agent.mode = 'hub';
    }
    this.priority.order = this.sources.map((s) => s.id);
    return delay({ applied: chosen.length, sources: structuredClone(this.ordered()) }, 700);
  }

  listEvents(limit = 20) {
    return delay(structuredClone(this.events.slice(0, limit)));
  }

  getRuntimeStatus() {
    return delay(structuredClone(this.runtime));
  }

  startOAuth(vendor: string, channel: SupplyChannel) {
    const isDevice = vendor === 'openai';
    const flow: OAuthFlow = {
      flow_id: rid('oaf'),
      // Deterministic pending-source binding (schema: hub flows always set it),
      // consumed by createOAuthSource on finalize — mirrors the server, where
      // create_source assigns source.id = flow.source_id.
      source_id: rid('src'),
      vendor,
      channel,
      state: 'awaiting_action',
      presentation: isDevice
        ? {
            auth_url: 'https://chatgpt.com/device',
            device_code: 'KDWT-GBSF',
            expects: 'none',
            instructions_key: 'settings.models.oauth.deviceCode.hint',
          }
        : {
            auth_url: 'https://claude.ai/oauth/authorize?code=true&client_id=avibe&scope=org%3Acreate_api_key',
            device_code: null,
            expects: 'paste_code',
            instructions_key: 'settings.models.oauth.pasteCode.hint',
          },
      error_key: null,
      expires_at: new Date(Date.now() + 15 * 60_000).toISOString(),
    };
    this.flows.set(flow.flow_id, { flow, polls: 0, submitted: false });
    return delay(structuredClone(flow), 500);
  }

  getOAuthStatus(flowId: string) {
    const entry = this.flows.get(flowId);
    if (!entry) throw new ApiCallError('flow_not_found');
    entry.polls += 1;
    const { flow } = entry;
    if (flow.state === 'success' || flow.state === 'failed' || flow.state === 'cancelled') {
      return delay(structuredClone(flow));
    }
    if (flow.presentation.expects === 'none') {
      // Device flow self-completes after a few polls.
      if (entry.polls >= 3) this.completeFlow(entry);
    } else if (entry.submitted) {
      // Paste flows: verifying → success on the next poll.
      this.completeFlow(entry);
    }
    return delay(structuredClone(flow));
  }

  submitOAuth(flowId: string, _value: string) {
    const entry = this.flows.get(flowId);
    if (!entry) throw new ApiCallError('flow_not_found');
    entry.submitted = true;
    entry.flow.state = 'verifying';
    return delay(structuredClone(entry.flow));
  }

  cancelOAuth(flowId: string) {
    const entry = this.flows.get(flowId);
    if (entry) entry.flow.state = 'cancelled';
    return delay(undefined);
  }

  // A completed flow reaches `success` but does NOT itself materialize a Source
  // (mirrors the server, where flow completion and source creation are split):
  // the UI must finalize via createOAuthSource. Earlier the mock appended here,
  // which hid the live P0 gap the audit flagged.
  private completeFlow(entry: MockFlow) {
    entry.flow.state = 'success';
  }

  createOAuthSource(draft: OAuthSourceCreate) {
    const entry = this.flows.get(draft.oauth_flow_ref);
    if (!entry || entry.flow.state !== 'success') throw new ApiCallError('flow_not_found');
    const flow = entry.flow;
    const isOpenai = flow.vendor === 'openai';
    const id = flow.source_id ?? rid('src');
    // Idempotent finalize: a duplicate browser retry must not double-create
    // (the server raises migration_item_conflict; here we just re-echo).
    const existing = this.sources.find((s) => s.id === id);
    if (existing) return delay(structuredClone(existing), 300);
    const source: Source = {
      id,
      kind: 'subscription',
      vendor: flow.vendor,
      display_name: draft.display_name ?? (isOpenai ? 'ChatGPT 订阅' : 'Claude 订阅'),
      protocol: isOpenai ? 'openai_responses' : 'anthropic',
      base_url: null,
      supply_channel: draft.supply_channel,
      experimental_consent_at: draft.supply_channel === 'hub' ? new Date().toISOString() : null,
      billing: 'monthly',
      state: { status: 'standby', retry_at: null, detail_key: null },
      usage: { cycle_used_pct: 0, month_spend_cents: null, currency: null },
      // native_cli subscriptions surface the sanctioned CLI account; hub-held
      // experimental sources may stay null until a later adapter rev (schema).
      account_label: draft.supply_channel === 'native_cli' ? 'me@gmail.com' : null,
      masked_credential: null,
      models: isOpenai
        ? [{ id: 'gpt-5.6', display_name: 'GPT-5.6', provenance: 'discovered', discovered_at: new Date().toISOString() }]
        : [{ id: 'claude-opus-4-6', display_name: 'Opus 4.6', provenance: 'discovered', discovered_at: new Date().toISOString() }],
      credential_ref: draft.supply_channel === 'hub' ? rid('cred') : null,
    };
    this.sources.push(source);
    this.priority.order.push(source.id);
    return delay(structuredClone(source), 300);
  }

  patchSource(id: string, patch: SourcePatch) {
    const source = this.sources.find((s) => s.id === id);
    if (!source) throw new ApiCallError('source_not_found');
    if (typeof patch.display_name === 'string') source.display_name = patch.display_name;
    if ('base_url' in patch && source.kind === 'api_key') source.base_url = patch.base_url ?? null;
    return delay(structuredClone(source), 300);
  }

  testSource(id: string) {
    const source = this.sources.find((s) => s.id === id);
    if (!source) throw new ApiCallError('source_not_found');
    // Native-CLI subscriptions can't be re-discovered (server rejects them);
    // the UI only offers this action for hub sources, but fail closed anyway.
    if (source.supply_channel === 'native_cli') throw new ApiCallError('discovery_failed');
    source.state = { status: 'standby', retry_at: null, detail_key: null };
    return delay(source.models.length, 700);
  }
}

function vendorLabel(vendor: string): string {
  const table: Record<string, string> = {
    anthropic: 'Anthropic API Key',
    openai: 'OpenAI API Key',
    zhipuai: '智谱 API Key',
    kimi: 'Kimi API Key',
    xai: 'xAI API Key',
  };
  return table[vendor] ?? `${vendor} API Key`;
}

// Non-reversible display mask (contract rule: ≤7-char prefix + "…" + last 4).
function maskKey(key: string): string {
  const k = key.trim();
  if (k.length <= 5) return `${k}…`;
  const prefix = k.slice(0, Math.min(7, k.length - 4));
  return `${prefix}…${k.slice(-4)}`;
}

function hostLabel(baseUrl: string | null | undefined): string {
  if (!baseUrl) return 'API Key';
  try {
    return new URL(baseUrl).host;
  } catch {
    return 'API Key';
  }
}

const mockStore = new MockStore();

const mockApi: ModelsApi = {
  listSources: () => mockStore.listSources(),
  createApiKeySource: (draft) => mockStore.createApiKeySource(draft),
  createOAuthSource: (draft) => mockStore.createOAuthSource(draft),
  patchSource: (id, patch) => mockStore.patchSource(id, patch),
  testSource: (id) => mockStore.testSource(id),
  deleteSource: (id, force) => mockStore.deleteSource(id, force),
  putPriority: (order) => mockStore.putPriority(order),
  listAgents: () => mockStore.listAgents(),
  setAgentMode: (backend, mode) => mockStore.setAgentMode(backend, mode),
  putMappings: (backend, mappings) => mockStore.putMappings(backend, mappings),
  putMenu: (menu) => mockStore.putMenu(menu),
  addCustomModel: (draft) => mockStore.addCustomModel(draft),
  deleteCustomModel: (sourceId, modelId) => mockStore.deleteCustomModel(sourceId, modelId),
  scanMigration: () => mockStore.scanMigration(),
  applyMigration: (itemIds) => mockStore.applyMigration(itemIds),
  listEvents: (limit) => mockStore.listEvents(limit),
  getRuntimeStatus: () => mockStore.getRuntimeStatus(),
  startOAuth: (vendor, channel) => mockStore.startOAuth(vendor, channel),
  getOAuthStatus: (flowId) => mockStore.getOAuthStatus(flowId),
  submitOAuth: (flowId, value) => mockStore.submitOAuth(flowId, value),
  cancelOAuth: (flowId) => mockStore.cancelOAuth(flowId),
};

/** The single client instance. Stable across renders (safe in effect deps). */
export const modelsApi: ModelsApi = isLive() ? liveApi : mockApi;
