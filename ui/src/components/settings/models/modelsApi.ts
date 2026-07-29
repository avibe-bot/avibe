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
  buildMockRuntime,
  buildMockSources,
  mockDiscoveredCount,
  mockEligibility,
  mockRecommendedOrder,
} from './mockData';
import type {
  AgentBackend,
  AgentChain,
  AgentChainLink,
  AgentMapping,
  AgentMenu,
  AgentMode,
  AgentSourcesPut,
  AgentSupply,
  ApiKeySourceCreate,
  CustomModelCreate,
  MigrationApplyResult,
  MigrationScan,
  OAuthFlow,
  OAuthSourceCreate,
  ProbeResult,
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
  listAgents(): Promise<AgentSupply[]>;
  /** Per-backend enabled subset + order + policy (the 来源顺序 drawer's read). */
  getAgentSources(backend: AgentBackend): Promise<AgentSupply>;
  /** Total write: `follow` hands the order back to the server, `custom` freezes
   *  exactly the ids sent. The response re-echoes the canonical order. */
  putAgentSources(backend: AgentBackend, body: AgentSourcesPut): Promise<AgentSupply>;
  /** Resolution chain for one model. Hub mode only — direct answers `direct_mode`. */
  getAgentChain(backend: AgentBackend, model: string): Promise<AgentChain>;
  /** One real request through the chain. Hub mode only, same reason. */
  probeAgent(backend: AgentBackend, model?: string): Promise<ProbeResult>;
  setAgentMode(backend: AgentBackend, mode: AgentMode): Promise<AgentSupply>;
  putMappings(backend: AgentBackend, mappings: AgentMapping[]): Promise<AgentSupply>;
  putMenu(menu: AgentMenu): Promise<AgentSupply>;
  addCustomModel(draft: CustomModelCreate): Promise<Source>;
  deleteCustomModel(sourceId: string, modelId: string): Promise<Source>;
  scanMigration(): Promise<MigrationScan>;
  applyMigration(itemIds: string[]): Promise<MigrationApplyResult>;
  /** `before` is an event id cursor (「查看全部」 pagination). */
  listEvents(limit?: number, before?: string): Promise<ResolutionEvent[]>;
  getRuntimeStatus(): Promise<RuntimeDependency>;
  /** `experimentalConsent` MUST be true for a consent-gated hub-held
   *  subscription connect, or the server returns consent_required. */
  startOAuth(vendor: string, channel: SupplyChannel, experimentalConsent?: boolean): Promise<OAuthFlow>;
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
  listAgents: () => call<{ agents: AgentSupply[] }>('/api/models/agents').then((r) => r.agents),
  getAgentSources: (backend) => call<{ agent: AgentSupply }>(`/api/models/agents/${backend}/sources`).then((r) => r.agent),
  // The body is TOTAL and closed: the route rejects unknown keys, so
  // `contract_version` is deliberately absent (unlike every other write here).
  putAgentSources: (backend, body) => call<{ agent: AgentSupply }>(`/api/models/agents/${backend}/sources`, jsonInit('PUT', body)).then((r) => r.agent),
  getAgentChain: (backend, model) => call<{ chain: AgentChain }>(`/api/models/agents/${backend}/chain?model=${encodeURIComponent(model)}`).then((r) => r.chain),
  probeAgent: (backend, model) => call<{ probe: ProbeResult }>(`/api/models/agents/${backend}/probe`, jsonInit('POST', model ? { model } : {})).then((r) => r.probe),
  setAgentMode: (backend, mode) => call<{ agent?: AgentSupply } & AgentSupply>(`/api/models/agents/${backend}/mode`, jsonInit('PATCH', { mode })).then((r) => (r.agent ?? r) as AgentSupply),
  putMappings: (backend, mappings) => call<{ agent?: AgentSupply } & AgentSupply>(`/api/models/agents/${backend}/mappings`, jsonInit('PUT', { mappings })).then((r) => (r.agent ?? r) as AgentSupply),
  putMenu: (menu) => call<{ agent?: AgentSupply } & AgentSupply>('/api/models/agents/opencode/menu', jsonInit('PUT', { menu })).then((r) => (r.agent ?? r) as AgentSupply),
  addCustomModel: (draft) => call<{ source?: Source } & Source>('/api/models/custom-models', jsonInit('POST', draft)).then((r) => (r.source ?? r) as Source),
  deleteCustomModel: (sourceId, modelId) => call<{ source?: Source } & Source>('/api/models/custom-models', jsonInit('DELETE', { source_id: sourceId, model_id: modelId })).then((r) => (r.source ?? r) as Source),
  scanMigration: () => call<{ scan?: MigrationScan } & MigrationScan>('/api/models/migration/scan', jsonInit('POST')).then((r) => (r.scan ?? r) as MigrationScan),
  applyMigration: (itemIds) => call<MigrationApplyResult>('/api/models/migration/apply', jsonInit('POST', { item_ids: itemIds })),
  listEvents: (limit = 20, before) =>
    call<{ events: ResolutionEvent[] }>(
      `/api/models/events?limit=${limit}${before ? `&before=${encodeURIComponent(before)}` : ''}`,
    ).then((r) => r.events),
  getRuntimeStatus: () => call<{ runtime?: RuntimeDependency } & RuntimeDependency>('/api/models/runtime/status').then((r) => (r.runtime ?? r) as RuntimeDependency),
  startOAuth: (vendor, channel, experimentalConsent) =>
    call<{ flow?: OAuthFlow } & OAuthFlow>(
      '/api/models/oauth/start',
      jsonInit('POST', { vendor, channel, ...(experimentalConsent ? { experimental_consent: true } : {}) }),
    ).then((r) => (r.flow ?? r) as OAuthFlow),
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
  agents = buildMockAgents(this.sources);
  events = buildMockEvents();
  runtime = buildMockRuntime();
  flows = new Map<string, MockFlow>();

  // ── Fake server-side recomputation ───────────────────────────────────
  // Every read of an agent re-derives what the real server derives: the
  // per-backend order (recommended under `follow`, pruned under `custom`),
  // eligibility, and the supply rollup. That is what makes a drag-reorder or a
  // source deletion move 使用中 in the demo instead of leaving it stale.
  private syncAgents() {
    for (const a of this.agents) {
      if (a.mode === 'direct') {
        a.sources = null;
      } else {
        const policy = a.sources?.policy ?? 'follow';
        const eligibility = mockEligibility(this.sources, a.backend);
        const eligible = new Set(eligibility.filter((e) => e.eligible).map((e) => e.source_id));
        const order =
          policy === 'follow'
            ? mockRecommendedOrder(this.sources, a.backend)
            : // A `custom` subset is frozen, never extended — but a deleted or
              // newly ineligible id drops out (the invariant the server enforces).
              (a.sources?.order ?? []).filter((id) => eligible.has(id));
        a.sources = { policy, order, eligibility };
      }
      this.deriveSupply(a);
    }
  }

  /** §4.3 + §4.5 in miniature: capability (supplies the mapped id) split from
   *  runnability (not blocked), then the rollup over the resulting chain. */
  private deriveSupply(a: AgentSupply) {
    if (a.mode === 'direct') {
      a.current = null;
      a.selected_model_id = null;
      a.selected_by_agent = null;
      a.supply_status = null;
      a.model_supply = null;
      a.named_agents = (a.named_agents ?? []).map((n) => ({ ...n, effective_model_id: null, supply_status: null }));
      return;
    }
    const byId = new Map(this.sources.map((s) => [s.id, s]));
    const order = (a.sources?.order ?? []).map((id) => byId.get(id)).filter((s): s is Source => Boolean(s));
    const target = (model: string) => {
      const m = a.mappings?.find((x) => x.builtin_id === model && x.enabled && x.target_model_id);
      return m ? m.target_model_id : model;
    };
    const chainFor = (model: string) => order.filter((s) => s.models.some((mm) => mm.id === target(model)));
    if (a.builtin_models) {
      a.model_supply = a.builtin_models.map((m) => ({ model_id: m, chain_length: chainFor(m).length }));
    }
    const selected = a.selected_model_id ?? null;
    if (!selected) {
      a.current = null;
      a.supply_status = null;
    } else {
      const chain = chainFor(selected);
      const head = chain.find(isRunnable) ?? null;
      const blocked = chain.filter((s) => !isRunnable(s));
      if (!head) {
        a.current = null;
        a.supply_status =
          chain.length > 0 && blocked.every((s) => s.state.status === 'cooldown') ? 'waiting' : 'interrupted';
      } else {
        a.current = { model_id: selected, source_id: head.id, channel: head.supply_channel };
        a.supply_status = head.id === chain[0]?.id && blocked.length === 0 ? 'ok' : 'degraded';
      }
    }
    const rollup = a.supply_status ?? null;
    a.named_agents = (a.named_agents ?? []).map((n) =>
      n.effective_model_id ? { ...n, supply_status: rollup } : n,
    );
  }

  listSources() {
    // api.md: the inventory is explicitly UNORDERED — order is per-backend.
    return delay(structuredClone(this.sources));
  }

  createApiKeySource(draft: ApiKeySourceCreate) {
    const count = mockDiscoveredCount(draft.vendor);
    const source: Source = {
      id: rid('src'),
      created_at: new Date().toISOString(),
      kind: 'api_key',
      vendor: draft.vendor,
      display_name: draft.vendor === 'custom' ? hostLabel(draft.base_url) : vendorLabel(draft.vendor),
      protocol: draft.vendor === 'anthropic' ? 'anthropic' : 'openai_compatible',
      base_url: draft.base_url ?? null,
      supply_channel: 'hub',
      billing: 'metered',
      state: { status: 'standby', retry_at: null, detail_key: null },
      usage: { cycle_used_pct: null, month_spend_cents: 0, currency: 'USD' },
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
    return delay(structuredClone(source), 900); // simulate probe latency
  }

  deleteSource(id: string, force = false) {
    // Mirror the server's only-supplier guard (mode_switch_blocked): a source
    // currently bound as some agent's supply can't be dropped without force.
    if (!force && this.agents.some((a) => a.current?.source_id === id)) {
      throw new ApiCallError('mode_switch_blocked');
    }
    this.sources = this.sources.filter((s) => s.id !== id);
    // Orders and the rollup are recomputed on the next read (syncAgents).
    return delay(undefined);
  }

  listAgents() {
    this.syncAgents();
    return delay(structuredClone(this.agents));
  }

  private agentOr404(backend: AgentBackend): AgentSupply {
    const agent = this.agents.find((a) => a.backend === backend);
    if (!agent) throw new ApiCallError('source_not_found');
    return agent;
  }

  getAgentSources(backend: AgentBackend) {
    this.syncAgents();
    return delay(structuredClone(this.agentOr404(backend)));
  }

  putAgentSources(backend: AgentBackend, body: AgentSourcesPut) {
    const agent = this.agentOr404(backend);
    if (agent.mode === 'direct') throw new ApiCallError('direct_mode');
    if (body.policy === 'follow') {
      // 恢复推荐顺序 discards the frozen subset; the order comes back from §4.2.
      agent.sources = { policy: 'follow', order: [], eligibility: null };
    } else {
      // §4.4's invariants, server-side: every id exists, is eligible here, and
      // appears once. Omitting one is how the user says 未启用 — not an error.
      const eligible = new Set(
        mockEligibility(this.sources, backend).filter((e) => e.eligible).map((e) => e.source_id),
      );
      const seen = new Set<string>();
      for (const id of body.order) {
        if (!eligible.has(id) || seen.has(id)) throw new ApiCallError('invalid_source_order', id);
        seen.add(id);
      }
      agent.sources = { policy: 'custom', order: [...body.order], eligibility: null };
    }
    this.syncAgents();
    return delay(structuredClone(agent), 380);
  }

  getAgentChain(backend: AgentBackend, model: string) {
    this.syncAgents();
    const agent = this.agentOr404(backend);
    // AC-7: direct mode has no src_* identity to report, so the route refuses
    // rather than answering with an empty (falsely alarming) chain.
    if (agent.mode === 'direct') throw new ApiCallError('direct_mode');
    const byId = new Map(this.sources.map((s) => [s.id, s]));
    const mapping = agent.mappings?.find((x) => x.builtin_id === model && x.enabled && x.target_model_id);
    const resolved = mapping ? mapping.target_model_id : model;
    const chain: AgentChainLink[] = (agent.sources?.order ?? [])
      .map((id) => byId.get(id))
      .filter((s): s is Source => s !== undefined && s.models.some((mm) => mm.id === resolved))
      .map((s) => ({
        source_id: s.id,
        via_mapping: Boolean(mapping),
        resolved_model_id: mapping ? resolved : null,
        health: chainHealth(s),
        runnable: isRunnable(s),
        retry_at: s.state.status === 'cooldown' ? s.state.retry_at ?? null : null,
      }));
    const runnable = chain.some((l) => l.runnable);
    const supply_state: AgentChain['supply_state'] = runnable
      ? 'ok'
      : chain.length > 0 && chain.every((l) => l.health === 'cooldown')
        ? 'waiting'
        : 'interrupted';
    return delay({ contract_version: CONTRACT_VERSION, backend, model_id: model, chain, supply_state });
  }

  probeAgent(backend: AgentBackend, model?: string) {
    this.syncAgents();
    const agent = this.agentOr404(backend);
    if (agent.mode === 'direct') throw new ApiCallError('direct_mode');
    const modelId = model ?? agent.selected_model_id;
    if (!modelId) throw new ApiCallError('model_unsupported');
    const byId = new Map(this.sources.map((s) => [s.id, s]));
    const mapping = agent.mappings?.find((x) => x.builtin_id === modelId && x.enabled && x.target_model_id);
    const resolved = mapping ? mapping.target_model_id : modelId;
    const head = (agent.sources?.order ?? [])
      .map((id) => byId.get(id))
      .find((s): s is Source => s !== undefined && s.models.some((mm) => mm.id === resolved) && isRunnable(s));
    if (!head) throw new ApiCallError('no_runnable_source');
    const probe: ProbeResult = {
      contract_version: CONTRACT_VERSION,
      backend,
      reachable: true,
      source_id: head.id,
      model_id: modelId,
      latency_ms: 180 + Math.floor(Math.random() * 420),
      via_mapping: Boolean(mapping),
      error: null,
    };
    return delay(probe, 1200); // one real request takes a real moment
  }

  setAgentMode(backend: AgentBackend, mode: AgentMode) {
    const agent = this.agentOr404(backend);
    agent.mode = mode;
    if (mode === 'hub') {
      // Rejoining the hub starts on the recommendation, and picks up whatever
      // model the backend defaults to (first built-in / first supplied id).
      agent.sources = { policy: 'follow', order: [], eligibility: null };
      agent.selected_model_id = agent.builtin_models?.[0] ?? this.sources[0]?.models[0]?.id ?? null;
      agent.named_agents = (agent.named_agents ?? []).map((n) => ({
        ...n,
        effective_model_id: agent.selected_model_id ?? null,
      }));
    }
    this.syncAgents();
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
        created_at: new Date().toISOString(),
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
        usage: isKey ? { cycle_used_pct: null, month_spend_cents: 0, currency: 'USD' } : { cycle_used_pct: 0, month_spend_cents: null, currency: null },
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
    this.syncAgents();
    return delay({ applied: chosen.length, sources: structuredClone(this.sources) }, 700);
  }

  listEvents(limit = 20, before?: string) {
    const start = before ? this.events.findIndex((e) => e.id === before) + 1 : 0;
    return delay(structuredClone(this.events.slice(start, start + limit)));
  }

  getRuntimeStatus() {
    return delay(structuredClone(this.runtime));
  }

  startOAuth(vendor: string, channel: SupplyChannel, experimentalConsent?: boolean) {
    // Mirror the server: a hub-held subscription connect requires recorded consent.
    if (channel === 'hub' && !experimentalConsent) throw new ApiCallError('consent_required');
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
      created_at: new Date().toISOString(),
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

// §4.3's runnability half: retry-ready, and never needs_action / error. A
// cooling source stays visible in the chain but is skipped by the turn.
const isRunnable = (s: Source): boolean => s.state.status === 'active' || s.state.status === 'standby';

// SourceStatus → the chain link's health vocabulary (the two healthy statuses
// collapse; the three blockers map one-to-one).
const chainHealth = (s: Source): AgentChainLink['health'] =>
  s.state.status === 'cooldown' ? 'cooldown' : s.state.status === 'needs_action' ? 'needs_action' : s.state.status === 'error' ? 'error' : 'healthy';

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
  listAgents: () => mockStore.listAgents(),
  getAgentSources: (backend) => mockStore.getAgentSources(backend),
  putAgentSources: (backend, body) => mockStore.putAgentSources(backend, body),
  getAgentChain: (backend, model) => mockStore.getAgentChain(backend, model),
  probeAgent: (backend, model) => mockStore.probeAgent(backend, model),
  setAgentMode: (backend, mode) => mockStore.setAgentMode(backend, mode),
  putMappings: (backend, mappings) => mockStore.putMappings(backend, mappings),
  putMenu: (menu) => mockStore.putMenu(menu),
  addCustomModel: (draft) => mockStore.addCustomModel(draft),
  deleteCustomModel: (sourceId, modelId) => mockStore.deleteCustomModel(sourceId, modelId),
  scanMigration: () => mockStore.scanMigration(),
  applyMigration: (itemIds) => mockStore.applyMigration(itemIds),
  listEvents: (limit, before) => mockStore.listEvents(limit, before),
  getRuntimeStatus: () => mockStore.getRuntimeStatus(),
  startOAuth: (vendor, channel, experimentalConsent) => mockStore.startOAuth(vendor, channel, experimentalConsent),
  getOAuthStatus: (flowId) => mockStore.getOAuthStatus(flowId),
  submitOAuth: (flowId, value) => mockStore.submitOAuth(flowId, value),
  cancelOAuth: (flowId) => mockStore.cancelOAuth(flowId),
};

/** The single client instance. Stable across renders (safe in effect deps). */
export const modelsApi: ModelsApi = isLive() ? liveApi : mockApi;
