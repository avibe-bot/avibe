// Fixture builders for the Model Hub (Settings · Models) frames: the `/api/models/*`
// reads the shipped page makes, served through the design network stub.
import type {
  AgentBackend,
  AgentChain,
  AgentChainLink,
  AgentSupply,
  BackendModel,
  ChainHealth,
  HostedExecutionMode,
  HostedFetchExecutorCandidate,
  HostedServiceExecutorCandidate,
  HostedSuccess,
  HostedToolPriceUnit,
  HostedToolsStatus,
  MigrationItem,
  ModelSupply,
  PricedUsage,
  QuotaSummary,
  QuotaWindow,
  ResolutionEvent,
  RouteOrigin,
  RuntimeDependency,
  RuntimeHealth,
  Source,
  SourceQuotaValue,
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

const price = <U extends HostedToolPriceUnit>(unit: U, usd: number, url = 'https://avibe.bot') => ({ unit, usd_per_1k: usd, extra: [], as_of: '2026-10-24', url });
const searchService = (id: string, model: string, usd: number, state: SourceState = STANDBY): HostedServiceExecutorCandidate => ({
  kind: 'service', source_id: id, model_id: model, billing: 'metered', relay: false, price: price('search', usd), settings: { results: 5, enabled: true }, state, trial_pending: false, cooldown_until: null,
});
const fetchService = (kind: 'local' | 'service', id: string, model: string, usd: number, state: SourceState = ACTIVE): HostedFetchExecutorCandidate => ({
  kind, source_id: id, model_id: model, billing: 'metered', relay: false, price: price('fetch', usd), settings: { enabled: true }, state, trial_pending: false, cooldown_until: null,
});
const verified = (src: Source | string, model: string, mode: HostedExecutionMode, minutesAgo: number): HostedSuccess =>
  ({ source_id: typeof src === 'string' ? src : src.id, model_id: model, mode, at: at(-minutesAgo) });

export type HostedToolsSpec = {
  /** Tavily is added as a search service (otherwise only the built-in free Exa searches). */
  tavilyAdded?: boolean;
  /** Tavily's key was revoked: it stopped, and searches moved on to the next service. */
  tavilyRevoked?: boolean;
};

/** The four hosted tools as Vibey uses them: search with services and model executors, web fetch, X search, images. */
export const hostedTools = ({ tavilyAdded = true, tavilyRevoked = false }: HostedToolsSpec = {}): HostedToolsStatus => {
  const tavilyState: SourceState = tavilyRevoked ? { status: 'needs_action', retry_at: null, detail_key: 'models.source.needs_action.credential_revoked' } : STANDBY;
  const tavilyIds = tavilyAdded ? [tavily.id] : [];
  const searchOrder = [...tavilyIds, 'src_exakeyless', relayClaude.id, chatgptPro.id];
  return {
    tools: [
      {
        name: 'web_search',
        enabled: true,
        prefer_native: true,
        services: {},
        service_order: tavilyIds,
        executor_order: null,
        effective_order: searchOrder,
        status: {
          latest_ok: tavilyRevoked ? verified('src_exakeyless', 'exa/mcp', 'service', 4) : verified(chatgptPro, 'gpt-6-astra', 'native', 12),
          verified: [
            verified(chatgptPro, 'gpt-6-astra', 'native', 12),
            verified(relayClaude, 'claude-opus-5', 'native', 95),
            ...(tavilyAdded && !tavilyRevoked ? [verified(tavily, 'tavily/search', 'service', 40)] : []),
          ],
          refused: [
            { source_id: deepseek.id, model_id: 'deepseek-v4', aspect: 'call', at: at(-300), reason: '400 unsupported_tool_type' },
            { source_id: relayClaude.id, model_id: 'claude-opus-4-8', aspect: 'call', at: at(-720), reason: '400 invalid_request_error' },
          ],
        },
        executor_candidates: [
          ...(tavilyAdded ? [searchService(tavily.id, 'tavily/search', 8, tavilyState)] : []),
          searchService('src_exakeyless', 'exa/mcp', 0, ACTIVE),
          { kind: 'model', source_id: relayClaude.id, model_id: 'claude-haiku-5', billing: 'metered', relay: true, price: price('call', 10) },
          { kind: 'model', source_id: chatgptPro.id, model_id: 'gpt-5.6-sol', billing: 'monthly', relay: false, price: price('call', 10) },
        ],
        service_vendors: [
          { id: 'tavily', name: 'Tavily', results_max: 20, price: price('search', 8), free_tier: '1,000 credits / month', source_ids: tavilyIds, facts: { tool_only: true, auto_enroll: false, free_check: true } },
          { id: 'exa', name: 'Exa', results_max: 10, price: price('search', 7), free_tier: null, source_ids: [], facts: { tool_only: true, auto_enroll: false, free_check: false } },
          { id: 'deepseek', name: 'DeepSeek', results_max: 50, price: price('search', 0.75), free_tier: null, source_ids: [], facts: { tool_only: false, auto_enroll: true, free_check: true } },
          { id: 'exa-mcp', name: 'Exa (free)', results_max: 100, price: price('search', 0), free_tier: 'Free; IP rate-limited', source_ids: ['src_exakeyless'], facts: { tool_only: true, auto_enroll: false, free_check: false, built_in: true, requires_setup: false } },
        ],
      },
      {
        name: 'web_fetch',
        enabled: true,
        services: {},
        executor_order: null,
        effective_order: ['src_localfetch', ...tavilyIds, 'src_exakeyless', 'src_jinakeyless'],
        status: { latest_ok: verified('src_localfetch', 'local/fetch', 'service', 8), verified: [verified('src_localfetch', 'local/fetch', 'service', 8)], refused: [] },
        executor_candidates: [
          fetchService('local', 'src_localfetch', 'local/fetch', 0),
          ...(tavilyAdded ? [fetchService('service', tavily.id, 'tavily/extract', 1.6, tavilyState)] : []),
          fetchService('service', 'src_exakeyless', 'exa/mcp', 0),
          fetchService('service', 'src_jinakeyless', 'jina/reader', 0),
        ],
        service_vendors: [
          { id: 'local', name: 'Local', price: price('fetch', 0), capabilities: ['web_fetch'], content_types: ['html', 'pdf', 'text', 'markdown', 'json'], source_ids: ['src_localfetch'], facts: { built_in: true, requires_setup: false, auto_enroll: true } },
          { id: 'tavily', name: 'Tavily', price: price('fetch', 1.6), capabilities: ['web_fetch'], content_types: ['markdown'], source_ids: tavilyIds, facts: { built_in: false, requires_setup: true, auto_enroll: true } },
          { id: 'exa', name: 'Exa', price: price('fetch', 1), capabilities: ['web_fetch'], content_types: ['markdown'], source_ids: [], facts: { built_in: false, requires_setup: true, auto_enroll: true } },
          { id: 'exa-mcp', name: 'Exa (free)', price: price('fetch', 0), capabilities: ['web_fetch'], content_types: ['markdown'], source_ids: ['src_exakeyless'], facts: { built_in: true, requires_setup: false, auto_enroll: true } },
          { id: 'jina-reader', name: 'Jina Reader (free)', price: price('fetch', 0), capabilities: ['web_fetch'], content_types: ['markdown'], source_ids: ['src_jinakeyless'], facts: { built_in: true, requires_setup: false, auto_enroll: true } },
        ],
      },
      {
        name: 'x_search',
        enabled: true,
        executor_order: null,
        effective_order: [],
        executor_candidates: [],
        status: { latest_ok: null, verified: [], refused: [] },
      },
      {
        name: 'image_generate',
        enabled: true,
        default_model: null,
        effective_default: { source_id: chatgptPro.id, model_id: 'gpt-image-2' },
        models: [
          { source_id: chatgptPro.id, model_id: 'gpt-image-2', description: 'GPT Image 2', billing: 'monthly', price: null },
          { source_id: relayGpt.id, model_id: 'gpt-image-2', description: 'GPT Image 2', billing: 'metered', price: { usd: 0.042, basis: 'list' } },
        ],
        image_offered_by: ['openai', 'gemini', 'xai'],
        status: { latest_ok: verified(chatgptPro, 'gpt-image-2', 'direct', 180), verified: [verified(chatgptPro, 'gpt-image-2', 'direct', 180)], refused: [] },
      },
    ],
  };
};

// ── Usage ──

const HOUR = 60;
const usageCounters = (requests: number, cost: number): UsageCounters => ({
  requests,
  token_reports: requests,
  input_tokens: requests * 62_000,
  cached_input_tokens: requests * 43_600,
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
    return { key: at(start).slice(0, hourly ? 13 : 10), start_at: at(start), end_at: at(start + step), history_complete: true, rows };
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
      models: USAGE_MIX.filter(([s]) => s === src).map(([, model]) => ({ model_id: model, label: model, ...sum(modelRows.filter((row) => row.model_id === model && row.source_id === src.id)) })),
    })),
    services: [{ source_id: tavily.id, service_id: 'tavily/search', tool: 'web_search', label: 'Tavily', service_label: 'Tavily Search', last_metered_at: at(-40), requests: searches, billable_requests: searches, uncertain_requests: 0, reported_requests: 0, api_cost_usd: searches * 0.008, reported_usd: 0, api_cost_lower_bound: false, priced: true }],
    days: [],
    buckets,
    pricing: { currency: 'USD', price_table_date: '2026-10-30' },
  };
}

