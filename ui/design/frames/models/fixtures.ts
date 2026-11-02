// Fixture builders for the Model Hub (Settings · Models) frames: the `/api/models/*`
// reads the shipped page makes, served through the design network stub.
import type {
  AgentBackend,
  AgentChain,
  AgentChainLink,
  AgentSupply,
  BackendModel,
  ChainHealth,
  HostedToolsStatus,
  MigrationItem,
  ModelSupply,
  RouteOrigin,
  RuntimeDependency,
  RuntimeHealth,
  Source,
  SourceState,
  SuppliedModel,
  UsageBucket,
  UsageBucketEntry,
  UsageBucketRow,
  UsageCounters,
  UsageReport,
  UsageWindowKey,
} from '@/components/settings/models/types';

import { DESIGN_NOW } from '../../shell/clock';

export const at = (minutesFromNow: number) => new Date(DESIGN_NOW + minutesFromNow * 60_000).toISOString();

/** A successful `/api/models/*` envelope. */
export const ok = <T extends object>(body: T) => ({ ok: true, contract_version: 23, ...body });

// ── Sources ─────────────────────────────────────────────────────────────

const models = (ids: string[], efforts: string[] = ['low', 'medium', 'high']): SuppliedModel[] =>
  ids.map((id) => ({ id, origin: 'discovered', reasoning_efforts: efforts, reasoning_efforts_source: 'catalog', discovered_at: at(-180) }));

const ACTIVE: SourceState = { status: 'active', retry_at: null, detail_key: null };
const STANDBY: SourceState = { status: 'standby', retry_at: null, detail_key: null };

export const source = (id: string, extra: Partial<Source> & Pick<Source, 'display_name' | 'vendor' | 'protocol'>): Source => ({
  id,
  created_at: at(-60 * 24 * 20),
  last_discovered_at: at(-180),
  kind: 'api_key',
  supply_channel: 'hub',
  billing: 'metered',
  state: STANDBY,
  base_url: null,
  account_label: null,
  masked_credential: null,
  credential_ref: `cred_${id}`,
  models: [],
  ...extra,
});

export const claudeMax = source('src_claudemax', {
  kind: 'subscription',
  vendor: 'anthropic',
  display_name: 'Claude Max',
  protocol: 'anthropic',
  supply_channel: 'native_cli',
  billing: 'monthly',
  state: ACTIVE,
  account_label: 'lin@chainbot.io',
  credential_ref: null,
  models: models(['claude-opus-5', 'claude-sonnet-5', 'claude-haiku-5'], ['low', 'medium', 'high', 'max']),
  adopted_by: [{ backend: 'claude', menu_model: 'claude-opus-5' }],
});

export const chatgptPro = source('src_chatgptpro', {
  kind: 'subscription',
  vendor: 'openai',
  display_name: 'ChatGPT Pro',
  protocol: 'openai_responses',
  supply_channel: 'hub',
  billing: 'monthly',
  state: ACTIVE,
  account_label: 'lin@chainbot.io',
  credential_ref: null,
  models: models(['gpt-6-astra', 'gpt-5.6-sol', 'gpt-5.6-terra', 'gpt-5.5'], ['minimal', 'low', 'medium', 'high', 'xhigh']),
  adopted_by: [{ backend: 'codex', menu_model: 'gpt-6-astra' }, { backend: 'vibey', menu_model: 'gpt-6-astra' }],
});

export const relayClaude = source('src_relayclaude', {
  vendor: 'custom',
  display_name: 'ai-relay-claude',
  protocol: 'anthropic',
  base_url: 'https://ai-relay.chainbot.io/v1',
  masked_credential: 'sk-9c5…738a',
  models: models(['claude-opus-5', 'claude-sonnet-5', 'claude-haiku-5', 'claude-opus-4-8']),
  adopted_by: [{ backend: 'claude', menu_model: 'claude-sonnet-5' }, { backend: 'opencode', menu_model: 'claude-sonnet-5' }],
});

export const relayGpt = source('src_relaygpt', {
  vendor: 'custom',
  display_name: 'ai-relay-gpt',
  protocol: 'openai_responses',
  base_url: 'https://ai-relay.chainbot.io/v1',
  masked_credential: 'sk-29d…de81',
  models: models(['gpt-6-astra', 'gpt-5.6-sol', 'gpt-5.6-terra', 'gpt-5.6-luna', 'gpt-5.5', 'gpt-5.4']),
  adopted_by: [{ backend: 'codex', menu_model: 'gpt-5.6-sol' }],
});

