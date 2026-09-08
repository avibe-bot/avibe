import { AgentCard } from '../../src/components/settings/models/AgentCard';
import { blankBackendModel } from '../../src/components/settings/models/backendCatalog';
import { modelChainKey } from '../../src/components/settings/models/modelRows';
import { readyRegion } from '../../src/components/settings/models/regionRead';
import { freshRuntimeProjection } from '../../src/components/settings/models/runtimeLifecycle';
import type { AgentChain, AgentSupply, Source } from '../../src/components/settings/models/types';

export function GatewayFixture() {
  const long = new URLSearchParams(location.search).has('long');
  const ids = ['claude-fable-5', 'claude-opus-4-8', 'claude-fable-5-1', 'claude-opus-5', 'claude-sonnet-5'];
  const source: Source = {
    id: 'src_fixture', display_name: 'Primary', kind: 'api_key', vendor: 'anthropic',
    protocol: 'anthropic', supply_channel: 'hub', billing: 'metered', last_discovered_at: null,
    state: { status: 'active', retry_at: null, detail_key: null },
    models: ids.map((id) => ({ id, origin: 'discovered', reasoning_efforts: [], reasoning_efforts_source: null })),
  };
  const agent: AgentSupply = {
    backend: 'opencode', cli_present: true, mode: 'hub', menu_kind: 'open',
    catalog_models: ids.map((id) => ({ ...blankBackendModel(), id })),
    sources: { order: [source.id], eligibility: [{ source_id: source.id, eligible: true }] },
    model_supply: ids.map((model_id) => ({ model_id, route_origin: 'automatic', chain_length: 1, has_runnable_hop: true })),
    named_agents: [{
      name: long ? `opencode-${'long-agent-name-'.repeat(16)}` : 'opencode',
      effective_model_id: long ? `grok/${'long-model-id-'.repeat(20)}grok-4.6` : 'grok/grok-4.6',
      route_reason: 'route_unconfigured', supply_status: 'interrupted',
    }],
  };
  const chains = Object.fromEntries(ids.map((model_id) => [modelChainKey('opencode', model_id), readyRegion<AgentChain>({
    contract_version: 10, backend: 'opencode', model_id, route_origin: 'automatic', manual_override: null,
    current: { source_id: source.id, model_id }, supply_state: 'ok',
    chain: [{ source_id: source.id, model_id, channel: 'hub', health: 'healthy', runnable: true, reason: null, retry_at: null }],
  })]));
  const runtime = freshRuntimeProjection(readyRegion({
    contract_version: 10,
    manifest: { name: 'cliproxyapi', resolution: 'resolved', version: '1.0.0', source_sha: 'fixture', assets: [] },
    status: { installed_version: '1.0.0', verified: true, listening: null, health: 'ok', last_check: null },
  }));
  return <main className="model-hub-shell mx-auto min-w-0 max-w-3xl p-4">
    <AgentCard agents={[agent]} sources={[source]} chains={chains} runtime={runtime}
      pendingBackends={new Set()} switchFailures={new Set()} connectingBackend={null}
      onConnectHub={() => {}} onSwitchDirect={() => {}} onOpenModels={() => {}}
      onOpenOrder={() => {}} onOpenRoute={() => {}} onProbeSettled={() => {}} />
  </main>;
}