// ── Subscription quota and the switch log ──────────────────────────────

const quotaWindow = (over: Partial<QuotaWindow> & Pick<QuotaWindow, 'id' | 'used_pct'>): QuotaWindow => ({
  kind: 'session', label: over.id, window_seconds: 18_000, resets_at: at(130), ...over,
});
const weekly = (id: string, used: number): QuotaWindow => quotaWindow({ id, label: id, kind: 'weekly', used_pct: used, window_seconds: 604_800, resets_at: at(4 * 24 * 60 + 300) });
const priced = (api_cost_usd: number): PricedUsage => ({ api_cost_usd, excluded_tokens: 0, api_cost_lower_bound: false });
const value = (over: Partial<SourceQuotaValue>): SourceQuotaValue => ({
  currency: 'USD', price_table_date: '2026-10-30', plan_key: null, fee_usd: null, multiple: null, week: priced(0),
  period: { basis: 'rolling_30d', from_day: '2026-10-03', to_day: '2026-11-01', renews_on: null, ...priced(0) },
  ...over,
});

/** Claude Max and ChatGPT Pro, each with its session and weekly windows and what the month was worth at API prices. */
export const quotaSummary = ({ claudeExpired = false } = {}): QuotaSummary => ({
  refresh_interval_seconds: 300,
  sources: [
    {
      source_id: claudeMax.id, vendor: 'anthropic', display_name: claudeMax.display_name, account_label: claudeMax.account_label ?? null,
      plan: 'max', fetched_at: at(-2), state: claudeExpired ? 'auth_expired' : 'ok',
      windows: claudeExpired ? [] : [quotaWindow({ id: 'five_hour', used_pct: 64 }), weekly('seven_day', 41), weekly('seven_day_opus', 23)],
      value: value({
        plan_key: 'claude_max_20x', fee_usd: 200, multiple: 5.62, week: priced(318.4),
        period: { basis: 'billing_cycle', from_day: '2026-10-12', to_day: '2026-11-01', renews_on: '2026-11-12', ...priced(1124.6) },
      }),
    },
    {
      source_id: chatgptPro.id, vendor: 'openai', display_name: chatgptPro.display_name, account_label: chatgptPro.account_label ?? null,
      plan: 'pro', fetched_at: at(-1), state: 'ok',
      windows: [quotaWindow({ id: 'primary_window', label: 'primary_window', used_pct: 22, resets_at: at(47) }), weekly('secondary_window', 58)],
      value: value({ plan_key: 'chatgpt_pro', fee_usd: 200, multiple: 2.31, week: priced(121.2), period: { basis: 'rolling_30d', from_day: '2026-10-03', to_day: '2026-11-01', renews_on: null, ...priced(462.9) } }),
    },
  ],
  value: { currency: 'USD', price_table_date: '2026-10-30', week: priced(439.6), period: { sources: 2, fee_usd: 400, multiple: 3.97, ...priced(1587.5) } },
});