export const deepseek = source('src_deepseek', {
  vendor: 'deepseek',
  display_name: 'DeepSeek',
  protocol: 'openai_chat',
  base_url: 'https://api.deepseek.com/v1',
  masked_credential: 'sk-4f1…a09c',
  models: models(['deepseek-v4', 'deepseek-v4-reasoner'], []),
  adopted_by: [{ backend: 'opencode', menu_model: 'deepseek-v4' }],
});

export const tavily = source('src_tavily', {
  vendor: 'tavily',
  display_name: 'Tavily',
  protocol: 'search',
  masked_credential: 'tvly-…3k9Q',
  models: [],
});

export const demoSources: Source[] = [claudeMax, chatgptPro, relayClaude, relayGpt, deepseek, tavily];

// ── Agents (backends) and their routes ──────────────────────────────────

export const backendModel = (id: string, extra: Partial<BackendModel> = {}): BackendModel => ({
  id,
  display_name: null,
  origin: 'builtin',
  models_dev_id: null,
  context_window: null,
  max_output_tokens: null,
  input_modalities: ['text', 'image'],
  output_modalities: ['text'],
  supports_tools: null,
  supports_reasoning: null,
  reasoning_efforts: [],
  locked: false,
  routeable: true,
  ...extra,
});

export type RouteSpec = {
  /** The hops a turn would try, in order: `[source, upstream model]`. */
  hops: [Source, string][];
  origin?: RouteOrigin;
  /** Hop health by Source id; healthy otherwise. */
  health?: Record<string, ChainHealth>;
};

export type AgentSpec = {
  backend: AgentBackend;
  mode?: 'hub' | 'direct';
  cliPresent?: boolean;
  menuKind?: 'fixed' | 'open';
  order?: Source[];
  routes?: Record<string, RouteSpec>;
  catalog?: BackendModel[];
  namedAgents?: AgentSupply['named_agents'];
};

const runnable = (health: ChainHealth) => health === 'healthy';

/** Which Sources may serve a backend, as the server decides (`ModelHubConfig.source_eligible_for_backend`):
 *  never a search service; any gateway-held Source; a native login only for its own CLI. */
const NATIVE_CLIENT: Record<string, AgentBackend> = { anthropic: 'claude', openai: 'codex' };
export const eligible = (src: Source, backend: AgentBackend): boolean =>
  src.protocol !== 'search'
  && (src.supply_channel === 'hub' || (src.kind === 'subscription' && NATIVE_CLIENT[src.vendor] === backend));

const modelSupply = (modelId: string, route: RouteSpec | undefined): ModelSupply => ({
  model_id: modelId,
  route_origin: route?.origin ?? (route ? 'automatic' : null),
  chain_length: route?.hops.length ?? 0,
  has_runnable_hop: (route?.hops ?? []).some(([hop]) => runnable(route?.health?.[hop.id] ?? 'healthy')),
});

export const agent = ({ backend, mode = 'hub', cliPresent = true, menuKind, order = [], routes = {}, catalog, namedAgents = [] }: AgentSpec): AgentSupply => {
  const catalogModels = catalog ?? Object.keys(routes).map((id) => backendModel(id, backend === 'opencode' ? { origin: 'provider', native_protocol: 'openai_responses' } : {}));
  return {
    backend,
    cli_present: cliPresent,
    mode,
    menu_kind: menuKind ?? (backend === 'opencode' ? 'open' : 'fixed'),
    sources: mode === 'hub'
      ? {
          order: order.map((s) => s.id),
          eligibility: demoSources.map((s) => eligible(s, backend)
            ? { source_id: s.id, eligible: true, reason_key: null }
            : { source_id: s.id, eligible: false, reason_key: 'models.eligibility.subscription_wrong_client' }),
        }
      : null,
    model_supply: mode === 'hub' ? catalogModels.filter((m) => m.routeable).map((m) => modelSupply(m.id, routes[m.id])) : null,
    named_agents: namedAgents,
    catalog_models: catalogModels,
  };
};

