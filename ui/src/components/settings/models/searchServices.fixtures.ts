import type { HostedServiceExecutorCandidate, HostedWebSearchToolStatus, Source } from './types';

/** Wire-read fixtures, not a simulated server. UI tests never derive their expected writes from these builders. */
export const SEARCH_SOURCES: Source[] = [
  { id: 'src_tavily001', vendor: 'tavily', display_name: 'Tavily', protocol: 'search', state: { status: 'needs_action', detail_key: 'models.source.needs_action.credential_revoked' } },
  { id: 'src_exa000001', vendor: 'exa', display_name: 'Exa', protocol: 'search', state: { status: 'standby' }, verification_pending: 'vp_fixture' },
  { id: 'src_claude001', vendor: 'anthropic', display_name: 'Anthropic API key', protocol: 'anthropic', state: { status: 'standby' } },
  { id: 'src_deepseek1', vendor: 'deepseek', display_name: 'DeepSeek', protocol: 'openai_chat', state: { status: 'standby' }, models: [{ id: 'deepseek-chat', origin: 'discovered', reasoning_efforts: [], reasoning_efforts_source: null }] },
].map((source) => ({
  last_discovered_at: null, kind: 'api_key', supply_channel: 'hub', billing: 'metered', models: [],
  credential_ref: `cred_${source.id}`, masked_credential: 'fixture…0000', ...source,
})) as Source[];

const price = (usd: number) => ({ unit: 'search' as const, usd_per_1k: usd, extra: [], as_of: '2026-10-19', url: 'https://pricing.example/search' });
const service = (source: Source, usd: number, enabled = true): HostedServiceExecutorCandidate => ({
  kind: 'service', source_id: source.id, model_id: `${source.vendor}/search`, billing: 'metered', relay: false,
  price: price(usd), settings: { results: 5, enabled }, state: source.state, trial_pending: false, cooldown_until: null,
});

export const ACTIVE_SEARCH: HostedWebSearchToolStatus = {
  name: 'web_search', enabled: true, prefer_native: true, services: { src_deepseek1: { enabled: false, results: 5 } },
  service_order: ['src_tavily001', 'src_exa000001'], executor_order: null,
  effective_order: ['src_tavily001', 'src_exa000001', 'src_claude001', 'src_deepseek1'],
  status: {
    latest_ok: null, refused: [],
    verified: [{ source_id: 'src_claude001', model_id: 'claude-haiku-5-5', mode: 'executor', at: '2026-10-19T12:00:00Z' }],
  },
  executor_candidates: [
    service(SEARCH_SOURCES[0], 8), service(SEARCH_SOURCES[1], 7),
    { kind: 'model', source_id: 'src_claude001', model_id: 'claude-haiku-5-5', billing: 'metered', relay: false, price: { ...price(10), unit: 'call' } },
    service(SEARCH_SOURCES[3], 0.75, false),
  ],
  service_vendors: [
    { id: 'deepseek', name: 'DeepSeek', results_max: 50, price: price(0.75), free_tier: null, source_ids: ['src_deepseek1'], facts: { tool_only: false, auto_enroll: true, free_check: true } },
    { id: 'tavily', name: 'Tavily', results_max: 20, price: price(8), free_tier: null, source_ids: ['src_tavily001'], facts: { tool_only: true, auto_enroll: false, free_check: true } },
    { id: 'exa', name: 'Exa', results_max: 10, price: price(7), free_tier: null, source_ids: ['src_exa000001'], facts: { tool_only: true, auto_enroll: false, free_check: false } },
  ],
};

export const KEYLESS_ACTIVE_SEARCH: HostedWebSearchToolStatus = {
  ...ACTIVE_SEARCH,
  services: { src_exakeyless: { enabled: true, results: 5 } },
  effective_order: ['src_tavily001', 'src_exa000001', 'src_deepseek1', 'src_exakeyless', 'src_claude001'],
  executor_candidates: [
    ...ACTIVE_SEARCH.executor_candidates.slice(0, 2),
    { kind: 'service', source_id: 'src_exakeyless', model_id: 'exa/mcp', billing: 'metered', relay: false,
      price: price(0), settings: { results: 5, enabled: true }, state: { status: 'active' }, trial_pending: false, cooldown_until: null },
    ...ACTIVE_SEARCH.executor_candidates.slice(2),
  ],
  service_vendors: [...ACTIVE_SEARCH.service_vendors!, {
    id: 'exa-mcp', name: 'Exa (free)', results_max: 100, price: price(0), free_tier: 'Free; IP rate-limited',
    source_ids: ['src_exakeyless'], facts: { tool_only: true, auto_enroll: false, free_check: false, built_in: true, requires_setup: false },
  }],
};