const event = (minutesAgo: number, over: Omit<ResolutionEvent, 'id' | 'ts'>): ResolutionEvent => ({ id: `evt_${minutesAgo}`, ts: at(-minutesAgo), ...over });

/** The switch log: a quota run-out and recovery, a cooling relay, a login that needs action. */
export const switchEvents: ResolutionEvent[] = [
  event(6, { agent: 'claude', kind: 'recover', model_id: 'claude-opus-5', from_source: relayClaude.id, to_source: claudeMax.id, reason: 'recovery', severity: 'info',
    human_zh: 'Claude Code 的 claude-opus-5 切回 Claude Max（额度已恢复）', human_en: 'Claude Code moved claude-opus-5 back to Claude Max (quota restored)' }),
  event(42, { agent: 'codex', kind: 'needs_action', model_id: null, from_source: chatgptPro.id, to_source: null, reason: 'credential_expired', severity: 'action_required',
    human_zh: 'ChatGPT Pro 登录已过期，需要重新登录', human_en: 'ChatGPT Pro sign-in expired; sign in again' }),
  event(43, { agent: 'codex', kind: 'switch', model_id: 'gpt-6-astra', from_source: chatgptPro.id, to_source: relayGpt.id, reason: 'credential_expired', billing_note: 'entered_metered', severity: 'info',
    human_zh: 'Codex 的 gpt-6-astra 从 ChatGPT Pro 切到 ai-relay-gpt（登录过期，开始按量计费）', human_en: 'Codex moved gpt-6-astra from ChatGPT Pro to ai-relay-gpt (sign-in expired; now metered)' }),
  event(190, { agent: 'opencode', kind: 'cooldown', model_id: 'deepseek-v4', from_source: deepseek.id, to_source: null, reason: 'rate_limited', severity: 'info',
    human_zh: 'DeepSeek 被限流，冷却 2 分钟', human_en: 'DeepSeek was rate limited; cooling down for 2 minutes' }),
  event(305, { agent: 'claude', kind: 'switch', model_id: 'claude-opus-5', from_source: claudeMax.id, to_source: relayClaude.id, reason: 'quota_exhausted', billing_note: 'entered_metered', severity: 'info',
    human_zh: 'Claude Code 的 claude-opus-5 从 Claude Max 切到 ai-relay-claude（5 小时额度用完）', human_en: 'Claude Code moved claude-opus-5 from Claude Max to ai-relay-claude (5-hour quota used up)' }),
  event(1460, { agent: 'system', kind: 'channel_switch', model_id: 'claude-sonnet-5', from_source: claudeMax.id, to_source: claudeMax.id, reason: 'manual', severity: 'info',
    human_zh: 'Claude Max 改由 Claude Code 本机登录提供', human_en: 'Claude Max is now served by the Claude Code login on this machine' }),
];

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