export const chain = (backend: AgentBackend, modelId: string, route: RouteSpec | undefined): AgentChain => {
  const links: AgentChainLink[] = (route?.hops ?? []).map(([hop, upstream]) => {
    const health = route?.health?.[hop.id] ?? 'healthy';
    return {
      source_id: hop.id,
      model_id: upstream,
      channel: hop.supply_channel,
      health,
      runnable: runnable(health),
      reason: health === 'needs_action' ? 'models.source.needs_action.oauth_expired' : null,
      retry_at: health === 'cooldown' ? at(42) : null,
    };
  });
  const first = links.find((link) => link.runnable) ?? null;
  const origin = route?.origin ?? 'automatic';
  return {
    contract_version: 23,
    backend,
    model_id: modelId,
    manual_override: origin === 'manual' ? { hops: links.map(({ source_id, model_id }) => ({ source_id, model_id })) } : null,
    route_origin: route ? origin : null,
    current: first ? { source_id: first.source_id, model_id: first.model_id } : null,
    chain: links,
    supply_state: first ? 'ok' : links.length ? 'waiting' : 'interrupted',
  };
};

// ── Gateway runtime ─────────────────────────────────────────────────────

export const runtime = (health: RuntimeHealth = 'ok', enabled = true): RuntimeDependency => ({
  contract_version: 23,
  enabled,
  manifest: { name: 'cliproxyapi', resolution: 'resolved', version: '6.9.2', source_sha: 'a'.repeat(40), assets: [] },
  status: {
    installed_version: health === 'not_installed' ? null : '6.9.2',
    verified: health !== 'not_installed',
    listening: health === 'ok' ? { host: '127.0.0.1', port: 40121 } : null,
    health,
    last_check: at(-1),
  },
});

// ── Hosted tools ────────────────────────────────────────────────────────

const searchPrice = (usd: number) => ({ unit: 'search' as const, usd_per_1k: usd, extra: [], as_of: '2026-10-24', url: 'https://avibe.bot' });

export const hostedTools = (): HostedToolsStatus => ({
  tools: [
    {
      name: 'web_search',
      enabled: true,
      prefer_native: true,
      services: {},
      service_order: [tavily.id],
      executor_order: null,
      effective_order: [tavily.id, 'src_exakeyless', relayClaude.id],
      status: { latest_ok: null, verified: [], refused: [] },
      executor_candidates: [
        { kind: 'service', source_id: tavily.id, model_id: 'tavily/search', billing: 'metered', relay: false, price: searchPrice(8), settings: { results: 5, enabled: true }, state: STANDBY, trial_pending: false, cooldown_until: null },
        { kind: 'service', source_id: 'src_exakeyless', model_id: 'exa/mcp', billing: 'metered', relay: false, price: searchPrice(0), settings: { results: 5, enabled: true }, state: ACTIVE, trial_pending: false, cooldown_until: null },
        { kind: 'model', source_id: relayClaude.id, model_id: 'claude-haiku-5', billing: 'metered', relay: true, price: { ...searchPrice(10), unit: 'call' } },
      ],
      service_vendors: [
        { id: 'tavily', name: 'Tavily', results_max: 20, price: searchPrice(8), free_tier: null, source_ids: [tavily.id], facts: { tool_only: true, auto_enroll: false, free_check: true } },
        { id: 'exa', name: 'Exa', results_max: 10, price: searchPrice(7), free_tier: null, source_ids: [], facts: { tool_only: true, auto_enroll: false, free_check: false } },
        { id: 'exa-mcp', name: 'Exa (free)', results_max: 100, price: searchPrice(0), free_tier: 'Free; IP rate-limited', source_ids: ['src_exakeyless'], facts: { tool_only: true, auto_enroll: false, free_check: false, built_in: true, requires_setup: false } },
      ],
    },
  ],
});

// ── Usage ──

const HOUR = 60;
const usageCounters = (requests: number, cost: number): UsageCounters => ({
  requests,
  token_reports: requests,
  input_tokens: requests * 18_400,
  cached_input_tokens: requests * 61_000,
  output_tokens: requests * 2_300,
  api_cost_usd: cost,
  excluded_tokens: 0,
  api_cost_lower_bound: false,
});

/** Who metered what, as shares of a window: [source, model, share of requests]. */
const USAGE_MIX: [Source, string, number][] = [
  [chatgptPro, 'gpt-6-astra', 0.34],
  [relayClaude, 'claude-opus-5', 0.22],
  [relayGpt, 'gpt-5.6-sol', 0.18],
  [claudeMax, 'claude-sonnet-5', 0.14],
  [deepseek, 'deepseek-v4', 0.12],
];
const COST_PER_REQUEST: Record<string, number> = { 'gpt-6-astra': 0.11, 'claude-opus-5': 0.19, 'gpt-5.6-sol': 0.06, 'claude-sonnet-5': 0.08, 'deepseek-v4': 0.004 };

/** A usage report over a trailing window, with a working-hours curve and one search service. */
export function usageReport(windowKey: UsageWindowKey = '24h'): UsageReport {
  const hourly = windowKey === '24h';
  const count = hourly ? 24 : Number(windowKey.slice(0, -1));
  const step = hourly ? HOUR : 24 * HOUR;
  const buckets: UsageBucket[] = Array.from({ length: count }, (_, index) => {
    const start = -(count - index) * step;
    // Busier in working hours and on recent days.
    const load = hourly ? [2, 1, 0, 0, 0, 0, 1, 3, 9, 14, 17, 15, 9, 13, 18, 16, 12, 8, 6, 7, 9, 6, 4, 3][index] : 20 + ((index * 37) % 23) + index;
    const rows: UsageBucketEntry[] = USAGE_MIX.flatMap(([src, model, share]) => {
      const requests = Math.round(load * share * (hourly ? 1 : 6));
      return requests > 0 ? [{ source_id: src.id, model_id: model, ...usageCounters(requests, requests * COST_PER_REQUEST[model]) }] : [];
    });
    if (load > 8) rows.push({ kind: 'service', source_id: tavily.id, service_id: 'tavily/search', tool: 'web_search', requests: 2, billable_requests: 2, uncertain_requests: 0, reported_requests: 0, api_cost_usd: 0.016, reported_usd: 0, api_cost_lower_bound: false });
    return { key: at(start).slice(0, hourly ? 13 : 10), start_at: at(start), end_at: at(start + step), history_complete: index < count - 1, rows };
  });
  const modelRows = buckets.flatMap((bucket) => bucket.rows).filter((row): row is UsageBucketRow => row.kind !== 'service');
  const sum = (rows: UsageBucketRow[]) => {
    const requests = rows.reduce((total, row) => total + row.requests, 0);
    return usageCounters(requests, Math.round(rows.reduce((total, row) => total + (row.api_cost_usd ?? 0), 0) * 100) / 100);
  };
  const searches = buckets.flatMap((bucket) => bucket.rows).filter((row) => row.kind === 'service').length * 2;
  return {
    window_key: windowKey,
    window_days: hourly ? 1 : count,
    granularity: hourly ? 'hour' : 'day',
    from_day: at(-count * step).slice(0, 10),
    to_day: at(0).slice(0, 10),
    from_at: at(-count * step),
    to_at: at(0),
    totals: sum(modelRows),
    sources: [...new Set(USAGE_MIX.map(([src]) => src))].map((src) => ({
      source_id: src.id,
      label: src.display_name,
      last_metered_at: at(-12),
      ...sum(modelRows.filter((row) => row.source_id === src.id)),
      models: USAGE_MIX.filter(([s]) => s === src).map(([, model]) => ({ model_id: model, label: null, ...sum(modelRows.filter((row) => row.model_id === model && row.source_id === src.id)) })),
    })),
    services: [{ source_id: tavily.id, service_id: 'tavily/search', tool: 'web_search', label: 'Tavily', service_label: 'Tavily Search', last_metered_at: at(-40), requests: searches, billable_requests: searches, uncertain_requests: 0, reported_requests: 0, api_cost_usd: searches * 0.008, reported_usd: 0, api_cost_lower_bound: false, priced: true }],
    days: [],
    buckets,
    pricing: { currency: 'USD', price_table_date: '2026-10-30' },
  };
}

// ── The page's reads ────────────────────────────────────────────────────

export type HubSpec = {
  sources?: Source[];
  agents?: AgentSpec[];
  runtime?: RuntimeDependency;
  tools?: HostedToolsStatus;
  /** Native logins the gateway could import (shows 迁移配置). */
  migration?: MigrationItem[];
};

/** Reads every frame's providers make: the Bots provider, and the CSRF token a write (or the migration scan) asks for. */
export const baseApi: Record<string, unknown> = {
  '/api/bots': { ok: true, bots: [] },
  '/api/csrf-token': { csrf_token: 'design' },
};

/** The usage read a search service's detail makes for its monthly line. */
export const usageApi = (windowKey: UsageWindowKey = '60d') => ({ '/api/models/usage': ok({ usage: usageReport(windowKey) }) });

/** An API error envelope, for a read that failed. */
export const failed = (error = 'internal_error') => ({ ok: false, contract_version: 23, error });

/** Every `/api/models/*` read the Models page makes on open, from one description of the Hub. */
export function hubApi({ sources = demoSources, agents = [], runtime: rt = runtime(), tools = hostedTools(), migration = [] }: HubSpec): Record<string, unknown> {
  const api: Record<string, unknown> = {
    ...baseApi,
    '/api/models/sources': ok({ sources }),
    '/api/models/agents': ok({ agents: agents.map(agent) }),
    '/api/models/runtime/status': ok({ runtime: rt }),
    '/api/models/hosted-tools': ok({ hosted_tools: tools }),
    '/api/models/migration/scan': ok({ scan: { items: migration } }),
    '/api/models/events': ok({ events: [] }),
    ...usageApi(),
  };
  for (const spec of agents) {
    if ((spec.mode ?? 'hub') !== 'hub') continue;
    const chains = Object.entries(spec.routes ?? {}).map(([modelId, route]) => chain(spec.backend, modelId, route));
    api[`/api/models/agents/${spec.backend}/chains`] = ok({ chains });
    api[`/api/models/agents/${spec.backend}/provenance`] = ok({ provenance: null });
    for (const read of chains) api[`/api/models/agents/${spec.backend}/chain?model=${encodeURIComponent(read.model_id)}`] = ok({ chain: read });
    // The model menu dialog and the default-route drawer read the backend's own supply.
    api[`/api/models/agents/${spec.backend}/sources`] = ok({ agent: agent(spec) });
  }
  return api;
}

/** One backend's supply as the page reads it, by spec. */
export const agentOf = (backend: AgentBackend, specs: AgentSpec[] = demoAgents): AgentSupply =>
  agent(specs.find((spec) => spec.backend === backend)!);

/** One model's route as the page reads it, by spec. */
export const chainOf = (backend: AgentBackend, modelId: string, specs: AgentSpec[] = demoAgents): AgentChain =>
  chain(backend, modelId, specs.find((spec) => spec.backend === backend)?.routes?.[modelId]);

/** The populated Hub: four backends on the gateway, each with its own routes. */
export const demoAgents: AgentSpec[] = [
  {
    backend: 'claude',
    order: [claudeMax, relayClaude],
    routes: {
      'claude-opus-5': { hops: [[claudeMax, 'claude-opus-5'], [relayClaude, 'claude-opus-5']] },
      'claude-sonnet-5': { hops: [[claudeMax, 'claude-sonnet-5'], [relayClaude, 'claude-sonnet-5']] },
      'claude-haiku-5': { hops: [[relayClaude, 'claude-haiku-5']], origin: 'manual' },
      'claude-opus-4-8': { hops: [[relayClaude, 'claude-opus-4-8']] },
    },
  },
  {
    backend: 'codex',
    order: [chatgptPro, relayGpt],
    routes: {
      'gpt-6-astra': { hops: [[chatgptPro, 'gpt-6-astra'], [relayGpt, 'gpt-6-astra']] },
      'gpt-5.6-sol': { hops: [[chatgptPro, 'gpt-5.6-sol'], [relayGpt, 'gpt-5.6-sol']] },
      'gpt-5.6-terra': { hops: [[relayGpt, 'gpt-5.6-terra']], origin: 'manual' },
      'gpt-5.6-luna': { hops: [[relayGpt, 'gpt-5.6-luna']], origin: 'passthrough' },
      'gpt-5.5': { hops: [[chatgptPro, 'gpt-5.5'], [relayGpt, 'gpt-5.5']] },
      'gpt-5.4': { hops: [[relayGpt, 'gpt-5.4']] },
      'gpt-5.4-mini': { hops: [[relayGpt, 'gpt-5.4-mini']] },
    },
  },
  {
    backend: 'opencode',
    order: [deepseek, relayGpt, relayClaude],
    routes: {
      'deepseek-v4': { hops: [[deepseek, 'deepseek-v4']] },
      'deepseek-v4-reasoner': { hops: [[deepseek, 'deepseek-v4-reasoner']] },
      'claude-sonnet-5': { hops: [[relayClaude, 'claude-sonnet-5']] },
      'gpt-5.6-sol': { hops: [[relayGpt, 'gpt-5.6-sol']] },
    },
  },
  {
    backend: 'vibey',
    cliPresent: false,
    order: [chatgptPro, relayGpt, relayClaude],
    routes: {
      'gpt-6-astra': { hops: [[chatgptPro, 'gpt-6-astra'], [relayGpt, 'gpt-6-astra']] },
      'claude-opus-5': { hops: [[relayClaude, 'claude-opus-5']] },
    },
  },
];

/** The Settings route pane the Models page renders in (SettingsLayout's content column). */
export const settingsPaneClass = 'w-full min-h-full px-4 pb-5 pt-5 md:px-8 md:pb-7 md:pt-7';
